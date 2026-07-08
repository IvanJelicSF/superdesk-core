import json

from uuid import uuid4
from unittest import TestCase
from unittest.mock import MagicMock

from websockets import ServerConnection

from superdesk import notification
from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.tenants import Tenant, tenant_context
from superdesk.types import WebsocketMessageData
from superdesk.websockets_comms import SocketCommunication
from superdesk.tests import MockWSGI


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))


class TestClient(ServerConnection):
    def __init__(self, host: str = "", path: str = ""):
        self.id = uuid4()
        self.request = MagicMock()
        self.request.path = path
        self.request.headers = {"Host": host} if host else {}


def make_comms(multi_tenant=True) -> SocketCommunication:
    return SocketCommunication("host", "1", "url", multi_tenant=multi_tenant)


class WebsocketTenantFilterTestCase(TestCase):
    def setUp(self):
        self.comms = make_comms()
        self.client_a = TestClient(host="tenant-a.example.com:443")
        self.client_b = TestClient(host="tenant-b.example.com")
        self.client_unknown = TestClient()
        for client in (self.client_a, self.client_b, self.client_unknown):
            self.comms._add_client(client)

    def test_client_tenant_resolved_from_host(self):
        self.assertEqual(self.comms.client_tenants[self.client_a.id], "tenant-a")
        self.assertEqual(self.comms.client_tenants[self.client_b.id], "tenant-b")
        self.assertIsNone(self.comms.client_tenants[self.client_unknown.id])

    def test_message_delivered_only_to_matching_tenant(self):
        message = WebsocketMessageData(event="item:updated")
        message["tenant"] = "tenant-a"  # type: ignore[typeddict-unknown-key]
        recipients = self.comms.get_message_recipients(message)
        self.assertEqual(recipients, {self.client_a})

    def test_untagged_message_dropped(self):
        recipients = self.comms.get_message_recipients(WebsocketMessageData(event="item:updated"))
        self.assertEqual(recipients, set())

    def test_single_tenant_mode_delivers_to_all(self):
        comms = make_comms(multi_tenant=False)
        for client in (self.client_a, self.client_b, self.client_unknown):
            comms._add_client(client)
        recipients = comms.get_message_recipients(WebsocketMessageData(event="item:updated"))
        self.assertEqual(len(recipients), 3)

    def test_remove_client_clears_tenant(self):
        self.comms._remove_client(self.client_a)
        self.assertNotIn(self.client_a.id, self.comms.client_tenants)


class NotificationTenantTagTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))

    def tearDown(self):
        self.app.stop()

    def test_message_tagged_with_tenant(self):
        with tenant_context(TENANT_A):
            message = json.loads(notification._create_socket_message(event="item:updated"))
        self.assertEqual(message["tenant"], "tenant-a")

    def test_message_without_tenant_is_untagged(self):
        message = json.loads(notification._create_socket_message(event="item:updated"))
        self.assertNotIn("tenant", message)

    def test_single_tenant_mode_untagged(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        with tenant_context(TENANT_A):
            message = json.loads(notification._create_socket_message(event="item:updated"))
        self.assertNotIn("tenant", message)
