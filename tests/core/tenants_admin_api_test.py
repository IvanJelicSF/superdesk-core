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
        with mock.patch.object(admin_api, "list_tenant_docs", return_value=docs):
            response = await self.client.get("/tenant-admin/tenants", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        payload = await response.get_json()
        self.assertEqual(payload[0]["slug"], "tenant-a")

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

    async def test_delete_with_purge(self):
        doc = {"_id": "tenant-a", "status": "suspended", "hosts": ["a.example.com"]}
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(admin_api, "purge_tenant_storage") as purge,
            mock.patch.object(admin_api, "delete_tenant_record") as delete,
        ):
            response = await self.client.delete("/tenant-admin/tenants/tenant-a?purge=1", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        purge.assert_called_once()
        delete.assert_called_once_with("tenant-a")

    async def test_patch_status_and_partners(self):
        doc = {"_id": "tenant-a", "status": "active", "hosts": []}
        with (
            mock.patch.object(admin_api, "get_tenant_doc", return_value=doc),
            mock.patch.object(admin_api, "set_tenant_status") as set_status,
            mock.patch.object(admin_api, "update_tenant") as update,
        ):
            response = await self.client.patch(
                "/tenant-admin/tenants/tenant-a",
                json={"status": "suspended", "exchange_partners": [{"tenant": "tenant-a", "direction": "both"}]},
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        set_status.assert_called_once()
        update.assert_called_once_with("tenant-a", {"exchange_partners": [{"tenant": "tenant-a", "direction": "both"}]})
