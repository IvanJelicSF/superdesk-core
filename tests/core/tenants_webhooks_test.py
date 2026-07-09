import hashlib
import hmac
import json

from datetime import timedelta
from unittest import IsolatedAsyncioTestCase, mock

from superdesk.utc import utcnow
from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.tenants import TenantStatus
from superdesk.core.tenants.registry import TenantRegistry
from superdesk.tests import MockWSGI

from superdesk.tenants import webhooks
from superdesk.celery_app import tenant_tasks


CONTROL_PLANE_DB = "sptests_controlplane_hooks"
WEBHOOK_URL = "https://hooks.example.com/tenants"


class WebhooksBaseTestCase(IsolatedAsyncioTestCase):
    config: dict = {}

    def setUp(self):
        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "MULTI_TENANT_ENABLED": True,
                    "TENANTS_MONGO_DBNAME": CONTROL_PLANE_DB,
                    "TENANTS_MONGO_URI": f"mongodb://localhost/{CONTROL_PLANE_DB}",
                    **self.config,
                }
            )
        )
        webhooks._collection().database.client.drop_database(CONTROL_PLANE_DB)

    def tearDown(self):
        webhooks._collection().database.client.drop_database(CONTROL_PLANE_DB)
        self.app.stop()


class WebhookStoreTestCase(WebhooksBaseTestCase):
    def test_crud(self):
        doc = webhooks.create_webhook(WEBHOOK_URL, secret="s3cret", name="partner feed")
        listed = webhooks.list_webhooks()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["url"], WEBHOOK_URL)

        public = webhooks.public_webhook(listed[0])
        self.assertNotIn("secret", public)
        self.assertTrue(public["has_secret"])

        updated = webhooks.update_webhook(doc["_id"], {"is_enabled": False})
        self.assertFalse(updated["is_enabled"])
        # secret kept when absent from updates
        self.assertEqual(updated["secret"], "s3cret")
        # secret cleared with empty string
        self.assertEqual(webhooks.update_webhook(doc["_id"], {"secret": ""})["secret"], "")

        self.assertTrue(webhooks.delete_webhook(doc["_id"]))
        self.assertEqual(webhooks.list_webhooks(), [])

    def test_enabled_webhooks(self):
        webhooks.create_webhook("https://one.example.com", name="one")
        webhooks.create_webhook("https://two.example.com", name="two")
        webhooks.create_webhook("https://disabled.example.com", is_enabled=False, name="off")

        urls = sorted(hook["url"] for hook in webhooks.enabled_webhooks())
        self.assertEqual(urls, ["https://one.example.com", "https://two.example.com"])

    def test_config_fallback_listed_and_matches_all(self):
        self.app.wsgi.config["TENANT_WEBHOOK_URL"] = WEBHOOK_URL
        self.app.wsgi.config["TENANT_WEBHOOK_SECRET"] = "conf-secret"
        hooks = webhooks.list_webhooks()
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["_id"], webhooks.CONFIG_WEBHOOK_ID)
        self.assertEqual(webhooks.enabled_webhooks()[0]["url"], WEBHOOK_URL)
        config_hook = webhooks.get_webhook(webhooks.CONFIG_WEBHOOK_ID)
        self.assertEqual(config_hook["secret"], "conf-secret")


class WebhookNotifyTestCase(WebhooksBaseTestCase):
    async def test_notify_queues_one_delivery_per_enabled_webhook(self):
        webhooks.create_webhook("https://one.example.com")
        webhooks.create_webhook("https://two.example.com")
        webhooks.create_webhook("https://off.example.com", is_enabled=False)

        doc = {"_id": "tenant-a", "status": "suspended", "hosts": ["a.example.com"]}
        with mock.patch.object(tenant_tasks.webhook_notify_task, "apply_async", mock.AsyncMock()) as apply:
            await webhooks.notify_tenant_event(webhooks.EVENT_SUSPENDED, doc)

        self.assertEqual(apply.await_count, 2)
        payloads = [call.kwargs["kwargs"]["payload"] for call in apply.await_args_list]
        self.assertTrue(all(p["event"] == "tenant.suspended" and p["tenant"] == "tenant-a" for p in payloads))

    async def test_notify_noop_without_webhooks(self):
        with mock.patch.object(tenant_tasks.webhook_notify_task, "apply_async", mock.AsyncMock()) as apply:
            await webhooks.notify_tenant_event(webhooks.EVENT_DELETED, {"_id": "tenant-a"})
        apply.assert_not_awaited()


class WebhookDeliveryTestCase(WebhooksBaseTestCase):
    def test_delivery_signs_payload(self):
        doc = webhooks.create_webhook(WEBHOOK_URL, secret="hook-secret")
        payload = {"event": "tenant.suspended", "tenant": "tenant-a"}
        with mock.patch("requests.post") as post:
            post.return_value.raise_for_status = mock.Mock()
            tenant_tasks.webhook_notify_task.run(payload, webhook_id=str(doc["_id"]))

        call = post.call_args
        self.assertEqual(call.args[0], WEBHOOK_URL)
        body = call.kwargs["data"]
        expected = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(call.kwargs["headers"]["X-Superdesk-Signature"], expected)
        self.assertEqual(json.loads(body), payload)

    def test_delivery_skips_deleted_or_disabled_webhook(self):
        doc = webhooks.create_webhook(WEBHOOK_URL)
        webhooks.update_webhook(doc["_id"], {"is_enabled": False})
        with mock.patch("requests.post") as post:
            tenant_tasks.webhook_notify_task.run({"event": "x"}, webhook_id=str(doc["_id"]))
            tenant_tasks.webhook_notify_task.run({"event": "x"}, webhook_id="6a4f00000000000000000000")
        post.assert_not_called()

    def test_delivery_via_config_webhook(self):
        self.app.wsgi.config["TENANT_WEBHOOK_URL"] = WEBHOOK_URL
        with mock.patch("requests.post") as post:
            post.return_value.raise_for_status = mock.Mock()
            tenant_tasks.webhook_notify_task.run({"event": "x"}, webhook_id=webhooks.CONFIG_WEBHOOK_ID)
        self.assertEqual(post.call_args.args[0], WEBHOOK_URL)


class RetentionPurgeTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "MULTI_TENANT_ENABLED": True,
                    "TENANT_DELETED_RETENTION_DAYS": 30,
                }
            )
        )

    def tearDown(self):
        self.app.stop()

    async def _run(self, docs):
        collection = mock.MagicMock()
        collection.find.return_value = docs

        updates: dict = {}

        with (
            mock.patch.object(TenantRegistry, "collection", new_callable=mock.PropertyMock, return_value=collection),
            mock.patch("superdesk.tenants.provisioning.purge_tenant_storage") as purge,
            mock.patch("superdesk.tenants.service.update_tenant", side_effect=lambda t, u: updates.update({t: u})),
            mock.patch(
                "superdesk.tenants.service.get_tenant_doc", side_effect=lambda t: {"_id": t, "purged_at": utcnow()}
            ),
            mock.patch("superdesk.tenants.webhooks.notify_tenant_event", mock.AsyncMock()) as notify,
        ):
            purged = await tenant_tasks._purge_deleted_tenants()
        return purged, purge, updates, notify, collection

    async def test_purges_expired_deleted_tenants(self):
        expired = {
            "_id": "tenant-a",
            "status": TenantStatus.DELETED.value,
            "hosts": [],
            "deleted_at": utcnow() - timedelta(days=40),
        }
        purged, purge, updates, notify, collection = await self._run([expired])
        self.assertEqual(purged, 1)
        purge.assert_called_once()
        self.assertIn("purged_at", updates["tenant-a"])
        notify.assert_awaited_once()

        query = collection.find.call_args.args[0]
        self.assertEqual(query["status"], "deleted")
        self.assertIsNone(query["purged_at"])
        self.assertIn("$lte", query["deleted_at"])

    async def test_noop_when_single_tenant(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        purged = await tenant_tasks._purge_deleted_tenants()
        self.assertEqual(purged, 0)
