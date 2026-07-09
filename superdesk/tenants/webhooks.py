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

Any number of webhook endpoints can be registered in the control plane
(managed from the tenant admin panel via ``/tenant-admin/webhooks``). Each
webhook has a url and an optional HMAC-SHA256 signing secret; every enabled
webhook receives all tenant lifecycle events.

Deliveries are queued per matching webhook (Celery, with retries) and signed
with ``X-Superdesk-Signature: sha256=<hexdigest>`` when a secret is set.
The ``TENANT_WEBHOOK_URL``/``TENANT_WEBHOOK_SECRET`` settings act as one
implicit all-tenants webhook (config-file fallback).

Events: ``tenant.suspended``, ``tenant.activated``, ``tenant.deleted``
(marked for deletion, data retained), ``tenant.purged`` (data dropped after
the retention period), ``tenant.test`` (manual test delivery from the panel).
"""

import json
import hmac
import hashlib
import logging
from typing import Any, Optional

import requests
from bson import ObjectId

from superdesk.utc import utcnow
from superdesk.utils import json_serialize_datetime_objectId
from superdesk.core import get_app_config, get_current_async_app

logger = logging.getLogger(__name__)

EVENT_SUSPENDED = "tenant.suspended"
EVENT_ACTIVATED = "tenant.activated"
EVENT_DELETED = "tenant.deleted"
EVENT_PURGED = "tenant.purged"
EVENT_TEST = "tenant.test"

WEBHOOKS_COLLECTION = "webhooks"

#: pseudo id for the implicit config-file webhook (TENANT_WEBHOOK_URL)
CONFIG_WEBHOOK_ID = "config"


def _collection():
    return get_current_async_app().tenants.get_control_plane_collection(WEBHOOKS_COLLECTION)


def _config_webhook() -> Optional[dict]:
    """The implicit all-tenants webhook from the config file, if configured."""
    url = get_app_config("TENANT_WEBHOOK_URL") or ""
    if not url:
        return None
    return {
        "_id": CONFIG_WEBHOOK_ID,
        "name": "config",
        "url": url,
        "secret": get_app_config("TENANT_WEBHOOK_SECRET") or "",
        "is_enabled": True,
    }


def public_webhook(doc: dict) -> dict:
    """Webhook doc for API responses: id stringified, secret never exposed."""
    out = {key: value for key, value in doc.items() if key != "secret"}
    out["_id"] = str(doc["_id"])
    out["has_secret"] = bool(doc.get("secret"))
    return out


def list_webhooks(include_config: bool = True) -> list[dict]:
    docs = list(_collection().find({}).sort("_created", 1))
    config_hook = _config_webhook() if include_config else None
    if config_hook is not None:
        docs.append(config_hook)
    return docs


def get_webhook(webhook_id) -> Optional[dict]:
    if webhook_id == CONFIG_WEBHOOK_ID:
        return _config_webhook()
    try:
        oid = ObjectId(webhook_id)
    except Exception:
        return None
    return _collection().find_one({"_id": oid})


def create_webhook(url: str, secret: str = "", name: str = "", is_enabled: bool = True) -> dict:
    now = utcnow()
    doc = {
        "name": name.strip(),
        "url": url.strip(),
        "secret": secret or "",
        "is_enabled": bool(is_enabled),
        "_created": now,
        "_updated": now,
    }
    result = _collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def update_webhook(webhook_id, updates: dict[str, Any]) -> Optional[dict]:
    """Update a webhook; ``secret`` semantics: absent = keep, empty string = clear."""
    try:
        oid = ObjectId(webhook_id)
    except Exception:
        return None

    allowed = {key: updates[key] for key in ("name", "url", "secret", "is_enabled") if key in updates}
    allowed["_updated"] = utcnow()
    result = _collection().update_one({"_id": oid}, {"$set": allowed})
    if result.matched_count == 0:
        return None
    return _collection().find_one({"_id": oid})


def delete_webhook(webhook_id) -> bool:
    try:
        oid = ObjectId(webhook_id)
    except Exception:
        return False
    return _collection().delete_one({"_id": oid}).deleted_count > 0


def enabled_webhooks() -> list[dict]:
    """All enabled webhooks (incl. the config-file one when set)."""
    return [hook for hook in list_webhooks() if hook.get("is_enabled", True) and hook.get("url")]


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
    """Queue deliveries for a tenant lifecycle event on every matching webhook."""

    if doc is None:
        return

    hooks = enabled_webhooks()
    if not hooks:
        return

    from superdesk.celery_app.tenant_tasks import webhook_notify_task

    payload = build_payload(event, doc)
    for hook in hooks:
        await webhook_notify_task.apply_async(kwargs={"payload": payload, "webhook_id": str(hook["_id"])})
        logger.info("queued tenant webhook event=%s tenant=%s webhook=%s", event, doc["_id"], hook["_id"])


def deliver_webhook(payload: dict, url: str, secret: str) -> requests.Response:
    """Synchronously POST a signed webhook payload; raises on HTTP errors."""

    # tolerant encoder: the context-aware celery serializer coerces iso timestamp
    # strings back into datetimes on the way to the delivery task
    body = json.dumps(payload, default=json_serialize_datetime_objectId).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Superdesk-Signature"] = f"sha256={signature}"

    response = requests.post(url, data=body, headers=headers, timeout=(5, 30))
    response.raise_for_status()
    return response
