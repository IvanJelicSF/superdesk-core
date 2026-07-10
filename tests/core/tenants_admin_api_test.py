import json

from unittest import IsolatedAsyncioTestCase, mock

from quart import Quart

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.tests import MockWSGI

from superdesk.tenants import admin_api


CONFIG = {
    "MULTI_TENANT_ENABLED": True,
    "TENANT_ADMIN_HOST": "admin.example.com",
    "TENANT_ADMIN_API_TOKEN": "secret-token",
}

AUTH = {"Authorization": "Bearer secret-token", "Host": "admin.example.com"}


class TenantAdminApiTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        # config reads go through get_app_config -> the global async app
        self.async_app = SuperdeskAsyncApp(MockWSGI(config=dict(CONFIG)))
        app = Quart(__name__)
        admin_api.init_app(app)
        self.client = app.test_client()

    def tearDown(self):
        self.async_app.stop()

    async def test_disabled_without_token(self):
        self.async_app.wsgi.config["TENANT_ADMIN_API_TOKEN"] = ""
        response = await self.client.get("/tenant-admin/tenants", headers=AUTH)
        self.assertEqual(response.status_code, 404)

    async def test_rejects_wrong_token(self):
        response = await self.client.get("/tenant-admin/tenants", headers={**AUTH, "Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 404)

    async def test_rejects_wrong_host(self):
        response = await self.client.get("/tenant-admin/tenants", headers={**AUTH, "Host": "tenant-a.example.com"})
        self.assertEqual(response.status_code, 404)

    async def test_rejects_single_tenant_mode(self):
        self.async_app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        response = await self.client.get("/tenant-admin/tenants", headers=AUTH)
        self.assertEqual(response.status_code, 404)

    async def test_list(self):
        docs = [{"_id": "tenant-a", "status": "active", "hosts": ["a.example.com"]}]
        with (
            mock.patch.object(admin_api, "list_tenant_docs", return_value=docs) as lister,
            mock.patch.object(admin_api, "count_tenant_docs", return_value=123),
        ):
            response = await self.client.get("/tenant-admin/tenants?page=2&max_results=25&q=ten", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        payload = await response.get_json()
        self.assertEqual(payload["_items"][0]["slug"], "tenant-a")
        self.assertEqual(payload["_meta"], {"page": 2, "max_results": 25, "total": 123})
        self.assertEqual(lister.call_args.kwargs, {"page": 2, "max_results": 25})
        # q param produced a search query
        self.assertIn("$or", lister.call_args.args[0])

    async def test_list_pagination_bounds(self):
        with (
            mock.patch.object(admin_api, "list_tenant_docs", return_value=[]) as lister,
            mock.patch.object(admin_api, "count_tenant_docs", return_value=0),
        ):
            await self.client.get("/tenant-admin/tenants?page=0&max_results=9999", headers=AUTH)
            self.assertEqual(lister.call_args.kwargs, {"page": 1, "max_results": 200})
            await self.client.get("/tenant-admin/tenants?page=abc&max_results=abc", headers=AUTH)
            self.assertEqual(lister.call_args.kwargs, {"page": 1, "max_results": 50})

    async def test_create_provisions(self):
        provision = mock.AsyncMock()
        doc = {"_id": "tenant-a", "status": "active", "hosts": ["a.example.com"]}
        with (
            mock.patch.object(admin_api, "provision_tenant", provision),
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
        ):
            response = await self.client.post(
                "/tenant-admin/tenants",
                json={"slug": "tenant-a", "hosts": ["a.example.com"]},
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 201)
        provision.assert_awaited_once()
        self.assertEqual(provision.await_args.args[0].id, "tenant-a")

    async def test_create_validates_slug_and_hosts(self):
        response = await self.client.post(
            "/tenant-admin/tenants", json={"slug": "Bad Slug", "hosts": ["a.example.com"]}, headers=AUTH
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/tenant-admin/tenants", json={"slug": "tenant-a"}, headers=AUTH)
        self.assertEqual(response.status_code, 400)

    async def test_delete_requires_disabled(self):
        doc = {"_id": "tenant-a", "status": "active", "hosts": []}
        with mock.patch.object(admin_api, "get_tenant_doc", return_value=doc):
            response = await self.client.delete("/tenant-admin/tenants/tenant-a", headers=AUTH)
        self.assertEqual(response.status_code, 409)

    async def test_delete_soft_deletes_with_retention(self):
        doc = {"_id": "tenant-a", "status": "suspended", "hosts": ["a.example.com"]}
        notify = mock.AsyncMock()
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(admin_api, "mark_tenant_deleted") as mark,
            mock.patch.object(admin_api, "notify_tenant_event", notify),
        ):
            response = await self.client.delete("/tenant-admin/tenants/tenant-a", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        payload = await response.get_json()
        self.assertEqual(payload["retention_days"], 30)
        mark.assert_called_once_with("tenant-a")
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.args[0], "tenant.deleted")

    async def test_delete_already_deleted(self):
        doc = {"_id": "tenant-a", "status": "deleted", "hosts": []}
        with mock.patch.object(admin_api, "get_tenant_doc", return_value=doc):
            response = await self.client.delete("/tenant-admin/tenants/tenant-a", headers=AUTH)
        self.assertEqual(response.status_code, 409)

    async def test_patch_restores_deleted_tenant(self):
        doc = {"_id": "tenant-a", "status": "deleted", "hosts": []}
        notify = mock.AsyncMock()
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(admin_api, "restore_deleted_tenant") as restore,
            mock.patch.object(admin_api, "notify_tenant_event", notify),
        ):
            response = await self.client.patch(
                "/tenant-admin/tenants/tenant-a", json={"status": "active"}, headers=AUTH
            )
        self.assertEqual(response.status_code, 200)
        restore.assert_called_once_with("tenant-a")
        self.assertEqual(notify.await_args.args[0], "tenant.activated")

    async def test_patch_restore_purged_rejected(self):
        doc = {"_id": "tenant-a", "status": "deleted", "hosts": []}
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(
                admin_api, "restore_deleted_tenant", side_effect=ValueError("purged, cannot be restored")
            ),
        ):
            response = await self.client.patch(
                "/tenant-admin/tenants/tenant-a", json={"status": "active"}, headers=AUTH
            )
        self.assertEqual(response.status_code, 409)

    async def test_patch_status_and_partners(self):
        doc = {"_id": "tenant-a", "status": "active", "hosts": []}
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(admin_api, "set_tenant_status") as set_status,
            mock.patch.object(admin_api, "update_tenant") as update,
            mock.patch.object(admin_api, "notify_tenant_event", mock.AsyncMock()),
        ):
            response = await self.client.patch(
                "/tenant-admin/tenants/tenant-a",
                json={"status": "suspended", "exchange_partners": [{"tenant": "tenant-a", "direction": "both"}]},
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        set_status.assert_called_once()
        update.assert_called_once_with("tenant-a", {"exchange_partners": [{"tenant": "tenant-a", "direction": "both"}]})


class TenantAdminSessionTestCase(IsolatedAsyncioTestCase):
    """Super-admin session auth against a real control-plane db."""

    CONTROL_PLANE_DB = "sptests_controlplane_admin"

    def setUp(self):
        import bcrypt
        from superdesk.accounts import service as accounts_service

        self.accounts_service = accounts_service
        self.async_app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    **CONFIG,
                    "SHARED_ACCOUNTS_ENABLED": True,
                    "TENANTS_MONGO_DBNAME": self.CONTROL_PLANE_DB,
                    "TENANTS_MONGO_URI": f"mongodb://localhost/{self.CONTROL_PLANE_DB}",
                    "TENANT_ADMIN_API_TOKEN": "",  # session auth only
                }
            )
        )
        accounts_service._collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        accounts_service.ensure_account_indexes()

        password_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt(4)).decode()
        accounts_service._collection().insert_one(
            {"email": "root@example.com", "password": password_hash, "is_enabled": True, "is_super_admin": True}
        )
        accounts_service._collection().insert_one(
            {"email": "user@example.com", "password": password_hash, "is_enabled": True, "is_super_admin": False}
        )

        app = Quart(__name__)
        app.secret_key = "test-secret"
        admin_api.init_app(app)
        self.client = app.test_client()
        self.headers = {"Host": "admin.example.com"}

    def tearDown(self):
        self.accounts_service._collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        self.async_app.stop()

    async def _login(self, email="root@example.com", password="secret"):
        return await self.client.post(
            "/tenant-admin/login", json={"email": email, "password": password}, headers=self.headers
        )

    async def test_login_grants_session_access(self):
        # without login the api is cloaked
        response = await self.client.get("/tenant-admin/tenants", headers=self.headers)
        self.assertEqual(response.status_code, 404)

        response = await self._login()
        self.assertEqual(response.status_code, 200)

        with mock.patch.object(admin_api, "list_tenant_docs", return_value=[]):
            response = await self.client.get("/tenant-admin/tenants", headers=self.headers)
        self.assertEqual(response.status_code, 200)

        response = await self.client.get("/tenant-admin/me", headers=self.headers)
        self.assertEqual((await response.get_json())["email"], "root@example.com")

    async def test_non_super_admin_rejected(self):
        response = await self._login(email="user@example.com")
        self.assertEqual(response.status_code, 401)

    async def test_wrong_password_rejected(self):
        response = await self._login(password="nope")
        self.assertEqual(response.status_code, 401)

    async def test_logout_ends_session(self):
        await self._login()
        await self.client.post("/tenant-admin/logout", headers=self.headers)
        response = await self.client.get("/tenant-admin/tenants", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    async def test_accounts_crud(self):
        await self._login()

        response = await self.client.post(
            "/tenant-admin/accounts",
            json={"email": "new@example.com", "password": "welcome1"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        created = await response.get_json()
        self.assertNotIn("password", created)

        response = await self.client.get("/tenant-admin/accounts?q=new@", headers=self.headers)
        payload = await response.get_json()
        emails = [account["email"] for account in payload["_items"]]
        self.assertEqual(emails, ["new@example.com"])
        self.assertEqual(payload["_meta"]["total"], 1)

        response = await self.client.patch(
            "/tenant-admin/accounts/new@example.com", json={"is_super_admin": True}, headers=self.headers
        )
        self.assertTrue((await response.get_json())["is_super_admin"])

    async def test_cannot_revoke_own_access(self):
        await self._login()
        response = await self.client.patch(
            "/tenant-admin/accounts/root@example.com", json={"is_super_admin": False}, headers=self.headers
        )
        self.assertEqual(response.status_code, 400)

    async def test_create_tenant_user(self):
        await self._login()
        handler = mock.AsyncMock()
        from superdesk.core.tenants import Tenant
        from superdesk.core.tenants.registry import TenantRegistry

        with (
            mock.patch("apps.auth.db.commands.create_user_command_handler", handler),
            mock.patch.object(
                TenantRegistry, "get_by_id_sync", return_value=Tenant(id="tenant-a", hosts=("a.example.com",))
            ),
        ):
            response = await self.client.post(
                "/tenant-admin/tenants/tenant-a/users",
                json={"username": "john", "password": "secret1", "email": "john@example.com"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 201)
        handler.assert_awaited_once()


class WebhooksAdminCrudTestCase(IsolatedAsyncioTestCase):
    CONTROL_PLANE_DB = "sptests_controlplane_hookapi"

    def setUp(self):
        from quart import Quart
        from superdesk.tenants import webhooks

        self.webhooks = webhooks
        self.async_app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    **CONFIG,
                    "TENANTS_MONGO_DBNAME": self.CONTROL_PLANE_DB,
                    "TENANTS_MONGO_URI": f"mongodb://localhost/{self.CONTROL_PLANE_DB}",
                }
            )
        )
        webhooks._collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        app = Quart(__name__)
        admin_api.init_app(app)
        self.client = app.test_client()

    def tearDown(self):
        self.webhooks._collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        self.async_app.stop()

    async def test_crud_flow(self):
        response = await self.client.post(
            "/tenant-admin/webhooks",
            json={"url": "https://hooks.example.com/x", "secret": "s", "name": "x"},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 201)
        created = await response.get_json()
        self.assertNotIn("secret", created)
        self.assertTrue(created["has_secret"])
        hook_id = created["_id"]

        response = await self.client.get("/tenant-admin/webhooks", headers=AUTH)
        self.assertEqual(len(await response.get_json()), 1)

        response = await self.client.patch(
            f"/tenant-admin/webhooks/{hook_id}", json={"is_enabled": False}, headers=AUTH
        )
        patched = await response.get_json()
        self.assertFalse(patched["is_enabled"])

        response = await self.client.delete(f"/tenant-admin/webhooks/{hook_id}", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/tenant-admin/webhooks", headers=AUTH)
        self.assertEqual(await response.get_json(), [])

    async def test_create_validations(self):
        response = await self.client.post("/tenant-admin/webhooks", json={"url": "ftp://nope"}, headers=AUTH)
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/tenant-admin/webhooks", json={}, headers=AUTH)
        self.assertEqual(response.status_code, 400)

    async def test_config_webhook_read_only(self):
        self.async_app.wsgi.config["TENANT_WEBHOOK_URL"] = "https://conf.example.com"
        response = await self.client.get("/tenant-admin/webhooks", headers=AUTH)
        hooks = await response.get_json()
        self.assertEqual(hooks[-1]["_id"], "config")
        response = await self.client.patch("/tenant-admin/webhooks/config", json={"url": "https://x"}, headers=AUTH)
        self.assertEqual(response.status_code, 400)
        response = await self.client.delete("/tenant-admin/webhooks/config", headers=AUTH)
        self.assertEqual(response.status_code, 400)

    async def test_per_webhook_test_delivery(self):
        response = await self.client.post("/tenant-admin/webhooks", json={"url": "https://t.example.com"}, headers=AUTH)
        hook_id = (await response.get_json())["_id"]
        with mock.patch("requests.post") as post:
            post.return_value.status_code = 204
            post.return_value.raise_for_status = mock.Mock()
            response = await self.client.post(f"/tenant-admin/webhooks/{hook_id}/test", headers=AUTH)
        payload = await response.get_json()
        self.assertEqual(payload["response_status"], 204)
        sent = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(sent["event"], "tenant.test")
