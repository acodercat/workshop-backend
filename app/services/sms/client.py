"""Aliyun SMS (短信服务 / dysmsapi) ``SendSms`` wrapper.

One async function (:func:`send_verification_sms`) that dispatches a
6-digit verification code to a phone via the Aliyun SDK. All other
modules in :mod:`app.services.sms` go through this seam — the SDK isn't
imported anywhere else. Mirrors :mod:`app.services.email.client`:

- The SDK's ``TeaException`` is surfaced as :class:`SmsSendError` so
  callers catch one specific exception without depending on the SDK's
  taxonomy.
- dysmsapi returns HTTP 200 with a business code in the body
  (``OK`` = success; ``isv.*`` = throttled / illegal number / etc.), so
  a non-``OK`` code is treated as a failure too.
"""

import json
import logging

from alibabacloud_dysmsapi20170525 import models as sms_models
from alibabacloud_dysmsapi20170525.client import Client as SmsClient
from alibabacloud_tea_openapi import models as openapi_models
from Tea.exceptions import TeaException

from config import settings

logger = logging.getLogger(__name__)

# dysmsapi's success body code; anything else is a business failure.
_SUCCESS_CODE = "OK"


class SmsSendError(Exception):
    """Raised when Aliyun rejects or the network drops an SMS send.

    Always carries a human-readable ``message``. ``code`` is the Aliyun
    error code (e.g. ``isv.BUSINESS_LIMIT_CONTROL``) when available,
    ``None`` for transport errors; ``request_id`` is the Aliyun trace id
    for log correlation."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id


def _make_client() -> SmsClient:
    if not settings.aliyun_sms_access_key_id or not settings.aliyun_sms_access_key_secret:
        raise SmsSendError(
            "Aliyun SMS credentials missing — set "
            "``aliyun_sms_access_key_id`` and ``aliyun_sms_access_key_secret``"
        )
    # dysmsapi uses the unified ``dysmsapi.aliyuncs.com`` endpoint for
    # mainland regions; only the Singapore overseas node has its own
    # (mirrors :func:`app.services.email.client._make_client`).
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


async def send_verification_sms(*, phone: str, code: str) -> str:
    """Send a verification-code SMS to ``phone``. Returns the Aliyun
    ``request_id`` on success; raises :class:`SmsSendError` otherwise.

    The approved template's variable is ``${code}`` (see
    ``aliyun_sms_template_code``), so the param is ``{"code": code}``.
    The SDK call is awaited via its async variant so the event loop
    isn't blocked.
    """
    client = _make_client()
    request = sms_models.SendSmsRequest(
        phone_numbers=phone,
        sign_name=settings.aliyun_sms_sign_name,
        template_code=settings.aliyun_sms_template_code,
        template_param=json.dumps({"code": code}),
    )

    try:
        response = await client.send_sms_async(request)
    except TeaException as exc:
        request_id = exc.data.get("RequestId") if exc.data else None
        logger.warning(
            "Aliyun SMS send failed: code=%s message=%s request_id=%s phone=%s",
            exc.code,
            exc.message,
            request_id,
            phone,
        )
        raise SmsSendError(
            message=exc.message or "Aliyun SMS rejected the send",
            code=exc.code,
            request_id=request_id,
        ) from exc
    except Exception as exc:
        logger.exception("Aliyun SMS transport failure to %s", phone)
        raise SmsSendError(
            message=f"Aliyun SMS transport failure: {type(exc).__name__}: {exc}"
        ) from exc

    body = response.body
    if body.code != _SUCCESS_CODE:
        logger.warning(
            "Aliyun SMS rejected: code=%s message=%s request_id=%s phone=%s",
            body.code,
            body.message,
            body.request_id,
            phone,
        )
        raise SmsSendError(
            message=body.message or "Aliyun SMS rejected the send",
            code=body.code,
            request_id=body.request_id,
        )

    logger.info("Aliyun SMS sent: request_id=%s phone=%s", body.request_id, phone)
    return body.request_id
