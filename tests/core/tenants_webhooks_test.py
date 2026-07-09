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


WEBHOOK_URL = "https://hooks.example.com/tenants"


class WebhookDeliveryTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "MULTI_TENANT_ENABLED": True,
                    "TENANT_WEBHOOK_URL": WEBHOOK_URL,
                    "TENANT_WEBHOOK_SECRET": "hook-secret",
                }
            )
        )

    def tearDown(self):
        self.app.stop()

    def test_delivery_signs_payload(self):
        payload = {"event": "tenant.suspended", "tenant": "tenant-a"}
        with mock.patch("requests.post") as post:
            post.return_value.raise_for_status = mock.Mock()
            tenant_tasks.webhook_notify_task.run(payload)

        post.assert_called_once()
        call = post.call_args
        self.assertEqual(call.args[0], WEBHOOK_URL)
        body = call.kwargs["data"]
        self.assertEqual(json.loads(body), payload)
        expected = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(call.kwargs["headers"]["X-Superdesk-Signature"], expected)

    def test_delivery_without_secret_unsigned(self):
        self.app.wsgi.config["TENANT_WEBHOOK_SECRET"] = ""
        with mock.patch("requests.post") as post:
            post.return_value.raise_for_status = mock.Mock()
            tenant_tasks.webhook_notify_task.run({"event": "tenant.deleted", "tenant": "tenant-a"})
        self.assertNotIn("X-Superdesk-Signature", post.call_args.kwargs["headers"])

    def test_noop_when_unconfigured(self):
        self.app.wsgi.config["TENANT_WEBHOOK_URL"] = ""
        with mock.patch("requests.post") as post:
            tenant_tasks.webhook_notify_task.run({"event": "tenant.deleted", "tenant": "tenant-a"})
        post.assert_not_called()

    async def test_notify_builds_payload_and_queues(self):
        doc = {
            "_id": "tenant-a",
            "status": "deleted",
            "hosts": ["a.example.com"],
            "deleted_at": utcnow(),
        }
        with mock.patch.object(tenant_tasks.webhook_notify_task, "apply_async", mock.AsyncMock()) as apply:
            await webhooks.notify_tenant_event(webhooks.EVENT_DELETED, doc)
        payload = apply.await_args.kwargs["kwargs"]["payload"]
        self.assertEqual(payload["event"], "tenant.deleted")
        self.assertEqual(payload["tenant"], "tenant-a")
        self.assertIn("deleted_at", payload)

    async def test_notify_noop_without_url(self):
        self.app.wsgi.config["TENANT_WEBHOOK_URL"] = ""
        with mock.patch.object(tenant_tasks.webhook_notify_task, "apply_async", mock.AsyncMock()) as apply:
            await webhooks.notify_tenant_event(webhooks.EVENT_DELETED, {"_id": "tenant-a"})
        apply.assert_not_awaited()


class RetentionPurgeTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "MULTI_TENANT_ENABLED": True,
                    "TENANT_DELETED_RETENTION_DAYS": 30,
                    "TENANT_WEBHOOK_URL": WEBHOOK_URL,
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


class WebhookAdminConfigTestCase(IsolatedAsyncioTestCase):
    CONTROL_PLANE_DB = "sptests_controlplane_webhook"

    def setUp(self):
        from quart import Quart
        from superdesk.tenants import admin_api

        self.app = SuperdeskAsyncApp(
            MockWSGI(
                config={
                    "MULTI_TENANT_ENABLED": True,
                    "TENANT_ADMIN_HOST": "admin.example.com",
                    "TENANT_ADMIN_API_TOKEN": "secret-token",
                    "TENANTS_MONGO_DBNAME": self.CONTROL_PLANE_DB,
                    "TENANTS_MONGO_URI": f"mongodb://localhost/{self.CONTROL_PLANE_DB}",
                    "TENANT_WEBHOOK_URL": "https://fallback.example.com/hook",
                    "TENANT_WEBHOOK_SECRET": "fallback-secret",
                }
            )
        )
        webhooks._settings_collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        app = Quart(__name__)
        admin_api.init_app(app)
        self.client = app.test_client()
        self.headers = {"Authorization": "Bearer secret-token", "Host": "admin.example.com"}

    def tearDown(self):
        webhooks._settings_collection().database.client.drop_database(self.CONTROL_PLANE_DB)
        self.app.stop()

    async def test_get_falls_back_to_config_file(self):
        response = await self.client.get("/tenant-admin/webhook", headers=self.headers)
        payload = await response.get_json()
        self.assertEqual(payload, {"url": "https://fallback.example.com/hook", "has_secret": True, "source": "config"})

    async def test_put_overrides_config_and_hides_secret(self):
        response = await self.client.put(
            "/tenant-admin/webhook",
            json={"url": "https://hooks.example.com/x", "secret": "new-secret"},
            headers=self.headers,
        )
        payload = await response.get_json()
        self.assertEqual(payload["url"], "https://hooks.example.com/x")
        self.assertEqual(payload["source"], "control-plane")
        self.assertTrue(payload["has_secret"])
        self.assertNotIn("secret", payload)
        self.assertEqual(webhooks.get_webhook_config()["secret"], "new-secret")

        # omitting the secret keeps it; empty string clears it
        await self.client.put(
            "/tenant-admin/webhook", json={"url": "https://hooks.example.com/y"}, headers=self.headers
        )
        self.assertEqual(webhooks.get_webhook_config()["secret"], "new-secret")
        await self.client.put(
            "/tenant-admin/webhook", json={"url": "https://hooks.example.com/y", "secret": ""}, headers=self.headers
        )
        self.assertEqual(webhooks.get_webhook_config()["secret"], "")

    async def test_put_empty_url_restores_fallback(self):
        await self.client.put(
            "/tenant-admin/webhook", json={"url": "https://hooks.example.com/x"}, headers=self.headers
        )
        response = await self.client.put("/tenant-admin/webhook", json={"url": ""}, headers=self.headers)
        payload = await response.get_json()
        self.assertEqual(payload["source"], "config")
        self.assertEqual(payload["url"], "https://fallback.example.com/hook")

    async def test_put_rejects_bad_url(self):
        response = await self.client.put("/tenant-admin/webhook", json={"url": "ftp://nope"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    async def test_test_delivery(self):
        await self.client.put(
            "/tenant-admin/webhook", json={"url": "https://hooks.example.com/x", "secret": "s"}, headers=self.headers
        )
        with mock.patch("requests.post") as post:
            post.return_value.status_code = 204
            post.return_value.raise_for_status = mock.Mock()
            response = await self.client.post("/tenant-admin/webhook/test", headers=self.headers)
        payload = await response.get_json()
        self.assertEqual(payload["response_status"], 204)
        sent = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(sent["event"], "tenant.test")

    async def test_test_delivery_failure_reported(self):
        await self.client.put(
            "/tenant-admin/webhook", json={"url": "https://hooks.example.com/x"}, headers=self.headers
        )
        with mock.patch("requests.post", side_effect=Exception("connection refused")):
            response = await self.client.post("/tenant-admin/webhook/test", headers=self.headers)
        self.assertEqual(response.status_code, 502)
