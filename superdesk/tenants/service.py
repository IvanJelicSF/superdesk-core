# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from superdesk.utc import utcnow
from superdesk.core import get_current_async_app
from superdesk.core.tenants import Tenant, TenantStatus, TenantNotFoundError
from superdesk.core.tenants.registry import TenantRegistry


class TenantExistsError(Exception):
    pass


def _registry() -> TenantRegistry:
    return get_current_async_app().tenants


def create_tenant(tenant: Tenant, status: TenantStatus = TenantStatus.ACTIVE) -> None:
    registry = _registry()
    registry.ensure_indexes()
    doc = tenant.to_dict()
    doc["status"] = status.value
    doc["provisioning"] = {}
    doc["_created"] = doc["_updated"] = utcnow()
    try:
        registry.collection.insert_one(doc)
    except DuplicateKeyError as error:
        raise TenantExistsError(f"Tenant '{tenant.id}' (or one of its hosts) already exists") from error
    registry.invalidate()


def get_tenant_doc(tenant_id: str) -> Optional[dict]:
    """Raw tenant document, including provisioning markers (uncached)."""

    return _registry().collection.find_one({"_id": tenant_id})


def list_tenant_docs() -> list[dict]:
    return list(_registry().collection.find({}).sort("_id", 1))


def update_tenant(tenant_id: str, updates: dict[str, Any]) -> None:
    registry = _registry()
    updates = {**updates, "_updated": utcnow()}
    result = registry.collection.update_one({"_id": tenant_id}, {"$set": updates})
    if result.matched_count == 0:
        raise TenantNotFoundError(tenant_id)
    registry.invalidate(tenant_id)


def set_tenant_status(tenant_id: str, status: TenantStatus) -> None:
    update_tenant(tenant_id, {"status": status.value})


def mark_tenant_deleted(tenant_id: str) -> None:
    """Soft delete: the tenant stays in the system (hosts answer 404, beat skips it)
    with its data intact until the retention purge empties it.
    """

    update_tenant(tenant_id, {"status": TenantStatus.DELETED.value, "deleted_at": utcnow()})


def restore_deleted_tenant(tenant_id: str) -> None:
    """Undo a soft delete; only possible while the data has not been purged yet."""

    doc = get_tenant_doc(tenant_id)
    if doc is None:
        raise TenantNotFoundError(tenant_id)
    if doc.get("purged_at"):
        raise ValueError(f"Tenant '{tenant_id}' has been purged and cannot be restored")
    update_tenant(tenant_id, {"status": TenantStatus.ACTIVE.value, "deleted_at": None})


def mark_provisioning_step(tenant_id: str, step: str) -> None:
    update_tenant(tenant_id, {f"provisioning.{step}": utcnow()})


def delete_tenant_record(tenant_id: str) -> None:
    registry = _registry()
    result = registry.collection.delete_one({"_id": tenant_id})
    if result.deleted_count == 0:
        raise TenantNotFoundError(tenant_id)
    registry.invalidate()
