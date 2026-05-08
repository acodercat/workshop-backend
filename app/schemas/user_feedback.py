"""Wire schemas for the user-feedback feature.

Endpoint surface today is owner-side only (the user submitting
feedback). An admin-side response shape will be added when the
internal review tool ships; for now admins read directly from the
table.
"""

from pydantic import BaseModel, Field, field_validator

# Hard caps mirror the frontend's input limits — backend re-enforces
# so a tampered client can't bypass.
_MESSAGE_MAX_LENGTH = 2000
_MAX_IMAGES = 5
_FILE_KEY_MAX_LENGTH = 256


class FeedbackSubmitRequest(BaseModel):
    """Body for ``POST /feedback``.

    The ``message`` validator strips whitespace and rejects rows that
    are blank after trimming — clients must send substantive text.
    Image keys are accepted as-is; ownership / existence in OSS is not
    pre-validated (the upload flow already minted these keys for the
    same JWT, so a key that doesn't exist would be a self-inflicted
    issue at admin-display time, not a security boundary)."""

    message: str = Field(..., min_length=1, max_length=_MESSAGE_MAX_LENGTH)
    image_keys: list[str] = Field(
        default_factory=list, max_length=_MAX_IMAGES
    )

    @field_validator("message")
    @classmethod
    def _strip_or_reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Feedback message cannot be blank.")
        return stripped

    @field_validator("image_keys")
    @classmethod
    def _validate_image_keys(cls, value: list[str]) -> list[str]:
        for key in value:
            if not key or len(key) > _FILE_KEY_MAX_LENGTH:
                raise ValueError(
                    f"Image key length must be 1..{_FILE_KEY_MAX_LENGTH}."
                )
        return value


__all__ = ["FeedbackSubmitRequest"]
