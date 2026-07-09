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

from superdesk.utc import utcnow
from superdesk.core import get_app_config
from superdesk.commands.async_cli import cli
from superdesk.core.tenants import Tenant, TenantStatus

from .service import (
    get_tenant_doc,
    list_tenant_docs,
    set_tenant_status,
    mark_tenant_deleted,
    restore_deleted_tenant,
    update_tenant,
)
from .provisioning import provision_tenant, purge_tenant_storage
from .webhooks import notify_tenant_event, EVENT_SUSPENDED, EVENT_ACTIVATED, EVENT_DELETED, EVENT_PURGED


@cli.command("tenants:create", tenant_command=False)
@click.argument("slug")
@click.option("--name", default="", help="Human-readable display name (defaults to the slug).")
@click.option("--description", default="", help="Free-text description shown in the admin panel.")
@click.option("--host", "hosts", multiple=True, required=True, help="Hostname served by this tenant (repeatable).")
@click.option("--admin-username", help="Username for the initial admin user.")
@click.option("--admin-password", help="Password for the initial admin user.")
@click.option("--admin-email", help="Email for the initial admin user.")
@click.option("--resume", is_flag=True, default=False, help="Resume provisioning an existing tenant.")
async def tenants_create(slug, name, description, hosts, admin_username, admin_password, admin_email, resume):
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

    tenant = Tenant(id=slug, name=name.strip(), description=description.strip(), hosts=tuple(hosts))
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
        echo(f"{doc['_id']}\t{doc.get('name') or doc['_id']}\t{doc.get('status')}\t{','.join(doc.get('hosts') or [])}")


@cli.command("tenants:enable", tenant_command=False)
@click.argument("slug")
async def tenants_enable(slug):
    """Re-enable a suspended (or soft-deleted, not yet purged) tenant."""

    doc = get_tenant_doc(slug)
    if doc is None:
        raise click.UsageError(f"Tenant '{slug}' not found")
    if doc.get("status") == TenantStatus.DELETED.value:
        try:
            restore_deleted_tenant(slug)
        except ValueError as error:
            raise click.UsageError(str(error))
    else:
        set_tenant_status(slug, TenantStatus.ACTIVE)
    await notify_tenant_event(EVENT_ACTIVATED, get_tenant_doc(slug))
    echo(f"Tenant '{slug}' enabled")


@cli.command("tenants:disable", tenant_command=False)
@click.argument("slug")
async def tenants_disable(slug):
    """Suspend a tenant: requests are rejected and beat tasks stop fanning out to it."""

    set_tenant_status(slug, TenantStatus.SUSPENDED)
    await notify_tenant_event(EVENT_SUSPENDED, get_tenant_doc(slug))
    echo(f"Tenant '{slug}' disabled")


@cli.command("tenants:update", tenant_command=False)
@click.argument("slug")
@click.option("--add-partner", help="Allow content exchange with the given tenant id.")
@click.option(
    "--direction",
    type=click.Choice(["send", "receive", "both"]),
    default="both",
    help="Exchange direction for --add-partner.",
)
@click.option("--remove-partner", help="Remove the given tenant id from the exchange partners.")
@click.option("--name", default=None, help="Set the human-readable display name.")
@click.option("--description", default=None, help="Set the description shown in the admin panel.")
async def tenants_update(slug, add_partner, direction, remove_partner, name, description):
    """Update a tenant's display name, description or exchange partner allowlist."""

    doc = get_tenant_doc(slug)
    if doc is None:
        raise click.UsageError(f"Tenant '{slug}' not found")

    metadata_updates = {}
    if name is not None:
        metadata_updates["name"] = name.strip()
    if description is not None:
        metadata_updates["description"] = description.strip()
    if metadata_updates:
        update_tenant(slug, metadata_updates)
        echo(f"Tenant '{slug}' metadata updated")

    partners = [p for p in (doc.get("exchange_partners") or [])]
    if remove_partner:
        partners = [p for p in partners if p.get("tenant") != remove_partner]
    if add_partner:
        if get_tenant_doc(add_partner) is None:
            raise click.UsageError(f"Partner tenant '{add_partner}' not found")
        partners = [p for p in partners if p.get("tenant") != add_partner]
        partners.append({"tenant": add_partner, "direction": direction})

    update_tenant(slug, {"exchange_partners": partners})
    echo(f"Tenant '{slug}' partners: {partners}")


@cli.command("tenants:delete", tenant_command=False)
@click.argument("slug")
async def tenants_delete(slug):
    """Soft-delete a tenant. The tenant must be disabled first.

    The tenant stays in the system with its data intact; the periodic purge
    empties it after ``TENANT_DELETED_RETENTION_DAYS``. Until then it can be
    restored with ``tenants:enable``.
    """

    doc = get_tenant_doc(slug)
    if doc is None:
        raise click.UsageError(f"Tenant '{slug}' not found")
    if doc.get("status") == TenantStatus.ACTIVE.value:
        raise click.UsageError(f"Tenant '{slug}' is active, run tenants:disable first")
    if doc.get("status") == TenantStatus.DELETED.value:
        raise click.UsageError(f"Tenant '{slug}' is already deleted")

    mark_tenant_deleted(slug)
    await notify_tenant_event(EVENT_DELETED, get_tenant_doc(slug))
    retention_days = get_app_config("TENANT_DELETED_RETENTION_DAYS", 30)
    echo(f"Tenant '{slug}' marked deleted; data will be purged after {retention_days} days")


@cli.command("tenants:purge", tenant_command=False)
@click.argument("slug")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
async def tenants_purge(slug, yes):
    """Immediately empty a soft-deleted tenant, without waiting for the retention purge.

    Drops all tenant mongo databases (incl. GridFS media and versions) and
    elastic indexes. S3 media is not touched. No undo.
    """

    doc = get_tenant_doc(slug)
    if doc is None:
        raise click.UsageError(f"Tenant '{slug}' not found")
    if doc.get("status") != TenantStatus.DELETED.value:
        raise click.UsageError(f"Tenant '{slug}' is not deleted, run tenants:delete first")
    if doc.get("purged_at"):
        raise click.UsageError(f"Tenant '{slug}' is already purged")

    if not yes:
        click.confirm(f"Drop ALL databases and indexes of tenant '{slug}'?", abort=True)

    purge_tenant_storage(Tenant.from_dict(doc))
    update_tenant(slug, {"purged_at": utcnow()})
    await notify_tenant_event(EVENT_PURGED, get_tenant_doc(slug))
    echo(f"Tenant '{slug}' purged")
