"""Provider-layer internal exceptions.

These are raised inside the payment adapters for control flow within the
service package. They are distinct from the ``AppError`` subclasses in
``app/errors.py`` (which the HTTP layer serializes): the adapter raises a
domain-level provider error, and the service translates a failed charge
or query into the public :class:`~app.errors.PaymentProviderError`
envelope. A notification that fails verification is not translated —
the webhook answers it with a bare 400 so the provider stops there. Not
re-exported from the package ``__init__``.
"""


class NotifyVerificationError(Exception):
    """An async notification could not be verified as the provider's word
    about one of our charges: its signature failed, its body did not
    parse, or it names another merchant. The handler must reject it
    (HTTP 400) and must NOT send an acknowledgement — acking an
    unverifiable body would tell the provider to stop redelivering a
    message we never trusted."""
