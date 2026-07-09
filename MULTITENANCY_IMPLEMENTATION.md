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
| 9 | Accounts phase 1 (shared credentials, additive) | ✅ done |
| 10 | Cross-tenant content exchange | ✅ done |
| 11 | Accounts phase 2 + tenant admin API | ✅ done |

## New settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `MULTI_TENANT_ENABLED` | `false` | Master switch; everything below is inert without it |
| `TENANTS_MONGO_URI` / `TENANTS_MONGO_DBNAME` | `mongodb://localhost/superdesk_tenants` | Control-plane db holding the `tenants` registry (never a tenant db) |
| `TENANTS_CACHE_TTL` | `60` | Seconds registry lookups are cached in process |
| `TENANT_EXEMPT_PATHS` | `[]` | Path prefixes served without tenant resolution (health checks) |
| `SHARED_ACCOUNTS_ENABLED` | `false` | Shared credentials across tenants via control-plane accounts (M9) |
| `SHARED_ACCOUNTS_AUTHORITATIVE` | `false` | Phase 2: passwords only on the account, no tenant-local fallback (M11) |
| `TENANT_ADMIN_HOST` | `""` | Reserved non-tenant host serving the tenant admin api (M11) |
| `TENANT_ADMIN_API_TOKEN` | `""` | Bearer token guarding the tenant admin api; empty = api disabled (M11) |
| `TENANT_WEBHOOK_URL` | `""` | HTTPS endpoint for tenant lifecycle webhooks; empty = disabled |
| `TENANT_WEBHOOK_SECRET` | `""` | HMAC-SHA256 signing secret for webhook payloads |
| `TENANT_DELETED_RETENTION_DAYS` | `30` | Days deleted tenants keep their data before the periodic purge |

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

## M9 — Shared credentials, per-tenant users (`superdesk/accounts/`)

Strictly additive, gated by `SHARED_ACCOUNTS_ENABLED` (default off). An **account** holds the
credentials (email unique across all tenants, optional unique username, bcrypt hash) in the
control-plane db; each tenant keeps its own `users` doc (profile, role, privileges, preferences)
linked via a new optional `account_id` field.

- `superdesk/accounts/service.py` — accounts store on the control-plane db (via new
  `TenantRegistry.get_control_plane_collection[_async]` accessors): `find_account` (by username or
  lowercased email), `verify_account_password`, `account_password_expired`,
  `upsert_account_credentials` (username unique-index conflict → account linked by email only),
  and the dual-write hook `link_user_credentials(user_doc)`.
- **Login** (`apps/auth/db/db.py::DbAuthService.authenticate`): when enabled, account-first —
  credentials are checked against the account (enabled flag, bcrypt, expiry/needs-reset), then the
  tenant-local user is resolved by `account_id` with a one-time email fallback that lazily links
  (self-healing). No tenant-local user → auth error (an account alone grants nothing on a tenant).
  No account for the username → the existing per-tenant `auth_users` path runs unchanged.
- **Dual-write**: `DBUsersService.on_create_async` (user created with password) and
  `DBUsersService.update_password` (used by both change-password and reset-password flows) upsert
  the account credentials and set `account_id`. Sessions stay per-tenant; preferences untouched.
- **CLI**: `accounts:create`, `accounts:set-password` (control-plane, `tenant_command=False`) and
  `accounts:migrate` (a tenant command — combine with `--tenant`/`--all-tenants`): for each tenant
  user with a hashed password, create/link the account; on cross-tenant password conflicts the
  newest `password_changed_on` wins and the account is flagged `needs_password_reset`; SSO
  (`user_type=external`) and non-hashed passwords are skipped. Idempotent.
- Tests: `tests/core/tenants_accounts_test.py` (account CRUD against real control-plane mongo,
  username conflicts, dual-write hook, expiry; account-first authentication incl. fail-closed
  no-local-user, lazy linking, needs-reset).

## M10 — Cross-tenant content exchange (`superdesk/tenants/exchange/`)

Content flows tenant→tenant through the regular **publish→ingest pipeline**: a subscriber in the
sender tenant with a destination of type `internal_tenant` (ninjs formatter) decides *what* flows
(products/content filters as usual, publish_queue retry+audit included); the ingest side gives
dedupe by guid, provenance and desk routing. If a tenant later moves to another deployment the
same subscriber just switches to the `http_push` transmitter.

- **Permissions**: both tenants must allow each other on their `exchange_partners` allowlist
  (`{"tenant": id, "direction": "send"|"receive"|"both"}`) — checked on the sending side *and*
  re-checked in the receiver. Managed via `tenants:update SLUG --add-partner X --direction both`
  / `--remove-partner X`.
- **Transmitter** (`transmitter.py`): `InternalTenantTransmitter`, registered as
  `internal_tenant`. Destination config `{"tenant": <target>, "auto_fetch": bool, "desk", "stage"}`.
  Validates partners, stamps provenance (`extra.original_tenant`, `extra.original_item_id`,
  `source`; guid unchanged so re-sends update rather than duplicate), copies media, then enqueues
  the delivery task with the target tenant's Celery header (the sender never writes to the target
  db directly — the tenant boundary is crossed only via the media-storage API and the task queue).
- **Per-tenant media copy switch**: `Tenant.exchange_copy_media` (default on; CLI
  `tenants:update --copy-media/--no-copy-media`, admin API `PATCH {exchange_copy_media}`).
  On: media (pictures/audio/video) is copied into the target tenant's storage and asset urls
  are rewritten to the target. Off: exchanged items keep the source tenant's asset urls
  verbatim (the source media endpoint must be reachable/authorized for target users).
- **Media** (`media.py::copy_item_media`): rendition + association-rendition files are read in the
  source context and written inside `tenant_context(target)`; `media`/`href` are rewritten in the
  payload. On the target side `transfer_renditions`/ingest skip re-downloading because the media
  ids resolve locally.
- **Receiver** (`receiver.py`): Celery task `tenants.deliver_to_tenant` (tenant restored from the
  task header by the M5 prologue) re-checks the allowlist, gets-or-creates a per-partner ingest
  provider ("Tenant exchange: {source}", feeding service `tenant_exchange`, parser ninjs — so
  routing schemes apply and items carry a proper source), converts the ninjs payload with
  `NINJSFeedParser` (keeping the copied media references), and ingests via the standard
  `ingest_items` path; optional auto-fetch to a desk/stage via `apps.archive.common.fetch_item`.
- **Feeding service** (`feeding_service.py`): `tenant_exchange` is push-only (`_update` returns
  nothing) — it exists so the providers validate and the ingest machinery has its hooks.
- Registration: importing `superdesk.publish.transmitters` (done by `superdesk.publish.init_app`)
  pulls in the exchange package, registering the transmitter, feeding service and Celery task.
- Tests: `tests/core/tenants_exchange_test.py` — partner-direction model, media copy across tenant
  stores with reference rewriting, transmitter validation (unknown/non-partner/one-way targets)
  and the enqueue contract (tenant header, provenance fields).
- Follow-up (moved to M11 scope): manual "send to tenant" endpoint + `send_to_tenant` privilege
  for unpublished content; behave round-trip e2e.

## M11 — Accounts phase 2 + tenant admin API

- **Authoritative accounts mode** (`SHARED_ACCOUNTS_AUTHORITATIVE`, default off): passwords live
  **only** on the control-plane account — the dual-write hook strips the hash from tenant user
  docs, and login has no tenant-local fallback (unknown account → auth error).
- **Account → tenants mapping** (`account_tenants` control-plane collection), maintained by the
  dual-write hook; `accounts:tenants --email ...` CLI.
- **Tenant switcher endpoint** (`superdesk/accounts/api.py`): `GET /accounts/me/tenants`
  (session-authenticated) returns the active tenants + hosts where the current user's account has
  a linked user; the client redirects to the chosen host.
- **Tenant switcher SSO** (same module): sessions never span tenants, but switching signs the
  user in automatically. `POST /accounts/me/switch-token {tenant}` (session-authenticated)
  issues a signed, 60-second, single-use token (itsdangerous over the shared `SECRET_KEY`;
  nonce in the `account_switch_tokens` control-plane collection, TTL-cleaned) bound to the
  target tenant; the client redirects to `https://{host}/?tenant_switch={token}` and the target
  tenant exchanges it via unauthenticated `POST /accounts/switch-login {token}`, which validates
  signature/age/nonce/tenant-binding and creates a session for the linked user through the
  `oauth` resource (response shaped like the `auth_db` login). Any failure is a uniform `401`
  and the client falls back to the manual login screen.
- **HTTP tenant admin API** (`superdesk/tenants/admin_api.py`), in `CORE_APPS`:
  `GET|POST /tenant-admin/tenants`, `GET|PATCH|DELETE /tenant-admin/tenants/<slug>[?purge=1]`
  (create provisions via the same resumable flow as the CLI; PATCH updates status and the partner
  allowlist; DELETE requires the tenant to be disabled). Served only when `MULTI_TENANT_ENABLED`
  and the request host equals `TENANT_ADMIN_HOST` — otherwise every endpoint answers 404 (the api
  does not advertise itself). The tenant middleware serves the admin host without binding a
  tenant, so any tenant-scoped access on it fails closed. Two auth mechanisms:
  a **super-admin session** (`POST /tenant-admin/login` with shared-account credentials of an
  account flagged `is_super_admin`; `GET /tenant-admin/me`, `POST /tenant-admin/logout`; used by
  the tenant administration panel UI) and the optional static bearer token
  `TENANT_ADMIN_API_TOKEN` (automation/ops). Bootstrap the first super admin with
  `accounts:set-super-admin --email ...`.
- **Cross-tenant user/account administration** (same api): `GET|POST /tenant-admin/accounts`,
  `PATCH /tenant-admin/accounts/<email>` (flags, password; refuses revoking your own admin
  access) and `POST /tenant-admin/tenants/<slug>/users` (creates a tenant-local user inside
  `tenant_context`, auto-linking the shared account).
- **Soft delete + retention purge**: deleting a tenant (CLI `tenants:delete` or the DELETE
  endpoint) only marks it `deleted` + stamps `deleted_at` — hosts answer 404, beat/exchange/
  switcher skip it, data stays. The tenant-agnostic beat task `tenants.purge_deleted` (daily)
  empties tenants deleted more than `TENANT_DELETED_RETENTION_DAYS` ago (mongo dbs incl. GridFS
  and versions, elastic indexes; S3 stays) and stamps `purged_at`. Until purged, a deleted
  tenant can be restored (`tenants:enable` / `PATCH {"status": "active"}`); afterwards restore
  is refused. `tenants:purge SLUG --yes` forces an immediate purge (ops escape hatch). The
  tombstone record is kept (audit + host reservation).
- **Lifecycle webhooks** (`superdesk/tenants/webhooks.py` + `tenants.webhook_notify` task):
  `tenant.suspended`, `tenant.activated`, `tenant.deleted` and `tenant.purged` events POST
  `{event, tenant, status, hosts, timestamp, deleted_at?, purged_at?}` — delivered via Celery
  with exponential-backoff retries and HMAC-SHA256-signed (`X-Superdesk-Signature`) per-webhook
  secrets. **Multiple webhooks** are stored in the control-plane `webhooks` collection; every
  enabled webhook receives all tenant lifecycle events, with one queued delivery per webhook
  and url/secret re-read at delivery time. Managed from the panel via `/tenant-admin/webhooks` CRUD +
  `POST /tenant-admin/webhooks/{id}/test`; `TENANT_WEBHOOK_URL`/`TENANT_WEBHOOK_SECRET` act as
  one implicit read-only all-tenants webhook (id `config`).
- **Client integration**: `client_config` exposes `multi_tenant_enabled`,
  `shared_accounts_enabled` and `tenant_admin_url`; `GET /accounts/me/tenants` includes
  `is_super_admin` to gate the client's "Tenant administration" menu entry. Client-side spec:
  [`MULTITENANCY_CLIENT_SPEC.md`](MULTITENANCY_CLIENT_SPEC.md).
- **Manual "send to tenant"** (`superdesk/tenants/exchange/api.py`, in `CORE_APPS`):
  `POST /archive/send_to_tenant {item_id, target_tenant, desk?, stage?, auto_fetch?}`, guarded by
  the new `send_to_tenant` privilege; snapshots the archive item with the ninjs formatter and runs
  the same transmit path as the subscriber-based exchange (partner allowlists still apply).
- Reset-password decision: tokens stay per tenant — with shared accounts a password reset done on
  any tenant propagates to the account via the dual-write on `update_password`, and reset links
  land on the host they were requested from. Control-plane tokens are not needed.
- Not included: behave e2e suites for provisioning and the exchange round-trip (follow-up).
- New settings: `TENANT_ADMIN_HOST`, `TENANT_ADMIN_API_TOKEN`, `SHARED_ACCOUNTS_AUTHORITATIVE`.
- OpenAPI spec for all new endpoints: [`specs/tenantsapi.yaml`](specs/tenantsapi.yaml)
  (same 3.0.2 convention as the existing `specs/*.yaml`).
- Tests: `tests/core/tenants_admin_api_test.py` (guard fail-closed matrix, list/create/patch/delete
  flows) plus the mapping/authoritative additions in `tests/core/tenants_accounts_test.py`.

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

- (resolved) The recurring `datetime - int` worker errors were celery's gossip consumer
  choking on its own event messages: the superdesk context-aware celery serializer was
  registered under the standard `application/json` content type, hijacking the decoder for
  celery's internal messages and datetime-coercing their fields. It now registers as
  `application/x-context-aware-json` (added to `CELERY_ACCEPT_CONTENT`).

- Config overrides only apply to runtime `get_app_config()` reads; boot-time config is global.
- The websocket server matches tenants by subdomain label only (no registry lookup in that process).
- Queues are shared: a noisy tenant can delay others (per-tenant queues/rate limits are a follow-up).
- `tenants:delete --purge` does not remove S3 media (bucket prefix cleanup is manual for now).
- Custom project mongo prefixes get a derived `_{prefix.lower()}` db-name suffix — projects should
  verify the resulting names.
- Data migration of an existing single-tenant deployment into a named tenant = create a tenant
  record whose `db_prefix`/`elastic_prefix` point at the existing db/index names (no data copy).
