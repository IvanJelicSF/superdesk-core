# Multi-Tenancy Implementation Log

Implementation log for the plan in [`MULTITENANCY_PLAN.md`](MULTITENANCY_PLAN.md), tracking what
was actually built per milestone, the design decisions made along the way, and what remains.

Everything is gated behind the `MULTI_TENANT_ENABLED` setting (default `false`). With it off, a
synthetic **default tenant** backed by the existing `MONGO_*`/`ELASTICSEARCH_*` config is used by
every code path, so single-tenant deployments behave exactly as before with zero config changes.

## Status

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Tenants package (registry, context, naming, config overlay) | ✅ done |
| 2 | Request middleware + auth tenant binding | ✅ done |
| 3 | Per-tenant Mongo (manager cache split, eve shims, GridFS) | ✅ done |
| 4 | Per-tenant Elastic (dynamic index, shims) | ✅ done |
| 5 | Celery tenancy (headers, prologue, beat fan-out) | ✅ done |
| 6 | CLI tenant options + provisioning | ✅ done |
| 7 | Websocket/notification tenant scoping | ✅ done |
| 8 | Hardening (cache keys, locks, S3 subfolder, audit) | ✅ done |
| 9 | Accounts phase 1 (shared credentials, additive) | ⏳ in progress |
| 10 | Cross-tenant content exchange | ⬜ pending |
| 11 | Accounts phase 2 + tenant admin API | ⬜ pending |

## New settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `MULTI_TENANT_ENABLED` | `false` | Master switch; everything below is inert without it |
| `TENANTS_MONGO_URI` / `TENANTS_MONGO_DBNAME` | `mongodb://localhost/superdesk_tenants` | Control-plane db holding the `tenants` registry (never a tenant db) |
| `TENANTS_CACHE_TTL` | `60` | Seconds registry lookups are cached in process |
| `TENANT_EXEMPT_PATHS` | `[]` | Path prefixes served without tenant resolution (health checks) |

Operational requirement: with mongo auth and per-tenant databases, the mongo URI **must** carry an
explicit `authSource` (e.g. `authSource=admin`) — the legacy default of `authSource=<dbname>` does
not work when the db name varies per tenant.

## M1 — Tenants package (`superdesk/core/tenants/`)

- `models.py` — immutable (frozen dataclass) `Tenant`: `id` (DNS-label slug), `hosts`, `status`
  (`creating|active|suspended|deleted`), `db_prefix`/`elastic_prefix` (default `sd_{id}` with `-`→`_`),
  `s3_subfolder`, `config_overrides`, `feature_flags`, `is_default`.
- `registry.py` — `TenantRegistry`, attached as `SuperdeskAsyncApp.tenants`. Owns its own
  pymongo/motor clients to the control-plane db (deliberately *not* via `MongoResources`, which is
  tenant-routed). TTL cache with sync+async `get_by_id`/`get_by_host`, `get_all_active_sync`,
  `invalidate`; unknown hosts are negative-cached.
- `context.py` — `current_tenant` ContextVar; `get_current_tenant()` is **fail-closed** (raises
  `TenantNotSetError` in multi-tenant mode when unset; returns the default tenant otherwise);
  `tenant_context(tenant_or_id)` context manager; config-override hook.
- `naming.py` — pure naming rules: mongo db `{db_prefix}{suffix}` with suffix map
  (`MONGO`→``, `ARCHIVED`→`_archived`, `LEGAL_ARCHIVE`→`_legal_archive`,
  `CONTENTAPI_MONGO`→`_contentapi`; unknown project prefixes get `_{prefix.lower()}`), and the
  equivalent `tenant_index_prefix` for elastic.
- **Config overlay**: `get_app_config()` (`superdesk/core/app.py`) consults
  `Tenant.config_overrides`, restricted to an explicit `TENANT_OVERRIDABLE_CONFIG_KEYS` whitelist
  (empty by default; extend via `register_tenant_overridable_config`). Boot-time-computed config
  (Eve DOMAIN, blueprints) stays global — documented v1 limitation.

## M2 — Request middleware + auth binding

- `superdesk/core/tenants/middleware.py::setup_tenant_middleware` is registered inside
  `SuperdeskEve.__init__` right after Quart init — before any other request hook. All three app
  factories (main, `content_api`, `prod_api`) build `SuperdeskEve`, so one registration point
  covers them.
- Per request (multi-tenant only): Host header → registry lookup. Unknown/deleted → 404,
  suspended/creating → 423; otherwise the tenant is bound to the ContextVar (+`g.tenant`) and reset
  on teardown. `TENANT_EXEMPT_PATHS` prefixes skip resolution.
- Cookies stay host-only; a startup warning fires if `SESSION_COOKIE_DOMAIN` is set while
  multi-tenant.
- **Auth binding** in `superdesk/core/auth/utils.py::set_user_request_auth_data` (the chokepoint
  both the Eve and async auth stacks call): the session records its `tenant_id`; a mismatch with
  the request's tenant → 401. This is belt-and-braces on top of db-per-tenant isolation (a token
  from tenant A simply doesn't exist in tenant B's `auth` collection).

## M3 — Per-tenant Mongo

- `superdesk/core/mongo/mongo_manager.py::MongoResources`: split caches — **clients stay cached per
  config prefix** (one connection pool per cluster URI, shared by all tenants), **`Database` handles
  cached per `(tenant_id, prefix, versioning)`**. This also fixed a latent bug where the cached
  `(client, db)` tuple meant the first caller's `versioning` flag decided which db later callers got.
- `superdesk/core/mongo/utils.py::get_mongo_client_config(config, prefix, tenant=None)` — tenant
  overrides the db name (at the end, so `authSource` derivation is unchanged).
- **Legacy eve path** — `superdesk/core/tenants/eve_shims.py`: eve's `PyMongo` and superdesk's
  `FlaskMongoAsync` expose `.db` as a property read on every operation; `TenantPyMongo` /
  `TenantFlaskMongoAsync` resolve the tenant db at property-access time off the shared client
  (cached in `app.extensions` under `{prefix}::{tenant}`). `TenantMongo`/`TenantMongoAsync`
  instantiate them from `pymongo()`; wired in `SuperdeskDataLayer.init_app`. Everything eve does
  (versioning, aggregation, GridFS) goes through these properties.
- **GridFS** follows the tenant db automatically; the `_fs`/`_fs_async` instance caches in
  `superdesk/storage/desk_media_storage.py` and `superdesk/storage/__init__.py` are keyed by
  `(tenant_id, prefix)`.

## M4 — Per-tenant Elastic

- The core problem: `ElasticResources.register_resource_config` baked
  `{ELASTICSEARCH_INDEX}_{source}` into the frozen client config at registration. Now
  `BaseElasticResourceClient` has a **query-time `index` property**: default tenant → the
  registration-time name (unchanged behavior); real tenant → `{tenant_index_prefix}_{source}`.
- Swept every `config.index` read in `superdesk/core/elastic/` (`base_client`, `sync_client`,
  `async_client`, `resources`, `reindex`) to the property. Verified with
  `grep -rn "config.index" superdesk/core/elastic/` — the only remaining read is the default-tenant
  branch inside the property itself.
- Legacy `eve_elastic`: `TenantElastic`/`TenantElasticAsync` override `_resource_index()` (which
  eve_elastic computes per call from config) to substitute the tenant prefix; wired in
  `SuperdeskDataLayer.init_app`.
- Elastic connections stay cached per config prefix (shared cluster clients).
- `SuperdeskDataLayer.init_elastic` lock name is tenant-scoped (`elastic:{tenant}`) so one tenant's
  index init doesn't block or skip another's; plain `elastic` kept for the default tenant.

## M5 — Celery tenancy

- `superdesk/core/tenants/celery.py`:
  - **Publish**: a `before_task_publish` signal handler injects `headers["tenant_id"]` from the
    ContextVar (both at the top level and the nested `headers` dict, covering the kombu protocol
    quirk). Fail-closed: publishing without a tenant raises unless the task is registered in
    `TENANT_AGNOSTIC_TASKS` (via `register_tenant_agnostic_task`).
  - **Consume**: `task_tenant_context(task)` restores the tenant from the message before the task
    body. Falls back to the ambient ContextVar tenant (eager mode / direct calls); fail-closed
    otherwise.
  - **Beat fan-out**: `rewrite_beat_schedule_for_tenants` rewrites `CELERY_BEAT_SCHEDULE` entries to
    the tenant-agnostic `tenants.fan_out` dispatcher (`superdesk/celery_app/tenant_tasks.py`), which
    re-publishes the original task once per active tenant with the tenant header. The ~12 task
    definitions are untouched; per-tenant scheduling can be disabled with a `beat:{task_name}`
    feature flag on the tenant.
- Wiring in `superdesk/celery_app/`:
  - `context_task.py` — prologue wraps the task run in `task_tenant_context` before the app-context
    push (beat + classic worker paths).
  - `async_worker.py` — the async worker calls `task.run()` directly, bypassing the prologue, so the
    tenant is restored *inside the task coroutine* (`_run_task_with_soft_timeout`); each coroutine
    has an isolated context copy on the shared loop, so concurrent tasks for different tenants can't
    leak into one another.
  - `init_celery` connects the publish signal and rewrites the beat schedule when multi-tenant.
- Queues stay shared (namespaced by `SUPERDESK_CELERY_PREFIX`); the tenant travels in headers only.
  Noisy-neighbor mitigation (per-tenant queues/rate limits) is a documented non-goal for v1.

## M6 — CLI + provisioning

- `superdesk/core/cli.py::AsyncAppGroup.command` now wraps commands with
  `with_tenant_options` (opt out with `tenant_command=False`): adds `--tenant <id>` (repeatable) and
  `--all-tenants`, runs the callback once per selected tenant inside `tenant_context`. Multi-tenant
  mode with neither option → usage error (fail closed); single-tenant mode unchanged.
- New package `superdesk/tenants/` (admin/write layer on the same control-plane collection the
  runtime registry reads):
  - `service.py` — `create_tenant` / `update_tenant` / `set_tenant_status` /
    `mark_provisioning_step` / `delete_tenant_record` / `list_tenant_docs`, all invalidating the
    registry cache; uniqueness enforced by unique indexes on `_id` and `hosts`.
  - `provisioning.py` — `provision_tenant(tenant, admin=None, resume=False)`: creates the record
    with `status=creating`, then inside `tenant_context` reuses
    `apps/prepopulate/app_initialize.py::app_initialize_data_handler()` (mongo indexes for both
    stacks + elastic mappings + seed data) and
    `apps/auth/db/commands.py::create_user_command_handler` for the initial admin; each completed
    step is recorded in the tenant doc's `provisioning` map so re-runs with `resume` skip finished
    steps; finally `status=active`. `purge_tenant_storage` drops all tenant mongo dbs
    (incl. `_versions`, which also removes GridFS media) and the tenant's elastic indexes.
  - `commands.py` — `tenants:create SLUG --host ... [--admin-*] [--resume]`, `tenants:list`,
    `tenants:enable|disable SLUG`, `tenants:delete SLUG [--purge] [--yes]` (delete requires the
    tenant to be disabled first). Registered via `superdesk/commands/__init__.py`.

## M7 — Websockets / notifications

- **Tagging**: `superdesk/notification.py::_create_socket_message` adds `tenant` to every message
  when multi-tenant (single chokepoint for all `push_notification` callers, including
  Celery-originated ones — the task prologue restored the tenant). Messages created without a
  tenant in context are logged and left untagged.
- **Filtering**: `SocketCommunication` (ws server, separate process) gets `multi_tenant=True` via
  config/env; it records each client's tenant at connect time as the **first DNS label of the Host
  header** (v1 constraint: `tenant.id` must equal the subdomain label) and delivers a message only
  to clients whose tenant matches its tag. Fail closed: untagged messages are dropped, clients
  without a resolvable tenant receive nothing. Single-tenant mode is byte-for-byte unchanged.

## M8 — Hardening

- **Cache** (`superdesk/cache.py`): all hermes `@cache` keys are transparently prefixed with
  `tenant:{id}:` at the `SuperdeskCacheBackend` proxy (lock/save/load/remove), with multi-key loads
  mapped back to the caller's keys. This covers vocabularies (`tags=("vocabularies",)`),
  `CacheableService`, and every other `@cache` user. Audit found no `functools.lru_cache` on
  db-derived data anywhere in `superdesk/`, `apps/`, `content_api/`, `prod_api/`;
  `PublishCache` is Quart-`g`-scoped (per app context) and therefore tenant-safe.
- **Locks** (`superdesk/lock.py`): the mongolock `_lock` collection now lives in the tenant's
  database (`get_mongo_client_config(..., tenant=get_current_tenant())`), so item/task locks are
  isolated per tenant.
- **S3** (`superdesk/storage/amazon_media_storage.py`): new `get_subfolder()` composes the
  configured `AMAZON_S3_SUBFOLDER` with the tenant's `s3_subfolder` (default: tenant id) —
  `{base}/{tenant}/{key}` in the shared bucket; used by `get_key` and both `list_objects` paths.

## Tests

`tests/core/tenants_*_test.py` — 79 unit tests, no live services needed:

- `tenants_test.py` — model validation, naming rules, ContextVar isolation across concurrent asyncio
  tasks, fail-closed accessors, config-overlay whitelist, registry TTL/negative caching/invalidation.
- `tenants_middleware_test.py` — host resolution, port stripping, unknown/suspended/deleted
  handling, exempt paths, context reset after request.
- `tenants_mongo_test.py` — tenant db-name resolution, shared-client/per-tenant-db caching,
  versioned dbs, eve shim `_tenant_db`.
- `tenants_elastic_test.py` — query-time index resolution incl. contentapi prefix, query-arg
  building, eve_elastic shim.
- `tenants_celery_test.py` — header injection (incl. fail-closed and agnostic tasks), task context
  restore (header, request-attribute, ambient fallback), beat schedule rewrite.
- `tenants_cli_test.py` — `--tenant`/`--all-tenants` behavior, provisioning flow (fresh, resume,
  duplicate detection) with mocked handlers.
- `tenants_websockets_test.py` — client tenant from Host, tenant-matched delivery, untagged-message
  drop, single-tenant passthrough; notification tagging.
- `tenants_hardening_test.py` — cache key scoping (save/load/remove, multi-key mapping), S3
  subfolder composition.

### Verification gate (per milestone-PR, per the plan)

- Single-tenant regression: full pytest + behave suites with `MULTI_TENANT_ENABLED=False` against
  live Mongo/Elastic/Redis.
- Two-tenant leak tests against live services (write in A, assert invisible in B — API, search,
  media, celery, websocket) — to be added on top of the platform.

## Known v1 limitations / follow-ups

- Config overrides only apply to runtime `get_app_config()` reads; boot-time config is global.
- The websocket server matches tenants by subdomain label only (no registry lookup in that process).
- Queues are shared: a noisy tenant can delay others (per-tenant queues/rate limits are a follow-up).
- `tenants:delete --purge` does not remove S3 media (bucket prefix cleanup is manual for now).
- Custom project mongo prefixes get a derived `_{prefix.lower()}` db-name suffix — projects should
  verify the resulting names.
- Data migration of an existing single-tenant deployment into a named tenant = create a tenant
  record whose `db_prefix`/`elastic_prefix` point at the existing db/index names (no data copy).
