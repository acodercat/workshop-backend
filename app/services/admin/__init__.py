"""Admin-console domain logic.

The console's machinery is re-exported here so callers write
``from app.services.admin import AdminContext`` without knowing which
submodule it lives in — the same convention as
:mod:`app.services.session`. The exception is
:mod:`~app.services.admin.staff`, imported as a module and aliased
(``from app.services.admin import staff as staff_service``) because its
functions are named for their noun — ``staff_service.create``,
``staff_service.update`` — and the bare name collides with the ``Staff``
rows its callers are holding.

Submodule responsibilities:

- :mod:`~app.services.admin.context` — :class:`AdminContext`, one
  authenticated console request as pure data.
- :mod:`~app.services.admin.staff` — operator-account lifecycle.

This deployment keeps **no operator trail**: the ``admin_audit_log``
table, its ``audit()`` funnel and the action vocabulary were removed
(revision ``c8f2a5e1d94b``). Each console mutation now ends with its own
``await ctx.db.commit()``; the funnel's side benefit — an endpoint that
forgot to audit also failed to save — is gone with it, so a mutation
that forgets to commit silently does not stick.

Note what does **not** belong here. The console's credit adjustments and
skill publishing go through the services that already own those tables
(:mod:`app.services.credits`, :mod:`app.services.skill_registration`).
Withdrawal approvals are master's — this deployment sells no credits, so
the commission wallet and its payouts are excluded (``HKGAI_MERGE.md``).
Re-implementing them under an ``admin`` namespace would fork the
invariants they protect — which is how ledgers stop balancing. Staff
accounts live here only because no other module owns them.
"""

from app.services.admin.context import AdminContext

__all__ = [
    "AdminContext",
]
