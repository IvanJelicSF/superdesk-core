# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from .models import Tenant


#: suffixes appended to ``Tenant.db_prefix`` per mongo config prefix
MONGO_PREFIX_SUFFIXES = {
    "MONGO": "",
    "ARCHIVED": "_archived",
    "LEGAL_ARCHIVE": "_legal_archive",
    "CONTENTAPI_MONGO": "_contentapi",
}

#: suffixes appended to ``Tenant.elastic_prefix`` per elastic config prefix
ELASTIC_PREFIX_SUFFIXES = {
    "ELASTICSEARCH": "",
    "CONTENTAPI_ELASTICSEARCH": "_contentapi",
}


def _suffix(suffixes: dict[str, str], config_prefix: str) -> str:
    try:
        return suffixes[config_prefix]
    except KeyError:
        # project-defined prefix, derive a stable suffix from it
        return "_" + config_prefix.lower()


def tenant_db_name(tenant: Tenant, config_prefix: str = "MONGO") -> str:
    """Mongo database name for a tenant and config prefix, e.g. ``sd_tenant_a_archived``.

    Only valid for real tenants; the default tenant keeps using the
    ``{PREFIX}_DBNAME``/``{PREFIX}_URI`` config as before.
    """

    if tenant.is_default:
        raise ValueError("Default tenant db names are resolved from config, not naming rules")
    return tenant.db_prefix + _suffix(MONGO_PREFIX_SUFFIXES, config_prefix)


def tenant_index_prefix(tenant: Tenant, config_prefix: str = "ELASTICSEARCH") -> str:
    """Elastic index prefix for a tenant and config prefix; resource indexes append ``_{source}``.

    Only valid for real tenants; the default tenant keeps using the
    ``{PREFIX}_INDEX`` config as before.
    """

    if tenant.is_default:
        raise ValueError("Default tenant index prefixes are resolved from config, not naming rules")
    return tenant.elastic_prefix + _suffix(ELASTIC_PREFIX_SUFFIXES, config_prefix)
