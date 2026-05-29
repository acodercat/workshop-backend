"""Approve PENDING users in one shot: find → activate → email them.

DRY-RUN by default — lists who *would* be activated/emailed and changes
nothing. Pass ``--send`` to actually flip status PENDING → ACTIVE (one
transaction) and dispatch the activation notice via the existing email
service (``app.services.email``).

Design:
- Activation uses ``UPDATE ... RETURNING`` so we act on (and email)
  exactly the rows flipped in this run — no SELECT-then-UPDATE race.
- Activation and notification are separate concerns: a PENDING user
  with no email on file is still activated, just not emailed (warned).
  Email failures do NOT roll back activation — activation is the
  primary action; the notice is best-effort and backstopped by the
  printed request_ids.

    # from backend/ :
    uv run python scripts/activate_and_notify.py                  # dry-run (preview only)
    uv run python scripts/activate_and_notify.py --send           # activate + email ALL pending
    uv run python scripts/activate_and_notify.py --ids 123 124 --send
    uv run python scripts/activate_and_notify.py --send --locale en-US
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make ``app`` / ``config`` importable when run as a plain file path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.email import (  # noqa: E402
    EmailSendError,
    render_account_activated,
    send_html_email,
)
from config import settings  # noqa: E402


def _engine():
    return create_engine(settings.db_uri.replace("+asyncpg", ""))


def _where(ids: list[int] | None) -> tuple[str, dict]:
    """Build the PENDING-scope WHERE clause + params."""
    if ids:
        return "status='PENDING' AND id = ANY(:ids)", {"ids": ids}
    return "status='PENDING'", {}


def _list_pending(ids: list[int] | None) -> list[dict]:
    clause, params = _where(ids)
    engine = _engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f'SELECT id, name, email FROM "user" WHERE {clause} '
                    "ORDER BY created_at"
                ),
                params,
            ).all()
    finally:
        engine.dispose()
    return [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]


def _activate(ids: list[int] | None) -> list[dict]:
    """Flip matching PENDING rows to ACTIVE in one transaction.

    Returns the rows actually flipped (id, name, email) via RETURNING,
    so the caller emails exactly this run's activations.
    """
    clause, params = _where(ids)
    engine = _engine()
    try:
        with engine.begin() as conn:  # commits on success, rolls back on error
            rows = conn.execute(
                text(
                    f'UPDATE "user" SET status=\'ACTIVE\', updated_at=now() '
                    f"WHERE {clause} RETURNING id, name, email"
                ),
                params,
            ).all()
    finally:
        engine.dispose()
    return [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]


async def _email_activated(activated: list[dict], locale: str) -> tuple[int, int, int]:
    """Email each activated user that has an address.

    Returns (emailed_ok, skipped_no_email, failed)."""
    ok = skipped = failed = 0
    for u in activated:
        if not u["email"]:
            skipped += 1
            print(f"  [skip]   id={u['id']} ({u['name']}): 无邮箱，已激活但不发信")
            continue
        subject, html_body = render_account_activated(name=u["name"], locale=locale)
        try:
            rid = await send_html_email(
                to=u["email"], subject=subject, html_body=html_body,
            )
            ok += 1
            print(f"  [sent]   id={u['id']} → {u['email']}  request_id={rid}")
        except EmailSendError as exc:
            failed += 1
            print(
                f"  [FAILED] id={u['id']} → {u['email']}  {exc.message} "
                f"(code={exc.code} request_id={exc.request_id})"
            )
    return ok, skipped, failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find PENDING users, activate them, and email them."
    )
    parser.add_argument(
        "--ids", type=int, nargs="+", default=None,
        help="Restrict to these user ids (default: all PENDING users).",
    )
    parser.add_argument(
        "--locale", choices=["zh-CN", "en-US"], default="zh-CN",
        help="Email locale (default: zh-CN).",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Actually activate + email. Without it, preview only (dry-run).",
    )
    args = parser.parse_args()

    scope = f"ids={args.ids}" if args.ids else "全部 PENDING"
    print(f"模式: {'执行（激活+发信）' if args.send else 'DRY-RUN（预览，不改不发）'} "
          f"| 范围: {scope} | locale={args.locale}\n")

    if not args.send:
        pending = _list_pending(args.ids)
        if not pending:
            print("没有匹配的 PENDING 用户。")
            return 0
        print(f"将激活并发信 {len(pending)} 个（预览）:")
        for u in pending:
            print(f"  id={u['id']}  name={u['name']!r}  "
                  f"email={u['email'] or '（无邮箱→只激活不发信）'}")
        print("\nDry-run 完成，未做任何改动。确认无误后加 --send 执行。")
        return 0

    # --- execute ---
    activated = _activate(args.ids)
    if not activated:
        print("没有匹配的 PENDING 用户，未激活任何账号。")
        return 0
    print(f"已激活 {len(activated)} 个: "
          + ", ".join(f"id={u['id']}({u['name']})" for u in activated))
    print("\n发送激活通知:")
    ok, skipped, failed = asyncio.run(_email_activated(activated, args.locale))
    print(f"\n完成: 激活 {len(activated)}，发信成功 {ok}，"
          f"无邮箱跳过 {skipped}，发信失败 {failed}。")
    if failed:
        print("注意: 失败的仅是“通知邮件”，相关账号已激活成功，不需要重激活；"
              "如需补发，可用 scripts/notify_activated_users.py --ids <id...> --send。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
