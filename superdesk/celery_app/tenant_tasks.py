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
from superdesk.core.tenants.celery import FAN_OUT_TASK_NAME, TENANT_TASK_HEADER, register_tenant_agnostic_task

from . import celery

register_tenant_agnostic_task("tenants.webhook_notify", "tenants.purge_deleted")

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


@celery.task(
    name="tenants.webhook_notify",
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
)
def webhook_notify_task(self, payload: dict):
    """Deliver a tenant lifecycle webhook (HTTPS POST, HMAC-signed when a secret is set)."""

    import hashlib
    import hmac as hmac_lib
    import json

    import requests

    from superdesk.core import get_app_config

    url = get_app_config("TENANT_WEBHOOK_URL")
    if not url:
        return

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = get_app_config("TENANT_WEBHOOK_SECRET") or ""
    if secret:
        signature = hmac_lib.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Superdesk-Signature"] = f"sha256={signature}"

    response = requests.post(url, data=body, headers=headers, timeout=(5, 30))
    response.raise_for_status()
    logger.info("tenant webhook delivered event=%s tenant=%s", payload.get("event"), payload.get("tenant"))


@celery.task(name="tenants.purge_deleted")
def purge_deleted_tenants_task():
    """Purge tenants deleted longer than TENANT_DELETED_RETENTION_DAYS ago."""

    return _purge_deleted_tenants()


async def _purge_deleted_tenants():
    from datetime import timedelta

    from superdesk.utc import utcnow
    from superdesk.core import get_app_config
    from superdesk.core.tenants import Tenant, TenantStatus
    from superdesk.tenants.service import get_tenant_doc, update_tenant
    from superdesk.tenants.provisioning import purge_tenant_storage
    from superdesk.tenants.webhooks import notify_tenant_event, EVENT_PURGED

    if not get_app_config("MULTI_TENANT_ENABLED"):
        return 0

    retention_days = int(get_app_config("TENANT_DELETED_RETENTION_DAYS", 30))
    cutoff = utcnow() - timedelta(days=retention_days)
    registry = get_current_async_app().tenants

    purged = 0
    query = {"status": TenantStatus.DELETED.value, "purged_at": None, "deleted_at": {"$lte": cutoff}}
    for doc in list(registry.collection.find(query)):
        try:
            purge_tenant_storage(Tenant.from_dict(doc))
        except Exception:
            logger.exception("failed to purge deleted tenant %s", doc["_id"])
            continue
        update_tenant(doc["_id"], {"purged_at": utcnow()})
        await notify_tenant_event(EVENT_PURGED, get_tenant_doc(doc["_id"]))
        purged += 1
        logger.info("purged deleted tenant %s (retention %sd)", doc["_id"], retention_days)

    return purged
