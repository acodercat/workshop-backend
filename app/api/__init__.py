"""The HTTP layer: two surfaces, symmetric by construction.

- :mod:`app.api.public` — the customer API, served by :mod:`app.main`
  under ``/api/v1``.
- :mod:`app.api.admin`  — the operations console, served by
  :mod:`app.admin_main` under ``/admin/api/v1``.

Each surface is a package with its own ``router.py`` (the ASGI app
mounts exactly one router) and owns its authorization; what both share —
request-scoped infrastructure like ``get_db`` — lives here in
:mod:`app.api.dependencies`. A surface earns a package because it is a
trust boundary (its own token class, gate, CORS, process), not because
it is a feature; a third surface would be a third sibling, never a
subdirectory of an existing one.
"""
