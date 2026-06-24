"""Aliyun SMS dispatch — phone verification codes.

One seam (:func:`send_verification_sms`) over Aliyun 短信服务 (dysmsapi);
the SDK is imported nowhere else. Mirrors :mod:`app.services.email`.
"""

from app.services.sms.client import SmsSendError, send_verification_sms

__all__ = ["SmsSendError", "send_verification_sms"]
