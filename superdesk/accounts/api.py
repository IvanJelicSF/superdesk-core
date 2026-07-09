# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Account endpoints for tenant users (the tenant switcher)."""

from quart import Blueprint, jsonify

from superdesk.flask import g
from superdesk.core import get_current_async_app
from superdesk.auth.decorator import blueprint_auth

from .service import is_shared_accounts_enabled, list_account_tenants

bp = Blueprint("accounts_api", __name__)


@bp.route("/accounts/me/tenants", methods=["GET"])
@blueprint_auth()
async def my_tenants():
    """List the active tenants where the current user's account has a linked user.

    The client uses this for the tenant switcher: it redirects to the other
    tenant's host, where the user logs in with the same shared credentials
    (sessions never span tenants).
    """

    user = getattr(g, "user", None) or {}
    account_id = user.get("account_id")

    tenants: list = []
    if account_id and is_shared_accounts_enabled():
        registry = get_current_async_app().tenants
        for tenant_id in list_account_tenants(account_id):
            tenant = registry.get_by_id_sync(tenant_id)
            if tenant is not None and tenant.is_active:
                tenants.append({"tenant": tenant.id, "hosts": list(tenant.hosts)})

    return jsonify({"tenants": tenants})


def init_app(app) -> None:
    app.register_blueprint(bp)
