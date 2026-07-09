# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import Any, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .models import Tenant, DEFAULT_TENANT_ID
from .errors import TenantNotSetError, TenantNotFoundError


#: the tenant bound to the current request/task/command, if any
current_tenant: ContextVar[Tenant | None] = ContextVar("current_tenant", default=None)

#: config keys a tenant record may override via ``config_overrides``;
#: infra keys (MONGO_URI, MODULES, SECRET_KEY, ...) must never be tenant-supplied
TENANT_OVERRIDABLE_CONFIG_KEYS: set[str] = {"MEDIA_PREFIX"}

#: sentinel returned by :func:`get_tenant_config_override` when there is no override
NO_OVERRIDE = object()


def register_tenant_overridable_config(*keys: str) -> None:
    """Allow the given config keys to be overridden per tenant via ``Tenant.config_overrides``."""

    TENANT_OVERRIDABLE_CONFIG_KEYS.update(keys)


def get_tenant_config_override(key: str) -> Any:
    """Per-tenant config override for ``key``, or :data:`NO_OVERRIDE`.

    Reads only the ContextVar, never the app config, so it is safe to call
    from ``get_app_config`` without recursion.
    """

    tenant = current_tenant.get()
    if tenant is not None and key in TENANT_OVERRIDABLE_CONFIG_KEYS and key in tenant.config_overrides:
        return tenant.config_overrides[key]
    return NO_OVERRIDE


def is_multi_tenant_enabled() -> bool:
    from superdesk.core.app import get_app_config

    try:
        return bool(get_app_config("MULTI_TENANT_ENABLED", False))
    except RuntimeError:
        # no app running (e.g. import time in tests) -> single tenant
        return False


def get_default_tenant() -> Tenant:
    """The synthetic tenant used when multi-tenancy is disabled.

    Its datastore names are resolved from the existing config
    (``MONGO_DBNAME``, ``ELASTICSEARCH_INDEX``, ...) by the datastore layers,
    so single-tenant deployments behave exactly as before.
    """

    from superdesk.core.app import get_app_config

    try:
        s3_subfolder = get_app_config("AMAZON_S3_SUBFOLDER") or None
    except RuntimeError:
        s3_subfolder = None

    return Tenant(id=DEFAULT_TENANT_ID, is_default=True, s3_subfolder=s3_subfolder)


def try_get_current_tenant() -> Tenant | None:
    """The tenant set in the current context, or ``None`` (no default-tenant fallback)."""

    return current_tenant.get()


def get_current_tenant() -> Tenant:
    """The tenant for the current context.

    Fail-closed: raises :class:`TenantNotSetError` when multi-tenancy is enabled
    and no tenant was resolved; returns the default tenant otherwise.
    """

    tenant = current_tenant.get()
    if tenant is not None:
        return tenant
    if is_multi_tenant_enabled():
        raise TenantNotSetError()
    return get_default_tenant()


@contextmanager
def tenant_context(tenant: Tenant | str) -> Iterator[Tenant]:
    """Bind ``tenant`` (instance or id) to the current context for the duration of the block."""

    if isinstance(tenant, str):
        from superdesk.core.app import get_current_async_app

        resolved = get_current_async_app().tenants.get_by_id_sync(tenant)
        if resolved is None:
            raise TenantNotFoundError(tenant)
        tenant = resolved

    token = current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        current_tenant.reset(token)
