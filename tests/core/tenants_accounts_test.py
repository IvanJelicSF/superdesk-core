import bcrypt

from datetime import timedelta
from unittest import IsolatedAsyncioTestCase, mock

from superdesk.utc import utcnow
from superdesk.core.app import SuperdeskAsyncApp
from superdesk.tests import MockWSGI

from superdesk.accounts import service as accounts_service
from apps.auth.db.db import DbAuthService
from apps.auth.errors import CredentialsAuthError, PasswordExpiredError

CONTROL_PLANE_DB = "sptests_controlplane"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode()


class AccountsServiceTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "SHARED_ACCOUNTS_ENABLED": True,
                    "TENANTS_MONGO_DBNAME": CONTROL_PLANE_DB,
                    "TENANTS_MONGO_URI": f"mongodb://localhost/{CONTROL_PLANE_DB}",
                }
            )
        )
        accounts_service._collection().database.client.drop_database(CONTROL_PLANE_DB)
        accounts_service.ensure_account_indexes()

    def tearDown(self):
        accounts_service._collection().database.client.drop_database(CONTROL_PLANE_DB)
        self.app.stop()

    async def test_upsert_and_find(self):
        account_id = await accounts_service.upsert_account_credentials(
            "John@Example.com", "john", hash_password("secret")
        )
        by_username = await accounts_service.find_account("john")
        by_email = await accounts_service.find_account("john@example.com")
        self.assertIsNotNone(by_username)
        self.assertEqual(by_username["_id"], account_id)
        self.assertEqual(by_email["_id"], account_id)
        self.assertEqual(by_username["email"], "john@example.com")
        self.assertTrue(accounts_service.verify_account_password(by_username, "secret"))
        self.assertFalse(accounts_service.verify_account_password(by_username, "wrong"))

    async def test_upsert_updates_password_keeps_account(self):
        first = await accounts_service.upsert_account_credentials("john@example.com", "john", hash_password("one"))
        second = await accounts_service.upsert_account_credentials("john@example.com", "john", hash_password("two"))
        self.assertEqual(first, second)
        account = await accounts_service.find_account("john")
        self.assertTrue(accounts_service.verify_account_password(account, "two"))

    async def test_username_conflict_links_by_email_only(self):
        await accounts_service.upsert_account_credentials("john@a.com", "john", hash_password("one"))
        other_id = await accounts_service.upsert_account_credentials("john@b.com", "john", hash_password("two"))
        other = await accounts_service.find_account("john@b.com")
        self.assertEqual(other["_id"], other_id)
        self.assertNotIn("username", other)
        # username still resolves to the first account
        self.assertEqual((await accounts_service.find_account("john"))["email"], "john@a.com")

    async def test_link_user_credentials(self):
        doc = {"email": "john@example.com", "username": "john", "password": hash_password("secret")}
        account_id = await accounts_service.link_user_credentials(doc)
        self.assertIsNotNone(account_id)
        self.assertEqual(doc["account_id"], account_id)

    async def test_link_user_credentials_disabled(self):
        self.app.wsgi.config["SHARED_ACCOUNTS_ENABLED"] = False
        doc = {"email": "john@example.com", "password": hash_password("secret")}
        self.assertIsNone(await accounts_service.link_user_credentials(doc))
        self.assertNotIn("account_id", doc)

    async def test_password_expiry(self):
        account = {"password_changed_on": utcnow() - timedelta(days=10)}
        self.app.wsgi.config["PASSWORD_EXPIRY_DAYS"] = 0
        self.assertFalse(accounts_service.account_password_expired(account))
        self.app.wsgi.config["PASSWORD_EXPIRY_DAYS"] = 5
        self.assertTrue(accounts_service.account_password_expired(account))
        self.app.wsgi.config["PASSWORD_EXPIRY_DAYS"] = 30
        self.assertFalse(accounts_service.account_password_expired(account))


class AuthenticateAccountTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"SHARED_ACCOUNTS_ENABLED": True, "PASSWORD_EXPIRY_DAYS": 0}))
        self.auth_service = DbAuthService.__new__(DbAuthService)  # no resource wiring needed
        self.account = {
            "_id": "account-1",
            "email": "john@example.com",
            "username": "john",
            "password": hash_password("secret"),
            "is_enabled": True,
        }
        self.local_user = {"_id": "user-1", "username": "john", "email": "john@example.com", "user_type": "user"}
        self.auth_user = {"_id": "user-1", "username": "john", "password": "irrelevant"}

        self.users_service = mock.MagicMock()
        self.auth_users_service = mock.MagicMock()
        self.users_service.find_one_async = mock.AsyncMock()
        self.auth_users_service.find_one_async = mock.AsyncMock(return_value=self.auth_user)

        patcher = mock.patch(
            "apps.auth.db.db.get_resource_service",
            side_effect=lambda name: {"users": self.users_service, "auth_users": self.auth_users_service}[name],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.app.stop()

    async def test_authenticates_and_resolves_linked_user(self):
        self.users_service.find_one_async.return_value = self.local_user
        result = await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "secret"})
        self.assertEqual(result, self.auth_user)

    async def test_wrong_password(self):
        with self.assertRaises(CredentialsAuthError):
            await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "nope"})

    async def test_disabled_account(self):
        self.account["is_enabled"] = False
        with self.assertRaises(CredentialsAuthError):
            await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "secret"})

    async def test_no_local_user_fails_closed(self):
        self.users_service.find_one_async.return_value = None
        with self.assertRaises(CredentialsAuthError):
            await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "secret"})

    async def test_email_fallback_lazily_links(self):
        # first lookup (by account_id) misses, second (by email) hits
        self.users_service.find_one_async.side_effect = [None, self.local_user]
        result = await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "secret"})
        self.assertEqual(result, self.auth_user)
        self.users_service.system_update.assert_called_once_with("user-1", {"account_id": "account-1"}, self.local_user)

    async def test_needs_password_reset(self):
        self.account["needs_password_reset"] = True
        self.users_service.find_one_async.return_value = self.local_user
        with self.assertRaises(PasswordExpiredError):
            await self.auth_service.authenticate_account(self.account, {"username": "john", "password": "secret"})


class AccountTenantsMappingTestCase(AccountsServiceTestCase):
    async def test_link_records_tenant_mapping(self):
        from superdesk.core.tenants import Tenant, tenant_context

        doc = {"email": "john@example.com", "username": "john", "password": hash_password("secret")}
        with tenant_context(Tenant(id="tenant-a", hosts=("a.example.com",))):
            account_id = await accounts_service.link_user_credentials(dict(doc))
        with tenant_context(Tenant(id="tenant-b", hosts=("b.example.com",))):
            await accounts_service.link_user_credentials(dict(doc))

        self.assertEqual(accounts_service.list_account_tenants(account_id), ["tenant-a", "tenant-b"])

    async def test_authoritative_mode_strips_tenant_password(self):
        self.app.wsgi.config["SHARED_ACCOUNTS_AUTHORITATIVE"] = True
        doc = {"email": "john@example.com", "password": hash_password("secret")}
        account_id = await accounts_service.link_user_credentials(doc)
        self.assertIsNotNone(account_id)
        self.assertNotIn("password", doc)
        account = await accounts_service.find_account("john@example.com")
        self.assertTrue(accounts_service.verify_account_password(account, "secret"))
