# HKGAI Merge Guide (backend)

**Audience**: future AI / engineer doing `master → hkgai` merges on the backend
(`axon-backend`). Read this **before** touching the hkgai branch.

## What `hkgai` is

A deployment fork of `master` for the HKGAI deployment. Same code skeleton; diverges in four
buckets that must NOT be synced from master:

1. **Email transport = SMTP (Lark Suite)** — `app/services/email/client.py` dispatches via
   `aiosmtplib`, NOT master's Aliyun DirectMail (`alibabacloud_dm20151123`). The `config.py`
   email knobs are `smtp_host/port/use_ssl/username/password/from_address` (not `aliyun_dm_*`),
   and the dep is `aiosmtplib` (not `alibabacloud-dm*`). Templates carry the HKGAI brand
   ("HKGAI Agent", 香港生成式人工智能研发中心).
2. **Model / tier config** (`config.py`) — `supported_session_model_ids` includes `glm-5.1`
   and `fable` (master omits both); `session_tier_models` maps **FAST=deepseek-v4-flash,
   STANDARD=glm-5.2, ADVANCED=deepseek-v4-pro** (master uses FAST=glm-5.2, etc.). Pricing in
   `credit_rates` must have an entry for every whitelisted model (incl. `glm-5.1`).
3. **Brand + deploy** — "HKGAI Agent" / 香港生成式人工智能研发中心 wherever they appear;
   `scripts/deploy.sh` (hkgai uvicorn worker count), `scripts/seed_users.py` (hkgai dev seed).
   OSS buckets / domains are `.env`-driven (Huawei OBS `agent-workshop-*` / `.hkgai.net`), so
   `config.py`'s metaseq-flavored defaults are harmless — env overrides at runtime.

4. **No payment / commission / withdrawal.** HKGAI does not sell credits, so master's
   汇付 (Huifu) recharge, the referral-commission wallet, and withdrawals are **excluded
   application-side**. See "Excluding payment" below — this one is not a conflict you resolve,
   it is a deletion you must remember to make.

**`hkgai → master` does NOT happen.** Deployment patches live on `hkgai` only.

## Merge direction (the only valid flow)

```
develop on master → push origin/master
                        ↓
                  --no-ff merge into hkgai → push origin/hkgai
```
Always `--no-ff` so the merge commit stays visible (`git log --merges origin/hkgai`).

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
| OAuth client name | `grep -A1 "mcp_client_name" config.py` and `grep -c _CLIENT_NAME app/services/mcp/oauth.py` | `default="HKGAI Connector"` in `config.py`, and **0** in `oauth.py`. This is the name an authorization server prints on its consent screen, so a wrong value is visible only to the user and to nothing else. It moved out of `oauth.py` precisely so the conflict lands in `config.py`, where the two branches diverge by design — master's default reads `PPIG Connector`, and taking master's side here is the mistake this row exists to catch. A `_CLIENT_NAME` constant reappearing in `oauth.py` means a merge undid the move |
| Migration head | `uv run alembic heads` | **One head, after you resolve it.** This branch owns revisions of its own, so a sync that brought new master revisions can leave two siblings: `uv run alembic merge heads -m "sync"`, once, then one head again. Two heads at deploy time is the failure this row exists to catch — but it is not the only chain failure a sync causes, and `merge heads` does not answer the other one. Read "⚠️ Alembic migration chain" before touching `alembic/versions/` |
| Removed schema stays removed | `ls alembic/versions/b7e2d9a4c1f5_*.py` | exactly one file. It drops `payment_order` / `commission_ledger` / `withdrawal_request`, their four enum types, `user.commission_balance_fen` and `user.phone` — a merge that loses the file loses all of it. **A master revision that alters any of them will fail this branch's `upgrade`** — about one in ten historically. A later revision cannot undo an earlier one's failure, so the guard goes **in the revision itself, on this branch** — see `e2b7d4f9a1c6` **and** `a1c7f3e9d2b6` — one change written once on each branch, so both carry the same guard, because which of them runs first is not fixed (the deployed database applied one order, an empty one takes the other) — and `a9c4e2f7d1b8`, which aligns a `user.phone` this branch dropped. Each carries its own reasoning; read the guard, not this row |
| No payment code | `grep -rn "services.payment\|services.commission\|dg_sdk" app/ scripts/ tests/` | empty |
| No payment routes | `uv run python -c "from app.public_main import app; print([p for p in app.openapi()['paths'] if 'payment' in p or 'commission' in p])"` | `[]` |
| No 汇付 dep | `grep -c 'dg-sdk' pyproject.toml uv.lock` | 0 in both |
| App imports | `uv run python -c "import app.public_main"` and `uv run python -c "from app.admin_main import app; print([p for p in app.openapi()['paths'] if 'payment' in p or 'audit' in p])"` | clean, and `[]`. Both import since the 2026-09-14 sync (master committed the console's routers). `public_main` is what `deploy.sh` starts; the admin probe is what catches a payment or audit router coming back |
| No audit funnel | `grep -rn "audit(\|AuditAction\|AdminAuditLog\|admin_audit_log" app/api app/services tests --include=*.py` | empty. See "Excluding the operator trail" below |
| No scheduled tasks | `ls app/models/schedule*.py alembic/versions/*schedule* 2>&1` | no such files. See "Scheduled tasks are not on this branch" below |
| Tests | `uv run pytest tests/` | green |

If any probe fails, master's version leaked over a hkgai delta — fix before pushing.

## Common conflict patterns + resolution

### `config.py` (the biggest)
Take master's **new** fields (e.g. `oss_preview_domain`, `max_session_files_*`, new model
entries) BUT keep hkgai's: email (`smtp_*`, drop any `aliyun_dm_*`), model whitelist
(`glm-5.1`/`fable`), and `session_tier_models` (FAST=deepseek-v4-flash / STANDARD=glm-5.2 / ADVANCED=deepseek-v4-pro). For a
**shared** model's price in `credit_rates`, master is canonical (converge); for a hkgai-only
model (`glm-5.1`), keep hkgai's entry — every whitelisted model needs a rate or reserve raises.

### `app/services/email/*` + `pyproject.toml` + `uv.lock`
Keep hkgai's SMTP/Lark entirely; reject master's Aliyun DirectMail client + its dep. Master
rarely touches email, so these usually auto-resolve to hkgai (no conflict) — verify with the
probes above regardless. The single email seam is `app/services/auth/verification.py`
(imports `send_html_email` / `render_*` from the `app.services.email` abstraction). It is
**master's file** — it has since grown a channel-agnostic email/phone OTP flow, so take master's
whole version and restate the transport as SMTP; the seam it calls is unchanged, so hkgai's SMTP
client backs it either way. Aliyun **SMS** (`alibabacloud-dysmsapi`, `app/services/sms/`) is a
different service from DirectMail and comes in with the phone channel — keep it.

### Parallel-developed functional code → converge to master
master and hkgai sometimes both touch the same infra (SSE `/me/events` fan-out — `user/hub.py`,
`user/events.py`, `main.py`, `settle_subscriber.py`; redis socket_timeout — `redis.py`; skills
bundle — `skill_bundle.py`; the credit ledger, invite-code rewards, daily check-in). These are
functional, not deployment — **take master's** on conflict (`git checkout --theirs <file>` when
the whole file is functional). Surface a genuinely-divergent one rather than guessing.

**"credits/billing → take master's" does not extend to payment.** The credit *ledger* converges;
the code that *sells* credits does not. See the next section.

### Aggregate routers — the third thing a plain merge gets wrong

`app/api/public/router.py` and `app/api/admin/router.py` arrive whole, and
both `include_router` the payment / commission halves. hkgai has never held
those modules, so git sees a pure addition and gives you **nothing to
resolve** — the same silence the payment files themselves used to arrive in.
Strip the imports and the `include_router` lines by hand after every merge
that touches either file; `import app.public_main` is what catches a miss.

Done for `app/api/admin/router.py` at the 2026-09-14 sync, when master
committed the console's routers and `import app.admin_main` started to
work: `payment` is gone from its imports and its `include_router`. The
admin-import probe in the table is what catches it coming back.

### `scripts/seed_users.py`, `scripts/deploy.sh`
Keep hkgai's (`--ours`).

### `CLAUDE.md`
Take master's flow description, but swap the email transport back to SMTP / Lark Suite
(`aiosmtplib`) and keep the SMTP creds / Lark-cap notes.

## Excluding the operator trail — `audit()` on the console

This deployment keeps no `admin_audit_log` (`b460116`, revision
`c8f2a5e1d94b`). Master's console writes every mutation through
`async with audit(ctx, action=AuditAction.…) as draft:`, which also owns
the commit. **New admin endpoints arrive as pure additions carrying it**,
so nothing conflicts and the first request fails on a missing module.

After every merge that touches `app/api/admin/`:

1. `git rm` master's `app/api/admin/audit.py`, `app/schemas/admin/audit.py`,
   `app/services/admin/{audit,actions}.py`, `app/models/admin_audit_log.py`,
   `tests/test_admin_audit.py` (modify/delete conflicts once deleted).
2. In each endpoint, replace the `async with audit(...)` block with its
   body and end the write with `await ctx.db.commit()`; drop the
   `draft.*` lines and the `AuditAction, audit` import. A change set built
   only to fill `draft.payload` becomes a set of changed field names.
   Keep `app/services/admin/__init__.py`, `app/services/admin/context.py`
   and `app/api/admin/dependencies.py` at this branch's versions (no
   `request_id` / `ip_address`, no `build_context`).
3. If the payload was the *only* record of something, log it instead —
   the skill upload's displaced `bundle_key` is logged at INFO.
4. Tests: drop assertions on audit rows / payloads, keep the behavioural
   ones and assert `db.commits`. `tests/test_admin_mutations_commit.py`
   is this branch's own and stays.
5. Docs: `docs/admin-console.md` route tables have no `Audit` column here.

## Excluding the phone channel — the second thing a plain merge gets wrong

Master registers and logs users in by **email or phone**, the phone half
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
| `app/errors.py` | no `PhoneTakenError` / `SamePhoneError` / `SmsSendFailedError` |
| `app/services/redis.py` | no `phone_verify` / `phone_change` prefixes or key builders |
| `config.py` | no `aliyun_sms_*` block — note master's default `SignName` is the **水杉智算** brand |
| `app/api/admin/user.py`, `app/schemas/admin/user.py` | the directory row has no `phone`, and `q` matches username / email only. **Arrives without a conflict** (new files) and fails only at runtime, on the first `GET /users` |
| deleted outright | `app/services/sms/`, `app/common/phone.py`, `app/services/auth/phone_change.py`, `scripts/test_send_verify_sms.py`, `tests/test_phone_normalization.py` |

`user.phone` **goes too**, in `b7e2d9a4c1f5` — along with its partial
`UNIQUE` index, which Postgres drops with the column. That revision is
this branch's first of its own; before it, the column had to stay because
dropping one is a migration and there was nowhere to put it. Master keeps
the field, so `user.py` is a merge conflict every time and taking master's
side there reintroduces a column this database does not have.

## Excluding payment (充值) — the one thing a plain merge gets wrong

**A plain merge brings the whole payment stack in, with zero conflicts.** hkgai has never held
those files, so git sees pure additions and gives you nothing to decide. Excluding them is a
**deletion you perform after the merge**, not a conflict you resolve. Only the first time is
silent: once hkgai carries the deletions, a later master change to any of these paths arrives as
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
| `app/workers/run.py` | drop the `payment_reconciler` import, its `create_task`, its docstring bullet; task count **four → three**; startup log string |
| `app/models/user.py` | drop `commission_balance_fen` (the DB column stays — see below) |
| `app/errors.py` | drop the 8 payment/commission/withdrawal error classes; the domain list in the module docstring |
| `config.py` | drop the `# ── Payments` and `# ── Referral commission` sections, and `credit_bonus_rate` (its only reader was order creation) |
| `app/services/credits/rates.py` | drop `recharge_bonus_credits` (same — no caller left) |
| docs | `CLAUDE.md` two sections + cross-refs; `README.md` background-process section; `docs/credits.md`; `docs/credits-future.md` "Shipped — Payment integration" |

**Kept on purpose:** `credit_price_cny` and `rates.credits_from_cny` — the admin
`scripts/topup_credits.py --cny` uses them. Invite-code rewards and daily check-in are
**in scope**; only the money-in and cash-out halves are cut.

### Migrations are NOT excluded

All of master's alembic revisions come across **unchanged**, including the three that create
`payment_order` / `commission_wallet` / the commission CHECK constraints. hkgai's database
therefore carries three permanently-empty tables and an always-zero `user.commission_balance_fen`.

That is deliberate. Those revisions are **interleaved** with wanted ones, not a contiguous block,
so excluding them would mean rewriting three `down_revision` links, surgically editing one
revision whose body mixes a `withdrawal_request` change with two unrelated index fixes
(`e1c5a9d7b3f4`), and re-doing all of it on every future sync — with `alembic upgrade head`
in production as the failure mode. Three empty tables are the cheaper side of that trade.

Consequence: **never run `alembic revision --autogenerate` on hkgai.** `Base.metadata` no longer
describes those tables, so autogenerate would emit `DROP TABLE` for them. Migrations come from
master, always — and a couple of their docstrings still point at `app/services/commission/`.
That is expected: a revision is a historical record, and editing one is the fork this whole
trade exists to avoid.

### Brand check the *new* templates, not just the conflicted ones

A template hkgai already had arrives as a conflict and `--ours` keeps its branding. A template
master **adds** merges in silently, carrying master's brand — the product name *and* the company
footer (`水杉智算技术有限公司` / `Metasequoia Technology Co., Ltd.`). After every merge:
`grep -c "水杉\|Metasequoia\|PPIG" app/services/email/templates.py` must be **0**.

## ⚠️ Alembic migration chain

After the merge run `uv run alembic heads`. It should be a **single** head (master's + hkgai's
migrations chain linearly). If there are **two**, create a join revision:
`uv run alembic merge -m "merge heads" <head1> <head2>`. Then `uv run alembic upgrade head`
must succeed. (Migrations are separate files, so they don't text-conflict — the risk is a
divergent DAG, not a merge conflict.)

**hkgai does own revisions of its own** — this section used to say it did not, contradicting
the migration-head probe row above, which is the one that is right. As of 2026-09-07 they are
`b7e2d9a4c1f5` (drops the removed subsystems' schema), `a1c7f3e9d2b6` (the OAuth-catalog twin),
`cd91a1a5179a` (a merge revision), `a3f9c1d7e2b4` (see below) and `c8f2a5e1d94b`
(drops `admin_audit_log`). Since then: `b6c1e83f27d4` / `c5e1a8f3b92d` (twins, below) and
`d8a4f1c6e290` (`session_files` unique per session — ahead of master).

### Revisions deliberately not taken, and re-points (2026-09-14 sync)

After that sync the chain reads
`… c5e1a8f3b92d → d8a4f1c6e290 → a7c3f1d9e4b2 → c2f8a41b7e60 → d5b1e93f7a48 → f4c8a2b91d63`.

| Master revision | What it does | On hkgai |
|---|---|---|
| `f3a7c9e15d82` | `skill.status` | **not taken** — byte-identical `upgrade()`/`downgrade()` to this branch's `b6c1e83f27d4`, applied here first. Taking both adds the columns twice |
| `a7d2f4b9c613` | backfill picked skills as preferences | **not taken** — identical twin of `c5e1a8f3b92d` |
| `b6d2f8a04c17` | drops `admin_audit_log.reason` | **not taken** — the table is gone (`c8f2a5e1d94b`) |
| `b7e2d4c8a915` | `payment_order.reconcile_attempted_at` | **not taken** — `payment_order` is gone (`b7e2d9a4c1f5`) |
| `c8b3e5f1a742`, `d4a1e8b62f37` | `schedule` / `schedule_run` | **not taken** — scheduled tasks are not on this branch |
| `a7c3f1d9e4b2` | `actor` table | taken, **re-pointed** onto this branch's head (upstream parent `a1f4c7e02b31` sits behind revisions this database may already stand on) |
| `c2f8a41b7e60` | skill / connector categories | taken, **re-pointed** past `b6d2f8a04c17` |
| `d5b1e93f7a48`, `f4c8a2b91d63` | two indexes | taken as-is (their parents came across) |

Both re-pointed files carry an "hkgai note" in the docstring; do not
"correct" them back. The next sync will see master's children of
`a7d2f4b9c613` (or later) — hang them off this branch's head the same way,
after reading what each intervening master revision does.

`alembic upgrade base:head --sql` does not render on this branch and did
not before this sync: `e2b7d4f9a1c6`'s guard inspects the live schema,
which offline mode cannot. Verify offline from a real revision instead
(`c5e1a8f3b92d:head --sql`), or against a throwaway database.

### Scheduled tasks are not on this branch

Master's scheduled-task feature (`schedule`, `schedule_run`, the
dispatcher) is excluded. Only its migrations had reached master when this
was decided, and they are the two rows above. When the application code
lands there, it arrives as pure additions — delete it the way payment is
deleted, and keep the migrations out of the graph.

### Two heads is not the only way a sync breaks the chain

`alembic merge heads` answers the case where an upstream revision's parent is on a *sibling*
branch. It does **not** answer the case where the parent is **absent from this branch
entirely** — then `alembic upgrade head` fails with `Can't locate revision identified by
'<parent>'`, and there are no two heads to merge.

That is what happened with `a3f9c1d7e2b4` (the worker's workspace-generation columns): upstream
it hangs off `f4c8a2b91d63`, one of six trunk revisions this branch never took. The fix is to
**re-point `down_revision` at this branch's head and keep the upstream revision id**, so the two
chains stay comparable. Say so in the file, or the next sync "corrects" it back.

Bringing the whole intervening trunk run over instead — it is linear and rooted exactly at this
branch's head, so it looks tidier — is the trap. `b6d2f8a04c17` sits in the middle of it and
drops `admin_audit_log.reason`, and at the time that column was live here. Check what each
intervening revision *does* before taking a chain for the shape of it.

### Never overwrite `b6e3d9a70f52_mcp_server_resources_enabled.py` from master

It is the one revision id both branches carry with a **different** `down_revision`: here its
parent is this branch's merge revision `cd91a1a5179a`, upstream it is `f2c8b04e5da7`. Taking
master's copy orphans the merge revision and drops `a1c7f3e9d2b6` and `b7e2d9a4c1f5` out of the
graph with it.

### The probes only look one way

Everything above asks "did master's version leak over a hkgai delta". Nothing asks **"have we
fallen behind on something outside this repo that now needs a schema change"** — and the
`agent-worker` this deployment runs is outside this repo. It is `core/agent-worker`, shared with
the metaseq deployment and pinned to nothing; when it grew the workspace-generation feature its
`claim_next_turn` began selecting four columns that did not exist here, with no feature flag, so
every turn claim would have failed. Add to the sync checklist: **read
`core/agent-worker`'s log since the last sync for anything the backend owns the schema of**
(`session`, `session_turn`, `sandbox`, `session_context`, `message`).

## Step-by-step workflow

```bash
cd backend-hkgai
git stash -u                       # park any local-only working-tree edits (e.g. seed_users)
git fetch origin
git checkout hkgai && git pull --ff-only origin hkgai
git merge --no-ff origin/master -m "merge: sync hkgai with master (<short summary>)"
# resolve conflicts per the rules above
uv sync                            # pick up any dep changes the merge brought
# run ALL the probes in "What MUST be preserved"
git push origin hkgai
git stash pop                      # restore the local-only edits
```

## Hot spots — where master changes most often collide with hkgai

1. `config.py` (email + model/tier + new fields — every merge that touches config)
2. `app/services/email/*`, `pyproject.toml`, `uv.lock` (SMTP vs Aliyun)
3. `app/services/{redis,user/events,user/hub,skill_bundle}.py`, `app/main.py` (parallel infra)
4. `scripts/seed_users.py`, `scripts/deploy.sh`
5. `CLAUDE.md`
6. Anything under `app/services/payment/` or `app/services/commission/` on master — arrives as a
   modify/delete conflict; resolve `git rm`

## Tier labels name no model

The frontend labels the tiers `快速 / 标准 / 高级` (`Fast / Standard / Advanced`) with no model
name — the same as share. It used to read "高级（Fable）" while this file mapped ADVANCED to a
different model; the frontend dropped the name (project-mars `ecd6ef5`) rather than chase it.
Which model a tier runs is decided here, in `session_tier_models`, and nowhere else — so a change
to that mapping needs no frontend edit. The frontend guide carries a probe that fails if a model
name comes back into a tier label.

## When in doubt

- `git diff origin/master HEAD -- <file>` shows ONLY the hkgai-specific delta for a file.
- After a merge, `git diff origin/master HEAD --stat` should list **only** the preserve-set
  files (email/SMTP, config, brand, scripts, lockfile) plus the payment/commission deletions —
  any *other* functional file there means you reverted a master change by mistake.
- `git rev-list --count HEAD..origin/master` must be `0` (every master commit is contained).
