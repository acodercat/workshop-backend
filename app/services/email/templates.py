"""Locale-aware email templates.

Each helper here returns ``(subject, html_body)`` ready to pass to
:func:`app.services.email.client.send_html_email`. HTML is held in
source (rather than fetched from an SMTP provider's template store)
so the operator notices — account activation, credit top-up — read the
same whichever provider sends them.
"""

import html
from typing import Literal, cast

Locale = Literal["zh-CN", "en-US"]


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


def render_account_activated(*, name: str, locale: str) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the account-activated notice.

    ``name`` is the user's display name, HTML-escaped before
    interpolation so a name containing ``<``/``&`` can't break or inject
    into the markup. Falls back to ``zh-CN`` for unknown locales.
    """
    # ``cast`` because membership in the map is what makes it a ``Locale``,
    # and an ``in`` test over a dict does not narrow a ``str``.
    resolved: Locale = cast("Locale", locale) if locale in _ACCOUNT_ACTIVATED_SUBJECT else "zh-CN"
    safe_name = html.escape(name)
    return (
        _ACCOUNT_ACTIVATED_SUBJECT[resolved],
        _ACCOUNT_ACTIVATED_BODY[resolved].format(name=safe_name),
    )


# Credits top-up notification. Like the account-activated notice this is a
# one-way notice — an admin added credits to the account,
# so the copy carries the amount added and the resulting balance. Same
# visual shell/branding.
_CREDITS_TOPPED_UP_SUBJECT = {
    "zh-CN": "【HKGAI Agent】已为您的账号充值 {credits} credits",
    "en-US": "[HKGAI Agent] {credits} credits added to your account",
}


_CREDITS_TOPPED_UP_BODY_ZH = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">账号充值成功</div>
  <p style="font-size:15px;color:#374151;margin:0 0 16px;">您好 {name}，</p>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">您的 HKGAI Agent 账号已成功充值 {credits} credits，当前余额 {balance} credits。</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0;">如有任何疑问，请直接回复管理员或联系您的对接人。</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">香港生成式人工智能研发中心</div>
</div>"""


_CREDITS_TOPPED_UP_BODY_EN = """<div style="max-width:480px;margin:0 auto;padding:48px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0a0a0a;background:#ffffff;line-height:1.6;">
  <div style="font-size:13px;font-weight:500;color:#0a0a0a;letter-spacing:0.5px;margin-bottom:48px;">HKGAI Agent</div>
  <div style="font-size:20px;font-weight:600;color:#0a0a0a;margin-bottom:24px;line-height:1.4;">Credits added</div>
  <p style="font-size:15px;color:#374151;margin:0 0 16px;">Hi {name},</p>
  <p style="font-size:15px;color:#374151;margin:0 0 32px;">Your HKGAI Agent account has been credited with {credits} credits. Your current balance is {balance} credits.</p>
  <div style="border-top:1px solid #e5e7eb;padding-top:32px;">
    <p style="font-size:13px;color:#6b7280;margin:0;">Questions? Just reply to this email or reach out to your contact.</p>
  </div>
  <div style="margin-top:48px;font-size:12px;color:#9ca3af;line-height:1.7;">Hong Kong Generative AI Research &amp; Development Center</div>
</div>"""


_CREDITS_TOPPED_UP_BODY = {
    "zh-CN": _CREDITS_TOPPED_UP_BODY_ZH,
    "en-US": _CREDITS_TOPPED_UP_BODY_EN,
}


def render_credits_topped_up(
    *, name: str, credits_added: int, balance: int, locale: str
) -> tuple[str, str]:
    """Returns ``(subject, html_body)`` for the credits top-up notice.

    A one-way notice that an admin added credits to the account, carrying
    the amount added and the resulting balance (both thousands-grouped).
    ``name`` is HTML-escaped before interpolation; the amounts are
    integers, so they need no escaping. Falls back to ``zh-CN`` for
    unknown locales — same shape as :func:`render_account_activated`.
    """
    resolved: Locale = cast("Locale", locale) if locale in _CREDITS_TOPPED_UP_SUBJECT else "zh-CN"
    credits_str = f"{credits_added:,}"
    return (
        _CREDITS_TOPPED_UP_SUBJECT[resolved].format(credits=credits_str),
        _CREDITS_TOPPED_UP_BODY[resolved].format(
            name=html.escape(name),
            credits=credits_str,
            balance=f"{balance:,}",
        ),
    )
