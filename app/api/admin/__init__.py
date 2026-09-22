"""Admin console — the internal operations HTTP surface.

A separate **bounded context**, not a separate service. The split runs
along two independent axes:

**Code and schema are shared.** Admin endpoints import ``app.models``,
``app.services`` and ``app.database`` directly, and admin tables ride in
the same alembic history as everything else. A second service against the
same database would guarantee model drift and — worse — would have to
re-implement the service funnels that hold the money invariants
(``credits._internal``). Re-implementing those is how ledgers stop
balancing.

**The runtime surface is not.** The console is served by its own ASGI app
in its own process — the public API never mounts an admin route, and the
admin app never mounts a customer route. Those runtime concerns (port,
CORS, deployment) are documented where they are configured, in
:mod:`app.admin_main`; the stack they share is :mod:`app.asgi`.

Admin code follows the same layer-first layout as the rest of the app
rather than forming a vertical slice of its own — it is a sub-namespace
*inside* each layer:

- ``app/api/admin/``      — this package: the HTTP surface, sibling of
                            :mod:`app.api.public`
- ``app/schemas/admin/``  — admin request/response models
- ``app/services/admin/`` — admin-only domain logic (staff)
- ``app/auth.py``         — the ``scope="staff"`` token class, beside the
                            two it must stay mutually exclusive with
- ``app/models/``         — ``staff``, like any other table

Within this package:

- :mod:`~app.api.admin.dependencies` — ``StaffContext``, the
  authenticated operator plus the session their writes run in. There is
  no unauthenticated way to obtain one; there are no role tiers (every
  active operator has full access — see ``docs/admin-console.md``).
- :mod:`~app.api.admin.router` — the route table; the ASGI app mounts
  exactly one router.
- everything else — one module per resource (``user``, ``skill``,
  ``invite``, ``connector``, …), directly in the package: inside a named
  surface, every module *is* an endpoint module except the three
  infrastructure files, which name themselves.

Two rules bind everything admin:

1. **Invariants live in the service layer; plain reads and plain writes
   stay inline.** A column with a rule attached — a status that must not
   orphan the last active operator, a balance that must stay equal to its
   ledger — is written through the service that owns the rule, never by
   an endpoint re-deriving it. A column with no invariant (``user.status``,
   ``feedback.status``, a skill's picker metadata) is assigned on the ORM
   row right here; routing it through a service function that would only
   forward buys indirection and no guarantee.
2. **Every mutation commits itself** — ``await ctx.db.commit()`` as the
   last write of the endpoint. This deployment keeps no operator trail,
   so the ``audit()`` funnel that owns the commit upstream is absent
   (revision ``c8f2a5e1d94b``); with it went the property that an
   endpoint which forgot to audit also failed to save. A mutation that
   forgets to commit silently does not stick, so this one is a
   convention to remember — ``tests/test_admin_mutations_commit.py``
   pins it for the endpoints it covers.

:mod:`app.api.admin.staff` is the worked example of both.
"""
