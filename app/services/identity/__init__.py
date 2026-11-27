"""Federated sign-in.

An external identity provider proves who someone is; this package turns
that into a local account and a session, and keeps the session honest for
as long as it lives. Other modules see a :class:`~app.models.user.User`;
the one exception is the meeting workspace, which acts at the provider as
the user (:mod:`app.services.meeting.delegation`).

Sub-modules, in the order a sign-in passes through them:

- :mod:`.provider` — the Meeting service as the identity provider: what
  its answers mean for sign-in, and the grant it yields.
- :mod:`.federation` — an external identity → the local account it owns,
  creating that account the first time it is seen.
- :mod:`.provisioning` — what "creating that account" consists of.
- :mod:`.sessions` — the ``identity_session`` row: opening one, re-confirming
  it with the provider while it lives, revoking it, and handing its
  credential to the one caller that acts with it.
- :mod:`.credentials` — sealing the provider's credential for that row.
- :mod:`.signin` — the orchestration the endpoints call.

Nothing is re-exported here. :mod:`app.auth` imports :mod:`.sessions` and
:mod:`.signin` imports :mod:`app.auth`; a package ``__init__`` that pulled
in :mod:`.signin` would close that loop at import time.
"""
