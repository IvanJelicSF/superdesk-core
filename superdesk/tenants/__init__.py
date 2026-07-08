# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Tenant administration: registry writes, provisioning and lifecycle.

The read/lookup layer lives in :mod:`superdesk.core.tenants`; this package
provides the control-plane write operations and the ``tenants:*`` CLI.
"""

from .service import (  # noqa: F401
    create_tenant,
    get_tenant_doc,
    list_tenant_docs,
    update_tenant,
    set_tenant_status,
    mark_provisioning_step,
    delete_tenant_record,
)
from .provisioning import provision_tenant, purge_tenant_storage  # noqa: F401
