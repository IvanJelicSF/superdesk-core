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

from .models import TenantStatus
from .context import current_tenant

logger = logging.getLogger(__name__)

_CTX_TOKEN_ATTR = "_tenant_ctx_token"


def setup_tenant_middleware(app) -> None:
    """Register host-based tenant resolution on a ``SuperdeskEve``/Quart app.

    Must be registered before any other request hooks so the tenant is bound
    before auth and resource code runs. Does nothing per-request unless
    ``MULTI_TENANT_ENABLED`` is set. Fail-closed: requests for hosts not in the
    registry are rejected, there is no fallback to a default database.
    """

    if app.config.get("MULTI_TENANT_ENABLED") and app.config.get("SESSION_COOKIE_DOMAIN"):
        logger.warning(
            "SESSION_COOKIE_DOMAIN is set while MULTI_TENANT_ENABLED is on; "
            "session cookies may be shared across tenant subdomains"
        )

    @app.before_request
    async def _resolve_tenant():
        from quart import request, g, jsonify

        if not app.config.get("MULTI_TENANT_ENABLED"):
            return None

        path = request.path or "/"
        for exempt in app.config.get("TENANT_EXEMPT_PATHS") or ():
            if path.startswith(exempt):
                return None

        host = (request.host or "").split(":")[0].lower()
        tenant = await app.async_app.tenants.get_by_host(host)

        if tenant is None or tenant.status == TenantStatus.DELETED:
            return jsonify({"_status": "ERR", "_error": {"code": 404, "message": "Unknown tenant host"}}), 404
        if not tenant.is_active:
            # suspended or still being provisioned
            return jsonify({"_status": "ERR", "_error": {"code": 423, "message": "Tenant is not available"}}), 423

        setattr(g, _CTX_TOKEN_ATTR, current_tenant.set(tenant))
        g.tenant = tenant
        return None

    @app.teardown_request
    async def _reset_tenant(exc=None):
        from quart import g

        token = g.pop(_CTX_TOKEN_ATTR, None)
        if token is not None:
            current_tenant.reset(token)
