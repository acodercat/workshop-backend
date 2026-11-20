"""Schemas for the /api/projects surface."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.text import printable
from app.models.project_file import ProjectFileOrigin
from app.schemas.session import FileRef

# What the instructions dialog lets the owner type. Every session in the
# project carries these in its system prompt, so this bounds a cost paid on
# every turn, not just storage.
_INSTRUCTIONS_MAX_LENGTH = 8000

# An icon is named, not drawn, here: a lowercase kebab-case name from the
# client's catalog (``project.icon``).
_ICON_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


def _checked_name(value: str | None) -> str | None:
    """Trim a project name, and refuse one that shows nothing.

    ``None`` passes: on ``PATCH`` it means "leave the name alone".

    A name made only of invisible characters — zero-width spaces, say —
    passes ``min_length`` and would list as a blank row in the sidebar.
    U+0000 is not representable in a ``varchar`` column, so it is refused
    here rather than failing the INSERT as an opaque 500.
    """
    if value is None:
        return None
    if "\x00" in value:
        raise ValueError("name must not contain a null character")
    name = value.strip()
    if not printable(name).strip():
        raise ValueError("name must contain visible text")
    return name


def _checked_instructions(value: str | None) -> str | None:
    """Refuse instructions that cannot be stored. Empty is allowed: it is
    how the owner clears them."""
    if value is not None and "\x00" in value:
        raise ValueError("instructions must not contain a null character")
    return value


class ProjectCreate(BaseModel):
    """Body of ``POST /api/projects``."""

    name: str = Field(..., min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=_INSTRUCTIONS_MAX_LENGTH)
    icon: str | None = Field(default=None, max_length=32, pattern=_ICON_PATTERN)

    _check_name = field_validator("name")(_checked_name)
    _check_instructions = field_validator("instructions")(_checked_instructions)


class ProjectUpdate(BaseModel):
    """Body of ``PATCH /api/projects/{id}``. ``None`` leaves a field alone.

    ``pinned``: ``True`` pins the project, ``False`` unpins it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    instructions: str | None = Field(default=None, max_length=_INSTRUCTIONS_MAX_LENGTH)
    icon: str | None = Field(default=None, max_length=32, pattern=_ICON_PATTERN)
    pinned: bool | None = None

    _check_name = field_validator("name")(_checked_name)
    _check_instructions = field_validator("instructions")(_checked_instructions)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    instructions: str
    icon: str | None
    created_at: datetime
    updated_at: datetime
    # When the owner pinned the project; ``None`` = not pinned.
    pinned_at: datetime | None


class SessionFileRef(BaseModel):
    """One of the caller's session files, by the identity it has on the
    wire: the session and the ``file_key`` bound to it."""

    session_id: UUID
    file_key: str = Field(..., min_length=1, max_length=512)


class ProjectFileCreate(BaseModel):
    """Body of ``POST /api/projects/{id}/files``: exactly one source.

    ``upload`` is a file the caller has just PUT to OSS through
    ``/sessions/upload-url``, the same shape a message attaches.
    ``session_file`` is one already bound to one of the caller's sessions.
    Either way the project references the object; nothing is copied.
    """

    upload: FileRef | None = None
    session_file: SessionFileRef | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ProjectFileCreate":
        if (self.upload is None) == (self.session_file is None):
            raise ValueError("Give exactly one of upload or session_file")
        return self


class ProjectFileResponse(BaseModel):
    """The current version of one file. Listed without its OSS key: the
    client names a project file by ``id``, never by key."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    version: int
    mime_type: str | None
    size_bytes: int
    origin: ProjectFileOrigin
    origin_session_id: UUID | None
    created_at: datetime
