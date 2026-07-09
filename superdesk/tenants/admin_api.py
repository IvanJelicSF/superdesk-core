# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""HTTP tenant administration API (control plane).

Served on the reserved non-tenant host (``TENANT_ADMIN_HOST``) and guarded by a
static bearer token (``TENANT_ADMIN_API_TOKEN``). Fail-closed: without
multi-tenancy, a configured token and a matching host, every endpoint is 404.
"""

import hmac
import logging
from functools import wraps

from quart import Blueprint, request, jsonify

from superdesk.core import get_app_config
from superdesk.core.tenants import Tenant, TenantStatus

from .service import (
    get_tenant_doc,
    list_tenant_docs,
    set_tenant_status,
    update_tenant,
    delete_tenant_record,
    TenantExistsError,
)
from .provisioning import provision_tenant, purge_tenant_storage

logger = logging.getLogger(__name__)

bp = Blueprint("tenant_admin", __name__)


def _authorized() -> bool:
    if not get_app_config("MULTI_TENANT_ENABLED"):
        return False

    token = get_app_config("TENANT_ADMIN_API_TOKEN") or ""
    if not token:
        return False

    admin_host = (get_app_config("TENANT_ADMIN_HOST") or "").split(":")[0].lower()
    request_host = (request.host or "").split(":")[0].lower()
    if not admin_host or request_host != admin_host:
        return False

    auth_header = request.headers.get("Authorization") or ""
    provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    return bool(provided) and hmac.compare_digest(provided, token)


def admin_only(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if not _authorized():
            # 404 (not 401/403) so the api does not advertise itself
            return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Not found"}}), 404
        return await f(*args, **kwargs)

    return decorated


def _public(doc: dict) -> dict:
    doc = dict(doc)
    doc["slug"] = doc.pop("_id")
    return doc


@bp.route("/tenant-admin/tenants", methods=["GET"])
@admin_only
async def tenants_list():
    return jsonify([_public(doc) for doc in list_tenant_docs()])


@bp.route("/tenant-admin/tenants", methods=["POST"])
@admin_only
async def tenants_create():
    payload = await request.get_json(force=True)
    slug = payload.get("slug") or ""
    hosts = tuple(payload.get("hosts") or ())
    admin = payload.get("admin")
    if admin and not all(admin.get(key) for key in ("username", "password", "email")):
        return jsonify({"_status": "ERR", "_error": {"message": "admin requires username, password and email"}}), 400

    try:
        tenant = Tenant(id=slug, hosts=hosts)
    except ValueError as error:
        return jsonify({"_status": "ERR", "_error": {"message": str(error)}}), 400
    if not hosts:
        return jsonify({"_status": "ERR", "_error": {"message": "at least one host is required"}}), 400

    try:
        await provision_tenant(tenant, admin=admin, resume=bool(payload.get("resume")))
    except TenantExistsError as error:
        return jsonify({"_status": "ERR", "_error": {"message": str(error)}}), 409

    return jsonify(_public(get_tenant_doc(slug))), 201


@bp.route("/tenant-admin/tenants/<slug>", methods=["GET"])
@admin_only
async def tenants_get(slug):
    doc = get_tenant_doc(slug)
    if doc is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant"}}), 404
    return jsonify(_public(doc))


@bp.route("/tenant-admin/tenants/<slug>", methods=["PATCH"])
@admin_only
async def tenants_update(slug):
    doc = get_tenant_doc(slug)
    if doc is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant"}}), 404

    payload = await request.get_json(force=True)

    if payload.get("status"):
        try:
            set_tenant_status(slug, TenantStatus(payload["status"]))
        except ValueError:
            return jsonify({"_status": "ERR", "_error": {"message": f"invalid status '{payload['status']}'"}}), 400

    if "exchange_partners" in payload:
        partners = payload["exchange_partners"] or []
        for partner in partners:
            if get_tenant_doc(partner.get("tenant") or "") is None:
                return (
                    jsonify({"_status": "ERR", "_error": {"message": f"unknown partner '{partner.get('tenant')}'"}}),
                    400,
                )
        update_tenant(slug, {"exchange_partners": partners})

    return jsonify(_public(get_tenant_doc(slug)))


@bp.route("/tenant-admin/tenants/<slug>", methods=["DELETE"])
@admin_only
async def tenants_delete(slug):
    doc = get_tenant_doc(slug)
    if doc is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant"}}), 404
    if doc.get("status") == TenantStatus.ACTIVE.value:
        return jsonify({"_status": "ERR", "_error": {"message": "tenant is active, disable it first"}}), 409

    if request.args.get("purge"):
        purge_tenant_storage(Tenant.from_dict(doc))
    delete_tenant_record(slug)
    return jsonify({"_status": "OK"}), 200


def init_app(app) -> None:
    app.register_blueprint(bp)
