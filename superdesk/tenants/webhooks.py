# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Tenant lifecycle webhooks.

When ``TENANT_WEBHOOK_URL`` is configured, lifecycle changes trigger an HTTPS
POST with a JSON payload, delivered asynchronously (Celery, with retries) and
optionally signed with HMAC-SHA256 (``TENANT_WEBHOOK_SECRET``, signature in the
``X-Superdesk-Signature`` header as ``sha256=<hexdigest>``).

Events: ``tenant.suspended``, ``tenant.activated``, ``tenant.deleted``
(marked for deletion, data retained), ``tenant.purged`` (data dropped after
the retention period).
"""

import logging

from superdesk.utc import utcnow
from superdesk.core import get_app_config

logger = logging.getLogger(__name__)

EVENT_SUSPENDED = "tenant.suspended"
EVENT_ACTIVATED = "tenant.activated"
EVENT_DELETED = "tenant.deleted"
EVENT_PURGED = "tenant.purged"


def build_payload(event: str, doc: dict) -> dict:
    payload = {
        "event": event,
        "tenant": doc["_id"],
        "status": doc.get("status"),
        "hosts": list(doc.get("hosts") or []),
        "timestamp": utcnow().isoformat(),
    }
    if doc.get("deleted_at"):
        payload["deleted_at"] = doc["deleted_at"].isoformat()
    if doc.get("purged_at"):
        payload["purged_at"] = doc["purged_at"].isoformat()
    return payload


async def notify_tenant_event(event: str, doc: dict | None) -> None:
    """Queue the webhook delivery for a tenant lifecycle event (noop when unconfigured)."""

    if doc is None or not get_app_config("TENANT_WEBHOOK_URL"):
        return

    from superdesk.celery_app.tenant_tasks import webhook_notify_task

    await webhook_notify_task.apply_async(kwargs={"payload": build_payload(event, doc)})
    logger.info("queued tenant webhook event=%s tenant=%s", event, doc["_id"])
