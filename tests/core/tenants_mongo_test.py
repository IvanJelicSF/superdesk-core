from unittest import TestCase

from pymongo import MongoClient

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.types import MongoResourceConfig
from superdesk.core.mongo.utils import get_mongo_client_config
from superdesk.core.tenants import tenant_context, get_default_tenant
from superdesk.core.tenants.models import Tenant
from superdesk.core.tenants.eve_shims import _tenant_db
from superdesk.tests import MockWSGI


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))
TENANT_B = Tenant(id="tenant-b", hosts=("tenant-b.example.com",))

CONFIG = {
    "MONGO_URI": "mongodb://localhost/superdesk",
    "MONGO_DBNAME": "superdesk",
    "ARCHIVED_URI": "mongodb://localhost/archived",
    "ARCHIVED_DBNAME": "archived",
}


class MongoClientConfigTenantTestCase(TestCase):
    def test_default_tenant_uses_configured_dbname(self):
        _kwargs, dbname = get_mongo_client_config(CONFIG, "MONGO")
        self.assertEqual(dbname, "superdesk")
        _kwargs, dbname = get_mongo_client_config(CONFIG, "MONGO", get_default_tenant())
        self.assertEqual(dbname, "superdesk")

    def test_tenant_overrides_dbname(self):
        _kwargs, dbname = get_mongo_client_config(CONFIG, "MONGO", TENANT_A)
        self.assertEqual(dbname, "sd_tenant_a")
        _kwargs, dbname = get_mongo_client_config(CONFIG, "ARCHIVED", TENANT_A)
        self.assertEqual(dbname, "sd_tenant_a_archived")

    def test_tenant_does_not_change_client_kwargs(self):
        default_kwargs, _ = get_mongo_client_config(CONFIG, "MONGO")
        tenant_kwargs, _ = get_mongo_client_config(CONFIG, "MONGO", TENANT_A)
        self.assertEqual(default_kwargs, tenant_kwargs)


class MongoResourcesTenantTestCase(TestCase):
    app: SuperdeskAsyncApp

    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config=dict(CONFIG)))
        self.app.mongo.register_resource_config("items", MongoResourceConfig(versioning=True))

    def tearDown(self):
        self.app.stop()

    def test_db_handles_are_tenant_scoped_on_a_shared_client(self):
        default_client, default_db = self.app.mongo.get_client("items")
        with tenant_context(TENANT_A):
            client_a, db_a = self.app.mongo.get_client("items")
        with tenant_context(TENANT_B):
            client_b, db_b = self.app.mongo.get_client("items")

        # one client per prefix, shared across tenants
        self.assertIs(default_client, client_a)
        self.assertIs(client_a, client_b)
        # but tenant-scoped databases
        self.assertEqual(default_db.name, "superdesk")
        self.assertEqual(db_a.name, "sd_tenant_a")
        self.assertEqual(db_b.name, "sd_tenant_b")

    def test_versioned_access_uses_same_tenant_db(self):
        with tenant_context(TENANT_A):
            _client, db = self.app.mongo.get_client("items", versioning=True)
            self.assertEqual(db.name, "sd_tenant_a")
            collection = self.app.mongo.get_collection("items", versioning=True)
            self.assertEqual(collection.name, "items_versions")
        _client, db = self.app.mongo.get_client("items", versioning=True)
        self.assertEqual(db.name, "superdesk")

    def test_async_db_handles_are_tenant_scoped(self):
        with tenant_context(TENANT_A):
            client_a, db_a = self.app.mongo.get_client_async("items")
        client_default, db_default = self.app.mongo.get_client_async("items")
        self.assertIs(client_a, client_default)
        self.assertEqual(db_a.name, "sd_tenant_a")
        self.assertEqual(db_default.name, "superdesk")


class EveShimTenantDbTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config=dict(CONFIG)))
        self.client = MongoClient("mongodb://localhost/superdesk", connect=False)
        self.extensions = {"MONGO": (self.client, self.client["superdesk"])}

    def tearDown(self):
        self.app.stop()
        self.client.close()

    def test_default_tenant_returns_configured_db(self):
        self.assertEqual(_tenant_db(self.extensions, "MONGO").name, "superdesk")

    def test_tenant_db_resolved_and_cached_off_shared_client(self):
        with tenant_context(TENANT_A):
            db = _tenant_db(self.extensions, "MONGO")
        self.assertEqual(db.name, "sd_tenant_a")
        self.assertIn("MONGO::tenant-a", self.extensions)
        self.assertIs(self.extensions["MONGO::tenant-a"][0], self.client)
        # default entry untouched
        self.assertEqual(self.extensions["MONGO"][1].name, "superdesk")
