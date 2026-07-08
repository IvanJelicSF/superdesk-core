from unittest import TestCase, mock

from superdesk.core.app import SuperdeskAsyncApp
from superdesk.core.tenants import Tenant, TenantNotSetError, tenant_context
from superdesk.core.tenants.celery import (
    TENANT_TASK_HEADER,
    FAN_OUT_TASK_NAME,
    _inject_tenant_header,
    task_tenant_context,
    rewrite_beat_schedule_for_tenants,
    register_tenant_agnostic_task,
    TENANT_AGNOSTIC_TASKS,
)
from superdesk.core.tenants.registry import TenantRegistry
from superdesk.tests import MockWSGI


TENANT_A = Tenant(id="tenant-a", hosts=("tenant-a.example.com",))


def make_task(name="test.task", headers=None, **request_attrs):
    task = mock.MagicMock()
    task.name = name
    request = mock.MagicMock(spec=[])  # no attributes unless set
    request.headers = headers or {}
    for key, val in request_attrs.items():
        setattr(request, key, val)
    task.request = request
    return task


class InjectTenantHeaderTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))

    def tearDown(self):
        self.app.stop()

    def test_injects_tenant_from_context(self):
        headers: dict = {}
        with tenant_context(TENANT_A):
            _inject_tenant_header(sender="test.task", headers=headers)
        self.assertEqual(headers[TENANT_TASK_HEADER], "tenant-a")
        self.assertEqual(headers["headers"][TENANT_TASK_HEADER], "tenant-a")

    def test_existing_header_is_kept(self):
        headers = {TENANT_TASK_HEADER: "tenant-b"}
        with tenant_context(TENANT_A):
            _inject_tenant_header(sender="test.task", headers=headers)
        self.assertEqual(headers[TENANT_TASK_HEADER], "tenant-b")

    def test_fails_closed_without_tenant(self):
        with self.assertRaises(TenantNotSetError):
            _inject_tenant_header(sender="test.task", headers={})

    def test_agnostic_task_allowed_without_tenant(self):
        headers: dict = {}
        _inject_tenant_header(sender=FAN_OUT_TASK_NAME, headers=headers)
        self.assertNotIn(TENANT_TASK_HEADER, headers)

    def test_noop_when_single_tenant(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        headers: dict = {}
        _inject_tenant_header(sender="test.task", headers=headers)
        self.assertEqual(headers, {})


class TaskTenantContextTestCase(TestCase):
    def setUp(self):
        self.app = SuperdeskAsyncApp(MockWSGI(config={"MULTI_TENANT_ENABLED": True}))

    def tearDown(self):
        self.app.stop()

    def test_binds_tenant_from_header(self):
        task = make_task(headers={TENANT_TASK_HEADER: "tenant-a"})
        with mock.patch.object(TenantRegistry, "get_by_id_sync", return_value=TENANT_A):
            with task_tenant_context(task) as tenant:
                self.assertEqual(tenant, TENANT_A)

    def test_binds_tenant_from_request_attribute(self):
        task = make_task(**{TENANT_TASK_HEADER: "tenant-a"})
        with mock.patch.object(TenantRegistry, "get_by_id_sync", return_value=TENANT_A):
            with task_tenant_context(task) as tenant:
                self.assertEqual(tenant, TENANT_A)

    def test_fails_closed_without_header(self):
        task = make_task()
        with self.assertRaises(TenantNotSetError):
            with task_tenant_context(task):
                pass

    def test_ambient_tenant_fallback_for_eager_mode(self):
        task = make_task()
        with tenant_context(TENANT_A):
            with task_tenant_context(task) as tenant:
                self.assertEqual(tenant, TENANT_A)

    def test_agnostic_task_runs_without_tenant(self):
        register_tenant_agnostic_task("gc.controlplane")
        try:
            task = make_task(name="gc.controlplane")
            with task_tenant_context(task) as tenant:
                self.assertIsNone(tenant)
        finally:
            TENANT_AGNOSTIC_TASKS.discard("gc.controlplane")

    def test_noop_when_single_tenant(self):
        self.app.wsgi.config["MULTI_TENANT_ENABLED"] = False
        task = make_task()
        with task_tenant_context(task) as tenant:
            self.assertIsNone(tenant)


class BeatScheduleRewriteTestCase(TestCase):
    def test_rewrite(self):
        schedule = {
            "ingest:update": {"task": "superdesk.io.update_ingest", "schedule": 30, "options": {"queue": "ingest"}},
            "publish:transmit": {"task": "superdesk.publish.transmit", "schedule": 10, "args": ("x",)},
        }
        rewritten = rewrite_beat_schedule_for_tenants(schedule)
        self.assertEqual(rewritten["ingest:update"]["task"], FAN_OUT_TASK_NAME)
        self.assertEqual(rewritten["ingest:update"]["args"], ("superdesk.io.update_ingest",))
        self.assertEqual(rewritten["ingest:update"]["options"], {"queue": "ingest"})
        self.assertEqual(rewritten["publish:transmit"]["args"], ("superdesk.publish.transmit", "x"))
        # original untouched
        self.assertEqual(schedule["ingest:update"]["task"], "superdesk.io.update_ingest")

    def test_agnostic_tasks_not_rewritten(self):
        schedule = {"fanout": {"task": FAN_OUT_TASK_NAME, "schedule": 30}}
        rewritten = rewrite_beat_schedule_for_tenants(schedule)
        self.assertEqual(rewritten["fanout"]["task"], FAN_OUT_TASK_NAME)
        self.assertNotIn("args", rewritten["fanout"])
