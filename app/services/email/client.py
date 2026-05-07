"""Aliyun DirectMail ``SingleSendMail`` wrapper.

One async function (:func:`send_html_email`) that takes a recipient,
subject, and HTML body and dispatches via the Aliyun SDK. All other
modules in :mod:`app.services.email` go through this seam — the SDK
isn't imported anywhere else.

Why a thin wrapper rather than calling the SDK directly from the
caller:

- The SDK's request model is verbose (5+ required parameters) and
  most are config-driven (``account_name``, ``from_alias``,
  ``address_type``, ``reply_to_address``). Hiding those keeps the
  call site readable.
- ``TeaException`` is a foreign exception type. We surface it as
  ``EmailSendError`` so callers can catch one specific exception
  without depending on the SDK's exception taxonomy.
- The endpoint construction (mainland vs ap-southeast-1) is fiddly
  and was already hardcoded in the smoke-test script — collapse it
  into one place.
"""

import logging

from alibabacloud_dm20151123 import models as dm_models
from alibabacloud_dm20151123.client import Client as DmClient
from alibabacloud_tea_openapi import models as openapi_models
from Tea.exceptions import TeaException

from config import settings

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when Aliyun rejects or the network drops a send.

    Always carries a human-readable ``message``. ``code`` is the
    Aliyun error code (e.g. ``InvalidAccessKeyId.NotFound``) when
    available, ``None`` for transport errors. ``request_id`` is the
    Aliyun trace id for log correlation."""

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


def _make_client() -> DmClient:
    if not settings.aliyun_dm_access_key_id or not settings.aliyun_dm_access_key_secret:
        raise EmailSendError(
            "Aliyun DM credentials missing — set "
            "``aliyun_dm_access_key_id`` and ``aliyun_dm_access_key_secret``"
        )
    if not settings.aliyun_dm_account_name:
        raise EmailSendError(
            "Aliyun DM sender address missing — set "
            "``aliyun_dm_account_name`` (must be approved in DM console)"
        )

    # Aliyun DirectMail uses a unified endpoint ``dm.aliyuncs.com`` for
    # mainland regions; only the Singapore overseas node has its own.
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


async def send_html_email(*, to: str, subject: str, html_body: str) -> str:
    """Send one transactional HTML email. Returns the Aliyun
    ``request_id`` on success; raises :class:`EmailSendError` on
    failure.

    The SDK call is awaited via the SDK's own async variant
    (``single_send_mail_async``) so the FastAPI event loop isn't
    blocked.
    """
    client = _make_client()
    request = dm_models.SingleSendMailRequest(
        account_name=settings.aliyun_dm_account_name,
        # ``address_type=1`` → use the verified sender at
        # ``account_name``. ``=0`` is the deprecated random-virtual-
        # sender path.
        address_type=1,
        from_alias=settings.aliyun_dm_from_alias,
        reply_to_address=False,
        to_address=to,
        subject=subject,
        html_body=html_body,
    )

    try:
        response = await client.single_send_mail_async(request)
    except TeaException as exc:
        request_id = exc.data.get("RequestId") if exc.data else None
        logger.warning(
            "Aliyun DM send failed: code=%s message=%s request_id=%s to=%s",
            exc.code,
            exc.message,
            request_id,
            to,
        )
        raise EmailSendError(
            message=exc.message or "Aliyun DM rejected the send",
            code=exc.code,
            request_id=request_id,
        ) from exc
    except Exception as exc:
        logger.exception("Aliyun DM transport failure to %s", to)
        raise EmailSendError(
            message=f"Aliyun DM transport failure: {type(exc).__name__}: {exc}"
        ) from exc

    request_id = response.body.request_id
    logger.info("Aliyun DM sent: request_id=%s to=%s", request_id, to)
    return request_id
