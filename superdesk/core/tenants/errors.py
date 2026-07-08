# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license


class TenantError(Exception):
    pass


class TenantNotSetError(TenantError):
    """Raised when multi-tenancy is enabled and code requires a tenant but none is set in context.

    Tenancy is fail-closed: datastore access without a resolved tenant must never
    silently fall back to a default database.
    """

    def __init__(self, message: str | None = None):
        super().__init__(message or "No tenant set in the current context")


class TenantNotFoundError(TenantError):
    def __init__(self, tenant_id: str):
        super().__init__(f"Tenant '{tenant_id}' not found")
        self.tenant_id = tenant_id
