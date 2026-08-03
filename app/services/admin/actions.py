"""The audit trail's action vocabulary.

A Python StrEnum over a plain VARCHAR column: adding a member is a
one-line change with no migration, but a typo doesn't compile and the
audit browser's filter has a list to offer. Only actions the console can
actually perform today belong here — land the slice, then add the member.
"""

from enum import StrEnum


class AuditAction(StrEnum):
    """``<domain>.<verb>`` — grouped by domain so the console can filter
    on a prefix (``staff.*``)."""

    STAFF_LOGIN = "staff.login"
    STAFF_CREATE = "staff.create"
    STAFF_UPDATE = "staff.update"
    STAFF_PASSWORD_RESET = "staff.password_reset"
    STAFF_PASSWORD_CHANGE = "staff.password_change"
