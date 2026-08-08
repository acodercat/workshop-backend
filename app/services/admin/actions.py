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
    USER_UPDATE = "user.update"
    CREDIT_ADJUST = "credit.adjust"
    FEEDBACK_UPDATE = "feedback.update"
    SKILL_UPLOAD = "skill.upload"
    SKILL_UPDATE = "skill.update"
    CONNECTOR_CATALOG_CREATE = "connector_catalog.create"
    CONNECTOR_CATALOG_UPDATE = "connector_catalog.update"
    INVITE_CREATE = "invite.create"
    # The console's one audited *read*: customer conversation content.
    SESSION_VIEW = "session.view"
