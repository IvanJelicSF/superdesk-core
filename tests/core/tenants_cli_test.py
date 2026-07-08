import click

from unittest import IsolatedAsyncioTestCase, mock

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.cli import with_tenant_options
from superdesk.core.tenants import Tenant, try_get_current_tenant
from superdesk.core.tenants.registry import TenantRegistry
from superdesk.tests import MockWSGI

import superdesk.tenants.provisioning as provisioning
from superdesk.tenants.service import TenantExistsError


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))
TENANT_B = Tenant(id="tenant-b", hosts=("tenant-b.example.com",))


class WithTenantOptionsTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))
        self.calls: list = []

        async def command():
            self.calls.append(try_get_current_tenant())
            return len(self.calls)

        self.wrapped = with_tenant_options(command)

    def tearDown(self):
        self.app.stop()

    async def test_multi_tenant_requires_option(self):
        with self.assertRaises(click.UsageError):
            await self.wrapped()

    async def test_single_tenant_runs_once_without_context(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        result = await self.wrapped()
        self.assertEqual(result, 1)
        self.assertEqual(self.calls, [None])

    async def test_explicit_tenants(self):
        tenants = {"tenant-a": TENANT_A, "tenant-b": TENANT_B}
        with mock.patch.object(TenantRegistry, "get_by_id_sync", side_effect=lambda tid: tenants.get(tid)):
            results = await self.wrapped(tenant_ids=("tenant-a", "tenant-b"))
        self.assertEqual(results, [1, 2])
        self.assertEqual([tenant.id for tenant in self.calls], ["tenant-a", "tenant-b"])

    async def test_all_tenants(self):
        with mock.patch.object(TenantRegistry, "get_all_active_sync", return_value=[TENANT_A, TENANT_B]):
            results = await self.wrapped(all_tenants=True)
        self.assertEqual(len(results), 2)
        self.assertEqual([tenant.id for tenant in self.calls], ["tenant-a", "tenant-b"])


class ProvisioningTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))
        self.docs: dict = {}

        collection = mock.MagicMock()
        collection.find_one.side_effect = lambda query: self.docs.get(query["_id"])
        collection.insert_one.side_effect = lambda doc: self.docs.__setitem__(doc["_id"], doc)

        def update_one(query, update):
            doc = self.docs.get(query["_id"])
            result = mock.MagicMock()
            result.matched_count = 1 if doc else 0
            if doc:
                for key, val in update["$set"].items():
                    if key.startswith("provisioning."):
                        doc.setdefault("provisioning", {})[key.split(".", 1)[1]] = val
                    else:
                        doc[key] = val
            return result

        collection.update_one.side_effect = update_one

        self.collection_patcher = mock.patch.object(
            TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection
        )
        self.collection_patcher.start()
        self.indexes_patcher = mock.patch.object(TenantRegistry, "ensure_indexes")
        self.indexes_patcher.start()

        self.init_data = mock.AsyncMock()
        self.create_user = mock.AsyncMock()
        import apps.prepopulate.app_initialize
        import apps.auth.db.commands

        self.init_patcher = mock.patch.object(
            apps.prepopulate.app_initialize, "app_initialize_data_handler", self.init_data
        )
        self.user_patcher = mock.patch.object(apps.auth.db.commands, "create_user_command_handler", self.create_user)
        self.init_patcher.start()
        self.user_patcher.start()

    def tearDown(self):
        for patcher in (self.collection_patcher, self.indexes_patcher, self.init_patcher, self.user_patcher):
            patcher.stop()
        self.app.stop()

    async def test_provision_new_tenant(self):
        admin = {"username": "admin", "password": "secret", "email": "admin@example.com"}
        await provisioning.provision_tenant(TENANT_A, admin=admin)

        doc = self.docs["tenant-a"]
        self.assertEqual(doc["status"], "active")
        self.assertIn(provisioning.STEP_INITIALIZE_DATA, doc["provisioning"])
        self.assertIn(provisioning.STEP_ADMIN_USER, doc["provisioning"])
        self.init_data.assert_awaited_once()
        self.create_user.assert_awaited_once_with(
            username="admin", password="secret", email="admin@example.com", admin=True
        )

    async def test_existing_tenant_requires_resume(self):
        await provisioning.provision_tenant(TENANT_A)
        with self.assertRaises(TenantExistsError):
            await provisioning.provision_tenant(TENANT_A)

    async def test_resume_skips_completed_steps(self):
        admin = {"username": "admin", "password": "secret", "email": "admin@example.com"}
        await provisioning.provision_tenant(TENANT_A)
        self.init_data.assert_awaited_once()

        # resume with admin: data init is already done, only the admin step runs
        await provisioning.provision_tenant(TENANT_A, admin=admin, resume=True)
        self.init_data.assert_awaited_once()
        self.create_user.assert_awaited_once()
        self.assertEqual(self.docs["tenant-a"]["status"], "active")
