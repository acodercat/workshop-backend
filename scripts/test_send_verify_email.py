"""Send a registration verification code via SMTP (Lark Suite Mail).

End-to-end smoke test for the email pipeline before integrating into
the actual ``/auth/register`` endpoint. Renders the same HTML body as
the production templates in :mod:`app.services.email.templates`,
substitutes the code locally, and dispatches via :mod:`aiosmtplib`.

Usage:
    uv run python -m scripts.test_send_verify_email --to me@example.com
    uv run python -m scripts.test_send_verify_email --to me@example.com --locale en-US
    uv run python -m scripts.test_send_verify_email --to me@example.com --code 123456

Requires the following keys in ``backend/.env`` (or ``.env.local``,
preferred — keep secrets out of the committed file):

    smtp_host                # smtp.larksuite.com
    smtp_port                # 465 (SSL) | 587 (STARTTLS)
    smtp_use_ssl             # True for 465, False for 587
    smtp_username            # full sender email
    smtp_password            # app-specific password
    smtp_from_address        # From: header email (usually = smtp_username)
    smtp_from_alias          # HKGAI Agent
"""

import argparse
import asyncio
import secrets
import sys

from app.services.email import (
    EmailSendError,
    render_register_verification,
    send_html_email,
)
from config import settings


async def _send(to: str, locale: str, code: str) -> None:
    subject, html_body = render_register_verification(code=code, locale=locale)
    message_id = await send_html_email(to=to, subject=subject, html_body=html_body)
    print(f"OK message_id={message_id}")
    print(f"   code     : {code}")
    print(f"   to       : {to}")
    print(f"   locale   : {locale}")
    print(
        f"   host     : {settings.smtp_host}:{settings.smtp_port} "
        f"({'SSL' if settings.smtp_use_ssl else 'STARTTLS'})"
    )
    print(f"   from     : {settings.smtp_from_address}")
    print(f"   alias    : {settings.smtp_from_alias}")


def _hint(exc: EmailSendError) -> None:
    """Print a one-line diagnostic for the most common SMTP failures."""
    message = (exc.message or "").lower()
    code = exc.code
    if code in (535, "535") or "authentication" in message or "auth" in message:
        print(
            "  → SMTP 认证失败：检查 smtp_username / smtp_password；"
            "Lark Suite 用 app-specific 密码（不是登录密码）",
            file=sys.stderr,
        )
    elif code in (550, "550") or "sender" in message or "from" in message:
        print(
            f"  → 发件地址 {settings.smtp_from_address} 被服务器拒绝；"
            "确认它在 Lark Suite 已绑定且 SPF/DKIM 通过",
            file=sys.stderr,
        )
    elif "ssl" in message or "tls" in message or "handshake" in message:
        print(
            f"  → TLS 握手失败：port={settings.smtp_port} use_ssl={settings.smtp_use_ssl}；"
            "465 配 use_ssl=true，587 配 use_ssl=false",
            file=sys.stderr,
        )
    elif "timeout" in message or "timed out" in message:
        print(
            f"  → 连接 {settings.smtp_host}:{settings.smtp_port} 超时；"
            "检查网络或防火墙是否放行出站 SMTP 端口",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--to", required=True, help="收件邮箱地址")
    parser.add_argument(
        "--locale",
        choices=["zh-CN", "en-US"],
        default="zh-CN",
        help="模板语言（默认 zh-CN）",
    )
    parser.add_argument(
        "--code",
        default=None,
        help="验证码（不传则随机生成 6 位）",
    )
    args = parser.parse_args()

    code = args.code or f"{secrets.randbelow(900_000) + 100_000}"

    try:
        asyncio.run(_send(args.to, args.locale, code))
    except EmailSendError as exc:
        suffix = f" (message_id={exc.request_id})" if exc.request_id else ""
        print(f"FAILED: {exc.message}{suffix}", file=sys.stderr)
        _hint(exc)
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
