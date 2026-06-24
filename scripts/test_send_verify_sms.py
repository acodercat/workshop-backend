"""Send a registration verification code via Aliyun SMS (dysmsapi).

End-to-end smoke test for the phone pipeline, the SMS sibling of
``scripts.test_send_verify_email``. Builds the same ``SendSms`` request
that :func:`app.services.sms.client.send_verification_sms` builds and
prints the raw Aliyun response, so a failing send shows its real
business ``code``/``message`` instead of the API's swallowed
``sms_send_failed``.

Usage:
    uv run python -m scripts.test_send_verify_sms --to 13800138000
    uv run python -m scripts.test_send_verify_sms --to 13800138000 --code 123456

Requires the following keys in ``backend/.env`` (or ``.env.local``,
preferred — keep AKs out of the committed file):

    aliyun_sms_access_key_id
    aliyun_sms_access_key_secret
    aliyun_sms_region            # cn-hangzhou (default) | ap-southeast-1
    aliyun_sms_sign_name         # 水杉智算深圳技术 (approved 短信签名)
    aliyun_sms_template_code     # SMS_305451063 (approved 模板, var ${code})
"""

import argparse
import json
import secrets
import sys

from alibabacloud_dysmsapi20170525 import models as sms_models
from alibabacloud_dysmsapi20170525.client import Client as SmsClient
from alibabacloud_tea_openapi import models as openapi_models
from Tea.exceptions import TeaException

from config import settings


def _today() -> str:
    """Today as ``yyyyMMdd`` (the SendDate format QuerySendDetails wants)."""
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d")


def _make_client() -> SmsClient:
    if (
        not settings.aliyun_sms_access_key_id
        or not settings.aliyun_sms_access_key_secret
    ):
        raise RuntimeError(
            "缺少 aliyun_sms_access_key_id / aliyun_sms_access_key_secret —— "
            "请在 backend/.env 或 backend/.env.local 里配置"
        )

    # dysmsapi 用统一端点 ``dysmsapi.aliyuncs.com``（境内）；新加坡
    # 海外节点是唯一例外 ``dysmsapi.ap-southeast-1.aliyuncs.com``。
    # 与 app.services.sms.client._make_client 保持一致。
    if settings.aliyun_sms_region == "ap-southeast-1":
        endpoint = "dysmsapi.ap-southeast-1.aliyuncs.com"
    else:
        endpoint = "dysmsapi.aliyuncs.com"

    config = openapi_models.Config(
        access_key_id=settings.aliyun_sms_access_key_id,
        access_key_secret=settings.aliyun_sms_access_key_secret,
    )
    config.endpoint = endpoint
    return SmsClient(config)


def _send(to: str, code: str) -> None:
    client = _make_client()
    request = sms_models.SendSmsRequest(
        phone_numbers=to,
        sign_name=settings.aliyun_sms_sign_name,
        template_code=settings.aliyun_sms_template_code,
        template_param=json.dumps({"code": code}),
    )
    response = client.send_sms(request)
    body = response.body
    if body.code != "OK":
        print(
            f"FAILED: {body.code}: {body.message} (request_id={body.request_id})",
            file=sys.stderr,
        )
        _hint(f"{body.code} {body.message}")
        sys.exit(1)
    print(f"OK request_id={body.request_id} biz_id={body.biz_id}")
    print(f"   code     : {code}")
    print(f"   to       : {to}")
    print(f"   sign     : {settings.aliyun_sms_sign_name}")
    print(f"   template : {settings.aliyun_sms_template_code}")


def _query(to: str, send_date: str, biz_id: str | None) -> None:
    """Pull the carrier delivery receipt for a previously-sent SMS.

    ``OK`` from ``SendSms`` only means Aliyun accepted the request;
    actual delivery is async. ``QuerySendDetails`` returns the
    carrier-level ``SendStatus`` (1=待回执 / 2=失败 / 3=成功) plus an
    ``ErrCode`` explaining a non-delivery (空号 / 黑名单 / 关键字拦截 …).
    """
    client = _make_client()
    request = sms_models.QuerySendDetailsRequest(
        phone_number=to,
        send_date=send_date,
        page_size=10,
        current_page=1,
        biz_id=biz_id,
    )
    response = client.query_send_details(request)
    body = response.body
    if body.code != "OK":
        print(f"QUERY FAILED: {body.code}: {body.message}", file=sys.stderr)
        sys.exit(1)

    details = body.sms_send_detail_dtos.sms_send_detail_dto or []
    if not details:
        print(f"无记录：{to} 在 {send_date} 没有发送明细（日期/号码对不上？）")
        return
    status_label = {"1": "待运营商回执", "2": "失败", "3": "成功"}
    for d in details:
        status = status_label.get(str(d.send_status), str(d.send_status))
        print(f"send_status : {status}")
        print(f"  receive_date : {d.receive_date}")
        print(f"  err_code     : {d.err_code}")   # DELIVERED=成功，其余见下方提示
        print(f"  content      : {d.content}")
        if d.err_code and d.err_code != "DELIVERED":
            _hint(d.err_code)


def _hint(message: str) -> None:
    """Print a one-line diagnostic for the most common dysmsapi errors."""
    if "InvalidAccessKeyId" in message:
        print("  → AccessKey ID 错了或被禁用", file=sys.stderr)
    elif "SignatureDoesNotMatch" in message:
        print("  → AccessKey Secret 错了", file=sys.stderr)
    elif "Forbidden.RAM" in message or "NoPermission" in message:
        print("  → RAM 子账号没授权 AliyunDysmsFullAccess", file=sys.stderr)
    elif "SMS_SIGNATURE_ILLEGAL" in message:
        print(
            f"  → 签名「{settings.aliyun_sms_sign_name}」未审核通过或与 AccessKey 所属账号不符",
            file=sys.stderr,
        )
    elif "SMS_TEMPLATE_ILLEGAL" in message:
        print(
            f"  → 模板「{settings.aliyun_sms_template_code}」未审核通过",
            file=sys.stderr,
        )
    elif "TEMPLATE_MISSING_PARAMETERS" in message or "TEMPLATE_PARAM" in message:
        print("  → 模板变量名对不上：模板里必须是 ${code}", file=sys.stderr)
    elif "MOBILE_NUMBER_ILLEGAL" in message:
        print("  → 手机号格式错误", file=sys.stderr)
    elif "BUSINESS_LIMIT_CONTROL" in message:
        print("  → 触发流控：同一号码发太频繁（默认 1 条/分、5 条/天）", file=sys.stderr)
    elif "AMOUNT_NOT_ENOUGH" in message or "OUT_OF_SERVICE" in message:
        print("  → 账户余额不足或短信服务已停机", file=sys.stderr)
    elif "ACCOUNT_NOT_EXISTS" in message or "PRODUCT_UN_SUBSCRIPT" in message:
        print("  → 该账号还没开通短信服务", file=sys.stderr)
    # 运营商回执码（QuerySendDetails 的 ErrCode）：
    elif "MOBILE_NOT_ON_SERVICE" in message or "MOBILE_ACCOUNT_ABNORMAL" in message:
        print("  → 手机停机/不在服务区/账户异常", file=sys.stderr)
    elif "MOBILE_IN_BLACK" in message:
        print("  → 号码在黑名单（曾回复 TD/N 退订过该签名，需联系阿里云移出）", file=sys.stderr)
    elif "INVALID_NUMBER" in message:
        print("  → 空号或号码无效", file=sys.stderr)
    elif "CONTENT_KEYWORD" in message:
        print("  → 内容触发运营商关键字拦截", file=sys.stderr)
    elif "BLACK_KEY_WORD" in message or "SP_UNKNOWN_ERROR" in message:
        print("  → 运营商侧未知错误/关键字，建议重发或换号测试", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--to", required=True, help="收件手机号（11 位）")
    parser.add_argument(
        "--code",
        default=None,
        help="验证码（不传则随机生成 6 位）",
    )
    parser.add_argument(
        "--query",
        action="store_true",
        help="不发送，改为查这个号码当天的运营商投递回执（排查已发但没收到）",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="--query 的发送日期 yyyyMMdd（默认今天）",
    )
    parser.add_argument("--biz-id", default=None, help="--query 可选：缩小到某次 biz_id")
    args = parser.parse_args()

    try:
        if args.query:
            send_date = args.date or _today()
            _query(args.to, send_date, args.biz_id)
            return
        code = args.code or f"{secrets.randbelow(900_000) + 100_000}"
        _send(args.to, code)
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
