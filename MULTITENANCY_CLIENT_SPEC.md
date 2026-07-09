# Multi-Tenancy — Client Implementation Spec

Handoff document for implementing the UI side of multi-tenancy (superdesk-client-core /
client repo) against the server work on the `multitenant` branch of superdesk-core.

Companion server docs in this repo:

- [`MULTITENANCY_PLAN.md`](MULTITENANCY_PLAN.md) — the overall design and decisions
- [`MULTITENANCY_IMPLEMENTATION.md`](MULTITENANCY_IMPLEMENTATION.md) — what the server actually does, per milestone
- [`specs/tenantsapi.yaml`](specs/tenantsapi.yaml) — OpenAPI 3.0.2 spec of every new endpoint (request/response schemas, error codes, auth)

## 1. The server model in one paragraph

One deployment serves many tenants (newsrooms). **The tenant is the hostname**: the client at
`https://tenant-a.example.com` talks to the API at that same host and only ever sees tenant-a's
data — resolution is entirely server-side, there is no tenant parameter anywhere in the API.
Users, roles, privileges, desks, preferences and sessions are all per tenant. Optionally
(`SHARED_ACCOUNTS_ENABLED`) the same email/password works on every tenant where the person has a
user, and content can be sent between tenants that allow each other as exchange partners.

Everything is off by default (`MULTI_TENANT_ENABLED=false`); the client must degrade gracefully
to plain single-tenant behavior (see feature detection, §3).

## 2. What does NOT change for the client

- **All existing API calls, resources, auth flows and the websocket connection are unchanged.**
  The client keeps using relative URLs against the host it is served from — that is the whole
  tenant mechanism. Never hardcode or cache absolute URLs of another host.
- Login, session cookie handling, password change/reset: same endpoints, same payloads.
  (Server-side, credentials may be checked against a shared account, but the API contract is
  identical, including `401` on bad credentials and the existing password-expiry error flow —
  a "password reset required" on a shared account surfaces through that same expiry flow.)
- Websocket: connect to the same host as today. The ws server delivers only the current
  tenant's notifications (filtered server-side by the connection's Host header) — no client
  filtering needed.
- Media/rendition URLs arrive already correct for the current tenant.

## 3. Feature detection

`GET /client_config` (the existing config endpoint) now includes:

```json
{
  "multi_tenant_enabled": true,
  "shared_accounts_enabled": true,
  "tenant_admin_url": "https://admin.example.com"
}
```

- `multi_tenant_enabled` → gate the "send to tenant" action and any tenant-related UI.
- `shared_accounts_enabled` → gate the tenant switcher.
- `tenant_admin_url` → permanent location of the tenant administration panel (§8); empty
  string when no admin host is configured.
- The current tenant has no explicit id in config: the host **is** the tenant. For display
  purposes, `/accounts/me/tenants` (below) can be matched against `window.location.host`.

Both flags are `false` (or absent — treat absent as `false`) on single-tenant deployments.

## 4. UI feature 1 — Tenant switcher

**Endpoint:** `GET /accounts/me/tenants` (session-authenticated, same-origin)

```json
{ "tenants": [
    { "tenant": "tenant-a", "hosts": ["tenant-a.example.com"] },
    { "tenant": "tenant-b", "hosts": ["tenant-b.example.com"] }
  ],
  "is_super_admin": false }
```

`is_super_admin` is `true` when the logged-in user's shared account may manage tenants —
it gates the "Tenant administration" hamburger menu entry (§8).

Lists the **active** tenants where the logged-in user's shared account has a linked user.
Returns `{"tenants": []}` when shared accounts are off or the user has no account link.

**Expected UI:**

- In the user avatar/profile menu, when `shared_accounts_enabled` and the list has ≥ 2 entries,
  show a "Switch tenant" submenu listing the tenants (mark the current one — the entry whose
  `hosts` contains `window.location.host`).
- Selecting another tenant navigates the browser to `https://{hosts[0]}` (top-level navigation,
  not XHR — it is a different origin).
- **Sessions do not span tenants** (by design): after the redirect the user lands on the other
  tenant's login screen and signs in with the same credentials. Copy suggestion on the menu item
  or login screen: "You are switching to {tenant}; sign in with your usual password."
- Cache the response per session; refresh on demand (menu open) is fine — it's a cheap call.

## 5. UI feature 2 — "Send to tenant" action

**Target picker endpoint:** `GET /exchange/partners` (session-authenticated)

```json
{ "partners": [ { "tenant": "tenant-b", "hosts": ["tenant-b.example.com"] } ] }
```

Only tenants this tenant may send to (partner allowlist checked in both directions, active
targets only). Empty on single-tenant deployments or when no partners are configured.

**Action endpoint:** `POST /archive/send_to_tenant`

```json
{ "item_id": "urn:newsml:...", "target_tenant": "tenant-b",
  "desk": "<desk-id>", "stage": "<stage-id>", "auto_fetch": false }
```

- `desk`/`stage`/`auto_fetch` optional; with `auto_fetch: true` the item is fetched onto the
  target desk after ingest, otherwise it lands in the target tenant's ingest for manual fetch.
- Responses: `200 {"_status": "OK", ...}` queued ok; `400` unknown/inactive target or tenants
  not partners (message in `_error.message`); `403` missing privilege; `404` item not found.
- Delivery is **asynchronous** (Celery) — a `200` means "queued", not "arrived". The UI should
  confirm with "Sent to {tenant}" and not wait for anything further.

**Privilege:** `send_to_tenant` — arrives in the user's `active_privileges` like any other and
must also be added to the role-privileges management screen (it registers server-side with label
"Send content to another tenant", so it appears in the privileges list automatically).

**Expected UI:**

- New item action ("Send to tenant…") in the archive item actions menu / authoring "send to"
  panel, visible when `multi_tenant_enabled` && user has `send_to_tenant` && `/exchange/partners`
  is non-empty (fetch once, cache per session).
- Dialog: target tenant select (from partners), optional auto-fetch toggle. Desk/stage pickers
  for the *target* tenant are **not possible** (the client cannot browse another tenant's desks)
  — either omit desk/stage (items land in target ingest) or provide free-text/config-provided
  ids. Recommended v1: omit them, send `auto_fetch: false`.
- The action makes sense for stories in any state (the server formats the current archive item
  snapshot with ninjs); if product wants to restrict to published-only, enforce in the UI.

**Received content (target side):** exchanged items appear in the target tenant's ingest with
provider name "Tenant exchange: {source}" and carry provenance in `extra.original_tenant` and
`extra.original_item_id`, `source` = the source tenant id. Optional UI nicety: show a
"from {original_tenant}" hint on such items (fields are plain item metadata, no API change).

## 6. UI feature 3 — Subscriber destination type `internal_tenant`

The transmitter list in `client_config.transmitter_types` now includes
`{"type": "internal_tenant", "name": "internal tenant"}`. In the subscriber/destination
management UI, add a destination config form for this type with fields (stored in
`destination.config`, all consumed server-side):

| field | type | required | notes |
|-------|------|----------|-------|
| `tenant` | string (tenant id) | yes | populate the select from `GET /exchange/partners` |
| `auto_fetch` | boolean | no (default false) | fetch on the target desk after ingest |
| `desk` | string (target desk id) | no | free text — target tenant's desks are not browsable |
| `stage` | string (target stage id) | no | free text |

Format for such destinations must be **ninjs** (the standard formatter select applies).

## 7. Error surfaces specific to multi-tenancy

- `404 {"_error": {"message": "Unknown tenant host"}}` — the host is not a registered tenant.
  Only relevant for misconfigured deployments; show the raw message.
- `423 {"_error": {"message": "Tenant is not available"}}` — tenant suspended or still being
  provisioned. Recommended: a full-page "This newsroom is currently unavailable" state instead
  of a generic error toast (check for HTTP 423 in the api error interceptor).
- `401` on requests after switching hosts is the normal "not logged in here" — route to login.

## 8. UI feature 4 — Tenant administration panel

Tenant administration is part of the client. It lives at a **permanent, unique location**:
the reserved admin host from `client_config.tenant_admin_url` (e.g. `https://admin.example.com`
— server setting `TENANT_ADMIN_HOST`). All `/tenant-admin/*` endpoints are served **only** on
that host; they answer 404 anywhere else.

### 8.1 Entry point (on every tenant)

- Add a **"Tenant administration"** entry to the Superdesk hamburger (main) menu.
- Visible only when `multi_tenant_enabled` && `tenant_admin_url` is non-empty &&
  `GET /accounts/me/tenants` returns `is_super_admin: true` for the current user.
- Selecting it opens `tenant_admin_url` (top-level navigation or new tab — it is a different
  origin; do **not** XHR to it from a tenant host).

### 8.2 Authentication on the admin host

The panel is a separate view of the client app served on the admin host. It has its own
session (the tenant session does not carry over):

- `POST /tenant-admin/login {email, password}` — same shared-account credentials the user
  logs into tenants with; the account must be flagged `is_super_admin`. `401` on failure
  (no distinction between wrong password and missing rights), `200 {email}` on success —
  a signed session cookie scoped to the admin host is set.
- `GET /tenant-admin/me` → `{auth: "session", email}` — use for session restore on load;
  404 means not logged in (the whole API cloaks as 404 when unauthorized).
- `POST /tenant-admin/logout`.
- Bootstrap note for ops: the first super admin is granted on the server with
  `python manage.py accounts:set-super-admin --email root@example.com`.

### 8.3 Panel screens (endpoints in `specs/tenantsapi.yaml`)

**Tenants** — `GET /tenant-admin/tenants` list (slug, status, hosts, partners, provisioning
markers); detail/actions:

- Create (`POST /tenant-admin/tenants {slug, hosts[], admin?{username,password,email}, resume?}`)
  — show a provisioning-in-progress state; `409` = exists (offer "resume"); `400` = invalid
  slug (`^[a-z][a-z0-9-]{0,61}$`) or missing hosts.
- Suspend / re-enable (`PATCH {status: "suspended"|"active"}`).
- Exchange partners editor (`PATCH {exchange_partners: [{tenant, direction}]}`) — direction
  `send`/`receive`/`both`; `400` names unknown partner tenants.
- Delete (`DELETE /tenant-admin/tenants/{slug}?purge=1`) — only when suspended (`409`
  otherwise); confirm dialog must spell out that purge drops all databases and media.

**Accounts (cross-tenant users)** — `GET /tenant-admin/accounts` list (email, username, flags,
`tenants: [...]` where the account has users); actions:

- Create account (`POST {email, password, username?, is_super_admin?}`).
- Edit flags / reset password (`PATCH /tenant-admin/accounts/{email}
  {is_enabled?, is_super_admin?, needs_password_reset?, password?}`) — the server refuses
  revoking your own admin access (`400`).
- **Add user to a tenant** (`POST /tenant-admin/tenants/{slug}/users
  {username, password, email, admin?}`) — creates the tenant-local user; with shared accounts
  the credentials auto-link to the account. Noop if the username already exists in that tenant.
  This is the cross-tenant user administration: pick an account, pick a tenant, create the user.

Per-tenant profile details (roles, desks, avatars…) remain managed inside each tenant with the
existing user screens; the panel handles existence + credentials + rights across tenants.
(`account_id` may appear on tenant user docs; ignore/hide it in the tenant user forms.)

## 9. Local dev / testing setup

1. In superdesk-core (branch `multitenant`): start services (`docker compose up -d`), then in the
   server settings set `MULTI_TENANT_ENABLED=true` (+ `SHARED_ACCOUNTS_ENABLED=true` for the
   switcher).
2. Create tenants:
   `python manage.py tenants:create tenant-a --host tenant-a.localhost --admin-username admin --admin-password admin --admin-email admin@example.com`
   (repeat for `tenant-b`), then allow exchange both ways:
   `python manage.py tenants:update tenant-a --add-partner tenant-b --direction both` (and inverse).
3. Add `tenant-a.localhost` / `tenant-b.localhost` to `/etc/hosts` (or rely on `*.localhost`
   resolving to 127.0.0.1) and serve/proxy the client so that API requests carry those Host
   headers — the Host header is what selects the tenant.
4. Same admin email on both tenants + `SHARED_ACCOUNTS_ENABLED` gives you a working switcher
   (`accounts:migrate --all-tenants` links pre-existing users).
5. Unknown-host check: `curl -H "Host: nope.localhost" http://localhost:5000/api` → 404;
   suspended tenant (`tenants:disable tenant-b`) → 423.
6. Admin panel: set `TENANT_ADMIN_HOST=admin.localhost` (add to `/etc/hosts`), then
   `python manage.py accounts:set-super-admin --email admin@example.com` and log into
   `POST /tenant-admin/login` on that host. (`TENANT_ADMIN_API_TOKEN` is optional — it is a
   second, machine-oriented way in; the panel uses the session login.)

## 10. Summary of client work items

1. Read `multi_tenant_enabled` / `shared_accounts_enabled` / `tenant_admin_url` from client
   config (feature gates).
2. Tenant switcher in the user menu (`GET /accounts/me/tenants`, top-level redirect, re-login).
3. "Send to tenant" item action (`GET /exchange/partners` + `POST /archive/send_to_tenant`),
   gated by the `send_to_tenant` privilege; success toast on 200, message passthrough on 400/403.
4. Destination config form for the `internal_tenant` transmitter type (§6).
5. HTTP 423 handling as a "tenant unavailable" page state.
6. **Tenant administration panel** (§8): hamburger menu entry gated on `is_super_admin`,
   admin-host view with its own login, tenants screen (create/provision, suspend, partners,
   delete/purge) and accounts screen (create, flags, reset password, add user to tenant).
7. Optional: provenance hint ("from {tenant}") on exchanged ingest items.
