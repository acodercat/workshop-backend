"""Provider-layer internal exceptions.

These are raised inside the payment adapters for control flow within the
service package. They are distinct from the ``AppError`` subclasses in
``app/errors.py`` (which the HTTP layer serializes): the adapter raises a
domain-level provider error, and the endpoint / service translates it to
the public :class:`~app.errors.PaymentProviderError` envelope. Not
re-exported from the package ``__init__``.
"""


class NotifySignatureError(Exception):
    """An async notification failed signature verification. The handler
    must reject it (HTTP 400) and must NOT send an acknowledgement —
    acking an unverifiable body would tell the provider to stop
    redelivering a message we never trusted."""
