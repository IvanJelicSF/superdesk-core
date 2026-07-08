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
from typing import Optional

from pymongo import MongoClient

from superdesk.core import get_current_async_app
from superdesk.core.tenants import Tenant, TenantStatus, tenant_context
from superdesk.core.tenants.naming import MONGO_PREFIX_SUFFIXES
from superdesk.core.mongo.utils import get_mongo_client_config

from .service import (
    create_tenant,
    get_tenant_doc,
    set_tenant_status,
    mark_provisioning_step,
    TenantExistsError,
)

logger = logging.getLogger(__name__)

STEP_INITIALIZE_DATA = "initialize_data"
STEP_ADMIN_USER = "admin_user"


async def provision_tenant(tenant: Tenant, admin: Optional[dict] = None, resume: bool = False) -> Tenant:
    """Create and initialize a tenant; resumable via provisioning step markers.

    :param tenant: the tenant definition to provision
    :param admin: optional dict with ``username``, ``password``, ``email`` for the initial admin user
    :param resume: continue provisioning an existing, partially provisioned tenant
    """

    doc = get_tenant_doc(tenant.id)
    if doc is None:
        create_tenant(tenant, status=TenantStatus.CREATING)
        completed: dict = {}
    elif not resume:
        raise TenantExistsError(f"Tenant '{tenant.id}' already exists, use resume to continue provisioning")
    else:
        tenant = Tenant.from_dict(doc)
        completed = doc.get("provisioning") or {}

    with tenant_context(tenant):
        if STEP_INITIALIZE_DATA not in completed:
            # creates mongo indexes (both stacks), elastic mappings and seeds default data
            from apps.prepopulate.app_initialize import app_initialize_data_handler

            await app_initialize_data_handler()
            mark_provisioning_step(tenant.id, STEP_INITIALIZE_DATA)
            logger.info("tenant %s: data initialized", tenant.id)

        if admin and STEP_ADMIN_USER not in completed:
            from apps.auth.db.commands import create_user_command_handler

            await create_user_command_handler(
                username=admin["username"],
                password=admin["password"],
                email=admin["email"],
                admin=True,
            )
            mark_provisioning_step(tenant.id, STEP_ADMIN_USER)
            logger.info("tenant %s: admin user created", tenant.id)

    set_tenant_status(tenant.id, TenantStatus.ACTIVE)
    logger.info("tenant %s: active", tenant.id)
    return tenant


def purge_tenant_storage(tenant: Tenant) -> None:
    """Drop all tenant databases and elastic indexes. Destructive, no undo.

    Covers the core mongo prefixes (incl. their ``_versions`` databases and
    GridFS, which lives in the same databases) and the tenant's elastic
    indexes. S3 media (if configured) must be cleaned up separately.
    """

    app = get_current_async_app()

    for prefix in MONGO_PREFIX_SUFFIXES:
        client_config, dbname = get_mongo_client_config(app.wsgi.config, prefix, tenant)
        client: MongoClient = MongoClient(**client_config)
        try:
            client.drop_database(dbname)
            client.drop_database(f"{dbname}_versions")
            logger.info("tenant %s: dropped mongo db %s (+versions)", tenant.id, dbname)
        finally:
            client.close()

    with tenant_context(tenant):
        app.elastic.drop_indexes()
        logger.info("tenant %s: dropped elastic indexes", tenant.id)
