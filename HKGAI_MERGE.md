# HKGAI Merge Guide (backend)

**Audience**: future AI / engineer syncing this repository with the metaseq backend.
Read this **before** merging.

## What this fork is

This repository (`workshop-backend`, branch `master`) is the HKGAI deployment's fork of the
**metaseq** backend (`axon-backend`, branch `master`). Same code skeleton; diverges in four
buckets that must NOT be synced from metaseq:

1. **Email transport = SMTP (Lark Suite)** — `app/services/email/client.py` dispatches via
   `aiosmtplib`, NOT metaseq's Aliyun DirectMail (`alibabacloud_dm20151123`). The `config.py`
   email knobs are `smtp_host/port/use_ssl/username/password/from_address` (not `aliyun_dm_*`),
   and the dep is `aiosmtplib` (not `alibabacloud-dm*`). Templates carry the HKGAI brand
   ("HKGAI Agent", 香港生成式人工智能研发中心).
2. **Model / tier config** (`config.py`) — `supported_session_model_ids` includes `glm-5.1`
   and `fable` (metaseq omits both); `session_tier_models` maps **FAST=deepseek-v4-flash,
   STANDARD=glm-5.2, ADVANCED=deepseek-v4-pro** (metaseq uses FAST=glm-5.2, etc.). Pricing in
   `credit_rates` must have an entry for every whitelisted model (incl. `glm-5.1`).
3. **Brand + deploy** — "HKGAI Agent" / 香港生成式人工智能研发中心 wherever they appear;
   `scripts/deploy.sh` (hkgai uvicorn worker count).
   OSS buckets / domains are `.env`-driven (Huawei OBS `agent-workshop-*` / `.hkgai.net`), so
   `config.py`'s metaseq-flavored defaults are harmless — env overrides at runtime.

4. **No payment / commission / withdrawal.** HKGAI does not sell credits, so metaseq's
   汇付 (Huifu) recharge, the referral-commission wallet, and withdrawals are **excluded
   application-side**. See "Excluding payment" below — this one is not a conflict you resolve,
   it is a deletion you must remember to make.

**Nothing flows from this repository back to metaseq.** Deployment patches live here only.

## Merge direction (the only valid flow)

```
metaseq: develop on axon-backend master → push
                        ↓
this repository: sync branch from master → merge metaseq master (--no-ff) → checks → fast-forward master → push
```

The two repositories share their history, so a sync is a real merge: fetch metaseq's `master`
into this repository and merge `FETCH_HEAD`, on a branch of its own so `master` stays untouched
until every check below has passed. Always `--no-ff`, so each sync stays one visible merge commit
(`git log --merges --grep="sync hkgai"`). Where this guide says "metaseq's version" of a file,
that is `FETCH_HEAD`'s. Merge commits up to 2026-11-04 are titled `sync hkgai with master` —
`hkgai` was a branch of `axon-backend` then, merged from its `master`.

**A port must still be recorded as a merge.** When metaseq work comes over by porting —
cherry-picks or a hand-adapted commit such as `ef1fe63` — the merge base does not move, and the
next sync replays every ported commit as a conflict against its own port: in the 2026-11 sync,
replaying 14 already-ported commits conflicted in 45 files, while the 13 new ones conflicted in one. After the port, once the tree differs from the
ported metaseq commit only by this fork's deltas (`git diff <metaseq-commit> HEAD --stat`),
record it with `git merge -s ours <metaseq-commit>`: two parents, no file changes, and the base
advances. `4da59ef` is the example, and its message says what was checked and what was
deliberately not taken (`e47c14a`, metaseq's brand).

## What MUST be preserved — run these probes after merge, before push

| Check | Command | Expected |
|---|---|---|
| No Aliyun DirectMail in app | `grep -rniE "alibabacloud_dm\|DmClient\|aliyun_dm" app/` | empty (`alibabacloud_dysmsapi` is the **SMS** SDK and is legitimate — do not grep bare `alibabacloud`) |
| Email client = SMTP | `grep -c aiosmtplib app/services/email/client.py` | ≥1 |
| Email dep in lockfile | `grep -c 'name = "alibabacloud-dm' uv.lock` and `grep -c 'name = "aiosmtplib"' uv.lock` | 0 DirectMail, ≥1 aiosmtplib |
| Email brand | `grep "HKGAI Agent\|香港生成式人工智能研发中心" app/services/email/templates.py` | present |
| Model whitelist | `grep -E "glm-5.1\|fable" config.py` | both present |
| Tier mapping | `grep -A4 session_tier_models config.py` | `"FAST": "deepseek-v4-flash"` |
| No phone / SMS channel | `grep -rniE "phone\|sms\|dysms" app/ config.py` | Only the comment in `models/user.py` saying the field is gone. The channel was removed in `5fcf505` and the column in `b7e2d9a4c1f5`; a `phone:` field reappearing on the model is a merge bringing back a column this database does not have |
| SMS dep gone | `grep -c 'dysmsapi' pyproject.toml uv.lock` | 0 in both |
| OAuth client name | `grep -A1 "mcp_client_name" config.py` and `grep -c _CLIENT_NAME app/services/mcp/oauth.py` | `default="HKGAI Connector"` in `config.py`, and **0** in `oauth.py`. This is the name an authorization server prints on its consent screen, so a wrong value is visible only to the user and to nothing else. It moved out of `oauth.py` precisely so the conflict lands in `config.py`, where the two branches diverge by design — metaseq's default reads `PPIG Connector`, and taking metaseq's side here is the mistake this row exists to catch. A `_CLIENT_NAME` constant reappearing in `oauth.py` means a merge undid the move |
| Migration head | `uv run alembic heads` | **One head, after you resolve it.** This branch owns revisions of its own, so a sync that brought new metaseq revisions can leave two siblings: `uv run alembic merge heads -m "sync"`, once, then one head again. Two heads at deploy time is the failure this row exists to catch — but it is not the only chain failure a sync causes, and `merge heads` does not answer the other one. Read "⚠️ Alembic migration chain" before touching `alembic/versions/` |
| Removed schema stays removed | `ls alembic/versions/b7e2d9a4c1f5_*.py` | exactly one file. It drops `payment_order` / `commission_ledger` / `withdrawal_request`, their four enum types, `user.commission_balance_fen` and `user.phone` — a merge that loses the file loses all of it. **A metaseq revision that alters any of them will fail this fork's `upgrade`** — about one in ten historically. A later revision cannot undo an earlier one's failure, so the guard goes **in the revision itself, in this fork** — see `e2b7d4f9a1c6` **and** `a1c7f3e9d2b6` — one change written once on each branch, so both carry the same guard, because which of them runs first is not fixed (the deployed database applied one order, an empty one takes the other) — and `a9c4e2f7d1b8`, which aligns a `user.phone` this fork dropped. Each carries its own reasoning; read the guard, not this row |
| No payment code | `grep -rn "services.payment\|services.commission\|dg_sdk" app/ scripts/ tests/` | empty |
| No payment routes | `uv run python -c "from app.public_main import app; print([p for p in app.openapi()['paths'] if 'payment' in p or 'commission' in p])"` | `[]` |
| No 汇付 dep | `grep -c 'dg-sdk' pyproject.toml uv.lock` | 0 in both |
| App imports | `uv run python -c "import app.public_main"` and `uv run python -c "from app.admin_main import app; print([p for p in app.openapi()['paths'] if 'payment' in p or 'audit' in p])"` | clean, and `[]`. Both import since the 2026-09-14 sync (metaseq committed the console's routers). `public_main` is what `deploy.sh` starts; the admin probe is what catches a payment or audit router coming back |
| No audit funnel | `grep -rn "audit(\|AuditAction\|AdminAuditLog\|admin_audit_log" app/api app/services tests --include=*.py \| grep -v '\`\`'` | empty. The filter drops this fork's own docstrings, which name the removed funnel in double backticks to explain its absence. See "Excluding the operator trail" below |
| Scheduled tasks carried whole | `grep -c schedule_dispatcher app/workers/run.py`, `grep -c payment_reconciler app/workers/run.py`, and `uv run alembic history -r f4c8a2b91d63:head` | ≥1, **0**, and the chain `f4c8a2b91d63 → c8b3e5f1a742 → d4a1e8b62f37 → b5e9d3f70a26 → e1c6a8d40b73 → a9e4c2d71f38` (or later on top of it). Metaseq's `run.py` starts the payment reconciler next to the dispatcher, so that file conflicts every time the dispatcher's lines move — take the dispatcher, drop the reconciler. Also `grep -rn PAYRECON app/` → 0: metaseq's `schedule_dispatcher.py` names the reconciler's lock key beside its own. See "Scheduled tasks" below for the chain |
| Tests | `uv run pytest tests/` | green |

If any probe fails, metaseq's version leaked over a hkgai delta — fix before pushing.

## Common conflict patterns + resolution

### `config.py` (the biggest)
Take metaseq's **new** fields (e.g. `oss_preview_domain`, `max_session_files_*`, new model
entries) BUT keep hkgai's: email (`smtp_*`, drop any `aliyun_dm_*`), model whitelist
(`glm-5.1`/`fable`), and `session_tier_models` (FAST=deepseek-v4-flash / STANDARD=glm-5.2 / ADVANCED=deepseek-v4-pro). For a
**shared** model's price in `credit_rates`, metaseq is canonical (converge); for a hkgai-only
model (`glm-5.1`), keep hkgai's entry — every whitelisted model needs a rate or reserve raises.

### `app/services/email/*` + `pyproject.toml` + `uv.lock`
Keep hkgai's SMTP/Lark entirely; reject metaseq's Aliyun DirectMail client + its dep. Metaseq
rarely touches email, so these usually auto-resolve to hkgai (no conflict) — verify with the
probes above regardless. The single email seam is `app/services/auth/verification.py`
(imports `send_html_email` / `render_*` from the `app.services.email` abstraction). It is
**metaseq's file** — it has since grown a channel-agnostic email/phone OTP flow, so take metaseq's
whole version and restate the transport as SMTP; the seam it calls is unchanged, so hkgai's SMTP
client backs it either way. Aliyun **SMS** (`alibabacloud-dysmsapi`, `app/services/sms/`) is a
different service from DirectMail and comes in with the phone channel — keep it.

### Parallel-developed functional code → converge to metaseq
metaseq and hkgai sometimes both touch the same infra (SSE `/me/events` fan-out — `user/hub.py`,
`user/events.py`, `main.py`, `settle_subscriber.py`; redis socket_timeout — `redis.py`; skills
bundle — `skill_bundle.py`; the credit ledger, invite-code rewards, daily check-in). These are
functional, not deployment — **take metaseq's** on conflict (`git checkout --theirs <file>` when
the whole file is functional). Surface a genuinely-divergent one rather than guessing.

**"credits/billing → take metaseq's" does not extend to payment.** The credit *ledger* converges;
the code that *sells* credits does not. See the next section.

### Aggregate routers — the third thing a plain merge gets wrong

`app/api/public/router.py` and `app/api/admin/router.py` arrive whole, and
both `include_router` the payment / commission halves. hkgai has never held
those modules, so git sees a pure addition and gives you **nothing to
resolve** — the same silence the payment files themselves used to arrive in.
Strip the imports and the `include_router` lines by hand after every merge
that touches either file; `import app.public_main` is what catches a miss.

Done for `app/api/admin/router.py` at the 2026-09-14 sync, when metaseq
committed the console's routers and `import app.admin_main` started to
work: `payment` is gone from its imports and its `include_router`. The
admin-import probe in the table is what catches it coming back.

### `scripts/deploy.sh`
Keep hkgai's (`--ours`).

### `scripts/seed_users.py`
Deleted on the branch that signs in through Meeting (`office`): it inserted password accounts, and an
account is now created only by a first sign-in through Meeting
(`app/services/identity/provisioning.py`). If a merge brings it back, delete it again.

### `CLAUDE.md`
Take metaseq's flow description, but swap the email transport back to SMTP / Lark Suite
(`aiosmtplib`) and keep the SMTP creds / Lark-cap notes.

## Excluding the operator trail — `audit()` on the console

This deployment keeps no `admin_audit_log` (`b460116`, revision
`c8f2a5e1d94b`). Metaseq's console writes every mutation through
`async with audit(ctx, action=AuditAction.…) as draft:`, which also owns
the commit. **New admin endpoints arrive as pure additions carrying it**,
so nothing conflicts and the first request fails on a missing module.

After every merge that touches `app/api/admin/`:

1. `git rm` metaseq's `app/api/admin/audit.py`, `app/schemas/admin/audit.py`,
   `app/services/admin/{audit,actions}.py`, `app/models/admin_audit_log.py`,
   `tests/test_admin_audit.py` (modify/delete conflicts once deleted).
2. In each endpoint, replace the `async with audit(...)` block with its
   body and end the write with `await ctx.db.commit()`; drop the
   `draft.*` lines and the `AuditAction, audit` import. A change set built
   only to fill `draft.payload` becomes a set of changed field names.
   Keep `app/services/admin/__init__.py`, `app/services/admin/context.py`
   and `app/api/admin/dependencies.py` at this fork's versions (no
   `request_id` / `ip_address`, no `build_context`).
3. If the payload was the *only* record of something, log it instead —
   the skill upload's displaced `bundle_key` is logged at INFO.
4. Tests: drop assertions on audit rows / payloads, keep the behavioural
   ones and assert `db.commits`. `tests/test_admin_mutations_commit.py`
   is this fork's own and stays. Tests that exist only for the trail go
   whole: metaseq's `tests/test_admin_staff_update.py` (a no-op PATCH writes
   no audit row) and the forwarded-hop case in `tests/test_admin_dependencies.py`
   (the parse that keeps `admin_audit_log.ip_address` within its width).
5. Docs: `docs/admin-console.md` route tables have no `Audit` column here.

## Excluding the phone channel — the second thing a plain merge gets wrong

Metaseq registers and logs users in by **email or phone**, the phone half
delivered by Aliyun SMS. This branch is email-only, so a merge that touches
auth needs the same treatment payment gets: the code comes back silently
because it is *addition*, not conflict.

| File | Edit |
|---|---|
| `app/schemas/auth.py` | `_IdentifierBody` carries `email` alone — no `phone`, no `channel`/`identifier` properties, no "exactly one" validator |
| `app/services/auth/verification.py` | one channel: no `Channel` type, no `_sms_sender`, keys come straight from `email_verify_key` |
| `app/services/auth/{login,register}.py` | look up `User.email` directly; no `channel` parameter |
| `app/api/endpoints/auth.py` | pass `identifier=body.email` |
| `app/api/endpoints/user.py` | no `/me/phone` endpoints |
| `app/services/auth/email_change.py`, `sudo.py` | metaseq folds email and phone change into `credential_change.py` and lets `request_sudo_code` pick a channel. Here `email_change.py` stays — taking `proof: SudoProof` — and `sudo.py` sends the code to `user.email`. A merge brings `credential_change.py` and `tests/test_credential_change.py` back as new files: fold any change into `email_change.py` / `tests/test_email_change.py` and delete them |
| `app/schemas/user.py` | `SudoCodeRequest` carries `locale` alone — no `channel` |
| `app/errors.py` | no `PhoneTakenError` / `SamePhoneError` / `SmsSendFailedError` |
| `app/services/redis.py` | no `phone_verify` / `phone_change` prefixes or key builders |
| `config.py` | no `aliyun_sms_*` block — note metaseq's default `SignName` is the **水杉智算** brand |
| `app/api/admin/user.py`, `app/schemas/admin/user.py` | the directory row has no `phone`, and `q` matches username / email only. **Arrives without a conflict** (new files) and fails only at runtime, on the first `GET /users` |
| deleted outright | `app/services/sms/`, `app/common/phone.py`, `app/services/auth/phone_change.py`, `scripts/test_send_verify_sms.py`, `tests/test_phone_normalization.py` |

`user.phone` **goes too**, in `b7e2d9a4c1f5` — along with its partial
`UNIQUE` index, which Postgres drops with the column. That revision is
this fork's first of its own; before it, the column had to stay because
dropping one is a migration and there was nowhere to put it. Metaseq keeps
the field, so `user.py` is a merge conflict every time and taking metaseq's
side there reintroduces a column this database does not have.

## Excluding payment (充值) — the one thing a plain merge gets wrong

**A plain merge brings the whole payment stack in, with zero conflicts.** hkgai has never held
those files, so git sees pure additions and gives you nothing to decide. Excluding them is a
**deletion you perform after the merge**, not a conflict you resolve. Only the first time is
silent: once hkgai carries the deletions, a later metaseq change to any of these paths arrives as
a modify/delete conflict — resolve it `git rm`.

Excluded (delete outright; nothing kept imports them):

```
app/services/payment/            app/services/commission/
app/api/endpoints/payment.py     app/api/endpoints/commission.py
app/schemas/payment.py           app/schemas/commission.py
app/models/{payment_order,commission_ledger,withdrawal_request}.py
app/workers/payment_reconciler.py
app/api/admin/payment.py         app/schemas/admin/payment.py
app/services/payment/analytics.py
scripts/{process_withdrawals,recover_paid_orders}.py   scripts/gen_huifu_keys.sh
tests/test_payment_*.py  tests/test_commission_*.py  tests/test_huifu_adapter.py
```

The three admin-side files arrived at the 2026-09-14 sync as **pure
additions** (no conflict): the same silent shape as the first exclusion.

Then the edits deletion alone does not cover:

| File | Edit |
|---|---|
| `app/main.py` | drop `commission`, `payment` from the import block and from `ROUTERS` |
| `app/workers/run.py` | drop the `payment_reconciler` import, its `create_task`, its docstring bullet; task count **six → five** (settle sweeper, subscriber, schedule dispatcher, message queue dispatcher, budget enforcer); startup log string |
| `app/models/user.py` | drop `commission_balance_fen` (the DB column stays — see below) |
| `app/errors.py` | drop the 8 payment/commission/withdrawal error classes; the domain list in the module docstring |
| `config.py` | drop the `# ── Payments` and `# ── Referral commission` sections, and `credit_bonus_rate` (its only reader was order creation) |
| `app/services/credits/rates.py` | drop `recharge_bonus_credits` (same — no caller left) |
| `app/services/credits/topup.py` | `TopupSource` is `welcome_grant` / `admin` — metaseq adds `payment` / `commission_conversion` |
| `app/services/credits/analytics.py` | no `_SOLD_ROWS`: `_FREE_GRANT_ROWS` is `_GRANT_ROWS`. Metaseq's reads `payment_order`, which **arrives without a conflict** and fails the dashboard's summary query at runtime, on a table `b7e2d9a4c1f5` dropped |
| docs | `CLAUDE.md` two sections + cross-refs; `README.md` background-process section; `docs/credits.md`; `docs/credits-future.md` "Shipped — Payment integration" |

**Kept on purpose:** `credit_price_cny` and `rates.credits_from_cny` — the admin
`scripts/topup_credits.py --cny` uses them. Invite-code rewards and daily check-in are
**in scope**; only the money-in and cash-out halves are cut.

### Migrations are NOT excluded

All of metaseq's alembic revisions come across **unchanged**, including the three that create
`payment_order` / `commission_wallet` / the commission CHECK constraints. hkgai's database
therefore carries three permanently-empty tables and an always-zero `user.commission_balance_fen`.

That is deliberate. Those revisions are **interleaved** with wanted ones, not a contiguous block,
so excluding them would mean rewriting three `down_revision` links, surgically editing one
revision whose body mixes a `withdrawal_request` change with two unrelated index fixes
(`e1c5a9d7b3f4`), and re-doing all of it on every future sync — with `alembic upgrade head`
in production as the failure mode. Three empty tables are the cheaper side of that trade.

Consequence: **never run `alembic revision --autogenerate` on hkgai.** `Base.metadata` no longer
describes those tables, so autogenerate would emit `DROP TABLE` for them. Migrations come from
metaseq, always — and a couple of their docstrings still point at `app/services/commission/`.
That is expected: a revision is a historical record, and editing one is the fork this whole
trade exists to avoid.

### Brand check the *new* templates, not just the conflicted ones

A template hkgai already had arrives as a conflict and `--ours` keeps its branding. A template
metaseq **adds** merges in silently, carrying metaseq's brand — the product name *and* the company
footer (`水杉智算技术有限公司` / `Metasequoia Technology Co., Ltd.`). After every merge:
`grep -c "水杉\|Metasequoia\|PPIG" app/services/email/templates.py` must be **0**.

## ⚠️ Alembic migration chain

After the merge run `uv run alembic heads`. It should be a **single** head (metaseq's + hkgai's
migrations chain linearly). If there are **two**, create a join revision:
`uv run alembic merge -m "merge heads" <head1> <head2>`. Then `uv run alembic upgrade head`
must succeed. (Migrations are separate files, so they don't text-conflict — the risk is a
divergent DAG, not a merge conflict.)

**hkgai does own revisions of its own** — this section used to say it did not, contradicting
the migration-head probe row above, which is the one that is right. As of 2026-09-07 they are
`b7e2d9a4c1f5` (drops the removed subsystems' schema), `a1c7f3e9d2b6` (the OAuth-catalog twin),
`cd91a1a5179a` (a merge revision), `a3f9c1d7e2b4` (see below) and `c8f2a5e1d94b`
(drops `admin_audit_log`). Since then: `b6c1e83f27d4` / `c5e1a8f3b92d` (twins, below) and
`d8a4f1c6e290` (`session_files` unique per session — ahead of metaseq; metaseq's
`e1c6a8d40b73` does the same change and is edited here, see "Scheduled tasks").

### Revisions deliberately not taken, and re-points (2026-09-14 sync)

After that sync the chain reads
`… c5e1a8f3b92d → d8a4f1c6e290 → a7c3f1d9e4b2 → c2f8a41b7e60 → d5b1e93f7a48 → f4c8a2b91d63`.

| Metaseq revision | What it does | On hkgai |
|---|---|---|
| `f3a7c9e15d82` | `skill.status` | **not taken** — byte-identical `upgrade()`/`downgrade()` to this fork's `b6c1e83f27d4`, applied here first. Taking both adds the columns twice |
| `a7d2f4b9c613` | backfill picked skills as preferences | **not taken** — identical twin of `c5e1a8f3b92d` |
| `b6d2f8a04c17` | drops `admin_audit_log.reason` | **not taken** — the table is gone (`c8f2a5e1d94b`) |
| `b7e2d4c8a915` | `payment_order.reconcile_attempted_at` | **not taken** — `payment_order` is gone (`b7e2d9a4c1f5`) |
| `a7c3f1d9e4b2` | `actor` table | taken, **re-pointed** onto this fork's head (upstream parent `a1f4c7e02b31` sits behind revisions this database may already stand on) |
| `c2f8a41b7e60` | skill / connector categories | taken, **re-pointed** past `b6d2f8a04c17` |
| `d5b1e93f7a48`, `f4c8a2b91d63` | two indexes | taken as-is (their parents came across) |

Every re-pointed or edited file carries an "hkgai note" in the docstring; do not
"correct" them back. The next sync will see metaseq's children of
`a7d2f4b9c613` (or later) — hang them off this fork's head the same way,
after reading what each intervening metaseq revision does.

`alembic upgrade base:head --sql` does not render in this fork and did
not before this sync: `e2b7d4f9a1c6`'s guard inspects the live schema,
which offline mode cannot. Verify offline from a real revision instead
(`c5e1a8f3b92d:head --sql`), or against a throwaway database.

### Scheduled tasks (2026-09-14, second sync)

Excluded at the first sync of that day, carried whole at the second. Their
migrations did not come across as a clean run — upstream they hang off
revisions this fork never took, and one of them repeats a change this
branch made first. After the sync the chain reads
`… f4c8a2b91d63 → c8b3e5f1a742 → d4a1e8b62f37 → b5e9d3f70a26 → e1c6a8d40b73 → a9e4c2d71f38`.

| Metaseq revision | What it does | On hkgai |
|---|---|---|
| `c8b3e5f1a742` | `schedule` + `schedule_run` | taken, **re-pointed** onto `f4c8a2b91d63` (upstream parent `b7e2d4c8a915` alters `payment_order`, never taken) |
| `d4a1e8b62f37` | `schedule_run.user_id` index | taken as-is |
| `b5e9d3f70a26` | `schedule.skills`, `schedule_mcp_server` | taken, **re-pointed** onto `d4a1e8b62f37` (upstream parent `a7d2f4b9c613` is the not-taken twin above) |
| `e1c6a8d40b73` | upstream: the `session_files` constraint change **and** `schedule_file` | taken, **edited in place**: only `schedule_file` remains. The `session_files` half is this fork's `d8a4f1c6e290`, always an ancestor here, so upstream's body would fail on its first statement. Edited rather than guarded — the order is fixed, and a schema-inspecting guard would break offline `--sql` rendering |
| `a9e4c2d71f38` | drops `schedule.last_run_at` | taken as-is |

The application code converges to metaseq, including the file errors: they
are `AppError`s in `app/errors.py` now (`file_not_owned`, `file_not_uploaded`,
`file_too_large`, `connector_not_owned`), which the dispatcher maps to skip
reasons — keeping this fork's older `SessionServiceError` copies would leave
it catching classes the binder no longer raises. The dispatcher is always on
and spends credits through the ordinary enqueue; this deployment does not sell
credits, so an owner's balance comes from invites and check-in only.

### Projects (2026-11)

`d3a8f5c1e7b2` (project + project_file, session.project_id) → `f2c7b9e4a1d6`
(session_context.project_snapshot) → `b8e2d4f6a3c1` (project.icon) → `c7d2e9a4b1f3` (drops the
`AGENT` file origin) — taken as-is: the first one's parent, `0e2330db080c`, was already this
branch's head. **`core/agent-worker` depends on all four**: its claim joins `project` and selects
`session_context.project_snapshot`, so a worker at or past its project support fails every claim
against a database without them. Apply them before (or with) that worker.

### Message queue, pins, sidebar layout, schedules in projects (2026-11-25)

Eight revisions, one straight run rooted at this fork's head `c7d2e9a4b1f3`, all taken as-is —
none touches a table or column this fork removed:
`d861ee8ff0ea` (`session.pinned_at`) → `a4e1c8f27b90` (`session_turn.timezone`) →
`f4d8b2a6c931` (drops `schedule_file`; this fork's edited `e1c6a8d40b73` created it, so the drop
applies) → `c2f7a9e4b518` (`queued_message`) → `9bd99073dc16` (`project.pinned_at`,
`sidebar_layout`) → `e1f4a7c2b9d3` → `413c0bcf9230` (steering a running turn) →
`e09400a96810` (`schedule.project_id`). Verified against a throwaway database: upgrade from
`c7d2e9a4b1f3`, downgrade back, upgrade again.

Both sides depend on them. The frontend's queued messages, steering, pins, sidebar order and
schedules in a project call endpoints that need these tables; **`core/agent-worker` reads
`queued_message`, `session_turn.timezone` and `schedule.project_id`** in its claim and its
`create_schedule` tool, so a worker at or past that support fails against a database without
them. Run `alembic upgrade head` before (or with) that worker and the frontend.

### Two heads is not the only way a sync breaks the chain

`alembic merge heads` answers the case where an upstream revision's parent is on a *sibling*
branch. It does **not** answer the case where the parent is **absent from this fork
entirely** — then `alembic upgrade head` fails with `Can't locate revision identified by
'<parent>'`, and there are no two heads to merge.

That is what happened with `a3f9c1d7e2b4` (the worker's workspace-generation columns): upstream
it hangs off `f4c8a2b91d63`, one of six trunk revisions this fork never took. The fix is to
**re-point `down_revision` at this fork's head and keep the upstream revision id**, so the two
chains stay comparable. Say so in the file, or the next sync "corrects" it back.

Bringing the whole intervening trunk run over instead — it is linear and rooted exactly at this
branch's head, so it looks tidier — is the trap. `b6d2f8a04c17` sits in the middle of it and
drops `admin_audit_log.reason`, and at the time that column was live here. Check what each
intervening revision *does* before taking a chain for the shape of it.

### Never overwrite `b6e3d9a70f52_mcp_server_resources_enabled.py` from metaseq

It is the one revision id both branches carry with a **different** `down_revision`: here its
parent is this fork's merge revision `cd91a1a5179a`, upstream it is `f2c8b04e5da7`. Taking
metaseq's copy orphans the merge revision and drops `a1c7f3e9d2b6` and `b7e2d9a4c1f5` out of the
graph with it.

### The probes only look one way

Everything above asks "did metaseq's version leak over a hkgai delta". Nothing asks **"have we
fallen behind on something outside this repo that now needs a schema change"** — and the
`agent-worker` this deployment runs is outside this repo. It is `core/agent-worker`, shared with
the metaseq deployment and pinned to nothing; when it grew the workspace-generation feature its
`claim_next_turn` began selecting four columns that did not exist here, with no feature flag, so
every turn claim would have failed. Add to the sync checklist: **read
`core/agent-worker`'s log since the last sync for anything the backend owns the schema of**
(`session`, `session_turn`, `sandbox`, `session_context`, `message`).

## Step-by-step workflow

```bash
# In this repository, from a clean, up-to-date metaseq
git checkout master && git pull --ff-only origin master
git checkout -b sync/metaseq-master-<date>

git fetch <axon-backend> master       # a local checkout's path, or the axon-backend URL
git merge-base HEAD FETCH_HEAD        # the metaseq commit the last sync took
git merge --no-ff FETCH_HEAD -m "merge: sync hkgai master with metaseq master (<short summary>)"
# resolve conflicts per the rules above
uv sync                               # pick up any dep changes the merge brought
# run ALL the probes in "What MUST be preserved", ruff, mypy, pytest,
# and the new revisions against a throwaway database

git commit                            # finishes the merge
git checkout master && git merge --ff-only sync/metaseq-master-<date>
git push origin master
git branch -d sync/metaseq-master-<date>
```

## Hot spots — where metaseq changes most often collide with hkgai

1. `config.py` (email + model/tier + new fields — every merge that touches config)
2. `app/services/email/*`, `pyproject.toml`, `uv.lock` (SMTP vs Aliyun)
3. `app/services/{redis,user/events,user/hub,skill_bundle}.py`, `app/main.py` (parallel infra)
4. `scripts/deploy.sh`; `scripts/seed_users.py` — deleted here, so an edit to it arrives as a
   modify/delete conflict; resolve `git rm`
5. `CLAUDE.md`
6. Anything under `app/services/payment/` or `app/services/commission/` on metaseq — arrives as a
   modify/delete conflict; resolve `git rm`

## Tier labels name no model

The frontend labels the tiers `快速 / 标准 / 高级` (`Fast / Standard / Advanced`) with no model
name — the same as the metaseq frontend. It used to read "高级（Fable）" while this file mapped ADVANCED to a
different model; the frontend dropped the name (project-mars `ecd6ef5`) rather than chase it.
Which model a tier runs is decided here, in `session_tier_models`, and nowhere else — so a change
to that mapping needs no frontend edit. The frontend guide carries a probe that fails if a model
name comes back into a tier label.

## When in doubt

- `git diff FETCH_HEAD HEAD -- <file>` shows ONLY this fork's delta for a file (after `git fetch <axon-backend> master`).
- After a merge, `git diff FETCH_HEAD HEAD --stat` should list **only** the preserve-set
  files (email/SMTP, config, brand, scripts, lockfile) plus the payment/commission deletions —
  any *other* functional file there means you reverted a metaseq change by mistake.
- `git rev-list --count HEAD..FETCH_HEAD` must be `0` (every metaseq commit is contained).
