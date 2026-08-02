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
   and `fable` (master omits both); `session_tier_models` maps **FAST=HKGAI-V3,
   STANDARD=kimi-k2, ADVANCED=glm-5.2** (master uses FAST=deepseek-v4-flash, etc.). Pricing in
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
| Tier mapping | `grep -A4 session_tier_models config.py` | `FAST: "HKGAI-V3"` |
| Single migration head | `uv run alembic heads` | exactly one head; `uv run alembic branches` empty |
| No payment code | `grep -rn "services.payment\|services.commission\|dg_sdk" app/ scripts/ tests/` | empty |
| No payment routes | `uv run python -c "from app.main import app; print([p for p in app.openapi()['paths'] if 'payment' in p or 'commission' in p])"` | `[]` |
| No 汇付 dep | `grep -c 'dg-sdk' pyproject.toml uv.lock` | 0 in both |
| App imports | `uv run python -c "import app.main"` | clean |
| Tests | `uv run pytest tests/` | green |

If any probe fails, master's version leaked over a hkgai delta — fix before pushing.

## Common conflict patterns + resolution

### `config.py` (the biggest)
Take master's **new** fields (e.g. `oss_preview_domain`, `max_session_files_*`, new model
entries) BUT keep hkgai's: email (`smtp_*`, drop any `aliyun_dm_*`), model whitelist
(`glm-5.1`/`fable`), and `session_tier_models` (FAST=HKGAI-V3 / ADVANCED=glm-5.2). For a
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

### `scripts/seed_users.py`, `scripts/deploy.sh`
Keep hkgai's (`--ours`).

### `CLAUDE.md`
Take master's flow description, but swap the email transport back to SMTP / Lark Suite
(`aiosmtplib`) and keep the SMTP creds / Lark-cap notes.

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
scripts/{process_withdrawals,recover_paid_orders}.py   scripts/gen_huifu_keys.sh
tests/test_payment_*.py  tests/test_commission_*.py  tests/test_huifu_adapter.py
```

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
master, always.

## ⚠️ Alembic migration chain

After the merge run `uv run alembic heads`. It should be a **single** head (master's + hkgai's
migrations chain linearly). If there are **two**, create a join revision:
`uv run alembic merge -m "merge heads" <head1> <head2>`. Then `uv run alembic upgrade head`
must succeed. (Migrations are separate files, so they don't text-conflict — the risk is a
divergent DAG, not a merge conflict.) hkgai adds no revisions of its own — see
"Migrations are NOT excluded" above.

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

## Known inconsistency to watch

Backend `session_tier_models` maps **ADVANCED → glm-5.2**, but the frontend hkgai labels the
advanced tier "高级（Fable）". Pick one (set ADVANCED=fable backend-side, or relabel the
frontend) so the UI label matches the model actually used — out of scope for a sync merge.

## When in doubt

- `git diff origin/master HEAD -- <file>` shows ONLY the hkgai-specific delta for a file.
- After a merge, `git diff origin/master HEAD --stat` should list **only** the preserve-set
  files (email/SMTP, config, brand, scripts, lockfile) plus the payment/commission deletions —
  any *other* functional file there means you reverted a master change by mistake.
- `git rev-list --count HEAD..origin/master` must be `0` (every master commit is contained).
