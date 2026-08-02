"""One-off admin notice: email users that their account was activated.

Reuses the existing email service (``app.services.email.send_html_email``,
SMTP) and the ``render_account_activated`` template — same
visual shell as the verification emails, no ``{code}``.

**DRY-RUN by default** — it only resolves recipients and renders the
mail. Pass ``--send`` to actually dispatch (irreversible: real email to
real users). Recipients with no email on file, or whose status isn't
ACTIVE, are skipped with a warning (we don't tell someone "you're
activated" unless they actually are).

    # from backend/ :
    uv run python scripts/notify_activated_users.py                 # dry-run, ids 119-122
    uv run python scripts/notify_activated_users.py --send          # actually send
    uv run python scripts/notify_activated_users.py --ids 119 120 --locale en-US --send
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

# The four accounts activated in this batch. Override with --ids.
DEFAULT_IDS = [119, 120, 121, 122]


def _fetch_recipients(ids: list[int]) -> list[dict]:
    """Read (id, name, email, status) for the requested user ids."""
    engine = create_engine(settings.db_uri.replace("+asyncpg", ""))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    'SELECT id, name, email, status FROM "user" '
                    "WHERE id = ANY(:ids) ORDER BY id"
                ),
                {"ids": ids},
            ).all()
    finally:
        engine.dispose()
    return [
        {"id": r[0], "name": r[1], "email": r[2], "status": r[3]} for r in rows
    ]


def _partition(recipients: list[dict], requested_ids: list[int]) -> tuple[list[dict], list[str]]:
    """Split into sendable recipients and skip-reasons."""
    sendable: list[dict] = []
    skipped: list[str] = []
    found_ids = {r["id"] for r in recipients}
    for missing in sorted(set(requested_ids) - found_ids):
        skipped.append(f"id={missing}: 用户不存在")
    for r in recipients:
        status = str(r["status"])
        if not r["email"]:
            skipped.append(f"id={r['id']} ({r['name']}): 无邮箱，跳过")
        elif status != "ACTIVE":
            skipped.append(f"id={r['id']} ({r['name']}): status={status}（非 ACTIVE），跳过")
        else:
            sendable.append(r)
    return sendable, skipped


async def _send_all(sendable: list[dict], locale: str, do_send: bool) -> int:
    """Render (and optionally send) one email per recipient. Returns the
    count of hard failures so the caller can set the exit code."""
    failures = 0
    for r in sendable:
        subject, html_body = render_account_activated(name=r["name"], locale=locale)
        if not do_send:
            print(
                f"  [dry-run] → {r['email']}  |  {subject}  "
                f"(html {len(html_body)} 字符)"
            )
            continue
        try:
            request_id = await send_html_email(
                to=r["email"], subject=subject, html_body=html_body,
            )
            print(f"  [sent]    → {r['email']}  request_id={request_id}")
        except EmailSendError as exc:
            failures += 1
            print(
                f"  [FAILED]  → {r['email']}  {exc.message} "
                f"(code={exc.code} request_id={exc.request_id})"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Email activated users.")
    parser.add_argument(
        "--ids", type=int, nargs="+", default=DEFAULT_IDS,
        help=f"User ids to notify (default: {DEFAULT_IDS})",
    )
    parser.add_argument(
        "--locale", choices=["zh-CN", "en-US"], default="zh-CN",
        help="Email locale (default: zh-CN)",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Actually send. Without this flag the script only previews (dry-run).",
    )
    args = parser.parse_args()

    recipients = _fetch_recipients(args.ids)
    sendable, skipped = _partition(recipients, args.ids)

    mode = "发送" if args.send else "DRY-RUN（不发送）"
    print(f"模式: {mode} | locale={args.locale} | 目标 ids={args.ids}\n")
    if skipped:
        print("跳过:")
        for s in skipped:
            print(f"  - {s}")
        print()
    if not sendable:
        print("没有可发送的收件人。")
        return 0

    print(f"将{'发送' if args.send else '渲染'} {len(sendable)} 封:")
    failures = asyncio.run(_send_all(sendable, args.locale, args.send))

    if not args.send:
        print("\nDry-run 完成，未发送任何邮件。确认无误后加 --send 真正发送。")
        return 0
    print(f"\n完成: 成功 {len(sendable) - failures}，失败 {failures}。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
