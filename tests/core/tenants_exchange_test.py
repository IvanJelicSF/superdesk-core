import json

from unittest import IsolatedAsyncioTestCase, mock

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.tenants import Tenant, tenant_context, try_get_current_tenant
from superdesk.core.tenants.registry import TenantRegistry
from superdesk.tests import MockWSGI

from superdesk.tenants.exchange.media import copy_item_media
from superdesk.tenants.exchange.transmitter import InternalTenantTransmitter, PublishTenantExchangeError


SOURCE = Tenant(
    id="tenant-a",
    hosts=("a.example.com",),
    exchange_partners=({"tenant": "tenant-b", "direction": "both"},),
)
TARGET = Tenant(
    id="tenant-b",
    hosts=("b.example.com",),
    exchange_partners=({"tenant": "tenant-a", "direction": "receive"},),
)
STRANGER = Tenant(id="tenant-c", hosts=("c.example.com",))


class ExchangePartnersTestCase(IsolatedAsyncioTestCase):
    def test_partner_directions(self):
        self.assertTrue(SOURCE.can_send_to("tenant-b"))
        self.assertTrue(SOURCE.can_receive_from("tenant-b"))
        self.assertTrue(TARGET.can_receive_from("tenant-a"))
        self.assertFalse(TARGET.can_send_to("tenant-a"))
        self.assertFalse(STRANGER.can_send_to("tenant-a"))
        self.assertFalse(SOURCE.can_send_to("tenant-c"))

    def test_partners_survive_dict_roundtrip(self):
        self.assertEqual(Tenant.from_dict(SOURCE.to_dict()).exchange_partners, SOURCE.exchange_partners)


class FakeMediaFile:
    def __init__(self, content, filename, content_type):
        self._content = content
        self.filename = filename
        self.content_type = content_type
        self.metadata = {}

    async def to_bytes(self):
        return self._content


class FakeMediaStorage:
    """Tenant-aware fake: files stored per current tenant id."""

    def __init__(self):
        self.stores: dict = {}
        self.counter = 0

    def _store(self):
        tenant = try_get_current_tenant()
        return self.stores.setdefault(tenant.id if tenant else "default", {})

    async def get_async(self, media_id):
        return self._store().get(media_id)

    async def put_async(self, content, filename=None, content_type=None, metadata=None, **kwargs):
        self.counter += 1
        media_id = f"media-{self.counter}"
        self._store()[media_id] = FakeMediaFile(content, filename, content_type)
        return media_id

    def url_for_media(self, media_id, content_type=None):
        return f"/api/upload-raw/{media_id}"


class CopyItemMediaTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))
        self.media = FakeMediaStorage()
        self.app.wsgi.media = self.media

    def tearDown(self):
        self.app.stop()

    async def test_media_copied_and_references_rewritten(self):
        with tenant_context(SOURCE):
            source_id = await self.media.put_async(b"IMAGE", filename="pic.jpg", content_type="image/jpeg")
            item = {
                "guid": "item-1",
                "renditions": {"baseImage": {"media": source_id, "href": "http://a.example.com/x"}},
                "associations": {
                    "featuremedia": {"renditions": {"original": {"media": source_id, "href": "old"}}},
                },
            }
            copied = await copy_item_media(item, TARGET)

        self.assertEqual(copied, 2)
        base = item["renditions"]["baseImage"]
        assoc = item["associations"]["featuremedia"]["renditions"]["original"]
        self.assertNotEqual(base["media"], source_id)
        self.assertIn(base["media"], self.media.stores["tenant-b"])
        self.assertIn(assoc["media"], self.media.stores["tenant-b"])
        self.assertEqual(base["href"], f"/api/upload-raw/{base['media']}")
        # source storage untouched
        self.assertIn(source_id, self.media.stores["tenant-a"])


class TransmitterValidationTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))
        self.transmitter = InternalTenantTransmitter()
        self.tenants = {tenant.id: tenant for tenant in (SOURCE, TARGET, STRANGER)}
        patcher = mock.patch.object(TenantRegistry, "get_by_id_sync", side_effect=self.tenants.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.app.stop()

    def queue_item(self, target_id):
        return {
            "item_id": "item-1",
            "formatted_item": json.dumps({"guid": "item-1", "headline": "hello"}),
            "destination": {"config": {"tenant": target_id}},
        }

    async def test_unknown_target_rejected(self):
        with tenant_context(SOURCE):
            with self.assertRaises(PublishTenantExchangeError):
                await self.transmitter._transmit(self.queue_item("nope"), subscriber={})

    async def test_non_partner_rejected(self):
        with tenant_context(SOURCE):
            with self.assertRaises(PublishTenantExchangeError):
                await self.transmitter._transmit(self.queue_item("tenant-c"), subscriber={})

    async def test_one_way_partnership_rejected(self):
        # tenant-b only allows receive from tenant-a; sending b -> a must fail
        with tenant_context(TARGET):
            with self.assertRaises(PublishTenantExchangeError):
                await self.transmitter._transmit(self.queue_item("tenant-a"), subscriber={})

    async def test_partner_transmit_enqueues_delivery(self):
        with mock.patch("superdesk.tenants.exchange.receiver.deliver_to_tenant") as task_mock:
            task_mock.apply_async = mock.AsyncMock()
            with tenant_context(SOURCE):
                await self.transmitter._transmit(self.queue_item("tenant-b"), subscriber={})

        task_mock.apply_async.assert_awaited_once()
        call = task_mock.apply_async.await_args
        self.assertEqual(call.kwargs["headers"], {"tenant_id": "tenant-b"})
        delivered = call.kwargs["kwargs"]
        self.assertEqual(delivered["source_tenant"], "tenant-a")
        # the item travels as a json string (celery serializer would coerce datetimes)
        item = json.loads(delivered["item"])
        self.assertEqual(item["extra"]["original_tenant"], "tenant-a")
        self.assertEqual(item["extra"]["original_item_id"], "item-1")
