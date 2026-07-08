# Superdesk Core Multi-Tenancy Plan

## Context

Superdesk core and its APIs (main REST API, content_api, prod_api) are single-tenant today: one process serves one newsroom, backed by one set of Mongo databases (`superdesk`, `archived`, `legal_archive`, `contentapi`), one Elasticsearch index prefix, one Redis/Celery namespace. Running N newsrooms means N full deployments.

Goal: make a single Superdesk deployment serve many tenants (newsrooms) with strong data isolation, per best practices for SaaS multi-tenancy.

### Decisions

- **Isolation model: DB-per-tenant.** Shared app processes and shared Mongo/Elastic clusters; each tenant gets its own Mongo database(s) and its own Elasticsearch index prefix. GridFS media follows the tenant's Mongo DB; S3 media uses a per-tenant subfolder.
- **Tenant resolution: subdomain/host.** `tenant-a.example.com` → tenant `tenant-a`.
- **Scope:** core REST API + auth, Celery/background tasks, content_api + prod_api, tenant provisioning/admin, **plus** cross-tenant content exchange (send content tenant→tenant) and shared credentials (same user/password works on multiple tenants, while user management/preferences stay tenant-level).

### Key architectural facts (from exploration)

- The codebase is mid-migration from Eve/Flask (sync) to a new async Quart-based framework under `superdesk/core/`. Both request pipelines must be instrumented.
- One app object per process: `superdesk/factory/app.py::get_app()` → `SuperdeskEve` embedding `SuperdeskAsyncApp` (`superdesk/core/app.py`, module-global `_global_app`, flat config via `get_app_config()`).
- Mongo clients cached **by config prefix** in `superdesk/core/mongo/mongo_manager.py::MongoResources`; db name from `{PREFIX}_URI`/`{PREFIX}_DBNAME` via `superdesk/core/mongo/utils.py::get_mongo_client_config`. Legacy path: `superdesk/datalayer.py::SuperdeskDataLayer` (eve `Mongo`/`MongoAsync`).
- Elastic clients cached by prefix in `superdesk/core/elastic/resources.py::ElasticResources`; **index name `{ELASTICSEARCH_INDEX}_{source}` is baked into `ElasticClientConfig.index` at registration time** — must become tenant-dynamic.
- Auth: token sessions in `auth` collection; both stacks funnel through `superdesk/core/auth/utils.py::set_user_request_auth_data`. Async auth pluggable via `ASYNC_AUTH_CLASS`.
- Celery: `superdesk/celery_app/` — app context pushed by `HybridAppContextTask` via `celery_wsgi_instance` ContextVar; new async worker thread (`async_worker.py`) runs coroutines on a shared loop. ~12 beat tasks. Queues namespaced by `SUPERDESK_CELERY_PREFIX`.
- Media: `ProxyMediaStorage` → GridFS (per mongo-prefix DB) + `AmazonMediaStorage` (`AMAZON_S3_SUBFOLDER` path prefix exists already).
- Notifications: single Kombu fanout exchange `socket_notification` → websocket server broadcasts to all clients (no scoping).
- No tenancy hints exist anywhere; `multitenant` branch == `develop`.

## Design — Part 1: Core tenancy runtime

Guiding principles:
- All new tenancy code lives in a new package **`superdesk/core/tenants/`**.
- **Fail-closed everywhere**: with `MULTI_TENANT_ENABLED=True`, any DB/ES/media/task access without a resolved tenant raises `TenantNotSetError`; unknown hosts → 404; tasks without a tenant header fail (unless registered tenant-agnostic).
- **Default-tenant mode**: with `MULTI_TENANT_ENABLED=False` (default), the same accessors return a synthetic default tenant built from existing config (`MONGO_DBNAME`, `ELASTICSEARCH_INDEX`, `AMAZON_S3_SUBFOLDER`) — one code path everywhere, zero config changes for existing deployments.
- **Connections stay shared per cluster/prefix**; only DB handles / index names / GridFS buckets / S3 prefixes become tenant-scoped. No connection-pool explosion.

### 1.1 Tenant registry & context (`superdesk/core/tenants/`)

- `models.py` — `Tenant` model: `id` (DNS-safe slug), `hosts: list[str]`, `status` (active/suspended/deleted), `db_prefix` (default `sd_{id}`), `elastic_prefix`, `s3_subfolder`, `config_overrides` (whitelisted keys only), `feature_flags`.
- `registry.py` — `TenantRegistry`: control-plane storage in a **separate Mongo DB** (`TENANTS_MONGO_URI`/`TENANTS_MONGO_DBNAME`, default `superdesk_tenants`), collection `tenants`, unique indexes on `id` and `hosts`. Owns its own pymongo+motor clients (not via `MongoResources`, which becomes tenant-scoped). In-process TTL cache (`TENANTS_CACHE_TTL`, 60s) with sync + async lookups (`get_by_host`, `get_by_id`, `get_all_active_sync`, `invalidate`). Attached as `SuperdeskAsyncApp.tenants` in `superdesk/core/app.py`.
- `context.py` — `current_tenant: ContextVar[Tenant | None]`; `get_current_tenant()` (default tenant when single-tenant, raises `TenantNotSetError` when multi-tenant and unset), `try_get_current_tenant()`, `tenant_context(tenant)` context manager. Re-exported from `superdesk/core/__init__.py`.
- `naming.py` — pure functions `tenant_db_name(tenant, prefix)` (suffix map: `MONGO`→``, `ARCHIVED`→`_archived`, `LEGAL_ARCHIVE`→`_legal_archive`, `CONTENTAPI_MONGO`→`_contentapi`) and `tenant_index_prefix(tenant, config_prefix)`.
- **Config layering**: overlay in `superdesk/core/app.py::get_app_config()` — if current tenant has the key in `config_overrides` (and the key is in a `TENANT_OVERRIDABLE_CONFIG_KEYS` whitelist), return the override. Covers all `get_app_config()` call sites without rewriting them. Documented limitation: direct `app.config[...]` reads and boot-time-computed values (Eve DOMAIN, blueprints) stay global in v1.

### 1.2 Request middleware & auth binding

- `middleware.py` — `setup_tenant_middleware(app)` registers a `before_request` hook **inside `SuperdeskEve.__init__`** (`superdesk/factory/app.py:245`) right after `super().__init__()`, so tenant resolution runs before Eve auth. Async-framework endpoints dispatch through the same Quart app, so **one hook covers both stacks**. Also called from `content_api/app/__init__.py` and the prod_api factory.
- Hook: strip port from `request.host` → `TENANT_EXEMPT_PATHS` check (health) → registry lookup; unknown host → 404, suspended → 423; set `current_tenant` ContextVar (+`g.tenant`), reset in `teardown_request`.
- **Cookies**: keep `SESSION_COOKIE_DOMAIN = None` (host-only cookies per subdomain); startup warning if a wildcard parent domain is set while multi-tenant.
- **Auth binding**: DB-per-tenant already prevents cross-tenant token reuse (token isn't in the other tenant's `auth` collection), plus belt-and-braces in `superdesk/core/auth/utils.py::set_user_request_auth_data` (the shared chokepoint both auth stacks call): store `tenant_id` in the session, reject if it mismatches the current tenant; clear in `clear_user_request_auth_data`.

### 1.3 Per-tenant Mongo / Elastic / GridFS / S3

- **Mongo** (`superdesk/core/mongo/mongo_manager.py`): split caches — clients stay keyed by prefix (one per cluster URI), **`Database` handles keyed by `(tenant_id, prefix, versioning)`**. `get_client()`/`get_client_async()` resolve db name via `tenant_db_name()`; `get_mongo_client_config(app_config, prefix, tenant=None)` extended in `superdesk/core/mongo/utils.py`. Caveat: it defaults `authSource=dbname` — multi-tenant deployments must set `authSource=admin` in the URI (document it).
- **Legacy eve path** (`superdesk/core/tenants/eve_shims.py`): `TenantMongo(Mongo)` / `TenantMongoAsync(MongoAsync)` overriding `pymongo(resource, prefix)` to return shared client + tenant DB, cached by `(tenant, prefix)`; `TenantElastic(Elastic)` / `TenantElasticAsync` overriding `_resource_index()` to compute the index at call time. Wired in `superdesk/datalayer.py::SuperdeskDataLayer.init_app`.
- **Elastic** (`superdesk/core/elastic/resources.py` + `base_client.py`): fix the baked-in index — stop freezing `client_config.index` at registration; store `source_name` + config prefix on the client and add a **query-time `.index` property** on `BaseElasticResourceClient` computing `f"{tenant_index_prefix(tenant, prefix)}_{source}"`. Sweep every `config.index` usage inside `superdesk/core/elastic/` (contained; audit with grep). Connection caches stay prefix-keyed. Per-tenant lock key for `init_elastic` (`elastic:{tenant.id}`).
- **GridFS**: follows the tenant Mongo DB automatically once the shims are in; only fix the prefix-keyed `self._fs`/`self._fs_async` caches in `superdesk/storage/desk_media_storage.py` to `(tenant_id, prefix)` keys.
- **S3**: `get_s3_subfolder()` helper in `superdesk/storage/amazon_media_storage.py` → `f"{base}/{tenant.s3_subfolder}"`; shared bucket.

### 1.4 Celery / background tenancy (`superdesk/core/tenants/celery.py`)

- **Publish**: `before_task_publish` signal (registered in `init_celery`) injects `headers["tenant_id"]` from the ContextVar — covers `delay`/`apply_async`/canvas/beat. Fail-closed unless the task is in a `TENANT_AGNOSTIC_TASKS` registry.
- **Consume**: prologue in `HybridAppContextTask` (`superdesk/celery_app/context_task.py:74`) reads the header and wraps execution in `tenant_context_or_fail(...)` before the app-context push.
- **Async worker** (`superdesk/celery_app/async_worker.py`): set the tenant **inside the coroutine** (`_run_task_with_soft_timeout`), not at submit time — each task coroutine gets an isolated context copy on the shared loop, so concurrent tasks for different tenants can't leak.
- **Beat fan-out**: dispatcher pattern, no changes to the ~12 task definitions — when multi-tenant, `init_celery` rewrites `CELERY_BEAT_SCHEDULE` entries to a tenant-agnostic `tenants.fan_out` task that iterates active tenants (gated by per-tenant feature flags) and re-publishes the original task with `headers={"tenant_id": ...}`.
- **Queues**: stay shared/namespaced by `SUPERDESK_CELERY_PREFIX`; tenant travels in headers. Noisy-neighbor mitigation (per-tenant queues/rate limits) deferred, documented.

### 1.5 CLI

- Extend `superdesk/core/cli.py::AsyncAppGroup.command(..., tenant_required=True)` to auto-attach `--tenant <id>` (repeatable) and `--all-tenants` options, running the callback once per tenant inside `tenant_context`. Fail-closed in multi-tenant mode when neither is given; defaults to the default tenant in single-tenant mode. Legacy `manage.py` commands route through the same blueprint CLI already.

### 1.6 Websockets / notifications

- **Tag**: `superdesk/notification.py::_create_socket_message` adds `tenant: get_current_tenant().id` — single chokepoint for all `push_notification` callers.
- **Filter**: `superdesk/websockets_comms.py::SocketCommunication` records each client's tenant at connect time (from the connection's Host subdomain label) and drops messages whose `tenant` tag doesn't match. Fail closed: untagged messages delivered only in single-tenant mode; clients without a resolvable tenant get nothing. V1 constraint: `tenant.id` must equal the subdomain label (ws server has no app/registry); registry-backed lookup as follow-up.

## Design — Part 2: Provisioning, cross-tenant exchange, shared credentials

Layering note: the **runtime registry** (`superdesk/core/tenants/registry.py`, TTL-cached, own control-plane Mongo client) is the low-level read path used by middleware/Celery/CLI. The **admin module** `superdesk/tenants/` (new async `Module` in `MODULES`) exposes the same control-plane `tenants` collection as a resource (`MongoResourceConfig(prefix="MONGO_CONTROLPLANE")`, Mongo-only, unique indexes on `slug`/`subdomains`) with service, CLI, and provisioning on top. One collection, two access layers; writes go through the service and call `registry.invalidate()`.

Extended tenant record fields (beyond the runtime model): `provisioning: dict[step, datetime]` markers for idempotent resume, and `exchange.partners: [{tenant, direction: send|receive|both, default_desk, auto_fetch}]`.

### 2.1 Tenant provisioning & lifecycle (`superdesk/tenants/`)

- **Provisioning flow** (`provisioning.py::provision_tenant`, resumable — each completed step recorded in `provisioning`):
  1. Insert tenant doc with `status=creating`.
  2. Inside `tenant_context(tenant)`:
     - Mongo indexes: `SuperdeskEve.init_indexes()` (legacy Eve resources) + async-resource index creation via `MongoResources`.
     - Elastic: `elastic.init_all_indexes()` + legacy `init_elastic` (with the tenant-scoped lock from Part 1).
     - Seed data: reuse `apps/prepopulate/app_initialize.py::app_initialize_data_handler()` — seeds roles, vocabularies, validators, content_types, desks/stages from `data_init/*.json`. Exactly the per-tenant seeding routine needed; ordering: indexes before seed.
     - Admin user: reuse the logic behind `apps/auth/db/commands.py::create_user_command` (`users:create`).
  3. `status=active`.
- **Lifecycle**: `disable` → middleware returns 403/423, beat fan-out and ingest/publish polling skip non-active tenants. `delete` only from `disabled`, with `--yes`, default export-first (reuse `storage:dump` from `superdesk/commands/data_manipulation.py`), then `--purge` drops tenant Mongo DBs (incl. `_versions`), `elastic.drop_indexes(prefix)`, GridFS DB / S3 prefix.
- **CLI first** (server access = super-admin): `tenants:create SLUG --subdomain ... [--admin-* ...] [--set K=V] [--resume]`, `tenants:list/enable/disable/update/delete`. Registered `tenant_required=False`. **HTTP admin API later** (phase 2): REST on a reserved non-tenant host (`TENANT_ADMIN_HOST`), guarded by static bearer token initially, super-admin accounts later.

### 2.2 Cross-tenant content exchange (`superdesk/tenants/exchange/`)

**Chosen approach: reuse the publish→ingest pipeline with an in-process "internal tenant" transmitter** (over direct service-level copy). Rationale: reuses subscriber targeting, products/content filters, `publish_queue` retry+audit, ninjs formatter, and ingest-side dedupe/provenance/desk-routing; keeps a strict tenant boundary (sender never writes to the target DB directly); and if a tenant later moves to another deployment, the same subscriber just flips its destination to the existing `http_push` transmitter. Direct copy would bypass validation/filters/audit and require holding two tenant contexts in one request — fragile with ContextVar-scoped caches.

- **Transmitter** (`transmitter.py`): `InternalTenantTransmitter(PublishService)`, `NAME="internal_tenant"`, registered via `register_transmitter` (pattern: `superdesk/publish/transmitters/http_push.py`). Destination config `{tenant, desk?, stage?, auto_fetch}`. `_transmit`: validate **both** tenants' `exchange.partners` allowlists (both active) → copy media → enqueue `deliver_to_tenant` Celery task with `headers={"tenant_id": target}` (runtime propagation does the context switch).
- **Receiver** (`receiver.py`): Celery task runs in target tenant context; parses ninjs payload with `superdesk/io/feed_parsers/ninjs.py::NINJSFeedParser`; upserts into `ingest` via the same path `update_ingest` uses (dedupe by `guid`); auto-creates a per-partner ingest provider ("Tenant exchange: {source}") so routing schemes (`apps/rules/`) apply; optional auto-fetch to the configured desk.
- **Media copy** (`media.py::copy_item_media`): read bytes via `ProxyMediaStorage` in source context, `put()` in target context (mirrors `superdesk/media/renditions.py::transfer_renditions`); S3 setups can use server-side copy between prefixes. Rendition media ids rewritten in the payload.
- **Provenance**: `guid` unchanged; `extra.original_tenant` + `extra.original_item_id`; `source`/`original_source` from sender tenant; `family_id` preserved so re-sends update instead of duplicating.
- **Triggers**: (a) automatic — a subscriber in the sender tenant with `delivery_type="internal_tenant"`; products/content filters decide what flows, zero new routing code; (b) manual — `POST /archive/send_to_tenant {item_id, target_tenant, desk?, stage?}` guarded by a new `send_to_tenant` privilege, modeled on `superdesk/internal_destinations.py`, snapshots the item with the ninjs formatter into the same transmit path.

### 2.3 Shared credentials, per-tenant users (`superdesk/accounts/`)

- **Data model**: new control-plane resource `accounts` (`MONGO_CONTROLPLANE`): `email` (unique, lowercased), `username` (unique), `password` bcrypt hash, `password_changed_on`, `sso_identities`, `is_enabled`, `is_super_admin`, `needs_password_reset`. Tenant `users` doc gains optional `account_id`. Profile/role/privileges/`user_preferences`/`session_preferences` stay tenant-local — preferences code needs **no** change.
- **Login flow** (chokepoint: `apps/auth/db/db.py::DbAuthService.authenticate`): account-first — find control-plane account by username/email, bcrypt+expiry check against the account, then resolve the tenant-local user by `account_id` (one-time fallback by email with lazy linkage); no tenant-local user → auth error. **Phase 1 fallback**: no account → current per-tenant `auth_users` path unchanged (accounts strictly additive). Sessions stay per-tenant (auth docs in tenant DB, cookie on the subdomain).
- **Write paths**: password set/change/reset (`superdesk/users/services.py`, `apps/auth/db/change_password.py`, `reset_password.py`) — phase 1 dual-writes tenant doc + account; phase 2 account-authoritative (drop tenant `password`), reset tokens move to a control-plane collection (reset link carries the requesting subdomain). `users:create` gains create-or-link; new `accounts:create`/`accounts:set-password`/`accounts:migrate` CLI. SSO (email matching in `superdesk/auth/__init__.py::auth_user`) keeps working as-is.
- **Migration** (`accounts:migrate`): upsert account per user email, link `account_id`; email conflicts across tenants → newest `password_changed_on` wins + `needs_password_reset` + report. Idempotent.
- **Tenant switcher** (phase 3, optional): control-plane `account_tenants {account_id, tenant, user_id}` mapping maintained on tenant-user create/delete; `GET /accounts/me/tenants`; client redirects to the other subdomain and logs in there (sessions never span tenants).

## Milestones (PR-sized, ordered)

| # | Milestone | Content |
|---|-----------|---------|
| 1 | Tenants package | `superdesk/core/tenants/` (models, registry + control-plane DB, context, naming), default-tenant mode, `get_app_config` overlay, `SuperdeskAsyncApp.tenants`. No behavior change. |
| 2 | Request middleware + auth | Host resolution in `SuperdeskEve.__init__`, content_api/prod_api wiring, session tenant check in `set_user_request_auth_data`, cookie-domain assertion. |
| 3 | Mongo | Cache split in `mongo_manager.py`, `get_mongo_client_config(tenant=...)`, eve `TenantMongo`/`TenantMongoAsync` shims, GridFS cache keys. |
| 4 | Elastic | Dynamic `.index` property, `config.index` sweep, `TenantElastic` shims, tenant-scoped init lock. Two-tenant leak test (write in A, assert absent in B), both stacks. |
| 5 | Celery | `before_task_publish` header injection, task prologue restore, async-worker in-coroutine tenant set, beat fan-out dispatcher, `TENANT_AGNOSTIC_TASKS` registry. |
| 6 | CLI + provisioning | `--tenant`/`--all-tenants` on `AsyncAppGroup`, `superdesk/tenants/` module + resource + `tenants:*` commands, resumable `provision_tenant`, lifecycle (disable/delete/export). |
| 7 | Websockets/notifications | Tenant tag in `_create_socket_message`, per-client tenant filter in `SocketCommunication`. |
| 8 | Hardening sweep | S3 subfolder, `superdesk/cache.py` key prefixing, `superdesk/lock.py` audit, lru_cache/module-dict audit (vocabularies, sequences…), fail-closed audit, cross-tenant CI test suite. |
| 9 | Accounts phase 1 | `superdesk/accounts/` resource, `account_id` link, dual-write, account-first login with fallback, `accounts:migrate`. Backwards compatible. |
| 10 | Exchange | `internal_tenant` transmitter, `deliver_to_tenant` receiver, media copy, partner allowlists, `send_to_tenant` privilege + endpoint; then subscriber surface (client config, auto-created ingest providers, auto-fetch) + behave round-trip test incl. renditions. |
| 11 | Accounts phase 2 + admin API | Account-authoritative passwords, control-plane reset tokens, tenant-switcher endpoint, HTTP tenant admin API (`TENANT_ADMIN_HOST`, super-admin gated). |

Milestones 1–8 make the platform multi-tenant; 9–11 add the cross-tenant features. Each is independently mergeable behind `MULTI_TENANT_ENABLED=False`.

## Top risks

1. **Global in-process caches of DB-derived data** outside the datastore layer (lru_cache/module dicts: vocabularies, `superdesk/cache.py` keys, `PublishCache` in `superdesk/publish_async/publish_cache.py`) — each is a silent cross-tenant leak. Milestone 8 does a systematic grep audit; this is the long tail.
2. **`config.index` refactor** in the elastic package — one missed call site writes tenant A's docs into tenant B's index. Mitigation: grep sweep + mandatory two-tenant leak tests.
3. **Eve internals shims** (`TenantMongo.pymongo`, `TenantElastic._resource_index`) — eve/eve_elastic touch driver attributes in several places (versioning, media, aggregation); verify exactly what the returned driver must expose.
4. **ContextVar propagation gaps**: `run_in_executor`/thread pools don't copy context — audit executor usage touching datastores; async worker must set tenant inside the coroutine.
5. **Mongo `authSource`**: `get_mongo_client_config` defaults `authSource=dbname`; per-tenant DBs with one shared Mongo user require `authSource=admin` in the URI — document as a hard requirement.
6. **Dual-stack provisioning**: legacy Eve (`init_indexes`/`init_elastic`) and new pydantic resources (`init_all_indexes`) must both run per tenant; resumable step markers mitigate half-provisioned tenants.
7. **Global email uniqueness** (accounts) changes semantics vs per-tenant today; migration conflict policy (newest hash + forced reset) must be communicated; SSO users excluded from password migration.

## Verification

- **Unit**: ContextVar isolation across concurrent asyncio tasks; `tenant_db_name`/`tenant_index_prefix` naming; config overlay whitelist; registry TTL cache + invalidation.
- **Cross-tenant leak tests** (the critical suite, added in milestone 4 and grown thereafter): boot the app with two tenants against the docker-compose Mongo/Elastic/Redis; write an article/user/media in tenant A via API with `Host: a.test`; assert search/read on `Host: b.test` returns nothing; assert tenant A's session token gets 401 on tenant B; assert a Celery task published from tenant A context reads/writes only A's DBs; assert websocket client on `b.test` receives no A notifications.
- **Single-tenant regression**: full existing pytest + behave suites must pass unchanged with `MULTI_TENANT_ENABLED=False` — this is the gate for every milestone PR.
- **Provisioning e2e**: `tenants:create` on a clean cluster → login as seeded admin on the subdomain → create/publish an item → `tenants:disable` → requests rejected → `tenants:delete --purge` → DBs/indexes gone.
- **Exchange e2e** (behave): partner tenants A→B, publish item with picture in A via `internal_tenant` subscriber → item appears in B's ingest with renditions loading from B's storage and provenance fields set; re-publish updates rather than duplicates.
