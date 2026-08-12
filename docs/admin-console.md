# Admin console

**Status: dev-live (2026-08-06), production not yet deployed.**
Migration `c4a91f7d20b3` is applied on dev, operator accounts exist,
and the console runs end to end there — API on `:8006`,
`frontend-admin` on `:3400`. Production still follows the runbook at
the bottom; `deploy.sh` has no admin sibling yet.

This document started as a proposal and the section order still
reflects that: **decisions first, then the surfaces they produced.**
That order is worth keeping — the decisions are what a future change
has to argue with, and each surface below is short precisely because
the reasoning lives above it.

Historical note, because it cost a repair: the "keep the migration
unapplied" guardrail was real, and parallel connector work
(`e2b7d4f9a1c6`) chained onto the admin migration and applied it early
against the then-current schema, drifting the dev tables from the
evolved design (the `display_name`/`role` era). Repaired by **surgical
ALTERs, not `alembic downgrade`** — on a shared chain a downgrade rolls
back innocent migrations, and their data, on the way past.

Four changes landed with the console but stand on their own merits, so
they stay even if the console is reshaped: the `app/asgi.py` factory
(with `app/public_main.py` rewired onto it), the
`app/api/endpoints/` → `app/api/public/` rename, the rate-limit
principal (`set_rate_limit_principal`), and `WWW-Authenticate: Bearer`
on 401 `AppError`s.

## Load-bearing decisions (expensive to reverse)

These are the review agenda. Each is cheap to change today and painful
after first deploy.

1. **Separate process, private bind.** Own ASGI app on `:8006`,
   `root_path="/admin/api/v1"`. Security is a *deployment* property
   (VPN / firewall / no gateway route) — nothing in code enforces
   reachability. The alternative (code-level IP allowlist) was rejected
   as a false comfort, but this must be a conscious choice.
2. **`staff` is its own table**, never an `is_staff` flag on `user`:
   operator identity ≠ customer identity, independent credentials and
   revocation, and no customer row is one UPDATE from platform
   privileges.
3. **No roles.** Every `ACTIVE` staff account has full console access —
   the operator team is small and fully trusted, and the audit trail
   records who did what. A role column plus a gate in
   `app/api/admin/dependencies.py` is one additive migration away if
   tiers are ever needed; starting without them keeps every slice a
   single `StaffContext` check.
4. **Third JWT class via `scope="staff"`**, same `secret_key`, workday
   TTL, no refresh (operators sign in daily). All three decoders live in
   `app/auth.py` so their mutual exclusion stays auditable in one place.
5. **Audit is structural, not conventional.** `admin_audit_log` is
   append-only; `audit()` is the single writer *and* owns the commit, so
   a mutation persists only together with its row. Target is a loose
   `(target_type, target_id)` string pair, no FK — audit rows must
   outlive their targets. Actions are `<domain>.<verb>` in VARCHAR with
   a Python-side `AuditAction` enum (no migration to extend).
6. **The console never re-implements a domain service.** Credit
   adjustments, withdrawal review, skill publishing all call the funnels
   that already hold the invariants (`credits._internal`,
   `commission._internal`, …). `services/admin/` exists only for what
   nothing else owns (audit, staff accounts).
7. **`api/public` + `api/admin` sibling layout** — already applied
   repo-wide; a third surface would be a third sibling.
8. **Native PG enum** for `staff_status` (repo convention). Note the
   cost: adding a value later is `ALTER TYPE … ADD VALUE`; removing one
   is effectively never. Two values (`ACTIVE`/`DISABLED`) make churn
   unlikely.

## Surfaces

Every slice below is built on the staff-management reference shape
(`StaffContext` in the signature, mutations inside `audit()`, reads
inline), and **none added a service module**: every write calls a
funnel that already exists. That is the property to preserve when
adding the next one — a surface that needs a new place to write from is
the signal to put the rule in `app/services/`, not in the endpoint.

Most of these surfaces are read-only, which is also deliberate: the
console's job is usually to *see* state the platform already manages
for itself. A button appears only where an operator can do something no
automated path does.

### Users (`app/api/admin/user.py` — routes under `/users`)

| Route | What | Audit |
|---|---|---|
| `GET /users?q=&status=` | Paginated directory. One `q` ILIKE-matched against username / email / phone (at ~100 users no index needed). Row: id, username, name, email, phone, status, balance_credits, created_at. | — |
| `GET /users/{id}` | Profile + credit balance + session count. | — |
| `PATCH /users/{id}` | `{status, reason}` — ACTIVE ↔ DISABLED. Takes effect on the user's next request (status is checked per request; no token bump needed). | `user.update` |

Not included: editing email/phone (the sudo-gated self-serve flows own
those), password resets (OTP owns recovery), user deletion,
impersonation, user creation (invite flow owns it).

### Credits (same module — sub-resources of a user)

| Route | What | Audit |
|---|---|---|
| `GET /users/{id}/credits/transactions` | Paginated ledger via `credits.list_transactions` — kind, signed amount in credits, created_at, reference. | — |
| `POST /users/{id}/credits/adjustments` | `{amount_credits (signed, non-zero), reason, idempotency_key}` → `credits.apply_adjust(admin_id=staff.id)`. The console frontend mints the idempotency key per form submission, so a network retry can't double-apply; a duplicate key returns 409. | `credit.adjust` |

No topup endpoint: a signed adjust covers manual grants and clawbacks,
and real topups come from the payment webhook. Money on the wire is
**whole credits** (int, `credits_from_micro` / `micro_from_credits`),
storage stays µ-credits — same convention as the customer endpoints.
The console's idempotency keys are namespaced `admin:` in the ledger so
they can never collide with payment-webhook keys (`hf:…`).

### Sessions (`app/api/admin/session.py` — `/sessions/{id}`; the user-scoped list lives with the user resource)

| Route | What | Audit |
|---|---|---|
| `GET /users/{id}/sessions` | Paginated list **including soft-deleted** (`deleted_at` visible — unlike the customer list, support needs to see what was deleted). Row: id, title, status, agent_type, tier, created_at, updated_at, deleted_at. | — |
| `GET /sessions/{id}` | Meta + the rendered `blocks` — the conversation, read-only. | `session.view` |

`session.view` is the **only audited read** in the console: it is
customer conversation content, the most privacy-sensitive thing the
console can show, so "who looked at whose conversation" must be
answerable. The audit block simply wraps no mutation.

**No cancel, no delete** in v1. Cancel would need a Redis client in the
console process (deliberately absent) and stuck turns are already
recovered by the worker's stale-lease cron and the budget enforcer;
delete is the user's own action. If ops later needs a kill switch, that
is the decision that re-opens the no-Redis choice — take it then, not
by default.

### Feedback (`app/api/admin/feedback.py` — routes under `/feedback`)

The review queue the public surface anticipated: `POST /feedback` only
creates rows, and the `FeedbackStatus` workflow column exists for this
module.

| Route | What | Audit |
|---|---|---|
| `GET /feedback?status=&user_id=` | The queue, newest first — `?status=OPEN` rides `ix_user_feedback_status_created`, built for exactly this. `username` LEFT-joined (a deleted author's feedback still belongs in the queue). | — |
| `PATCH /feedback/{id}` | Triage: `OPEN` → `RESOLVED` / `IGNORED` (or back). `reason` is **optional** — benign triage's meaning is the status itself; requiring justification per click teaches operators to type noise. | `feedback.update` |

No delete — `IGNORED` is the dismissal and the record stays.
`image_keys` are returned raw; minting presigned view URLs is the
console UI's follow-up.

### Message ratings (`app/api/admin/message_feedback.py` — `/message-feedback`)

The other feedback table, and a different shape: `user_feedback` is
prose someone chose to write, `message_feedback` is a thumb on one
assistant reply. Both live behind the console's Feedback page as two
tabs, because an operator asking "what are users unhappy with" should
not have to know which table caught it.

| Route | What | Audit |
|---|---|---|
| `GET /message-feedback?rating=&days=` | The queue, newest first, joined to the rated message and its author. Each row carries a 240-char excerpt of the reply and a link to its session. | — |
| `GET /message-feedback/stats?days=` | up / down / `down_rate` over the window | — |

**Read-only, and therefore not audited.** There is no triage column to
write — a thumb is the user's own verdict, not a ticket, and giving
operators an "acknowledge" button would invent a workflow nobody asked
for. The UI defaults the filter to `DOWN`: a page of thumbs-up is a
morale exercise, a page of thumbs-down with the reply text is a list of
things to fix. `down_rate` is a share of *rated* replies, not of all
replies, and the tile says so — the denominator is a tiny self-selected
sample and reading it as a quality score would be wrong.

The excerpt is cut in SQL from `message.blocks`, so the list does not
carry whole transcripts across the wire; the full conversation is one
click away in the session view, which is the surface that already
audits a read.

### Invites (`app/api/admin/invite.py` — `/invite-codes`, `/invite-redemptions`)

Issuing a cold-start code, and seeing who came in through whom.
Replaces reaching for `scripts/create_invite_code.py` over SSH.

| Route | What | Audit |
|---|---|---|
| `GET /invite-codes?origin=&q=` | Codes, newest first; `REFERRAL` rows carry their owner. `used_count` / `max_uses` is the whole state. | — |
| `POST /invite-codes` | Mint a `SYSTEM` code (`max_uses` 1–10000, `reason` **required**) | `invite.create` |
| `GET /invite-redemptions` | Who redeemed what, invitee and referrer both resolved through aliased joins | — |

**Only `SYSTEM` codes are mintable here.** A `REFERRAL` code is a
user's own, minted at registration and capped by
`invite_referral_max_uses`; letting an operator conjure one would hand
out someone else's referral credit. The endpoint calls the same
`invite_service.create_invite_code` the script does — the console is a
second caller of one code path, never a second implementation of it.

`reason` is required here though a skill upload asks for none, and the
difference is what the payload can say for itself: a bundle names the
skill it ships, while an invite code's payload is a random string and a
use count — "why does this code exist, and who asked for it" is not
recoverable from it. No delete and no edit — exhausting a code is setting
`max_uses`, and the redemption history is an audit record.

### Dashboard (`app/api/admin/stats.py`, `app/services/credits/analytics.py`)

Credit-usage monitoring — the console's landing page.

| Route | What | Audit |
|---|---|---|
| `GET /stats/credits?days=7\|30\|90` | Tiles (spend, granted + the free share of it, clawed back, active users, settled turns, outstanding liability) + gap-filled daily series + spend by model. | — |
| `GET /stats/top-spenders?days=&limit=` | Spend leaderboard, biggest first. A bare list, not paginated — a top-N is what it is; paging into the tail is the users list. | — |
| `GET /stats/recent-users?days=&limit=` | Last-active users, newest first — the same row shape as top-spenders, ordered differently. | — |
| `GET /stats/check-ins?days=` | Check-ins claimed and the distinct users who claimed them | — |
| `GET /stats/check-in-leaders?days=&limit=` | Users ranked by days checked in | — |
| `GET /stats/recent-check-ins?days=&limit=` | The same rows, newest claim first | — |

**Granted is reported with its giveaway share split out.** `granted`
is every credit added — `TOPUP` (bought) alongside `CHECKIN`,
`REFERRAL` and positive `ADJUST` (given). Those move for opposite
reasons, so a single number cannot tell a good sales day from an
expensive one, and the purchased half is *already* reported in ¥ by the
payments summary. `granted_free` is defined as `_GRANT_ROWS AND kind <>
'TOPUP'` rather than by listing kinds, so a new grant kind counts as
free unless it is a purchase.

**`outstanding` is the one figure the window selector does not move** —
it is `SUM(user.credit_balance)`, a position rather than a flow. That is
correct but reads as a bug next to six windowed tiles, so the tile says
"all users, right now" where the others name their unit.

**Spend is the negative `SETTLE` row and nothing else.** A turn writes
`RESERVE −estimate`, then `SETTLE +estimate` (release) and
`SETTLE −actual` (charge); a dead turn writes `REFUND +estimate`. So
`spend = SUM(−amount) WHERE kind='SETTLE' AND amount<0` — summing
`RESERVE` would report estimates, and summing all turn kinds would smear
a turn across two days at midnight. That predicate is one constant
interpolated into all four queries, and a unit test asserts the code
spells it exactly once.

Daily buckets are cut in `settings.checkin_timezone`, the same
business-day rule check-in uses, so "today" means what an operator
means. Check-ins are counted the same way, and the count is a
**retention** reading, not a money one: the reward it grants is already
inside `granted`, so the tile says how many people came back today, and
the ledger tile says what that cost. Two counts on the row (claims and
distinct users) because `UNIQUE(user_id, check_in_date)` makes them
identical within one day and different across a window — the gap
between them is repeat visits.

The two check-in rankings are one aggregate under two `ORDER BY`s, the
same shape `credits.analytics.user_activity` uses for spend, so a user
cannot be a "leader" by one definition and missing from the other. They
live in `services/check_in.py` for the reason the count does: the ledger
does not know what a check-in is.

Each row carries the **streak from the user's latest claim**, not the
largest in the window — `streak_day` is a running count the claim
computes, so a maximum would report a streak the user has already
broken. The count alone cannot say this: ten days in a row is a habit,
ten scattered days is not. Count ties break on recency, so equal
leaders still put the currently-active user first.

Aggregates live in `credits/analytics.py` (the ledger's domain
owns questions about the ledger, like `credits/transactions.py`), not
under `services/admin/`. Not audited — aggregates, never one customer's
content. Path is `/stats/*` because the ASGI factory already serves
Prometheus at `/metrics`. No schema change, no new tables. At current
row counts the time-range scan is fine; if `credit_ledger` reaches
millions, add `ix_credit_ledger_created_at` then.

### Skills (`app/api/admin/skill.py`)

| Route | What | Audit |
|---|---|---|
| `GET /skills?origin=&q=` | Every origin, including the `SYSTEM` skills hidden from users. Platform rows first, then newest; owner LEFT-joined (platform rows have none by construction). | — |
| `POST /skills` | Upload a platform bundle — multipart, `{origin}` | `skill.upload` |
| `PATCH /skills/{id}` | `{display_name, description, enabled, reason}` — picker metadata and the **platform default** | `skill.update` |

The toggle's subtlety is surfaced in the UI, not just here: `enabled`
is the default, and `user_skill_preference` overrides it per user
(effective = `COALESCE(pref.enabled, skill.enabled)`), so disabling a
skill does **not** switch it off for users who opted in.

**`display_name` and `description` are editable, following the public
surface.** It calls them picker metadata — what the picker shows, not
what the bundle does — and lets a user edit them on a skill they own
while refusing them on a platform one, "because they'd need to mutate
the shared row". This is the surface that owns that row. `name` stays
immutable on both surfaces: the worker finds the staged directory by
it, so it changes by re-uploading and no other way. A rename here also
clears `display_name_i18n`, exactly as the public PATCH does — the list
endpoint resolves the locale map first, so a stale one would silently
shadow the new name.

**A replace keeps the platform default it found.** `skill.enabled` is
what every user who never touched their own toggle reads through
(`COALESCE(pref.enabled, skill.enabled)`), so re-deriving it from the
origin would switch a live `OFFICIAL` skill off for all of them the
moment an operator ships a fix — while the per-user preference rows,
the half people think of, survive untouched. Only a *new* skill takes
its origin's default; `scripts/replace_platform_skill_bundles.py` has
always left the flag alone for the same reason.

**Upload replaces by name rather than conflicting.** A user
re-uploading a name they own is a mistake worth a 409; an operator
uploading `pdf` again is shipping a new version of `pdf`, so the row is
updated in place — the id the worker has seen and every user's
`user_skill_preference` both survive it. The displaced `bundle_key`
goes into the audit payload, and the object is deliberately **left in
the bucket**: a turn may be staging it at that moment, and the audit
row is then the only way back to the previous version.

`origin` is the one thing the bundle cannot state and it decides who
sees the skill, so it is the **only** field the form asks for.
`enabled` follows from it (`SYSTEM` enabled, `OFFICIAL` disabled — the
every-user-default rule above), which is why the form explains the
choice rather than offering a toggle.

**No `reason`**, matching staff-create and connector-catalog-create:
for a create the payload *is* the record, and here it names the skill,
its origin, the bundle and the one it displaced. Requiring a typed
justification on top of a self-describing deed is the same mistake as
requiring one per feedback triage click — it produces filler, not
accountability. The console asks for a reason where the same action can
be right or wrong depending on intent (`credit.adjust`, `user.update`),
not where the row already explains itself.

**One funnel, two front doors.**
`skill_registration.register_platform_skill_bundle` is what both the
console and `scripts/upload_skill.py` call; the script previously
carried its own raw-SQL upsert, and the duplicate is gone. Like
`create_invite_code`, the service **flushes without committing** —
`audit()` owns the commit, so the bundle and its audit row land in one
transaction. That is the general rule for any service the console
calls. The script stays as the path that still works when the console
is down or an operator has only SSH; bulk re-packaging remains
`scripts/replace_platform_skill_bundles.py`, which is a different job
(dry-run first, curated metadata preserved).

### Connectors (`app/api/admin/connector.py`)

| Route | What | Audit |
|---|---|---|
| `GET /connector-catalog` | Whole catalog in display order, unpublished included. Unpaginated — paging a hand-sorted shortlist would hide the thing being sorted. | — |
| `POST /connector-catalog` | New entry, created **unpublished** so the URL/icon can be checked first | `connector_catalog.create` |
| `PATCH /connector-catalog/{id}` | Edit / publish / reorder. Editing `url` can 409 — it is the entry's identity. | `connector_catalog.update` |
| `GET /connectors?user_id=&q=&connected=` | Users' own Connectors, **read-only**. `?connected=false` is the support view. | — |

Adding an MCP server to the platform catalog is a console action, not a
script one — the add form sends the entry's whole field set (key, name,
url, auth type, description, icon, OAuth scope, sort order), so an
operator never has to finish a half-made entry from a shell. It lands
as a **draft**: publishing is the separate PATCH, which is what makes
"check the URL and icon first" a real step rather than advice.

**The URL is the entry's identity** (migration `b8f2a91c47d3`). Two
entries pointing at one server are the same Connector offered twice
whatever they are named, so `uq_mcp_server_catalog_url` is the table's
unique constraint. `name` is deliberately *not* unique: it is the copy
users read and it is copied onto their row, so renaming one entry must
not collide with another.

This replaced a `key` column — a third name for a row that already had
`id` for machines and `name` for people. It was documented here as the
operator-facing handle, and it was, but the only thing it uniquely did
was carry the constraint that belongs on `url`.

**Add and edit are one form**, since they are the same field set under
the same rules and two of them would drift. Editing sends a **partial**
patch — only the fields that actually changed — so the audit row's
before → after payload is the real edit rather than every field
rewritten with its own value. A blank optional field and an absent one
are treated as equal when diffing, so leaving a never-set description
alone does not read as a change.

Every field on the create form is editable. That is a change in kind:
while the identity was an immutable `key`, no edit could collide, and
now the identity is an editable `url`, so **the edit path checks for a
duplicate exactly as the create path does** — a named 409
(`connector_catalog_url_taken`), not an `IntegrityError` surfacing as a
500. The unique constraint is still what makes it true under a race;
the check is the readable path to it.

**Editing `name` or `description` clears that field's locale map.**
`localized_text.resolve` reads the map's default-locale entry *before*
falling back to the canonical column, so a stale map shadows an edit for
every locale rather than only the translated ones — the public catalog
would keep serving the old name and `build_connector` would copy it onto
each Connector created afterwards, while this endpoint returns the
column and looks correct. The map is cleared whole, not per-locale,
because the console can neither show nor edit it: a translation of the
superseded text would be a divergence an operator cannot discover. The
discarded map goes into the audit payload, since unlike a skill's
(re-supplied by its bundle) this one is hand-curated and the trail is
the only remaining copy.

**`auth_type` is editable**, following the public surface, where a user may change their
own Connector's auth; there it is edited "as a whole" with its
credential to keep validation coherent, and a catalog entry has no
per-user credential, so here it is just the one field. It is copied
into each `mcp_server` row at instantiation, so a correction affects
future connectors only.

**Only `NONE` and `OAUTH` are offered**, on both create and edit.
`catalog.py` defines `INSTANTIABLE_AUTH_TYPES` and says outright that
"operator tooling should offer only these": `BEARER`/`HEADER` need a
per-user secret the catalog cannot hold, so `build_connector` refuses
them and an entry declaring one is unusable the moment it is published.
`manage_connector_catalog.py` already limited its `--auth-type` choices
to that same constant; the console now validates against it rather than
being the one surface that lets an unusable entry through.

**The URL must be `https`, checked at the schema.** Not the full SSRF
guard: `url_guard.guarded_client` already resolves and *pins* the
address at connect time, which is both where the danger is and the only
place the TOCTOU window can be closed. Repeating the address check here
would add a DNS lookup that refuses an entry staged before its service
is live, and buy nothing. The scheme is different — it is a pure string
test, and an `http` entry would otherwise be accepted into the catalog
and then fail for every user who added it.

The **one** piece that stays in `scripts/manage_connector_catalog.py`
is seeding a preregistered OAuth client, because that means sealing a
client secret:

**No secret is representable on this surface.**
`encrypted_credential`, `oauth_tokens_encrypted` and
`oauth_client_secret_encrypted` are absent from every schema — not
masked, absent — so a later "just add the column" can't leak one.
`has_preregistered_client` reports *whether* the platform holds an OAuth
app, never any part of it. Seeding those apps stays in
`scripts/manage_connector_catalog.py`, which is what lets the console
handle no secrets at all. `tests/test_admin_connector_secrets.py` pins
this against the ORM's real column list, so a *new* secret column fails
the test until someone decides it's safe.

### Payments (`app/api/admin/payment.py`, `app/services/payment/analytics.py`)

The RMB side, kept off the credits dashboard so the two units never
blur — the console's only ¥ page, with one clearly-labelled revenue tile
on the dashboard linking here.

| Route | What |
|---|---|
| `GET /payments/stats?days=` | Revenue, paid/created orders (→ conversion), credits granted, **un-windowed** pending count, daily series | 
| `GET /payments?status=&user_id=` | Orders, newest first; `?status=PENDING` is the operational view |

**Revenue is `status='PAID'` and nothing else** — a `PENDING` order is a
QR code that may never be scanned, `EXPIRED`/`FAILED` are intentions
that ended. The predicate is one constant across both queries, pinned by
a unit test the same way the credit spend predicate is. `pending_orders`
is deliberately *not* windowed: a stuck order from last week is exactly
the one worth seeing.

**Read-only on purpose.** No "settle now" button — `payment_reconciler`
already polls the provider and grants credits idempotently through the
same `_grant_paid` path as the webhook. The gap this page fills is
*seeing* a stuck order, not poking a money path that self-heals.

An order row carries `amount_cny` beside `credits` — the only place the
recharge margin (sale price vs cost basis) is visible.

**One extraction this forced**: business-day logic (`window_start`,
`business_today`, `business_days`) moved to `app/common/business_time.py`
now that check-in, credit reporting and revenue all need it. "What is
today" has one definition platform-wide, tested in
`tests/test_business_time.py`.

### The audit vocabulary these produced

`AuditAction` carries **only what the console can actually do**, which
is why it is short: `staff.*`, `user.update`, `credit.adjust`,
`session.view`, `feedback.update`, `skill.upload`, `skill.update`,
`connector_catalog.create` / `.update`, `invite.create`. Every surface
added since the first slice added at most one member, and the
read-only ones (ratings, dashboard, payments, connector list) added
none — an action exists because something writes, not because a page
exists.

Still deliberately out, with scripts covering them: **withdrawal
review** (the only remaining item that blocks a *user* — the
highest-value next slice), org management, and the two script jobs that
are genuinely not console work: sealing a connector's OAuth client
secret, and bulk re-packaging platform bundles from a checkout.

## Frontend and the questions settled with it

- **Console frontend — settled and scaffolded (2026-08-06):
  `frontend-admin/`**, official `create-next-app` scaffold (Next 16 /
  React 19 / TS / Tailwind v4, latest unpinned), same stack as `ppig/`
  for one-toolchain consistency and to later port ppig's
  `message.blocks` renderer into the session view. **Auth mirrors
  ppig's architecture**: the staff JWT lives in an `httpOnly` cookie
  (`staff_jwt`); client code calls same-origin `/api/...` route
  handlers, and `app/api/_backend.ts` forwards to the backend with the
  cookie's token as a Bearer header — the token never reaches client
  JS, operators tunnel **one** port (`:3400`), and browser CORS never
  fires (`admin_cors_origins` stays as belt-and-braces). One generic
  relay (`/api/console/[...path]`) covers the uniform admin endpoints;
  typed clients live in `lib/api/*`. **Styling is intentionally stock
  Tailwind** (default `gray-*`/`red-*`/`green-*` utilities, no design
  tokens, bare `globals.css`) — the console ships functional and
  unstyled for a designer to theme; only the `Button` variant *names*
  (`default`/`outline`/`ghost`/`destructive`) are a call-site contract.
  Deploys like the mep frontend:
  `npm run build` + `npm run start` (binds `127.0.0.1:3400` in the
  script). Skeleton verified by a full-stack smoke (throwaway DB +
  backend + built console: login/cookie/guard/proxy/logout). Built so
  far: login, auth guard + sidebar shell, users list (the wired
  reference page) + user detail; feedback / staff / audit-log /
  session-view pages are placeholders naming their endpoints. No SSO —
  the login form + `/auth/access-tokens` is the design.
- **Deployment shape** — `deploy.sh` third sibling vs. manual.
  **Network exposure is settled: never public.** Bind `127.0.0.1:8006`;
  operators use an SSH tunnel (`ssh -L 8006:127.0.0.1:8006 <host>`), or
  a WireGuard/Tailscale interface if tunnels don't scale to the operator
  count. No public DNS, no gateway route, not in the TLS cert. There is
  deliberately no code-level IP allowlist — behind a proxy it degrades
  into trusting a spoofable ``X-Forwarded-For``; the bind address and
  the network are the controls, and the app-level defenses (token class,
  TTL, rate limit, audit) are the second layer, not a substitute.
- **Dual control** — current design is single-operator + audit trail. If
  any action (large credit adjustments?) needs a second approver, that
  is a structural addition, decide it here rather than retrofit.

### Languages (`frontend-admin/i18n/`)

English and Chinese, on **next-intl with ppig's exact resolution
chain** — `NEXT_LOCALE` cookie → `i18n/request.ts` → catalogs under
`i18n/locales/{zh-CN,en-US}.json`, with `proxy.ts` picking from
`Accept-Language` on first visit and `POST /api/locale` writing the
cookie when an operator uses the picker in the sidebar. One team runs
both surfaces; a console that resolved locale its own way would be a
second thing to remember for no gain.

`zh-CN` is the default, matching ppig, but it is only reached when a
request carries no `Accept-Language` signal at all — a browser set to
English gets English on its first visit.

**Every string is in the catalogs**, including the ones that only
appear when something is wrong (the backend-unreachable screen, each
table's empty and error text). A missed key does not fail the build:
next-intl renders the key path, so the smoke asserts both renders of
the shell and that no `nav.` / `common.` path leaks into the HTML.
`npm run check:i18n` (ported from ppig unchanged) fails on any key
present in one catalog and missing from the other.

The locale cookie is deliberately **not** httpOnly — the picker reads
it back — and carries no authority; `staff_jwt` remains httpOnly and is
the only thing the guard trusts.

## Verified end to end (2026-08-06)

A full smoke against a **throwaway database** (`metaseq_admin_smoke`,
created → migrated → dropped; the dev DB untouched): the whole migration
chain replays on a fresh Postgres, both processes boot against it, and
76 checks drive every route over real HTTP **through the built console**
— the browser's path, so the cookie, the route-handler proxy and the
server-side guard are exercised, not bypassed. It grew with the
surface: 24 checks when only the backend existed, 46 once the frontend
drove it, 54 with message ratings, invites and check-ins, 59 with
platform skill upload, 67 with connector add/edit and skill
metadata editing, 70 with the check-in rankings, 72 with i18n, 73
with the grant split, 74 once the catalog's identity moved to its URL, 76 with the
localized-edit and replace-default rules.

What it covers: an anonymous page redirecting to `/login`, sign-in
(uniform 401 + no cookie on refusal, `httpOnly` on success), a real
ledger adjustment relayed through the proxy (duplicate-key 409,
double-entry verified in SQL), the soft-deleted session still readable,
feedback triage, the ratings queue with its excerpt and filter, a
minted invite code coming back `SYSTEM`-origin and ownerless, the
catalog's create/publish pair with **no secret-shaped field on the
wire**, and an audit trail holding one row per mutating action —
including `session.view`, naming the conversation's owner.

Two real bugs it caught that unit tests could not: reading the
server-computed ``updated_at`` back after a flush raised
``MissingGreenlet`` (fixed with the repo's post-commit ``refresh``
pattern), and a 422 body carrying a raw ``ValueError`` in pydantic's
``ctx`` failed to serialize — a latent fault on the **public** surface
too, fixed in the shared handler.

The seed builds one customer with the ledger rows a settled turn
actually writes (reserve → release → charge, plus a refunded pair that
must contribute nothing), so the spend predicate is asserted against
the real shape rather than a convenient one.

The skill upload is the one check with a side effect outside the
throwaway database: it PUTs a real bundle into the real OSS bucket, so
the smoke reads the keys back out of the audit trail and deletes them
in teardown. It earns that cost by being the only test that pushes
**binary** through the Next relay — the proxy read bodies as text,
which silently corrupts a zip, and nothing else in the stack would have
caught it.

Logging: the console writes ``logs/admin/`` — loguru's rotating file
sinks are not multi-process safe, so it must not share the
web/background siblings' ``logs/`` files. (Those two already share it —
a pre-existing wart, out of the console's scope.)

## Going live (runbook)

In order, all on the production host:

1. `uv run alembic upgrade head` — brings in `staff` + `admin_audit_log`
   (revision `c4a91f7d20b3`; no accounts are created).
2. `STAFF_PASSWORD='…' uv run python -m scripts.manage_staff --create
   --username=<op> --name="<Name>"` — first operator.
3. Set `admin_cors_origins` in `.env` to the console SPA's origin.
4. `uv run uvicorn app.admin_main:app --host 127.0.0.1 --port 8006`
   — loopback bind only; operators reach it via SSH tunnel / VPN. No TLS
   needed on the process (the tunnel encrypts).
5. `deploy.sh` has **no admin sibling yet** — start/stop is manual until
   that's added (its truth source would be `lsof -t -i:8006`).

## Guardrails (retired 2026-08-06)

The proposal-phase guardrails ("keep the migration unapplied; re-parent
if another migration lands first") are done — the schema is applied on
dev and further changes are ordinary migrations on top of the chain.
What survives them is the lesson recorded in the status note: once a
revision has siblings chained on top, converge schema drift with
targeted ALTERs, never a multi-step downgrade.
