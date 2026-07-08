from unittest import IsolatedAsyncioTestCase, mock

from quart import Quart

from superdesk.core.tenants import Tenant, TenantStatus, try_get_current_tenant
from superdesk.core.tenants.middleware import setup_tenant_middleware


def make_app(config: dict) -> tuple[Quart, mock.MagicMock]:
    app = Quart(__name__)
    app.config.update(config)

    registry = mock.MagicMock()
    app.async_app = mock.MagicMock()
    app.async_app.tenants = registry

    async def get_by_host(host):
        return registry.tenants_by_host.get(host)

    registry.tenants_by_host = {}
    registry.get_by_host = get_by_host

    @app.route("/api/test")
    async def endpoint():
        tenant = try_get_current_tenant()
        return {"tenant": tenant.id if tenant else None}

    setup_tenant_middleware(app)
    return app, registry


class TenantMiddlewareTestCase(IsolatedAsyncioTestCase):
    async def test_disabled_multi_tenancy_is_a_noop(self):
        app, _registry = make_app({"MULTI_TENANT_ENABLED": False})
        client = app.test_client()
        response = await client.get("/api/test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["tenant"], None)

    async def test_known_host_binds_tenant(self):
        app, registry = make_app({"MULTI_TENANT_ENABLED": True})
        registry.tenants_by_host["tenant-a.example.com"] = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "tenant-a.example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["tenant"], "tenant-a")
        # context is reset after the request
        self.assertIsNone(try_get_current_tenant())

    async def test_host_port_is_stripped(self):
        app, registry = make_app({"MULTI_TENANT_ENABLED": True})
        registry.tenants_by_host["tenant-a.example.com"] = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "Tenant-A.example.com:8080"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["tenant"], "tenant-a")

    async def test_unknown_host_fails_closed(self):
        app, _registry = make_app({"MULTI_TENANT_ENABLED": True})
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "nope.example.com"})
        self.assertEqual(response.status_code, 404)

    async def test_suspended_tenant_rejected(self):
        app, registry = make_app({"MULTI_TENANT_ENABLED": True})
        registry.tenants_by_host["tenant-a.example.com"] = Tenant(
            id="tenant-a", hosts=("tenant-a.example.com",), status=TenantStatus.SUSPENDED
        )
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "tenant-a.example.com"})
        self.assertEqual(response.status_code, 423)

    async def test_deleted_tenant_rejected(self):
        app, registry = make_app({"MULTI_TENANT_ENABLED": True})
        registry.tenants_by_host["tenant-a.example.com"] = Tenant(
            id="tenant-a", hosts=("tenant-a.example.com",), status=TenantStatus.DELETED
        )
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "tenant-a.example.com"})
        self.assertEqual(response.status_code, 404)

    async def test_exempt_path_skips_resolution(self):
        app, _registry = make_app({"MULTI_TENANT_ENABLED": True, "TENANT_EXEMPT_PATHS": ["/api/test"]})
        client = app.test_client()
        response = await client.get("/api/test", headers={"Host": "nope.example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["tenant"], None)
