import asyncio

from unittest import TestCase, IsolatedAsyncioTestCase, mock

from superdesk.core.app import SuperdeskAsyncApp, get_app_config
from superdesk.core.tenants import (
    Tenant,
    TenantStatus,
    TenantNotSetError,
    TenantNotFoundError,
    get_current_tenant,
    try_get_current_tenant,
    tenant_context,
    tenant_db_name,
    tenant_index_prefix,
    register_tenant_overridable_config,
)
from superdesk.core.tenants.context import TENANT_OVERRIDABLE_CONFIG_KEYS
from superdesk.core.tenants.registry import TenantRegistry
from superdesk.tests import MockWSGI


def make_tenant(**kwargs) -> Tenant:
    kwargs.setdefault("id", "tenant-a")
    kwargs.setdefault("hosts", ("tenant-a.example.com",))
    return Tenant(**kwargs)


class TenantModelTestCase(TestCase):
    def test_id_validation(self):
        for invalid in ("Tenant", "tenant_a", "-tenant", "1tenant", ""):
            with self.assertRaises(ValueError, msg=invalid):
                Tenant(id=invalid)

    def test_default_prefixes_derived_from_id(self):
        tenant = make_tenant()
        self.assertEqual(tenant.db_prefix, "sd_tenant_a")
        self.assertEqual(tenant.elastic_prefix, "sd_tenant_a")

    def test_dict_roundtrip(self):
        tenant = make_tenant(status=TenantStatus.SUSPENDED, config_overrides={"KEY": 1})
        self.assertEqual(Tenant.from_dict(tenant.to_dict()), tenant)


class TenantNamingTestCase(TestCase):
    def test_db_names(self):
        tenant = make_tenant()
        self.assertEqual(tenant_db_name(tenant), "sd_tenant_a")
        self.assertEqual(tenant_db_name(tenant, "ARCHIVED"), "sd_tenant_a_archived")
        self.assertEqual(tenant_db_name(tenant, "LEGAL_ARCHIVE"), "sd_tenant_a_legal_archive")
        self.assertEqual(tenant_db_name(tenant, "CONTENTAPI_MONGO"), "sd_tenant_a_contentapi")
        # project-defined prefix falls back to a derived suffix
        self.assertEqual(tenant_db_name(tenant, "CUSTOM"), "sd_tenant_a_custom")

    def test_index_prefixes(self):
        tenant = make_tenant()
        self.assertEqual(tenant_index_prefix(tenant), "sd_tenant_a")
        self.assertEqual(tenant_index_prefix(tenant, "CONTENTAPI_ELASTICSEARCH"), "sd_tenant_a_contentapi")

    def test_default_tenant_rejected(self):
        default = Tenant(id="default", is_default=True)
        with self.assertRaises(ValueError):
            tenant_db_name(default)
        with self.assertRaises(ValueError):
            tenant_index_prefix(default)


class TenantContextTestCase(TestCase):
    app: SuperdeskAsyncApp

    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MONGO_DBNAME": "superdesk"}))

    def tearDown(self):
        self.app.stop()

    def test_single_tenant_mode_returns_default_tenant(self):
        self.assertIsNone(try_get_current_tenant())
        tenant = get_current_tenant()
        self.assertTrue(tenant.is_default)
        self.assertEqual(tenant.id, "default")

    def test_multi_tenant_mode_fails_closed(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = True
        with self.assertRaises(TenantNotSetError):
            get_current_tenant()

    def test_tenant_context_sets_and_resets(self):
        tenant = make_tenant()
        with tenant_context(tenant) as bound:
            self.assertEqual(bound, tenant)
            self.assertEqual(get_current_tenant(), tenant)
            self.assertEqual(try_get_current_tenant(), tenant)
        self.assertIsNone(try_get_current_tenant())

    def test_tenant_context_nesting(self):
        tenant_a = make_tenant()
        tenant_b = make_tenant(id="tenant-b", hosts=("tenant-b.example.com",))
        with tenant_context(tenant_a):
            with tenant_context(tenant_b):
                self.assertEqual(get_current_tenant(), tenant_b)
            self.assertEqual(get_current_tenant(), tenant_a)

    def test_tenant_context_by_unknown_id(self):
        with mock.patch.object(TenantRegistry, "get_by_id_sync", return_value=None):
            with self.assertRaises(TenantNotFoundError):
                with tenant_context("nope"):
                    pass

    def test_config_overlay_respects_whitelist(self):
        self.app.wsgi.config["SOME_KEY"] = "global"
        self.app.wsgi.config["OTHER_KEY"] = "global"
        tenant = make_tenant(config_overrides={"SOME_KEY": "tenant", "OTHER_KEY": "tenant"})

        original_keys = set(TENANT_OVERRIDABLE_CONFIG_KEYS)
        register_tenant_overridable_config("SOME_KEY")
        try:
            with tenant_context(tenant):
                self.assertEqual(get_app_config("SOME_KEY"), "tenant")
                self.assertEqual(get_app_config("OTHER_KEY"), "global")
            self.assertEqual(get_app_config("SOME_KEY"), "global")
        finally:
            TENANT_OVERRIDABLE_CONFIG_KEYS.clear()
            TENANT_OVERRIDABLE_CONFIG_KEYS.update(original_keys)


class TenantContextIsolationTestCase(IsolatedAsyncioTestCase):
    async def test_concurrent_tasks_are_isolated(self):
        tenant_a = make_tenant()
        tenant_b = make_tenant(id="tenant-b", hosts=("tenant-b.example.com",))
        a_bound = asyncio.Event()
        b_bound = asyncio.Event()

        async def run_as(tenant, bound, other_bound):
            with tenant_context(tenant):
                bound.set()
                await asyncio.wait_for(other_bound.wait(), timeout=5)
                # the other task has bound its own tenant by now; ours must be unchanged
                self.assertEqual(try_get_current_tenant(), tenant)
            self.assertIsNone(try_get_current_tenant())

        await asyncio.gather(
            run_as(tenant_a, a_bound, b_bound),
            run_as(tenant_b, b_bound, a_bound),
        )
        self.assertIsNone(try_get_current_tenant())


class TenantRegistryTestCase(TestCase):
    app: SuperdeskAsyncApp

    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"TENANTS_CACHE_TTL": 60}))
        self.registry = self.app.tenants
        self.doc = make_tenant().to_dict()

    def tearDown(self):
        self.app.stop()

    def collection_mock(self, docs_by_query):
        collection = mock.MagicMock()
        collection.find_one.side_effect = lambda query: docs_by_query.get(str(query))
        return collection

    def test_lookups_are_cached(self):
        collection = self.collection_mock({str({"_id": "tenant-a"}): self.doc})
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            first = self.registry.get_by_id_sync("tenant-a")
            second = self.registry.get_by_id_sync("tenant-a")
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(collection.find_one.call_count, 1)

    def test_host_lookup_populates_id_cache(self):
        collection = self.collection_mock({str({"hosts": "tenant-a.example.com"}): self.doc})
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            by_host = self.registry.get_by_host_sync("tenant-a.example.com")
            by_id = self.registry.get_by_id_sync("tenant-a")
        self.assertIsNotNone(by_host)
        self.assertEqual(by_host, by_id)
        self.assertEqual(collection.find_one.call_count, 1)

    def test_unknown_host_is_negative_cached(self):
        collection = self.collection_mock({})
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            self.assertIsNone(self.registry.get_by_host_sync("nope.example.com"))
            self.assertIsNone(self.registry.get_by_host_sync("nope.example.com"))
        self.assertEqual(collection.find_one.call_count, 1)

    def test_invalidate(self):
        collection = self.collection_mock({str({"_id": "tenant-a"}): self.doc})
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            self.registry.get_by_id_sync("tenant-a")
            self.registry.invalidate("tenant-a")
            self.registry.get_by_id_sync("tenant-a")
        self.assertEqual(collection.find_one.call_count, 2)

    def test_cache_expires(self):
        self.app.wsgi.config["TENANTS_CACHE_TTL"] = 0
        collection = self.collection_mock({str({"_id": "tenant-a"}): self.doc})
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            self.registry.get_by_id_sync("tenant-a")
            self.registry.get_by_id_sync("tenant-a")
        self.assertEqual(collection.find_one.call_count, 2)

    def test_get_all_active(self):
        collection = mock.MagicMock()
        collection.find.return_value = [self.doc]
        with mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection):
            active = self.registry.get_all_active_sync()
            self.registry.get_all_active_sync()
        self.assertEqual([tenant.id for tenant in active], ["tenant-a"])
        collection.find.assert_called_once_with({"status": "active"})


class TenantMetadataTestCase(TestCase):
    def test_name_defaults_to_id(self):
        tenant = make_tenant()
        self.assertEqual(tenant.name, "tenant-a")
        self.assertEqual(tenant.description, "")

    def test_name_and_description_roundtrip(self):
        tenant = make_tenant(name="Tenant A Newsroom", description="The A-team newsroom")
        doc = tenant.to_dict()
        self.assertEqual(doc["name"], "Tenant A Newsroom")
        self.assertEqual(doc["description"], "The A-team newsroom")
        restored = Tenant.from_dict(doc)
        self.assertEqual(restored, tenant)

    def test_empty_stored_name_falls_back_to_id(self):
        restored = Tenant.from_dict({"_id": "tenant-a", "hosts": ["a.example.com"], "name": ""})
        self.assertEqual(restored.name, "tenant-a")
