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

When a webhook endpoint is configured, lifecycle changes trigger an HTTPS POST
with a JSON payload, delivered asynchronously (Celery, with retries) and
optionally signed with HMAC-SHA256 (signature in the ``X-Superdesk-Signature``
header as ``sha256=<hexdigest>``).

The endpoint is configured in the control plane (managed from the tenant admin
panel via ``GET/PUT /tenant-admin/webhook``); the ``TENANT_WEBHOOK_URL`` /
``TENANT_WEBHOOK_SECRET`` settings act as a config-file fallback when nothing
is stored in the control plane.

Events: ``tenant.suspended``, ``tenant.activated``, ``tenant.deleted``
(marked for deletion, data retained), ``tenant.purged`` (data dropped after
the retention period), ``tenant.test`` (manual test delivery from the panel).
"""

import json
import hmac
import hashlib
import logging

import requests

from superdesk.utc import utcnow
from superdesk.core import get_app_config, get_current_async_app

logger = logging.getLogger(__name__)

EVENT_SUSPENDED = "tenant.suspended"
EVENT_ACTIVATED = "tenant.activated"
EVENT_DELETED = "tenant.deleted"
EVENT_PURGED = "tenant.purged"
EVENT_TEST = "tenant.test"

WEBHOOK_SETTINGS_ID = "tenant_webhook"
SETTINGS_COLLECTION = "settings"


def _settings_collection():
    return get_current_async_app().tenants.get_control_plane_collection(SETTINGS_COLLECTION)


def get_webhook_config() -> dict:
    """Effective webhook config: control-plane settings first, config file as fallback.

    Returns ``{"url": str, "secret": str, "source": "control-plane"|"config"|"none"}``.
    """

    doc = _settings_collection().find_one({"_id": WEBHOOK_SETTINGS_ID}) or {}
    if doc.get("url"):
        return {"url": doc["url"], "secret": doc.get("secret") or "", "source": "control-plane"}

    url = get_app_config("TENANT_WEBHOOK_URL") or ""
    return {
        "url": url,
        "secret": get_app_config("TENANT_WEBHOOK_SECRET") or "",
        "source": "config" if url else "none",
    }


def set_webhook_config(url: str, secret: str | None = None) -> None:
    """Store the webhook endpoint in the control plane.

    ``secret=None`` keeps the currently stored secret; an empty string clears it.
    An empty ``url`` removes the control-plane entry (falling back to the config file).
    """

    collection = _settings_collection()
    if not url:
        collection.delete_one({"_id": WEBHOOK_SETTINGS_ID})
        return

    updates: dict = {"url": url, "_updated": utcnow()}
    if secret is not None:
        updates["secret"] = secret
    collection.update_one({"_id": WEBHOOK_SETTINGS_ID}, {"$set": updates}, upsert=True)


def deliver_webhook(payload: dict, url: str, secret: str) -> requests.Response:
    """Synchronously POST a signed webhook payload; raises on HTTP errors."""

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Superdesk-Signature"] = f"sha256={signature}"

    response = requests.post(url, data=body, headers=headers, timeout=(5, 30))
    response.raise_for_status()
    return response


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

    if doc is None or not get_webhook_config()["url"]:
        return

    from superdesk.celery_app.tenant_tasks import webhook_notify_task

    await webhook_notify_task.apply_async(kwargs={"payload": build_payload(event, doc)})
    logger.info("queued tenant webhook event=%s tenant=%s", event, doc["_id"])
