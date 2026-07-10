# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Shared credentials across tenants (control-plane accounts).

An *account* holds the credentials (email/username + bcrypt password hash) in
the control-plane db, while each tenant keeps its own ``users`` document
(profile, role, privileges, preferences) linked via ``account_id``. This lets
the same username/password log into every tenant where a local user exists,
while user management stays per tenant.

Phase 1 (this module) is strictly additive, gated by ``SHARED_ACCOUNTS_ENABLED``:
login tries the account first and falls back to the tenant-local credentials,
and password writes are dual-written to the account.
"""

from .service import (  # noqa: F401
    is_shared_accounts_enabled,
    is_accounts_authoritative,
    ensure_account_indexes,
    find_account,
    find_account_sync,
    find_account_sync_by_id,
    list_accounts_sync,
    count_accounts_sync,
    build_account_query,
    update_account_sync,
    verify_account_password,
    account_password_expired,
    upsert_account_credentials,
    link_user_credentials,
    record_account_tenant,
    list_account_tenants,
)
