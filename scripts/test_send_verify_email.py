"""Send a registration verification code via Aliyun DirectMail.

End-to-end smoke test for the email pipeline before integrating into
the actual ``/auth/register`` endpoint. Renders the same HTML body as
the approved Aliyun templates (``axon_register_verify_zh`` /
``axon_register_verify_en``), substitutes the code locally, and calls
``SingleSendMail``.

Usage:
    uv run python -m scripts.test_send_verify_email --to me@example.com
    uv run python -m scripts.test_send_verify_email --to me@example.com --locale en-US
    uv run python -m scripts.test_send_verify_email --to me@example.com --code 123456

Requires the following keys in ``backend/.env`` (or ``.env.local``,
preferred — keep AKs out of the committed file):

    aliyun_dm_access_key_id
    aliyun_dm_access_key_secret
    aliyun_dm_region            # cn-hangzhou (default) | ap-southeast-1
    aliyun_dm_account_name      # axon@notify.metaseq.ai
    aliyun_dm_from_alias        # PPIG

Why inline rendering instead of Aliyun's ``TemplateName``:
``SingleSendMail`` (DM API 2015-11-23) accepts inline ``Subject`` +
``HtmlBody`` only — templates are wired into ``BatchSendMail``, which
requires a saved recipient list. For transactional sends we keep the
HTML in source and substitute the variable here.
"""

import argparse
import secrets
import sys

from alibabacloud_dm20151123 import models as dm_models
from alibabacloud_dm20151123.client import Client as DmClient
from alibabacloud_tea_openapi import models as openapi_models
from Tea.exceptions import TeaException

from config import settings

_SUBJECT_ZH = "【PPIG】您的注册验证码 {code}"
_SUBJECT_EN = "[PPIG] Verify your email — code {code}"

_BODY_ZH = """<div style="max-width:560px;margin:0 auto;padding:32px 24px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1a1a1a;background:#ffffff;">
  <div style="font-size:18px;font-weight:500;margin-bottom:24px;">PPIG</div>
  <p style="font-size:15px;line-height:1.7;margin:0 0 16px;">您好，</p>
  <p style="font-size:15px;line-height:1.7;margin:0 0 24px;">您正在注册 PPIG 账号。请使用以下验证码完成邮箱验证：</p>
  <div style="margin:24px 0 32px;padding:24px;background:#f5f6f8;border-radius:8px;text-align:center;">
    <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:32px;font-weight:600;letter-spacing:6px;color:#1254c7;">{code}</div>
  </div>
  <p style="font-size:14px;line-height:1.7;margin:0 0 8px;color:#6b7280;">验证码 10 分钟内有效，请勿向他人泄露。</p>
  <p style="font-size:14px;line-height:1.7;margin:0 0 24px;color:#6b7280;">如果这不是您本人的操作，请忽略本邮件。</p>
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;">本邮件由 PPIG 系统自动发送，请勿回复。</div>
</div>"""

_BODY_EN = """<div style="max-width:560px;margin:0 auto;padding:32px 24px;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;color:#1a1a1a;background:#ffffff;">
  <div style="font-size:18px;font-weight:500;margin-bottom:24px;">PPIG</div>
  <p style="font-size:15px;line-height:1.7;margin:0 0 16px;">Hi,</p>
  <p style="font-size:15px;line-height:1.7;margin:0 0 24px;">You're registering an PPIG account. Use the verification code below to confirm your email:</p>
  <div style="margin:24px 0 32px;padding:24px;background:#f5f6f8;border-radius:8px;text-align:center;">
    <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:32px;font-weight:600;letter-spacing:6px;color:#1254c7;">{code}</div>
  </div>
  <p style="font-size:14px;line-height:1.7;margin:0 0 8px;color:#6b7280;">The code is valid for 10 minutes. Do not share it with anyone.</p>
  <p style="font-size:14px;line-height:1.7;margin:0 0 24px;color:#6b7280;">If you didn't request this, you can safely ignore this email.</p>
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;">This is an automated message from PPIG. Please do not reply.</div>
</div>"""


def _render(locale: str, code: str) -> tuple[str, str]:
    """Pick subject + body for ``locale`` and substitute ``{code}``."""
    if locale == "en-US":
        return _SUBJECT_EN.format(code=code), _BODY_EN.format(code=code)
    return _SUBJECT_ZH.format(code=code), _BODY_ZH.format(code=code)


def _make_client() -> DmClient:
    if not settings.aliyun_dm_access_key_id or not settings.aliyun_dm_access_key_secret:
        raise RuntimeError(
            "缺少 aliyun_dm_access_key_id / aliyun_dm_access_key_secret —— "
            "请在 backend/.env 或 backend/.env.local 里配置"
        )
    if not settings.aliyun_dm_account_name:
        raise RuntimeError(
            "缺少 aliyun_dm_account_name（发信地址）—— 请填阿里云邮件推送已通过验证的发信地址"
        )

    # Aliyun DirectMail uses a unified endpoint ``dm.aliyuncs.com``
    # for all mainland regions (the regional ``dm.{region}.aliyuncs.com``
    # form returns SSL EOF — wrong domain). The Singapore (overseas)
    # node is the only exception with its own endpoint
    # ``dm.ap-southeast-1.aliyuncs.com``.
    if settings.aliyun_dm_region == "ap-southeast-1":
        endpoint = "dm.ap-southeast-1.aliyuncs.com"
    else:
        endpoint = "dm.aliyuncs.com"

    config = openapi_models.Config(
        access_key_id=settings.aliyun_dm_access_key_id,
        access_key_secret=settings.aliyun_dm_access_key_secret,
    )
    config.endpoint = endpoint
    return DmClient(config)


def _send(to: str, locale: str, code: str) -> None:
    subject, html_body = _render(locale, code)
    client = _make_client()

    # ``address_type=1`` 表示用 ``AccountName`` 指定的"已验证发信地址"
    # 发出（vs ``=0`` 用阿里云的随机虚拟地址，那条路径已经废弃了）。
    # ``reply_to_address=False`` 关闭回信地址 —— 注册验证邮件是单向
    # 通道，不期待用户回复。
    request = dm_models.SingleSendMailRequest(
        account_name=settings.aliyun_dm_account_name,
        address_type=1,
        from_alias=settings.aliyun_dm_from_alias,
        reply_to_address=False,
        to_address=to,
        subject=subject,
        html_body=html_body,
    )
    response = client.single_send_mail(request)
    body = response.body
    print(f"OK request_id={body.request_id} env_id={body.env_id}")
    print(f"   code     : {code}")
    print(f"   to       : {to}")
    print(f"   locale   : {locale}")
    print(f"   from     : {settings.aliyun_dm_account_name}")
    print(f"   alias    : {settings.aliyun_dm_from_alias}")


def _hint(message: str) -> None:
    """Print a one-line diagnostic for the most common Aliyun errors."""
    if "InvalidAccessKeyId" in message:
        print("  → AccessKey ID 错了或被禁用", file=sys.stderr)
    elif "SignatureDoesNotMatch" in message:
        print("  → AccessKey Secret 错了", file=sys.stderr)
    elif "AccountNoOwnership" in message or "DomainNotOwnedByUser" in message:
        print(
            f"  → 发信地址 {settings.aliyun_dm_account_name} 没在阿里云通过验证",
            file=sys.stderr,
        )
    elif "Forbidden.SubAccess" in message:
        print(
            "  → RAM 子账号没授权 AliyunDirectMailFullAccess（或 SingleSendMail 权限）",
            file=sys.stderr,
        )
    elif "Endpoint" in message or "DNS" in message or "Connection" in message.lower():
        print(
            f"  → 网络问题或 region {settings.aliyun_dm_region} 不对（默认 cn-hangzhou，新加坡是 ap-southeast-1）",
            file=sys.stderr,
        )
    elif "spam" in message.lower():
        print(
            "  → 内容触发反垃圾过滤，多半是 HTML 里出现了高敏感词",
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
        _send(args.to, args.locale, code)
    except TeaException as exc:
        request_id = exc.data.get("RequestId") if exc.data else None
        print(
            f"FAILED: {exc.code}: {exc.message}"
            + (f" (request_id={request_id})" if request_id else ""),
            file=sys.stderr,
        )
        _hint(f"{exc.code} {exc.message}")
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        _hint(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
