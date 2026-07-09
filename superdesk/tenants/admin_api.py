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

Served only on the reserved non-tenant host (``TENANT_ADMIN_HOST``). Two ways in:

* a **super-admin session**: ``POST /tenant-admin/login`` with the shared-account
  credentials of an account flagged ``is_super_admin`` (the tenant admin panel
  UI uses this), or
* the static bearer token ``TENANT_ADMIN_API_TOKEN`` (automation/ops; optional).

Fail-closed: without multi-tenancy and the matching host, every endpoint is 404.
"""

import hmac
import logging
from functools import wraps

from quart import Blueprint, request, jsonify, session

from superdesk.core import get_app_config
from superdesk.core.tenants import Tenant, TenantStatus
from superdesk import accounts

from .service import (
    get_tenant_doc,
    list_tenant_docs,
    set_tenant_status,
    mark_tenant_deleted,
    restore_deleted_tenant,
    update_tenant,
    TenantExistsError,
)
from .provisioning import provision_tenant
from .webhooks import notify_tenant_event, EVENT_SUSPENDED, EVENT_ACTIVATED, EVENT_DELETED

logger = logging.getLogger(__name__)

bp = Blueprint("tenant_admin", __name__)

ADMIN_SESSION_KEY = "tenant_admin_account_id"


def _on_admin_host() -> bool:
    if not get_app_config("MULTI_TENANT_ENABLED"):
        return False
    admin_host = (get_app_config("TENANT_ADMIN_HOST") or "").split(":")[0].lower()
    request_host = (request.host or "").split(":")[0].lower()
    return bool(admin_host) and request_host == admin_host


def _get_session_admin() -> dict | None:
    """The logged-in super-admin account for this session, revalidated per request."""
    account_id = session.get(ADMIN_SESSION_KEY)
    if not account_id:
        return None
    account = accounts.find_account_sync_by_id(account_id)
    if account is None or not account.get("is_enabled", True) or not account.get("is_super_admin"):
        session.pop(ADMIN_SESSION_KEY, None)
        return None
    return account


def _authorized() -> bool:
    if not _on_admin_host():
        return False

    token = get_app_config("TENANT_ADMIN_API_TOKEN") or ""
    auth_header = request.headers.get("Authorization") or ""
    provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if token and provided and hmac.compare_digest(provided, token):
        return True

    return _get_session_admin() is not None


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


def _public_account(doc: dict) -> dict:
    raw_id = doc["_id"]
    out = {key: value for key, value in doc.items() if key != "password"}
    out["_id"] = str(raw_id)
    out["tenants"] = accounts.list_account_tenants(raw_id)
    return out


@bp.route("/tenant-admin/login", methods=["POST"])
async def admin_login():
    """Start a tenant-admin session with super-admin shared-account credentials."""

    if not _on_admin_host() or not accounts.is_shared_accounts_enabled():
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Not found"}}), 404

    payload = await request.get_json(force=True)
    account = accounts.find_account_sync(payload.get("email") or payload.get("username") or "")
    if (
        account is None
        or not account.get("is_enabled", True)
        or not account.get("is_super_admin")
        or not accounts.verify_account_password(account, payload.get("password") or "")
    ):
        # one generic response; no hint whether the account exists or lacks rights
        return jsonify({"_status": "ERR", "_error": {"code": 401, "message": "Invalid credentials"}}), 401

    session[ADMIN_SESSION_KEY] = str(account["_id"])
    logger.info("tenant admin login account=%s", account["email"])
    return jsonify({"_status": "OK", "email": account["email"]})


@bp.route("/tenant-admin/logout", methods=["POST"])
async def admin_logout():
    if not _on_admin_host():
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Not found"}}), 404
    session.pop(ADMIN_SESSION_KEY, None)
    return jsonify({"_status": "OK"})


@bp.route("/tenant-admin/me", methods=["GET"])
@admin_only
async def admin_me():
    """The current admin identity (session or token)."""

    account = _get_session_admin()
    if account is not None:
        return jsonify({"auth": "session", "email": account["email"]})
    return jsonify({"auth": "token"})


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
            status = TenantStatus(payload["status"])
        except ValueError:
            return jsonify({"_status": "ERR", "_error": {"message": f"invalid status '{payload['status']}'"}}), 400

        if status == TenantStatus.DELETED:
            return jsonify({"_status": "ERR", "_error": {"message": "use DELETE to delete a tenant"}}), 400

        try:
            if doc.get("status") == TenantStatus.DELETED.value and status == TenantStatus.ACTIVE:
                # undo a soft delete (only until the retention purge has run)
                restore_deleted_tenant(slug)
            else:
                set_tenant_status(slug, status)
        except ValueError as error:
            return jsonify({"_status": "ERR", "_error": {"message": str(error)}}), 409

        if status == TenantStatus.SUSPENDED:
            await notify_tenant_event(EVENT_SUSPENDED, get_tenant_doc(slug))
        elif status == TenantStatus.ACTIVE and doc.get("status") != TenantStatus.ACTIVE.value:
            await notify_tenant_event(EVENT_ACTIVATED, get_tenant_doc(slug))

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
    """Soft delete: mark the tenant deleted; its data is emptied by the periodic
    purge after ``TENANT_DELETED_RETENTION_DAYS``. Until then it can be restored
    via ``PATCH {"status": "active"}``.
    """

    doc = get_tenant_doc(slug)
    if doc is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant"}}), 404
    if doc.get("status") == TenantStatus.ACTIVE.value:
        return jsonify({"_status": "ERR", "_error": {"message": "tenant is active, disable it first"}}), 409
    if doc.get("status") == TenantStatus.DELETED.value:
        return jsonify({"_status": "ERR", "_error": {"message": "tenant is already deleted"}}), 409

    mark_tenant_deleted(slug)
    await notify_tenant_event(EVENT_DELETED, get_tenant_doc(slug))
    retention_days = int(get_app_config("TENANT_DELETED_RETENTION_DAYS", 30))
    return jsonify({"_status": "OK", "retention_days": retention_days}), 200


@bp.route("/tenant-admin/accounts", methods=["GET"])
@admin_only
async def accounts_list():
    return jsonify([_public_account(doc) for doc in accounts.list_accounts_sync()])


@bp.route("/tenant-admin/accounts", methods=["POST"])
@admin_only
async def accounts_create():
    from superdesk.utils import get_hash

    payload = await request.get_json(force=True)
    email = payload.get("email") or ""
    password = payload.get("password") or ""
    if not email or not password:
        return jsonify({"_status": "ERR", "_error": {"message": "email and password are required"}}), 400

    accounts.ensure_account_indexes()
    password_hash = get_hash(password, get_app_config("BCRYPT_GENSALT_WORK_FACTOR", 12))
    await accounts.upsert_account_credentials(email, payload.get("username"), password_hash)
    if payload.get("is_super_admin"):
        accounts.update_account_sync(email, {"is_super_admin": True})
    return jsonify(_public_account(accounts.find_account_sync(email))), 201


@bp.route("/tenant-admin/accounts/<email>", methods=["PATCH"])
@admin_only
async def accounts_update(email):
    from superdesk.utils import get_hash

    account = accounts.find_account_sync(email)
    if account is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown account"}}), 404

    payload = await request.get_json(force=True)
    updates = {
        key: bool(payload[key]) for key in ("is_enabled", "is_super_admin", "needs_password_reset") if key in payload
    }

    admin = _get_session_admin()
    if (
        admin is not None
        and str(admin["_id"]) == str(account["_id"])
        and (updates.get("is_enabled") is False or updates.get("is_super_admin") is False)
    ):
        return jsonify({"_status": "ERR", "_error": {"message": "Cannot revoke your own admin access"}}), 400

    if payload.get("password"):
        await accounts.upsert_account_credentials(
            account["email"], None, get_hash(payload["password"], get_app_config("BCRYPT_GENSALT_WORK_FACTOR", 12))
        )
    if updates:
        accounts.update_account_sync(account["email"], updates)

    return jsonify(_public_account(accounts.find_account_sync(account["email"])))


@bp.route("/tenant-admin/tenants/<slug>/users", methods=["POST"])
@admin_only
async def tenant_users_create(slug):
    """Create a user inside a tenant (cross-tenant user administration).

    With shared accounts enabled the user's credentials are automatically
    linked to (or create) the matching control-plane account.
    """

    from superdesk.core.tenants import tenant_context, TenantNotFoundError
    from apps.auth.db.commands import create_user_command_handler

    payload = await request.get_json(force=True)
    if not all(payload.get(key) for key in ("username", "password", "email")):
        return jsonify({"_status": "ERR", "_error": {"message": "username, password and email are required"}}), 400

    try:
        with tenant_context(slug):
            await create_user_command_handler(
                username=payload["username"],
                password=payload["password"],
                email=payload["email"],
                admin=bool(payload.get("admin")),
            )
    except TenantNotFoundError:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant"}}), 404

    return jsonify({"_status": "OK", "tenant": slug, "username": payload["username"]}), 201


def init_app(app) -> None:
    app.register_blueprint(bp)
