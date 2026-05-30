"""Locale-aware email templates.

Each helper here returns ``(subject, html_body)`` ready to pass to
:func:`app.services.email.client.send_html_email`. HTML is held in
source (rather than fetched from an SMTP provider's template store)
so the registration flow works identically across providers.
"""

import html
from typing import Literal

Locale = Literal["zh-CN", "en-US"]


_REGISTER_VERIFY_SUBJECT = {
    "zh-CN": "【HKGAI Agent】您的注册验证码 {code}",
    "en-US": "[HKGAI Agent] Verify your email — code {code}",
}


_REGISTER_VERIFY_BODY_ZH = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">验证您的邮箱</div>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">使用下方验证码完成 HKGAI Agent 账号注册。</p>
  <div style="font-family:-apple-system,'SF Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:600;letter-spacing:8px;color:#0a0a0a;margin:0 0 16px;">{code}</div>
  <p style="font-size:14px;color:#6b7280;margin:0 0 48px;">验证码将在 10 分钟后过期。</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px;">如果这不是您本人的操作，请忽略此邮件。</p>
    <p style="font-size:13px;color:#6b7280;margin:0;">HKGAI Agent 不会通过任何渠道向您索取此验证码。</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">香港生成式人工智能研发中心</div>
</div>"""


_REGISTER_VERIFY_BODY_EN = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">Verify your email</div>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">Use the code below to complete your HKGAI Agent account registration.</p>
  <div style="font-family:-apple-system,'SF Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:600;letter-spacing:8px;color:#0a0a0a;margin:0 0 16px;">{code}</div>
  <p style="font-size:14px;color:#6b7280;margin:0 0 48px;">This code will expire in 10 minutes.</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px;">If you didn't request this, you can safely ignore this email.</p>
    <p style="font-size:13px;color:#6b7280;margin:0;">HKGAI Agent will never ask you for this code through any channel.</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">Hong Kong Generative AI Research &amp; Development Center</div>
</div>"""


_REGISTER_VERIFY_BODY = {
    "zh-CN": _REGISTER_VERIFY_BODY_ZH,
    "en-US": _REGISTER_VERIFY_BODY_EN,
}


def _render(
    subject_map: dict[str, str],
    body_map: dict[str, str],
    *,
    code: str,
    locale: Locale,
) -> tuple[str, str]:
    """Format a (subject, body) pair from per-locale templates,
    falling back to ``zh-CN`` for unknown locales."""
    resolved: Locale = locale if locale in subject_map else "zh-CN"
    return (
        subject_map[resolved].format(code=code),
        body_map[resolved].format(code=code),
    )


def render_register_verification(*, code: str, locale: Locale) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the registration
    verification email in the requested locale."""
    return _render(
        _REGISTER_VERIFY_SUBJECT, _REGISTER_VERIFY_BODY, code=code, locale=locale
    )


_EMAIL_CHANGE_VERIFY_SUBJECT = {
    "zh-CN": "【HKGAI Agent】您的邮箱变更验证码 {code}",
    "en-US": "[HKGAI Agent] Confirm your new email — code {code}",
}


_EMAIL_CHANGE_VERIFY_BODY_ZH = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">确认变更登录邮箱</div>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">您正在将 HKGAI Agent 账号的登录邮箱变更为该地址。使用下方验证码完成验证。</p>
  <div style="font-family:-apple-system,'SF Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:600;letter-spacing:8px;color:#0a0a0a;margin:0 0 16px;">{code}</div>
  <p style="font-size:14px;color:#6b7280;margin:0 0 48px;">验证码将在 10 分钟后过期。</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px;">如果这不是您本人的操作，请立即登录 HKGAI Agent 修改密码并检查账号安全。</p>
    <p style="font-size:13px;color:#6b7280;margin:0;">HKGAI Agent 不会通过任何渠道向您索取此验证码。</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">香港生成式人工智能研发中心</div>
</div>"""


_EMAIL_CHANGE_VERIFY_BODY_EN = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">Confirm your new email</div>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">You're changing the login email for your HKGAI Agent account to this address. Use the code below to confirm.</p>
  <div style="font-family:-apple-system,'SF Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:600;letter-spacing:8px;color:#0a0a0a;margin:0 0 16px;">{code}</div>
  <p style="font-size:14px;color:#6b7280;margin:0 0 48px;">This code will expire in 10 minutes.</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px;">If you didn't request this, please sign in to HKGAI Agent, change your password, and review your account security.</p>
    <p style="font-size:13px;color:#6b7280;margin:0;">HKGAI Agent will never ask you for this code through any channel.</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">Hong Kong Generative AI Research &amp; Development Center</div>
</div>"""


_EMAIL_CHANGE_VERIFY_BODY = {
    "zh-CN": _EMAIL_CHANGE_VERIFY_BODY_ZH,
    "en-US": _EMAIL_CHANGE_VERIFY_BODY_EN,
}


def render_email_change_verification(
    *, code: str, locale: Locale
) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the email-change
    verification email in the requested locale. Copy emphasizes "you
    are changing your login email" so the recipient can spot a hijack
    attempt where someone else triggered the change."""
    return _render(
        _EMAIL_CHANGE_VERIFY_SUBJECT,
        _EMAIL_CHANGE_VERIFY_BODY,
        code=code,
        locale=locale,
    )


# Account-approved notification. Unlike the two templates above this
# carries no ``{code}`` — it's a one-way notice that an admin flipped a
# PENDING account to ACTIVE, so the user can now sign in. Same visual
# shell/branding as the verification emails. Note: there is no Aliyun
# BatchSendMail-side counterpart for this one — it's sent only via
# SingleSendMail (``send_html_email``), which takes the HTML inline.
_ACCOUNT_ACTIVATED_SUBJECT = {
    "zh-CN": "【HKGAI Agent】您的账号已激活",
    "en-US": "[HKGAI Agent] Your account is now active",
}


_ACCOUNT_ACTIVATED_BODY_ZH = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">账号已激活</div>
  <p style="font-size:15px;color:#374151;margin:0 0 16px;">您好 {name}，</p>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">您的 HKGAI Agent 账号已通过审核并激活，现在可以使用注册时填写的邮箱登录并开始使用。</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0;">如有任何疑问，请直接回复管理员或联系您的对接人。</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">香港生成式人工智能研发中心</div>
</div>"""


_ACCOUNT_ACTIVATED_BODY_EN = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">Your account is now active</div>
  <p style="font-size:15px;color:#374151;margin:0 0 16px;">Hi {name},</p>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">Your HKGAI Agent account has been approved and activated. You can now sign in with the email you registered with and start using it.</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0;">Questions? Just reply to this email or reach out to your contact.</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">Hong Kong Generative AI Research &amp; Development Center</div>
</div>"""


_ACCOUNT_ACTIVATED_BODY = {
    "zh-CN": _ACCOUNT_ACTIVATED_BODY_ZH,
    "en-US": _ACCOUNT_ACTIVATED_BODY_EN,
}


def render_account_activated(*, name: str, locale: Locale) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the account-activated notice.

    ``name`` is the user's display name, HTML-escaped before
    interpolation so a name containing ``<``/``&`` can't break or inject
    into the markup. Falls back to ``zh-CN`` for unknown locales.
    """
    resolved: Locale = locale if locale in _ACCOUNT_ACTIVATED_SUBJECT else "zh-CN"
    safe_name = html.escape(name)
    return (
        _ACCOUNT_ACTIVATED_SUBJECT[resolved],
        _ACCOUNT_ACTIVATED_BODY[resolved].format(name=safe_name),
    )
