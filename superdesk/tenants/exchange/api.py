# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Manual "send to tenant" action for archive items."""

import logging

from quart import Blueprint, request, jsonify
from quart_babel import lazy_gettext

import superdesk
from superdesk import get_resource_service
from superdesk.auth.decorator import blueprint_auth
from superdesk.errors import SuperdeskPublishError
from superdesk.publish.formatters.ninjs_formatter import NINJSFormatter
from superdesk.users.services import current_user_has_privilege

from .transmitter import InternalTenantTransmitter

logger = logging.getLogger(__name__)

SEND_TO_TENANT_PRIVILEGE = "send_to_tenant"

bp = Blueprint("tenant_exchange_api", __name__)


@bp.route("/exchange/partners", methods=["GET"])
@blueprint_auth()
async def exchange_partners():
    """List the active tenants the current tenant may send content to.

    Backs the target picker of the "send to tenant" action and the
    ``internal_tenant`` destination form.
    """

    from superdesk.core import get_current_async_app
    from superdesk.core.tenants import try_get_current_tenant

    source = try_get_current_tenant()
    partners: list = []
    if source is not None and not source.is_default:
        registry = get_current_async_app().tenants
        for partner in source.exchange_partners:
            if partner.get("direction", "both") not in ("send", "both"):
                continue
            target = registry.get_by_id_sync(partner.get("tenant") or "")
            if target is not None and target.is_active and target.can_receive_from(source.id):
                partners.append({"tenant": target.id, "hosts": list(target.hosts)})

    return jsonify({"partners": partners})


@bp.route("/archive/send_to_tenant", methods=["POST"])
@blueprint_auth()
async def send_to_tenant():
    """Send an archive item to a partner tenant, bypassing subscriber filtering.

    Payload: ``{"item_id": ..., "target_tenant": ..., "desk"?: ..., "stage"?: ..., "auto_fetch"?: bool}``.
    Uses the same ninjs format, media copy and ingest path as the automatic
    subscriber-based exchange, so partner allowlists still apply.
    """

    if not current_user_has_privilege(SEND_TO_TENANT_PRIVILEGE):
        return jsonify({"_status": "ERR", "_error": {"code": 403, "message": "Insufficient privileges"}}), 403

    payload = await request.get_json(force=True)
    item_id = payload.get("item_id")
    target_tenant = payload.get("target_tenant")
    if not item_id or not target_tenant:
        return jsonify({"_status": "ERR", "_error": {"message": "item_id and target_tenant are required"}}), 400

    item = get_resource_service("archive").find_one(req=None, _id=item_id)
    if item is None:
        return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Item not found"}}), 404

    formatted = await NINJSFormatter().format(dict(item), subscriber=None)
    _seq, formatted_item = formatted[0]

    queue_item = {
        "item_id": item_id,
        "formatted_item": formatted_item,
        "destination": {
            "config": {
                "tenant": target_tenant,
                "auto_fetch": bool(payload.get("auto_fetch")),
                "desk": payload.get("desk"),
                "stage": payload.get("stage"),
            }
        },
    }

    try:
        await InternalTenantTransmitter()._transmit(queue_item, subscriber={})
    except SuperdeskPublishError as error:
        logger.warning("send_to_tenant rejected item=%s target=%s error=%s", item_id, target_tenant, error)
        return jsonify({"_status": "ERR", "_error": {"code": 400, "message": str(error)}}), 400

    return jsonify({"_status": "OK", "item_id": item_id, "target_tenant": target_tenant}), 200


def init_app(app) -> None:
    superdesk.privilege(
        name=SEND_TO_TENANT_PRIVILEGE,
        label=lazy_gettext("Send content to another tenant"),
        description=lazy_gettext("Allows sending content to partner tenants."),
    )
    app.register_blueprint(bp)
    tenant_admin_host = app.config.get("TENANT_ADMIN_HOST") or ""
    app.client_config.update(
        {
            "multi_tenant_enabled": bool(app.config.get("MULTI_TENANT_ENABLED")),
            "shared_accounts_enabled": bool(app.config.get("SHARED_ACCOUNTS_ENABLED")),
            "tenant_admin_url": f"https://{tenant_admin_host}" if tenant_admin_host else "",
        }
    )
