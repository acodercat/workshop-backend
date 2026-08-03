# Admin console — design proposal

**Status: proposal.** The code in the tree is a working reference sketch
(compiles, 516 unit tests pass), but nothing is live: the migration is
intentionally **unapplied**, no staff account exists, and the `:8006`
process has never been started. Structure is under review; implementation
is deferred.

## Proposal vs. already-live

Be precise about this boundary — it is what makes the proposal cheap to
change.

**Proposal only** (deleting or reshaping any of it touches nothing that
runs):

- `app/api/admin/`, `app/services/admin/`, `app/schemas/admin/`
- `app/models/staff.py`, `app/models/admin_audit_log.py`
- `app/admin_main.py`, `scripts/manage_staff.py`
- migration `c4a91f7d20b3` (unapplied)
- config knobs `admin_cors_origins`, `admin_access_token_expire_minutes`,
  `rate_limit_admin_login`
- the staff token section of `app/auth.py` (present but dead code — no
  public route depends on it)

**Already affects the running backend** (kept on their own merits,
independent of the console):

- `app/asgi.py` factory + `app/main.py` rewired onto it
- `app/api/endpoints/` → `app/api/public/` rename + `public/router.py`
- rate-limit principal (`set_rate_limit_principal`)
- `WWW-Authenticate: Bearer` on 401 `AppError`s

## Load-bearing decisions (expensive to reverse once implemented)

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
3. **Role model is a linear ladder** — `VIEWER < OPERATOR < OWNER`.
   Per-resource permissions were deliberately deferred; they can be added
   later with a table, additively. Reads gate on VIEWER, money moves on
   OPERATOR, staff management on OWNER.
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
8. **Native PG enums** for `staff_role` / `staff_status` (repo
   convention). Note the cost: adding a role later is
   `ALTER TYPE … ADD VALUE`; removing one is effectively never. If role
   churn is expected, plain VARCHAR is the escape hatch — decide before
   first apply.

## Open before implementation

- **The v1 feature list**, which fixes the real `AuditAction` vocabulary
  and each action's minimum role: user lookup? `credit.adjust`?
  `withdrawal.approve/reject`? `invite.create`? `skill.publish`?
- **Console frontend** — the CORS default assumes an SPA at `:3400`. It
  must sit behind the same network boundary as the API: a public SPA
  against a private API still advertises the console, and CORS is not a
  security control.
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

## Guardrails while this is a proposal

- **Do not run `alembic upgrade head`.** `c4a91f7d20b3` must stay the
  unapplied tip; schema changes are free only until first apply.
- **If an unrelated migration is needed meanwhile**, re-parent: point
  the new migration at `d3b7f5a1c682` and move `c4a91f7d20b3`'s
  `down_revision` to follow it. Otherwise the next `upgrade head` drags
  the proposal schema into the database.
- Don't provision staff accounts or start the `:8006` process.
