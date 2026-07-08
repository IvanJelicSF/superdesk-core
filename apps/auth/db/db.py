# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import bcrypt
from superdesk import accounts
from superdesk.core import get_config
from superdesk.flask import g
from apps.auth.service import AuthService
from superdesk import get_resource_service
from apps.auth.errors import CredentialsAuthError, PasswordExpiredError, ExternalUserError
from superdesk.utc import utcnow
import datetime
from quart_babel import gettext as _


class DbAuthService(AuthService):
    async def authenticate(self, credentials, ignore_expire=False):
        if accounts.is_shared_accounts_enabled():
            account = await accounts.find_account(credentials.get("username") or "")
            if account is not None:
                return await self.authenticate_account(account, credentials, ignore_expire=ignore_expire)
            if accounts.is_accounts_authoritative():
                # phase 2: no tenant-local credentials fallback
                raise CredentialsAuthError(credentials)

        user = await get_resource_service("auth_users").find_one_async(req=None, username=credentials.get("username"))
        if not user:
            raise CredentialsAuthError(credentials)

        _user = await get_resource_service("users").find_one_async(req=None, username=credentials.get("username"))
        if _user.get("user_type") == "external":
            raise ExternalUserError(
                message=_("Oops!This account has been changed to External. External accounts have no login capability.")
            )

        password = credentials.get("password").encode("UTF-8")
        hashed = user.get("password").encode("UTF-8")

        if not (password and hashed):
            raise CredentialsAuthError(credentials)

        if not bcrypt.checkpw(password, hashed):
            raise CredentialsAuthError(credentials)

        expiry_days = get_config(int, "PASSWORD_EXPIRY_DAYS")
        if not ignore_expire and expiry_days > 0:
            date = user.get("password_changed_on")
            if date is None or (date + datetime.timedelta(days=expiry_days)) < utcnow():
                raise PasswordExpiredError()

        return user

    async def authenticate_account(self, account, credentials, ignore_expire=False):
        """Authenticate against a control-plane account, then resolve the tenant-local user.

        Credentials are checked against the account only; the tenant-local user
        provides identity/permissions. No tenant-local user -> auth error.
        """

        if not account.get("is_enabled", True):
            raise CredentialsAuthError(credentials)

        if not accounts.verify_account_password(account, credentials.get("password") or ""):
            raise CredentialsAuthError(credentials)

        if not ignore_expire and (account.get("needs_password_reset") or accounts.account_password_expired(account)):
            raise PasswordExpiredError()

        users_service = get_resource_service("users")
        user = await users_service.find_one_async(req=None, account_id=account["_id"])
        if not user:
            # one-time fallback by email, lazily linking the account (self-healing linkage)
            user = await users_service.find_one_async(req=None, email=account["email"])
            if user:
                users_service.system_update(user["_id"], {"account_id": account["_id"]}, user)

        if not user:
            raise CredentialsAuthError(credentials)

        if user.get("user_type") == "external":
            raise ExternalUserError(
                message=_("Oops!This account has been changed to External. External accounts have no login capability.")
            )

        auth_user = await get_resource_service("auth_users").find_one_async(req=None, username=user.get("username"))
        if not auth_user:
            raise CredentialsAuthError(credentials)

        return auth_user

    async def is_authorized(self, **kwargs):
        if kwargs.get("_id") is None:
            return False

        auth = await self.find_one_async(_id=str(kwargs.get("_id")), req=None)
        return auth and str(g.auth["_id"]) == str(auth.get("_id"))
