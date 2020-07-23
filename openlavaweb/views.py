#!/usr/bin/env python
# Copyright 2011 David Irvine
#
# This file is part of Openlava Web
#
# Openlava Web is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Openlava Web is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Openlava Web. If not, see <http://www.gnu.org/licenses/>.
import json
import os
import pwd
import logging
import datetime
from multiprocessing import Process as MPProcess
from multiprocessing import Queue as MPQueue
from multiprocessing import log_to_stderr

# noinspection PyPackageRequirements
from django import forms
from django.http import HttpResponse, HttpResponseRedirect, Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.urlresolvers import reverse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.middleware.csrf import get_token
from django.conf import settings

from openlavaweb.cluster import ClusterException
from openlavaweb.cluster.openlavacluster import Cluster, Host, Job, Queue, User, ExecutionHost, NoSuchHostError
# noinspection PyUnresolvedReferences
from openlava import lsblib


class ClusterEncoder(json.JSONEncoder):
    """
    Encodes cluster objects objects to JSON,
    """
    @staticmethod
    def check(obj):
        if isinstance(obj, ExecutionHost):
            return {
                'type': "ExecutionHost",
                'name': obj.name,
                'num_slots': obj.num_slots_for_job,
                'url': reverse("olw_host_view", args=[obj.name]),
            }
        if isinstance(obj, Host):
            return {
                'type': "Host",
                'name': obj.name,
                'url': reverse("olw_host_view", args=[obj.name]),
            }
        if isinstance(obj, Job):
            return {
                'type': "Job",
                'name': obj.name,
                'job_id': obj.job_id,
                'array_index': obj.array_index,
                'url': reverse("olw_job_view_array", args=[obj.job_id, obj.array_index]),
                'user_name': obj.user_name,
                'user_url': reverse("olw_user_view", args=[obj.user_name]),
                'status': obj.status,
                'submit_time': obj.submit_time,
                'start_time': obj.start_time,
                'end_time': obj.end_time,
            }
        if isinstance(obj, Queue):
            return {
                'type': "Queue",
                'name': obj.name,
                'url': reverse("olw_queue_view", args=[obj.name]),
            }

        return obj

    def default(self, obj):
        if isinstance(obj, datetime.timedelta):
            return obj.total_seconds()

        d = {'type': obj.__class__.__name__}
        for name in obj.json_attributes():
            value = getattr(obj, name)
            if hasattr(value, '__call__'):
                value = value()
            if isinstance(value, list):
                value = [self.check(i) for i in value]
            else:
                value = self.check(value)

            d[name] = value
        return d


def create_js_response(data=None, message="", response=None, is_failure=False):
    """
    Takes a json serializable object, and an optional message, and creates a standard json response document.

    :param data: json serializable object
    :param message: Optional message to include with response
    :return: HttpResponse object

    """
    if is_failure:
        status = "FAIL"
    else:
        status = "OK"
    data = {
        'status': status,
        'data': data,
        'message': message,
    }
    if response is None:
        response = HttpResponse
    return response(json.dumps(data, sort_keys=True, indent=4, cls=ClusterEncoder),
                    content_type='application/json')


def handle_cluster_exception(e):
    if e.http_response == "HttpResponseForbidden":
        response = HttpResponseForbidden
    elif e.http_response == "Http404":
        response = Http404
    elif e.http_response == "HttpResponseBadRequest":
        response = HttpResponseBadRequest
    else:
        response = None

    return create_js_response(
        message=e.message,
        data=e.json_response(),
        response=response,
        is_failure=True
    )



@ensure_csrf_cookie
def get_csrf_token(request):
    """
    Returns the CSRF token to an AJAX client

    :param request: Request object
    :return:

        Returns a JSON serialized dictionary containing a single item, called cookie, the value of which
        is set to the CSRF token.

        Example Response::

            {
                "data": {
                    "cookie": "Ca7mCejV7LKu1LN13bGtSaKZqCtHYGTp"
                },
                "message": "",
                "status": "OK"
            }

    """

    return create_js_response({'cookie': get_token(request)})


@csrf_exempt
def ajax_login(request):
    """
    Logs in an ajax client by allowing the client to upload the username/password combination.

    .. todo: Check if this is a REALLY BAD IDEA?!?!?

    The username password combo should be JSON serialized and sent as the POST data.

    Example POST data::

        {
            "password": "topsecret",
            "username": "bob"
        }

    :param request:
    :return:

    On success, returns a JSON serialized response with no data, and the message set to "User logged in"

    Example Success Response::

        {
            "data": null,
            "message": "User logged in",
            "status": "OK"
        }

    On failure to authenticate, returns HTTP Error 403 not authenticated, the reason for the failure is specified
    in the message.

    Example Failure Response::

        {
            "data": null,
            "message": "Invalid username or password",
            "status": "FAIL"
        }

    """
    try:
        data = json.loads(request.body)
        user = authenticate(username=data['username'], password=data['password'])
    except ValueError:
        return HttpResponseBadRequest()
    except KeyError:
        return HttpResponseBadRequest()
    if user:
        if user.is_active:
            login(request, user)
            return create_js_response(message="User logged in")
        else:
            return create_js_response(message="User is inactive", response=HttpResponseForbidden, is_failure=True)
    else:
        return create_js_response(message="Invalid username or password", response=HttpResponseForbidden, is_failure=True)


def queue_list(request):
    queues = Queue.get_queue_list()
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(queues)

    return render(request, 'openlavaweb/queue_list.html', {"queue_list": queues})


def queue_view(request, queue_name):
    try:
        queue = Queue(queue_name)
    except ValueError:
        raise Http404("Queue not found")
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(queue)
    return render(request, 'openlavaweb/queue_detail.html', {"queue": queue}, )


@login_required
def queue_close(request, queue_name):
    queue_name = str(queue_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'queue_name': queue_name,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_queue_close, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, ClusterException):
                print "exception: ", rc
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        queue = Queue(queue_name)
        return render(request, 'openlavaweb/queue_close_confirm.html', {"object": queue})


def execute_queue_close(request, queue, queue_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        q = Queue(queue_name)
        q.close()
        if request.is_ajax():
            queue.put(
                create_js_response(message="Queue closed")
            )
        else:
            queue.put(HttpResponseRedirect(reverse("olw_queue_view", kwargs={'queue_name': queue_name})))
    except Exception as e:
        queue.put(e)


@login_required
def queue_open(request, queue_name):
    queue_name = str(queue_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'queue_name': queue_name,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_queue_open, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        queue = Queue(queue_name)
        return render(request, 'openlavaweb/queue_open_confirm.html', {"object": queue})


def execute_queue_open(request, queue, queue_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        q = Queue(queue_name)
        q.open()
        if request.is_ajax():
            queue.put(
                create_js_response(message="Queue opened"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_queue_view", kwargs={'queue_name': queue_name})))
    except Exception as e:
        queue.put(e)


@login_required
def queue_inactivate(request, queue_name):
    queue_name = str(queue_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'queue_name': queue_name,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_queue_inactivate, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        queue = Queue(queue_name)
        return render(request, 'openlavaweb/queue_inactivate_confirm.html', {"object": queue})


def execute_queue_inactivate(request, queue, queue_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        q = Queue(queue_name)
        q.inactivate()
        if request.is_ajax():
            queue.put(
                create_js_response(message="Queue inactivated"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_queue_view", kwargs={'queue_name': queue_name})))
    except Exception as e:
        queue.put(e)


@login_required
def queue_activate(request, queue_name):
    queue_name = str(queue_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'queue_name': queue_name,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_queue_activate, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        queue = Queue(queue_name)
        return render(request, 'openlavaweb/queue_activate_confirm.html', {"object": queue})


def execute_queue_activate(request, queue, queue_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        q = Queue(queue_name)
        q.activate()
        if request.is_ajax():
            queue.put(
                create_js_response(message="Queue activated"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_queue_view", kwargs={'queue_name': queue_name})))
    except Exception as e:
        queue.put(e)


def host_list(request):
    """
    Returns a list of hosts

    :param request: Request object
    :return: HTML rendered page of hosts, or AJAX list of host objects

    """
    hosts = Host.get_host_list()
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(data=hosts)

    paginator = Paginator(hosts, 25)
    page = request.GET.get('page')
    try:
        hosts = paginator.page(page)
    except PageNotAnInteger:
        hosts = paginator.page(1)
    except EmptyPage:
        hosts = paginator.page(paginator.num_pages)
    return render(request, 'openlavaweb/host_list.html', {"host_list": hosts})


def host_view(request, host_name):
    """
    Shows host details

    :param request: Request object
    :param host_name: name of host to show
    :return: HTML rendered page of host infomration, or JSON Host object

    """
    try:
        host = Host(host_name)
    except NoSuchHostError:
        raise Http404("Host not found")

    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(host)
    return render(request, 'openlavaweb/host_detail.html', {"host": host}, )


@login_required
def host_close(request, host_name):
    """
    Closes a host

    :param request: Request object
    :param host_name: Host object
    :return:

    """
    host_name = str(host_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'host_name': host_name,
                'request': request,
                'queue': q,
            }
            print "Executing"
            p = MPProcess(target=execute_host_close, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            print "executed"
            print type(rc)
            if isinstance(rc, ClusterException):
                print "exception: ", rc
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        host = Host(host_name)
        return render(request, 'openlavaweb/host_close_confirm.html', {"object": host})


def execute_host_close(request, queue, host_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        h = Host(host_name)
        h.close()

        if request.is_ajax():
            queue.put(create_js_response())
        else:
            queue.put(HttpResponseRedirect(reverse("olw_host_view", args=[host_name])))
    except Exception as e:
        queue.put(e)


@login_required
def host_open(request, host_name):
    host_name = str(host_name)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'host_name': host_name,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_host_open, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        host = Host(host_name)
        return render(request, 'openlavaweb/host_open_confirm.html', {"object": host})


def execute_host_open(request, queue, host_name):
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        h = Host(host_name)
        h.open()
        if request.is_ajax():
            queue.put(create_js_response())
        else:
            queue.put(HttpResponseRedirect(reverse("olw_host_view", args=[host_name])))
    except Exception as e:
        queue.put(e)


def user_list(request):
    users = User.get_user_list()
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(data=users)
    paginator = Paginator(users, 25)
    page = request.GET.get('page')
    try:
        users = paginator.page(page)
    except PageNotAnInteger:
        users = paginator.page(1)
    except EmptyPage:
        users = paginator.page(paginator.num_pages)
    return render(request, 'openlavaweb/user_list.html', {"user_list": users})


def user_view(request, user_name):
    try:
        user = User(user_name)
    except ValueError:
        raise Http404("User not found")
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(user)
    return render(request, 'openlavaweb/user_detail.html', {"oluser": user}, )


def system_view(request):
    cluster = Cluster()
    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(cluster)

    return render(request, 'openlavaweb/system_view.html', {'cluster': cluster})


# noinspection PyUnusedLocal
def system_overview_hosts(request):
    cluster = Cluster()
    states = {
        'Down': 0,
        'Full': 0,
        'In Use': 0,
        'Empty': 0,
        'Closed': 0,
    }
    for host in cluster.hosts():
        if not host.is_server:
            continue
        if host.is_down:
            states['Down'] += 1
        elif host.is_busy:
            states['Full'] += 1
        elif host.is_closed:
            states['Closed'] += 1
        elif len(host.jobs()) > 0:
            states['In Use'] += 1
        else:
            states['Empty'] += 1

    nvstates = []
    for k, v in states.iteritems():
        nvstates.append(
            {'label': k, 'value': v}
        )
    return create_js_response(nvstates)


# noinspection PyUnusedLocal
def system_overview_jobs(request):
    cluster = Cluster()
    states = {}

    for job in cluster.jobs():
        try:
            states[job.status.friendly] += 1
        except KeyError:
            states[job.status.friendly] = 1
    nvstates = []
    for k, v in states.iteritems():
        nvstates.append(
            {'label': k, 'value': v}
        )
    return create_js_response(nvstates)

# noinspection PyUnusedLocal
def system_overview_slots(request):
    cluster = Cluster()
    states = {}

    for job in cluster.jobs():
        try:
            states[job.status.friendly] += job.requested_slots
        except KeyError:
            states[job.status.friendly] = job.requested_slots

    nvstates = []
    for k, v in states.iteritems():
        nvstates.append(
            {'label': k, 'value': v}
        )
    return create_js_response(nvstates)

def get_job_list(request, job_id=0):
    """
    Renders a HTML page listing jobs that match the query.

    :param request:
        Request object

    :param job_id:
        Numeric Job ID.  If job_id != 0, then all elements of that job will be returned, use this to see all tasks from
        an array job.

    :param ?queue_name:
            The name of the queue.  If specified, implies that job_id and array_index are set to default.  Only returns
            jobs that are submitted into the named queue.

    :param ?host_name:
        The name of the host.  If specified, implies that job_id and array_index are set to default.  Only returns
        jobs that are executing on the specified host.

    :param ?user_name:
        The name of the user.  If specified, implies that job_id and array_index are set to default.  Only returns
        jobs that are owned by the specified user.

    :param ?job_state:
        Only return jobs in this state, state can be "ACT" - all active jobs, "ALL" - All jobs, including finished
        jobs, "EXIT" - Jobs that have exited due to an error or have been killed by the user or an administrator,
        "PEND" - Jobs that are in a pending state, "RUN" - Jobs that are currently running, "SUSP" Jobs that are
        currently suspended.

    :param ?job_name:
        Only return jobs that are named job_name.

    :return:
        If an ajax request, then returns an array of JSON Job objects that match the query. Otherwise returns a
        rendered HTML page listing each job.  Pages are paginated using a paginator.

        Example JSON response::

            {
                "data": [
                    {
                        "admins": [
                            "irvined",
                            "openlava"
                        ],
                        "array_index": 0,
                        "begin_time": 0,
                        "checkpoint_directory": "",
                        "checkpoint_period": 0,
                        "cluster_type": "openlava",
                        "command": "sleep 1000",
                        "consumed_resources": [
                            {
                                "limit": "-1",
                                "name": "Resident Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "Virtual Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "User Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "System Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "Num Active Processes",
                                "type": "ConsumedResource",
                                "unit": "Processes",
                                "value": "0"
                            }
                        ],
                        "cpu_factor": 0.0,
                        "cpu_time": 0.0,
                        "cwd": "openlava-web/tests",
                        "dependency_condition": "",
                        "email_user": "",
                        "end_time": 0,
                        "error_file_name": "/dev/null",
                        "execution_cwd": "//main/home/irvined/openlava-web/tests",
                        "execution_home_directory": "",
                        "execution_hosts": [],
                        "execution_user_id": 1000,
                        "execution_user_name": "irvined",
                        "host_specification": "",
                        "input_file_name": "/dev/null",
                        "is_completed": false,
                        "is_failed": false,
                        "is_pending": false,
                        "is_running": false,
                        "is_suspended": true,
                        "job_id": 9767,
                        "login_shell": "",
                        "max_requested_slots": 1,
                        "name": "sleep 1000",
                        "options": [
                            {
                                "description": "",
                                "friendly": "Job submitted with queue",
                                "name": "SUB_QUEUE",
                                "status": 2,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted to project",
                                "name": "SUB_PROJECT_NAME",
                                "status": 33554432,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted with output file",
                                "name": "SUB_OUT_FILE",
                                "status": 16,
                                "type": "SubmitOption"
                            }
                        ],
                        "output_file_name": "/dev/null",
                        "parent_group": "/",
                        "pending_reasons": " The job was suspended by the user while pending: 1 host;",
                        "pre_execution_command": "",
                        "predicted_start_time": 0,
                        "priority": -1,
                        "process_id": 11567,
                        "processes": [],
                        "project_names": [
                            "default"
                        ],
                        "queue": {
                            "name": "normal",
                            "type": "Queue",
                            "url": "/olweb/olw/queues/normal"
                        },
                        "requested_hosts": [],
                        "requested_resources": "",
                        "requested_slots": 1,
                        "reservation_time": 0,
                        "resource_usage_last_update_time": 1414241622,
                        "runtime_limits": [
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "CPU Time",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "File Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Data Segment Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Stack Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Core Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "RSS Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Num Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Max Open Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Swap Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Run Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Process Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            }
                        ],
                        "service_port": 0,
                        "start_time": 0,
                        "status": {
                            "description": "The pending job was suspended by its owner or the LSF system administrator.",
                            "friendly": "Held",
                            "name": "JOB_STAT_PSUSP",
                            "status": 2,
                            "type": "JobStatus"
                        },
                        "submission_host": {
                            "name": "main",
                            "type": "Host",
                            "url": "/olweb/olw/hosts/main"
                        },
                        "submit_home_directory": "/home/irvined",
                        "submit_time": 1414241607,
                        "suspension_reasons": " The job was suspended by user;",
                        "termination_signal": 0,
                        "termination_time": 0,
                        "type": "Job",
                        "user_name": "irvined",
                        "user_priority": -1,
                        "was_killed": false
                    },
                    {
                        "admins": [
                            "irvined",
                            "openlava"
                        ],
                        "array_index": 0,
                        "begin_time": 0,
                        "checkpoint_directory": "",
                        "checkpoint_period": 0,
                        "cluster_type": "openlava",
                        "command": "sleep 1000",
                        "consumed_resources": [
                            {
                                "limit": "-1",
                                "name": "Resident Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "Virtual Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "User Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "System Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "Num Active Processes",
                                "type": "ConsumedResource",
                                "unit": "Processes",
                                "value": "0"
                            }
                        ],
                        "cpu_factor": 0.0,
                        "cpu_time": 0.0,
                        "cwd": "openlava-web/tests",
                        "dependency_condition": "",
                        "email_user": "",
                        "end_time": 0,
                        "error_file_name": "/dev/null",
                        "execution_cwd": "//main/home/irvined/openlava-web/tests",
                        "execution_home_directory": "",
                        "execution_hosts": [],
                        "execution_user_id": 1000,
                        "execution_user_name": "irvined",
                        "host_specification": "",
                        "input_file_name": "/dev/null",
                        "is_completed": false,
                        "is_failed": false,
                        "is_pending": false,
                        "is_running": false,
                        "is_suspended": true,
                        "job_id": 9776,
                        "login_shell": "",
                        "max_requested_slots": 1,
                        "name": "sleep 1000",
                        "options": [
                            {
                                "description": "",
                                "friendly": "Job submitted with queue",
                                "name": "SUB_QUEUE",
                                "status": 2,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted to project",
                                "name": "SUB_PROJECT_NAME",
                                "status": 33554432,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted with output file",
                                "name": "SUB_OUT_FILE",
                                "status": 16,
                                "type": "SubmitOption"
                            }
                        ],
                        "output_file_name": "/dev/null",
                        "parent_group": "/",
                        "pending_reasons": " The job was suspended by the user while pending: 1 host;",
                        "pre_execution_command": "",
                        "predicted_start_time": 0,
                        "priority": -1,
                        "process_id": 13254,
                        "processes": [],
                        "project_names": [
                            "default"
                        ],
                        "queue": {
                            "name": "normal",
                            "type": "Queue",
                            "url": "/olweb/olw/queues/normal"
                        },
                        "requested_hosts": [],
                        "requested_resources": "",
                        "requested_slots": 1,
                        "reservation_time": 0,
                        "resource_usage_last_update_time": 1414242212,
                        "runtime_limits": [
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "CPU Time",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "File Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Data Segment Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Stack Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Core Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "RSS Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Num Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Max Open Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Swap Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Run Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Process Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            }
                        ],
                        "service_port": 0,
                        "start_time": 0,
                        "status": {
                            "description": "The pending job was suspended by its owner or the LSF system administrator.",
                            "friendly": "Held",
                            "name": "JOB_STAT_PSUSP",
                            "status": 2,
                            "type": "JobStatus"
                        },
                        "submission_host": {
                            "name": "main",
                            "type": "Host",
                            "url": "/olweb/olw/hosts/main"
                        },
                        "submit_home_directory": "/home/irvined",
                        "submit_time": 1414242197,
                        "suspension_reasons": " The job was suspended by user;",
                        "termination_signal": 0,
                        "termination_time": 0,
                        "type": "Job",
                        "user_name": "irvined",
                        "user_priority": -1,
                        "was_killed": false
                    },
                    {
                        "admins": [
                            "irvined",
                            "openlava"
                        ],
                        "array_index": 0,
                        "begin_time": 0,
                        "checkpoint_directory": "",
                        "checkpoint_period": 0,
                        "cluster_type": "openlava",
                        "command": "sleep 100",
                        "consumed_resources": [
                            {
                                "limit": "-1",
                                "name": "Resident Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "Virtual Memory",
                                "type": "ConsumedResource",
                                "unit": "KB",
                                "value": "0"
                            },
                            {
                                "limit": "-1",
                                "name": "User Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "System Time",
                                "type": "ConsumedResource",
                                "unit": "None",
                                "value": "0:00:00"
                            },
                            {
                                "limit": "None",
                                "name": "Num Active Processes",
                                "type": "ConsumedResource",
                                "unit": "Processes",
                                "value": "0"
                            }
                        ],
                        "cpu_factor": 0.0,
                        "cpu_time": 0.0,
                        "cwd": "openlava-web/tests",
                        "dependency_condition": "",
                        "email_user": "",
                        "end_time": 0,
                        "error_file_name": "/dev/null",
                        "execution_cwd": "//main/home/irvined/openlava-web/tests",
                        "execution_home_directory": "",
                        "execution_hosts": [],
                        "execution_user_id": 1000,
                        "execution_user_name": "irvined",
                        "host_specification": "",
                        "input_file_name": "/dev/null",
                        "is_completed": false,
                        "is_failed": false,
                        "is_pending": false,
                        "is_running": false,
                        "is_suspended": true,
                        "job_id": 9781,
                        "login_shell": "",
                        "max_requested_slots": 1,
                        "name": "sleep 100",
                        "options": [
                            {
                                "description": "",
                                "friendly": "Job submitted with queue",
                                "name": "SUB_QUEUE",
                                "status": 2,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted to project",
                                "name": "SUB_PROJECT_NAME",
                                "status": 33554432,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted with output file",
                                "name": "SUB_OUT_FILE",
                                "status": 16,
                                "type": "SubmitOption"
                            },
                            {
                                "description": "",
                                "friendly": "Job submitted with checkpoint period",
                                "name": "SUB_CHKPNT_PERIOD",
                                "status": 1024,
                                "type": "SubmitOption"
                            }
                        ],
                        "output_file_name": "/dev/null",
                        "parent_group": "/",
                        "pending_reasons": " The job was suspended by the user while pending: 1 host;",
                        "pre_execution_command": "",
                        "predicted_start_time": 0,
                        "priority": -1,
                        "process_id": 30160,
                        "processes": [],
                        "project_names": [
                            "default"
                        ],
                        "queue": {
                            "name": "normal",
                            "type": "Queue",
                            "url": "/olweb/olw/queues/normal"
                        },
                        "requested_hosts": [],
                        "requested_resources": "",
                        "requested_slots": 1,
                        "reservation_time": 0,
                        "resource_usage_last_update_time": 1414243932,
                        "runtime_limits": [
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "CPU Time",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "File Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Data Segment Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Stack Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Core Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "RSS Size",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Num Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Max Open Files",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Swap Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "KB"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Run Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            },
                            {
                                "description": "None",
                                "hard_limit": "-1",
                                "name": "Process Limit",
                                "soft_limit": "-1",
                                "type": "ResourceLimit",
                                "unit": "None"
                            }
                        ],
                        "service_port": 0,
                        "start_time": 0,
                        "status": {
                            "description": "The pending job was suspended by its owner or the LSF system administrator.",
                            "friendly": "Held",
                            "name": "JOB_STAT_PSUSP",
                            "status": 2,
                            "type": "JobStatus"
                        },
                        "submission_host": {
                            "name": "main",
                            "type": "Host",
                            "url": "/olweb/olw/hosts/main"
                        },
                        "submit_home_directory": "/home/irvined",
                        "submit_time": 1414243874,
                        "suspension_reasons": " The job was suspended by user;",
                        "termination_signal": 0,
                        "termination_time": 0,
                        "type": "Job",
                        "user_name": "irvined",
                        "user_priority": -1,
                        "was_killed": false
                    }
                ],
                "message": "",
                "status": "OK"
            }

    """
    job_id = int(job_id)
    if job_id != 0:
        # Get a list of active elements of the specified job.
        job_list = Job.get_job_list(job_id=job_id, array_index=-1)
    else:
        user_name = request.GET.get('user_name', 'all')
        queue_name = request.GET.get('queue_name', "")
        host_name = request.GET.get('host_name', "")
        job_state = request.GET.get('job_state', 'ACT')
        job_name = request.GET.get('job_name', "")
        job_list = Job.get_job_list(user_name=user_name, queue_name=queue_name, host_name=host_name,
                                    job_state=job_state,
                                    job_name=job_name)

    if request.is_ajax() or request.GET.get("json", None):
        return create_js_response(data=job_list)

    paginator = Paginator(job_list, 50)
    page = request.GET.get('page')
    try:
        job_list = paginator.page(page)
    except PageNotAnInteger:
        job_list = paginator.page(1)
    except EmptyPage:
        job_list = paginator.page(paginator.num_pages)
    return render(request, 'openlavaweb/job_list.html', {"job_list": job_list, })


def job_view(request, job_id, array_index=0):
    """
    Renders a HTML page showing the specified job.

    :param request: Request object
    :param job_id: Job ID.
    :param array_index: The array index, must be zero or higher.
    :return:

        If the request is AJAX, returns a single json encoded job object.  If it is not an ajax request, then renders
        a HTML page showing information about the specified job.

        If the job does not exist, raises and renders NoSuchJob


    """
    job_id = int(job_id)
    array_index = int(array_index)
    assert (array_index >= 0)
    try:
        job = Job(job_id=job_id, array_index=array_index)
        if request.is_ajax() or request.GET.get("json", None):
            return create_js_response(data=job)
        else:
            return render(request, 'openlavaweb/job_detail.html', {"job": job, }, )
    except ClusterException as e:
        if request.is_ajax() or request.GET.get("json", None):
            return handle_cluster_exception(e)
        else:
            return render(request, 'openlavaweb/exception.html', {'exception': e})


def execute_get_output_path(request, queue, job_id, array_index):
    """
    SetUIDS to the specified user and gets the output path for the specified job

    :param request: Request Object
    :param queue: MPQueue Object
    :param job_id: Job ID
    :param array_index: Array Index of Job (Optional)
    :return: Output path

    """
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        job = Job(job_id=job_id, array_index=array_index)
        path = job.get_output_path()
        if path:
            queue.put(path)
    except Exception as e:
        queue.put(e)


def job_error(request, job_id, array_index=0):
    """
    Returns the job error output

    :param request: Request Object
    :param job_id: Job ID
    :param array_index: Array Index (Optional)
    :return: Job STDERR
    :rtype: text/plain

    """
    job_id = int(job_id)
    array_index = int(array_index)
    try:
        q = MPQueue()
        kwargs = {
            'job_id': job_id,
            'array_index': array_index,
            'request': request,
            'queue': q,
        }
        p = MPProcess(target=execute_get_output_path, kwargs=kwargs)
        p.start()
        p.join()
        if q.empty():
            path = None
        else:
            path = q.get(False)

        if isinstance(path, Exception):
            raise path
        if path:
            path += ".err"
        if path and os.path.exists(path):

            f = open(path, 'r')
            return HttpResponse(f, mimetype="text/plain")
        else:
            return HttpResponse("Not Available", content_type="text/plain")

    except ClusterException as e:
        if request.is_ajax() or request.GET.get("json", None):
            return handle_cluster_exception(e)
        else:
            return render(request, 'openlavaweb/exception.html', {'exception': e})


def job_output(request, job_id, array_index=0):
    """
    Returns the job standard output

    :param request: Request Object
    :param job_id: Job ID
    :param array_index: Array Index (Optional)
    :return: Job STDOUT
    :rtype: text/plain

    """
    job_id = int(job_id)
    array_index = int(array_index)
    try:
        q = MPQueue()
        kwargs = {
            'job_id': job_id,
            'array_index': array_index,
            'request': request,
            'queue': q,
        }
        p = MPProcess(target=execute_get_output_path, kwargs=kwargs)
        p.start()
        p.join()
        if q.empty():
            path = None
        else:
            path = q.get(False)

        if isinstance(path, Exception):
            raise path

        if path:
            path += ".out"
        if path and os.path.exists(path):
            f = open(path, 'r')
            return HttpResponse(f, mimetype="text/plain")
        else:
            return HttpResponse("Not Available", mimetype="text/plain")

    except ClusterException as e:
        if request.is_ajax() or request.GET.get("json", None):
            return handle_cluster_exception(e)
        else:
            return render(request, 'openlavaweb/exception.html', {'exception': e})


@login_required
def job_kill(request, job_id, array_index=0):
    """
    Kills the specified job, if using ajax kills directly, if not using ajax, presents a confirmation
    screen first.

    :param request: Request object
    :param job_id: Job ID to kill
    :param array_index: Array index of array task (Optional)
    :return: Redirects to job list or returns AJAX succcess for ajax requests
    """
    job_id = int(job_id)
    array_index = int(array_index)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'job_id': job_id,
                'array_index': array_index,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_job_kill, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        job = Job(job_id=job_id, array_index=array_index)
        return render(request, 'openlavaweb/job_kill_confirm.html', {"object": job})


def execute_job_kill(request, queue, job_id, array_index):
    """
    Setuids to the user of the request, and then kills their job

    :param request: Request object
    :param queue:  MPQueue object
    :param job_id: ID of job to kill
    :param array_index:  Array index of job to kill
    :return: JS Success for ajax requests, else redirects to job list.
    """
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        job = Job(job_id=job_id, array_index=array_index)
        job.kill()
        if request.is_ajax():
            queue.put(create_js_response("Job Killed"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_job_list")))
    except Exception as e:
        queue.put(e)


@login_required
def job_suspend(request, job_id, array_index=0):
    """
    Suspends the requested job

    :param request:
    :param job_id:
    :param array_index:
    :return:
    """
    job_id = int(job_id)
    array_index = int(array_index)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'job_id': job_id,
                'array_index': array_index,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_job_suspend, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        job = Job(job_id=job_id, array_index=array_index)
        return render(request, 'openlavaweb/job_suspend_confirm.html', {"object": job})


def execute_job_suspend(request, queue, job_id, array_index):
    """
    Actually performs the job suspend action by setuid'ing to the requested user.

    :param request:
    :param queue:
    :param job_id:
    :param array_index:
    :return:
    """
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        job = Job(job_id=job_id, array_index=array_index)
        job.suspend()
        if request.is_ajax() or request.GET.get("json", None):
            queue.put(create_js_response(message="Job suspended"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_job_view_array", args=[job_id, array_index])))
    except Exception as e:
        queue.put(e)


@login_required
def job_resume(request, job_id, array_index=0):
    """
    Resumes a suspended job

    :param request:
    :param job_id:
    :param array_index:
    :return:

    """
    job_id = int(job_id)
    array_index = int(array_index)
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:
            q = MPQueue()
            kwargs = {
                'job_id': job_id,
                'array_index': array_index,
                'request': request,
                'queue': q,
            }
            p = MPProcess(target=execute_job_resume, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        job = Job(job_id=job_id, array_index=array_index)
        return render(request, 'openlavaweb/job_resume_confirm.html', {"object": job})


def execute_job_resume(request, queue, job_id, array_index):
    """
    Performs the job resume request

    :param request:
    :param queue:
    :param job_id:
    :param array_index:
    :return:

    """
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        job = Job(job_id=job_id, array_index=array_index)
        job.resume()
        if request.is_ajax() or request.GET.get("json", None):
            queue.put(create_js_response(message="Job Resumed"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_job_view_array", args=[job_id, array_index])))
    except Exception as e:
        queue.put(e)


@login_required
def job_requeue(request, job_id, array_index=0):
    """
    Requeues a running or suspended job

    :param request:
    :param job_id:
    :param array_index:
    :return:

    """
    job_id = int(job_id)
    array_index = int(array_index)
    hold = False
    if request.GET.get("hold", False):
        hold = True
    if request.GET.get('confirm', None) or request.is_ajax() or request.GET.get("json", None):
        try:

            q = MPQueue()
            kwargs = {
                'job_id': job_id,
                'array_index': array_index,
                'request': request,
                'queue': q,
                'hold': hold,
            }
            p = MPProcess(target=execute_job_requeue, kwargs=kwargs)
            p.start()
            p.join()
            rc = q.get(False)
            if isinstance(rc, Exception):
                raise rc
            else:
                return rc
        except ClusterException as e:
            if request.is_ajax() or request.GET.get("json", None):
                return handle_cluster_exception(e)
            else:
                return render(request, 'openlavaweb/exception.html', {'exception': e})
    else:
        job = Job(job_id=job_id, array_index=array_index)
        return render(request, 'openlavaweb/job_requeue_confirm.html', {"object": job, 'hold': hold})


def execute_job_requeue(request, queue, job_id, array_index, hold):
    """
    Actually performs the requeue operation

    :param request:
    :param queue:
    :param job_id:
    :param array_index:
    :param hold:
    :return:

    """
    try:
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        job = Job(job_id=job_id, array_index=array_index)
        job.requeue(hold=hold)
        if request.is_ajax() or request.GET.get("json", None):
            queue.put(create_js_response(message="Job Requeued"))
        else:
            queue.put(HttpResponseRedirect(reverse("olw_job_view_array", args=[job_id, array_index])))
    except Exception as e:
        queue.put(e)


@login_required
def job_submit(request, form_class="JobSubmitForm"):
    """
    Submits a new job using the specified form and redirects the user to view the job

    :param request: Request object
    :param form_class: The form class to use when rendering and validating
    :return: List of submitted jobs.
    """
    logger = log_to_stderr()
    logger.debug("Starting sub")
    ajax_args = None

    for cls in OLWSubmit.__subclasses__():
        if form_class == cls.__name__:
            form_class = cls
            break
    if not issubclass(form_class, OLWSubmit):
        raise ValueError

    if request.is_ajax() or request.GET.get("json", None):
        # configure form and arguments for ajax submission
        ajax_args = json.loads(request.body)
        form = form_class()
    else:
        # configure form an arguments for normal submission
        if request.method == 'POST':
            form = form_class(request.POST)
            # IF the form is not valid, then return the rendered form.
            if not form.is_valid():
                return render(request, 'openlavaweb/job_submit.html', {'form': form})
        else:
            # The form wasn't submitted yet, so render the form.
            form = form_class()
            return render(request, 'openlavaweb/job_submit.html', {'form': form})

    # Process the actual form.
    q = MPQueue()
    p = MPProcess(target=execute_job_submit,
                  kwargs={'queue': q, 'request': request, 'ajax_args': ajax_args, 'submit_form': form})
    p.start()
    rc = q.get(True)
    p.join()
    try:
        if isinstance(rc, Exception):
            raise rc
        else:
            return rc
    except ClusterException as e:
        if request.is_ajax() or request.GET.get("json", None):
            return handle_cluster_exception(e)
        else:
            return render(request, 'openlavaweb/exception.html', {'exception': e})


def execute_job_submit(request, queue, ajax_args, submit_form):
    """
    Changes to the current user using setuid, and submits a job using the provided arguments.

    Upon successful submission returns a list of jobs, or redirects to view the job (Non ajax requests)

    :param request: Request object
    :param queue: MPQueue object
    :param ajax_args: Job submit arguments sent using AJAX
    :param submit_form: Submission form
    :return: JSON job list if using ajax, else redirect to view job array
    """
    logger = log_to_stderr()
    logger.setLevel(logging.DEBUG)
    logger.debug("Entering execute_job_submit")
    try:
        logger.debug("Setting user ID")
        user_id = pwd.getpwnam(request.user.username).pw_uid
        os.setuid(user_id)
        logger.debug("Set UID")
        logger.debug("Submitting form")
        queue.put(submit_form.submit(ajax_args))
        logger.debug("Form Submitted to Queue")
    except Exception as e:
        logger.debug("Got Exception, putting to queue")
        queue.put(e)


class OLWSubmit(forms.Form):
    """Openlava job submission form, redirects the user to the first job, or if ajax, dumps all jobs"""
    name = "basesubmit"
    friendly_name = "Base Submit"
    # If this needs to be treated as a formset, set to true so the
    # template knows to iterate through each form etc.
    is_formset = False

    def get_name(self):
        return self.__class__.__name__

    def submit(self, ajax_args=None):
        logger = log_to_stderr()
        logger.debug("Entering OLWSubmit submit")
        if ajax_args:
            kwargs = ajax_args
        else:
            kwargs = self._get_args()
        logger.debug("Calling Pre Submit")
        self._pre_submit()
        logger.debug("Returned from Pre Submit")
        try:
            logger.debug("Submitting Job")
            jobs = Job.submit(**kwargs)
            logger.debug("Job Submitted, jobs has: %d elements" % len(jobs))
            logger.debug("Calling Post Submit")
            self._post_submit(jobs)
            logger.debug("Returned from Post Submit")
            if ajax_args:
                return create_js_response(jobs, message="Job Submitted")
            return HttpResponseRedirect(reverse("olw_job_view_array", args=[jobs[0].job_id, jobs[0].array_index]))
        except Exception as e:
            return e

    def _get_args(self):
        """Return all arguments for job submission.  For normal simple web submission forms, this is all that is needed
        simply parse the form data and return a dict which will be passed to Job.submit()"""
        raise NotImplemented()

    # noinspection PyMethodMayBeStatic
    def _pre_submit(self):
        """Called before the job is submitted, run as the user who is submitting the job."""
        return None

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _post_submit(self, job_list):
        """Called after the job has been submitted, job_list is a list of Job Objects that have been submitted"""
        return None


class JobSubmitForm(OLWSubmit):
    friendly_name = "Generic Job"

    def _get_args(self):
        kwargs = {}
        if 'options' in self.cleaned_data:
            opt = 0
            for i in self.cleaned_data['options']:
                opt |= int(i)
            kwargs['options'] = opt

        if 'options2' in self.cleaned_data:
            opt = 0
            for i in self.cleaned_data['options2']:
                opt |= int(i)
            kwargs['options2'] = opt

        for field, value in self.cleaned_data.items():
            if field in ['options', 'options2']:
                continue
            if value:
                kwargs[field] = value
        return kwargs

    # transfer files
    #rlimits

    # options....
    opts = [
        (lsblib.SUB_EXCLUSIVE, "Exclusive"),
        (lsblib.SUB_NOTIFY_END, "Notify End"),
        (lsblib.SUB_NOTIFY_BEGIN, "Notify Begin"),
        (lsblib.SUB_RERUNNABLE, "Re-Runnable"),
    ]
    opts2 = [
        (lsblib.SUB2_HOLD, "Hold Job"),
        (lsblib.SUB2_QUEUE_CHKPNT, "Checkpointable Queue Only"),
        (lsblib.SUB2_QUEUE_RERUNNABLE, "Re-Runnable Queue Only"),
    ]
    options = forms.MultipleChoiceField(choices=opts, required=False)
    options2 = forms.MultipleChoiceField(choices=opts2, required=False)
    requested_slots = forms.IntegerField(initial=1)
    command = forms.CharField(widget=forms.Textarea, max_length=512)
    job_name = forms.CharField(max_length=512, required=False)
    queues = [(u'', u'Default')]
    for q in Queue.get_queue_list():
        queues.append((q.name, q.name))
    queue_name = forms.ChoiceField(choices=queues, required=False)
    hosts = []
    for h in Host.get_host_list():
        hosts.append([h.name, h.name])
    requested_hosts = forms.MultipleChoiceField(choices=hosts, required=False)
    resource_request = forms.CharField(max_length=512, required=False)
    ## Rlimits
    host_specification = forms.CharField(max_length=512, required=False)
    dependency_conditions = forms.CharField(max_length=512, required=False)
    #begin_time=forms.DateTimeField(required=False)
    #term_time=forms.DateTimeField(required=False)
    signal_value = forms.IntegerField(required=False)
    input_file = forms.CharField(max_length=512, required=False)
    output_file = forms.CharField(max_length=512, required=False)
    error_file = forms.CharField(max_length=512, required=False)
    checkpoint_period = forms.IntegerField(required=False)
    checkpoint_directory = forms.CharField(max_length=512, required=False)
    email_user = forms.EmailField(required=False)
    project_name = forms.CharField(max_length=128, required=False)
    max_requested_slots = forms.IntegerField(required=False)
    login_shell = forms.CharField(128, required=False)
    user_priority = forms.IntegerField(required=False)


class SimpleJobSubmitForm(OLWSubmit):
    friendly_name = "Simple Job"

    def _get_args(self):
        kwargs = {}
        for field, value in self.cleaned_data.items():
            if field in ['options', 'options2']:
                continue
            if value:
                kwargs[field] = value
        return kwargs

    requested_slots = forms.IntegerField(initial=1)
    command = forms.CharField(widget=forms.Textarea, max_length=512)
    queues = [(u'', u'Default')]
    for q in Queue.get_queue_list():
        queues.append((q.name, q.name))
    queue_name = forms.ChoiceField(choices=queues, required=False)


class ConsumeResourcesJob(OLWSubmit):
    friendly_name = "Consume Resources"

    job_name = forms.CharField(max_length=512, required=False)
    requested_slots = forms.ChoiceField(choices=[(x, x) for x in xrange(1, 6)], initial=1,
                                        help_text="How many processors to execute on")
    run_time = forms.IntegerField(min_value=1, initial=120, help_text="How many seconds to execute for")

    memory_size = forms.IntegerField(min_value=1, initial=128, help_text="How many MB to consume")
    consume_cpu = forms.BooleanField(required=False, initial=True, help_text="Burn CPU cycles.")
    consume_network = forms.BooleanField(required=False, initial=False, help_text="Send MPI messages. (Experimental)")
    consume_disk = forms.BooleanField(required=False, initial=False, help_text="Read and write data to storage.")

    queues = [(u'', u'Default')]
    for q in Queue.get_queue_list():
        queues.append((q.name, q.name))
    queue_name = forms.ChoiceField(choices=queues, required=False)

    def _get_args(self):
        kwargs = {}

        if len(self.cleaned_data['job_name']) > 0:
            kwargs['job_name'] = self.cleaned_data['job_name']

        kwargs['requested_slots'] = self.cleaned_data['requested_slots']
        kwargs['queue_name'] = self.cleaned_data['queue_name']
        kwargs['job_name'] = self.cleaned_data['job_name']

        try:
            mpi_command = settings.MPIRUN_COMMAND
        except AttributeError:
            mpi_command = "mpirun"

        try:
            command = settings.CONSUME_RESOURCES_COMMAND
        except AttributeError:
            command = "consumeResources.py"

        if self.cleaned_data['consume_cpu']:
            command += " -c"
        if self.cleaned_data['consume_network']:
            command += " -n"
        if self.cleaned_data['consume_disk']:
            command += " -d"

        command += " -m "
        command += str(self.cleaned_data['memory_size'])
        command += " " + str(self.cleaned_data['run_time'])

        command = mpi_command + " " + command
        kwargs['command'] = command

        return kwargs


# noinspection PyUnusedLocal
def submit_form_context(request):
    clses = []
    for cls in OLWSubmit.__subclasses__():
        clses.append({
            'url': reverse("olw_job_submit_class", args=[cls.__name__]),
            'name': cls.friendly_name,
        })
    return {'submit_form_classes': clses}


def exception_test(request):
    exc_name = request.GET.get("exception_name", None)
    try:
        if exc_name:
            for ex in ClusterException.__subclasses__():
                if ex.__name__ == exc_name:
                    raise ex("Exception Test")
    except ClusterException as e:
        if request.is_ajax() or request.GET.get("json", None):
            return handle_cluster_exception(e)
        else:
            return render(request, 'openlavaweb/exception.html', {'exception': e})
    return render(request, 'openlavaweb/exception_test.html', {'classes': [e.__name__ for e in ClusterException.__subclasses__()]})


def js_tests(request):
    return render(request, 'openlavaweb/js_tests.html', {'classes': [e.__name__ for e in ClusterException.__subclasses__()]})
