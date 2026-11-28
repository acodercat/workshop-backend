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

This deployment keeps **no operator trail** (no ``admin_audit_log``, no
``audit()`` funnel). Each console mutation ends with its own
``await ctx.db.commit()``, so a mutation that forgets to commit silently
does not stick.

Note what does **not** belong here. The console's credit adjustments and
skill publishing go through the services that already own those tables
(:mod:`app.services.credits`, :mod:`app.services.skill_registration`).
Re-implementing them under an ``admin`` namespace would fork the
invariants they protect — which is how ledgers stop balancing. Staff
accounts live here only because no other module owns them.
"""

from app.services.admin.context import AdminContext

__all__ = [
    "AdminContext",
]
