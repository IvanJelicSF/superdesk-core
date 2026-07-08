from unittest import TestCase, mock

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.types import ElasticResourceConfig, ElasticClientConfig
from superdesk.core.elastic.sync_client import ElasticResourceClient
from superdesk.core.tenants import tenant_context
from superdesk.core.tenants.models import Tenant
from superdesk.core.tenants.eve_shims import _tenant_resource_index
from superdesk.tests import MockWSGI


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))


def make_client(prefix: str = "ELASTICSEARCH", index: str = "superdesk_items") -> ElasticResourceClient:
    # mirror how ElasticResources.register_resource_config builds the client config
    client_config = ElasticClientConfig.create_from_dict(
        {f"{prefix}_INDEX": index.rsplit("_", 1)[0]}, prefix, freeze=False
    )
    client_config.index += "_items"
    client_config.set_frozen(True)
    return ElasticResourceClient(
        "items",
        client_config,
        ElasticResourceConfig(prefix=prefix),
    )


class ElasticTenantIndexTestCase(TestCase):
    app: SuperdeskAsyncApp

    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"ELASTICSEARCH_INDEX": "superdesk"}))

    def tearDown(self):
        self.app.stop()

    def test_default_tenant_uses_registration_index(self):
        client = make_client()
        self.assertEqual(client.index, "superdesk_items")

    def test_tenant_index_resolved_at_query_time(self):
        client = make_client()
        with tenant_context(TENANT_A):
            self.assertEqual(client.index, "sd_tenant_a_items")
        self.assertEqual(client.index, "superdesk_items")

    def test_contentapi_prefix_gets_suffix(self):
        client = make_client(prefix="CONTENTAPI_ELASTICSEARCH", index="contentapi_items")
        with tenant_context(TENANT_A):
            self.assertEqual(client.index, "sd_tenant_a_contentapi_items")

    def test_query_args_use_tenant_index(self):
        client = make_client()
        with tenant_context(TENANT_A):
            self.assertEqual(client._get_insert_args({"foo": 1}, "id1")["index"], "sd_tenant_a_items")
            self.assertEqual(client._get_remove_args("id1")["index"], "sd_tenant_a_items")
            self.assertEqual(client._get_count_args()["index"], "sd_tenant_a_items")
            self.assertEqual(client._get_search_args({"query": {}})["index"], "sd_tenant_a_items")
        self.assertEqual(client._get_insert_args({"foo": 1}, "id1")["index"], "superdesk_items")


class EveElasticShimTestCase(TestCase):
    app: SuperdeskAsyncApp

    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={}))
        self.elastic = mock.MagicMock()
        self.elastic._resource_prefix.return_value = "ELASTICSEARCH"
        self.elastic.get_datasource.return_value = ("items", None, None, None)

    def tearDown(self):
        self.app.stop()

    def test_default_tenant_keeps_default_index(self):
        self.assertEqual(_tenant_resource_index(self.elastic, "items", "superdesk_items"), "superdesk_items")

    def test_tenant_index_uses_tenant_prefix(self):
        with tenant_context(TENANT_A):
            self.assertEqual(_tenant_resource_index(self.elastic, "items", "superdesk_items"), "sd_tenant_a_items")
