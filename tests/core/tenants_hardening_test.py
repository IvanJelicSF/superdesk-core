from unittest import TestCase, mock

from superdesk.cache import SuperdeskCacheBackend, SuperdeskMangler
from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.tenants import Tenant, tenant_context
from superdesk.storage.amazon_media_storage import AmazonMediaStorage
from superdesk.tests import MockWSGI


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))
TENANT_B = Tenant(id="tenant-b", hosts=("tenant-b.example.com",))


class FakeApp:
    def __init__(self, config):
        self.config = config
        self.extensions: dict = {}


class CacheTenantPrefixTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={}))
        self.backend = SuperdeskCacheBackend(SuperdeskMangler())
        self.backend.init_app(FakeApp({}))  # in-process dict backend

    def tearDown(self):
        self.app.stop()

    def test_keys_are_tenant_scoped(self):
        with tenant_context(TENANT_A):
            self.backend.save({"key": "value-a"})
        with tenant_context(TENANT_B):
            self.backend.save({"key": "value-b"})
        self.backend.save({"key": "value-default"})

        with tenant_context(TENANT_A):
            self.assertEqual(self.backend.load("key"), "value-a")
        with tenant_context(TENANT_B):
            self.assertEqual(self.backend.load("key"), "value-b")
        self.assertEqual(self.backend.load("key"), "value-default")

    def test_multi_key_load_maps_back_to_requested_keys(self):
        with tenant_context(TENANT_A):
            self.backend.save({"one": 1, "two": 2})
            values = self.backend.load(["one", "two", "missing"])
        self.assertEqual(values, {"one": 1, "two": 2})

    def test_remove_is_tenant_scoped(self):
        with tenant_context(TENANT_A):
            self.backend.save({"key": "value-a"})
        self.backend.save({"key": "value-default"})

        with tenant_context(TENANT_A):
            self.backend.remove("key")
            self.assertIsNone(self.backend.load("key"))
        self.assertEqual(self.backend.load("key"), "value-default")


class AmazonSubfolderTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={}))
        self.storage = object.__new__(AmazonMediaStorage)
        self.storage.app = mock.MagicMock()
        self.storage.app.config = {"AMAZON_S3_SUBFOLDER": "prod"}

    def tearDown(self):
        self.app.stop()

    def test_default_tenant_uses_configured_subfolder(self):
        self.assertEqual(self.storage.get_key("media/1.jpg"), "prod/media/1.jpg")

    def test_tenant_gets_nested_subfolder(self):
        with tenant_context(TENANT_A):
            self.assertEqual(self.storage.get_key("media/1.jpg"), "prod/tenant-a/media/1.jpg")

    def test_tenant_without_base_subfolder(self):
        self.storage.app.config = {}
        with tenant_context(TENANT_A):
            self.assertEqual(self.storage.get_key("media/1.jpg"), "tenant-a/media/1.jpg")
        self.assertEqual(self.storage.get_key("media/1.jpg"), "media/1.jpg")

    def test_false_subfolder_config(self):
        self.storage.app.config = {"AMAZON_S3_SUBFOLDER": "false"}
        self.assertEqual(self.storage.get_key("media/1.jpg"), "media/1.jpg")
        with tenant_context(TENANT_A):
            self.assertEqual(self.storage.get_key("media/1.jpg"), "tenant-a/media/1.jpg")
