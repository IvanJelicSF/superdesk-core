# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from .models import Tenant, TenantStatus, DEFAULT_TENANT_ID
from .errors import TenantError, TenantNotSetError, TenantNotFoundError
from .context import (
    current_tenant,
    get_current_tenant,
    try_get_current_tenant,
    get_default_tenant,
    tenant_context,
    is_multi_tenant_enabled,
    register_tenant_overridable_config,
)
from .naming import tenant_db_name, tenant_index_prefix
from .registry import TenantRegistry

__all__ = [
    "Tenant",
    "TenantStatus",
    "DEFAULT_TENANT_ID",
    "TenantError",
    "TenantNotSetError",
    "TenantNotFoundError",
    "current_tenant",
    "get_current_tenant",
    "try_get_current_tenant",
    "get_default_tenant",
    "tenant_context",
    "is_multi_tenant_enabled",
    "register_tenant_overridable_config",
    "tenant_db_name",
    "tenant_index_prefix",
    "TenantRegistry",
]
