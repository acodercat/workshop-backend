"""The customer API — everything :mod:`app.public_main` serves under ``/api/v1``.

One module per resource, plus :mod:`~app.api.public.router` as the route
table. Authorization is the customer token classes in :mod:`app.auth`
(user access tokens and share-view tokens); the operations console is the
sibling surface :mod:`app.api.admin` and never mounts here.

Convention (see ``CLAUDE.md``): endpoint modules keep inline DB queries;
only non-trivial logic moves to ``app/services/``. Request/response
models live in the ``app/schemas/`` root — which belongs to this
surface, the same way ``app/schemas/admin/`` belongs to the console.
"""
