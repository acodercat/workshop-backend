"""Locale-aware email templates.

Each helper here returns ``(subject, html_body)`` ready to pass to
:func:`app.services.email.client.send_html_email`. HTML mirrors the
approved Aliyun templates ``axon_register_verify_zh`` /
``axon_register_verify_en`` — kept in source so the registration
flow doesn't depend on Aliyun's BatchSendMail-only template path.

If you change wording / styling here, update the Aliyun templates
in lockstep so any future migration to BatchSendMail stays seamless.
"""

from typing import Literal

Locale = Literal["zh-CN", "en-US"]


_REGISTER_VERIFY_SUBJECT = {
    "zh-CN": "【AXON】您的注册验证码 {code}",
    "en-US": "[AXON] Verify your email — code {code}",
}


_REGISTER_VERIFY_BODY_ZH = """<div style="max-width:560px;margin:0 auto;padding:32px 24px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1a1a1a;background:#ffffff;">
  <div style="font-size:18px;font-weight:500;margin-bottom:24px;">AXON</div>
  <p style="font-size:15px;line-height:1.7;margin:0 0 16px;">您好，</p>
  <p style="font-size:15px;line-height:1.7;margin:0 0 24px;">您正在注册 AXON 账号。请使用以下验证码完成邮箱验证：</p>
  <div style="margin:24px 0 32px;padding:24px;background:#f5f6f8;border-radius:8px;text-align:center;">
    <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:32px;font-weight:600;letter-spacing:6px;color:#1254c7;">{code}</div>
  </div>
  <p style="font-size:14px;line-height:1.7;margin:0 0 8px;color:#6b7280;">验证码 10 分钟内有效，请勿向他人泄露。</p>
  <p style="font-size:14px;line-height:1.7;margin:0 0 24px;color:#6b7280;">如果这不是您本人的操作，请忽略本邮件。</p>
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;">本邮件由 AXON 系统自动发送，请勿回复。</div>
</div>"""


_REGISTER_VERIFY_BODY_EN = """<div style="max-width:560px;margin:0 auto;padding:32px 24px;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;color:#1a1a1a;background:#ffffff;">
  <div style="font-size:18px;font-weight:500;margin-bottom:24px;">AXON</div>
  <p style="font-size:15px;line-height:1.7;margin:0 0 16px;">Hi,</p>
  <p style="font-size:15px;line-height:1.7;margin:0 0 24px;">You're registering an AXON account. Use the verification code below to confirm your email:</p>
  <div style="margin:24px 0 32px;padding:24px;background:#f5f6f8;border-radius:8px;text-align:center;">
    <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:32px;font-weight:600;letter-spacing:6px;color:#1254c7;">{code}</div>
  </div>
  <p style="font-size:14px;line-height:1.7;margin:0 0 8px;color:#6b7280;">The code is valid for 10 minutes. Do not share it with anyone.</p>
  <p style="font-size:14px;line-height:1.7;margin:0 0 24px;color:#6b7280;">If you didn't request this, you can safely ignore this email.</p>
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;">This is an automated message from AXON. Please do not reply.</div>
</div>"""


_REGISTER_VERIFY_BODY = {
    "zh-CN": _REGISTER_VERIFY_BODY_ZH,
    "en-US": _REGISTER_VERIFY_BODY_EN,
}


def render_register_verification(*, code: str, locale: Locale) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the registration
    verification email in the requested locale. Unknown locales fall
    back to ``zh-CN``."""
    locale = locale if locale in _REGISTER_VERIFY_SUBJECT else "zh-CN"
    subject = _REGISTER_VERIFY_SUBJECT[locale].format(code=code)
    html_body = _REGISTER_VERIFY_BODY[locale].format(code=code)
    return subject, html_body
