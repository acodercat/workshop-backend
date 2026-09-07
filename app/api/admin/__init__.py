"""Admin console — the internal operations HTTP surface.

A separate **bounded context**, not a separate service. The split runs
along two independent axes:

**Code and schema are shared.** Admin endpoints import ``app.models``,
``app.services`` and ``app.database`` directly, and admin tables ride in
the same alembic history as everything else. A second service against the
same database would guarantee model drift and — worse — would have to
re-implement the service funnels that hold the money invariants
(``credits._internal``, ``commission._internal``, ``payment.orders``).
Re-implementing those is how ledgers stop balancing.

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
  authenticated operator plus the request context. There is no
  unauthenticated way to obtain one; there
  are no role tiers (every active operator has full access — see
  ``docs/admin-console.md``).
- :mod:`~app.api.admin.router` — the route table; the ASGI app mounts
  exactly one router.
- everything else — one module per resource (``auth``, ``me``,
  ``staff``), directly in the package: inside a named
  surface, every module *is* an endpoint module except the two
  infrastructure files, which name themselves.

Two rules bind everything admin:

1. **Never write a domain table directly.** Call the service that owns
   it. If no service function exists for what the console needs, add one
   under ``app/services/`` rather than issuing the UPDATE from an
   endpoint.
2. **Every mutation commits itself** — ``await ctx.db.commit()`` as the
   last statement of the endpoint. This deployment keeps no operator
   trail, so the ``audit()`` funnel that used to own the commit is gone
   (revision ``c8f2a5e1d94b``); with it went the property that an
   endpoint which forgot to audit also failed to save. A mutation that
   forgets to commit now silently does not stick, so this one is a
   convention to remember.

:mod:`app.api.admin.staff` is the worked example of both.
"""
