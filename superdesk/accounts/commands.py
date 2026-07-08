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
from superdesk.utils import get_hash, is_hashed
from superdesk.core import get_app_config, get_current_app
from superdesk.commands.async_cli import cli

from . import service


@cli.command("accounts:create", tenant_command=False)
@click.option("--email", "-e", required=True, help="Email address (unique across all tenants).")
@click.option("--username", "-u", help="Username for login (unique across all tenants).")
@click.option("--password", "-p", required=True, help="Password for the account.")
async def accounts_create(email, username, password):
    """Create (or update the credentials of) a control-plane account."""

    service.ensure_account_indexes()
    password_hash = get_hash(password, get_app_config("BCRYPT_GENSALT_WORK_FACTOR", 12))
    account_id = await service.upsert_account_credentials(email, username, password_hash)
    echo(f"Account {account_id} for '{email}' created/updated")


@cli.command("accounts:set-password", tenant_command=False)
@click.option("--email", "-e", required=True, help="Email of the account.")
@click.option("--password", "-p", required=True, help="New password.")
async def accounts_set_password(email, password):
    """Set the password on an existing control-plane account."""

    account = service.find_account_sync(email)
    if account is None:
        raise click.UsageError(f"No account for '{email}'")
    password_hash = get_hash(password, get_app_config("BCRYPT_GENSALT_WORK_FACTOR", 12))
    await service.upsert_account_credentials(account["email"], None, password_hash)
    echo(f"Password updated for '{email}'")


@cli.command("accounts:migrate")
async def accounts_migrate():
    """Migrate tenant users' credentials into control-plane accounts.

    Runs per tenant (combine with ``--tenant``/``--all-tenants``). For every user
    with a password: create the account if missing, link ``account_id`` on the
    user. If the account already has a password from another tenant, the newest
    ``password_changed_on`` wins and the account is flagged
    ``needs_password_reset``. Idempotent.
    """

    service.ensure_account_indexes()
    users = get_current_app().data.pymongo("users").db["users"]
    accounts_collection = service._collection()

    migrated = linked = conflicts = skipped = 0

    for user in users.find({"password": {"$exists": True}, "email": {"$exists": True}}):
        if user.get("user_type") == "external" or not is_hashed(user.get("password", "")):
            skipped += 1
            continue

        email = user["email"].strip().lower()
        user_changed_on = user.get("password_changed_on")
        account = accounts_collection.find_one({"email": email})

        if account is None or not account.get("password"):
            await service.upsert_account_credentials(email, user.get("username"), user["password"], user_changed_on)
            migrated += 1
        elif account["password"] != user["password"]:
            account_changed_on = account.get("password_changed_on")
            if user_changed_on and (account_changed_on is None or user_changed_on > account_changed_on):
                # this tenant's password is newer: it wins, flag the account for reset
                accounts_collection.update_one(
                    {"email": email},
                    {
                        "$set": {
                            "password": user["password"],
                            "password_changed_on": user_changed_on,
                            "needs_password_reset": True,
                            "_updated": utcnow(),
                        }
                    },
                )
            else:
                accounts_collection.update_one(
                    {"email": email}, {"$set": {"needs_password_reset": True, "_updated": utcnow()}}
                )
            conflicts += 1

        if not user.get("account_id"):
            account = accounts_collection.find_one({"email": email})
            users.update_one({"_id": user["_id"]}, {"$set": {"account_id": account["_id"]}})
            linked += 1

    echo(f"accounts migrated={migrated} linked={linked} conflicts={conflicts} skipped={skipped}")


@cli.command("accounts:tenants", tenant_command=False)
@click.option("--email", "-e", required=True, help="Email of the account.")
async def accounts_tenants(email):
    """List the tenants where the account has a linked user."""

    account = service.find_account_sync(email)
    if account is None:
        raise click.UsageError(f"No account for '{email}'")
    for tenant_id in service.list_account_tenants(account["_id"]):
        echo(tenant_id)
