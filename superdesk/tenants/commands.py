# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import click
from click import echo

from superdesk.commands.async_cli import cli
from superdesk.core.tenants import Tenant, TenantStatus

from .service import (
    get_tenant_doc,
    list_tenant_docs,
    set_tenant_status,
    delete_tenant_record,
)
from .provisioning import provision_tenant, purge_tenant_storage


@cli.command("tenants:create", tenant_command=False)
@click.argument("slug")
@click.option("--host", "hosts", multiple=True, required=True, help="Hostname served by this tenant (repeatable).")
@click.option("--admin-username", help="Username for the initial admin user.")
@click.option("--admin-password", help="Password for the initial admin user.")
@click.option("--admin-email", help="Email for the initial admin user.")
@click.option("--resume", is_flag=True, default=False, help="Resume provisioning an existing tenant.")
async def tenants_create(slug, hosts, admin_username, admin_password, admin_email, resume):
    """Create and provision a new tenant.

    Creates the tenant record, initializes mongo indexes and elastic mappings,
    seeds default data and optionally creates the initial admin user.

    Example:
    ::

        $ python manage.py tenants:create tenant-a --host tenant-a.example.com
            --admin-username admin --admin-password admin --admin-email admin@example.com

    Re-run with ``--resume`` to continue a partially provisioned tenant.
    """

    admin = None
    if admin_username or admin_password or admin_email:
        if not (admin_username and admin_password and admin_email):
            raise click.UsageError("Provide all of --admin-username, --admin-password and --admin-email, or none")
        admin = {"username": admin_username, "password": admin_password, "email": admin_email}

    tenant = Tenant(id=slug, hosts=tuple(hosts))
    await provision_tenant(tenant, admin=admin, resume=resume)
    echo(f"Tenant '{slug}' provisioned and active")


@cli.command("tenants:list", tenant_command=False)
async def tenants_list():
    """List all tenants with their status and hosts."""

    docs = list_tenant_docs()
    if not docs:
        echo("No tenants")
        return
    for doc in docs:
        echo(f"{doc['_id']}\t{doc.get('status')}\t{','.join(doc.get('hosts') or [])}")


@cli.command("tenants:enable", tenant_command=False)
@click.argument("slug")
async def tenants_enable(slug):
    """Re-enable a suspended tenant."""

    set_tenant_status(slug, TenantStatus.ACTIVE)
    echo(f"Tenant '{slug}' enabled")


@cli.command("tenants:disable", tenant_command=False)
@click.argument("slug")
async def tenants_disable(slug):
    """Suspend a tenant: requests are rejected and beat tasks stop fanning out to it."""

    set_tenant_status(slug, TenantStatus.SUSPENDED)
    echo(f"Tenant '{slug}' disabled")


@cli.command("tenants:delete", tenant_command=False)
@click.argument("slug")
@click.option("--purge", is_flag=True, default=False, help="Also drop the tenant databases and elastic indexes.")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
async def tenants_delete(slug, purge, yes):
    """Delete a tenant. The tenant must be disabled first.

    Without ``--purge`` only the registry record is removed (data stays).
    With ``--purge`` all tenant mongo databases (incl. GridFS media and
    versions) and elastic indexes are dropped. S3 media is not touched.
    """

    doc = get_tenant_doc(slug)
    if doc is None:
        raise click.UsageError(f"Tenant '{slug}' not found")
    if doc.get("status") == TenantStatus.ACTIVE.value:
        raise click.UsageError(f"Tenant '{slug}' is active, run tenants:disable first")

    if purge and not yes:
        click.confirm(f"Drop ALL databases and indexes of tenant '{slug}'?", abort=True)

    if purge:
        purge_tenant_storage(Tenant.from_dict(doc))

    delete_tenant_record(slug)
    echo(f"Tenant '{slug}' deleted" + (" (data purged)" if purge else " (data kept)"))
