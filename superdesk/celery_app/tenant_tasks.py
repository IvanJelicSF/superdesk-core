# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import logging

from superdesk.core import get_current_async_app
from superdesk.core.tenants.celery import FAN_OUT_TASK_NAME, TENANT_TASK_HEADER

from . import celery

logger = logging.getLogger(__name__)


@celery.task(name=FAN_OUT_TASK_NAME)
def fan_out_task(task_name: str, *args, **kwargs):
    """Beat dispatcher: re-publish a scheduled task once per active tenant."""

    registry = get_current_async_app().tenants
    tenants = registry.get_all_active_sync()
    published = 0
    for tenant in tenants:
        if not tenant.feature_enabled(f"beat:{task_name}", True):
            continue
        celery.signature(task_name, args=args, kwargs=kwargs).apply_async(
            headers={TENANT_TASK_HEADER: tenant.id},
        )
        published += 1

    logger.debug("fanned out task=%s tenants=%d", task_name, published)
