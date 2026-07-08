# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Tenant propagation for Celery tasks.

The tenant travels in a message header: a ``before_task_publish`` hook injects
it from the current context, and the task prologue (see
``superdesk/celery_app/context_task.py``) restores it before the task body runs.
Fail-closed: in multi-tenant mode a task without a tenant header is rejected
unless it is registered as tenant-agnostic.
"""

import logging
from typing import Iterator, Optional
from contextlib import contextmanager

from celery.signals import before_task_publish

from .models import Tenant
from .errors import TenantNotSetError
from .context import is_multi_tenant_enabled, try_get_current_tenant, tenant_context

logger = logging.getLogger(__name__)

TENANT_TASK_HEADER = "tenant_id"

#: name of the beat dispatcher task that re-publishes beat tasks once per active tenant
FAN_OUT_TASK_NAME = "tenants.fan_out"

#: tasks that may be published/consumed without a tenant (dispatchers, control-plane jobs)
TENANT_AGNOSTIC_TASKS: set[str] = {FAN_OUT_TASK_NAME}


def register_tenant_agnostic_task(*names: str) -> None:
    TENANT_AGNOSTIC_TASKS.update(names)


def setup_celery_tenant_signals() -> None:
    before_task_publish.connect(_inject_tenant_header, weak=False, dispatch_uid="tenants.inject_header")


def _inject_tenant_header(sender=None, headers=None, **kwargs) -> None:
    """``before_task_publish`` hook; ``sender`` is the task name."""

    if not is_multi_tenant_enabled() or headers is None:
        return

    # nested "headers" handles the kombu/protocol quirk where apply_async(headers=...)
    # and the publish signal see different levels of the message headers
    nested = headers.setdefault("headers", {})
    if not isinstance(nested, dict):
        nested = {}

    existing = headers.get(TENANT_TASK_HEADER) or nested.get(TENANT_TASK_HEADER)
    if existing:
        headers[TENANT_TASK_HEADER] = existing
        nested[TENANT_TASK_HEADER] = existing
        return

    tenant = try_get_current_tenant()
    if tenant is not None:
        headers[TENANT_TASK_HEADER] = tenant.id
        nested[TENANT_TASK_HEADER] = tenant.id
    elif sender not in TENANT_AGNOSTIC_TASKS:
        raise TenantNotSetError(f"Publishing task '{sender}' without a tenant in context")


def get_task_tenant_id(task) -> Optional[str]:
    """Extract the tenant id from a task's request, wherever the protocol put it."""

    request = task.request
    tenant_id = getattr(request, TENANT_TASK_HEADER, None)
    if tenant_id:
        return tenant_id
    headers = getattr(request, "headers", None) or {}
    return headers.get(TENANT_TASK_HEADER)


@contextmanager
def task_tenant_context(task) -> Iterator[Optional[Tenant]]:
    """Bind the tenant from the task's headers around the task body.

    Falls back to the ambient context tenant (eager mode / direct calls);
    fails closed for tasks without any tenant unless registered agnostic.
    """

    if not is_multi_tenant_enabled():
        yield None
        return

    tenant_id = get_task_tenant_id(task)
    if tenant_id is None:
        ambient = try_get_current_tenant()
        if ambient is not None:
            # eager mode or direct call, tenant already bound
            yield ambient
            return
        if task.name in TENANT_AGNOSTIC_TASKS:
            yield None
            return
        raise TenantNotSetError(f"Task '{task.name}' received without a tenant header")

    with tenant_context(tenant_id) as tenant:
        yield tenant


def rewrite_beat_schedule_for_tenants(schedule: dict) -> dict:
    """Rewrite beat entries to go through the fan-out dispatcher.

    ``{"task": X, "args": A}`` becomes ``{"task": FAN_OUT, "args": (X, *A)}``
    so the original ~task definitions stay untouched and each active tenant
    gets its own tenant-tagged copy of the task.
    """

    rewritten = {}
    for name, entry in schedule.items():
        entry = dict(entry)
        task_name = entry.get("task")
        if task_name and task_name not in TENANT_AGNOSTIC_TASKS:
            entry["args"] = (task_name, *tuple(entry.get("args") or ()))
            entry["task"] = FAN_OUT_TASK_NAME
        rewritten[name] = entry
    return rewritten
