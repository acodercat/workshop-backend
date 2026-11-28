"""A user's meeting library: summary templates and vocabularies.

**Templates** shape the minutes the model drafts: a title, ordered
sections, per-section guidance and options. The built-in templates are not
rows — they ship with the code (``app/services/meeting/data``) — and a
user's default may be either kind, so it is stored by id in
:class:`MeetingLibraryPreference` rather than as a flag on a row.

**Vocabularies** are the terms speech recognition should get right
(``hotword``) or correct (``replacement``). A user has a personal one and
one per project; each is versioned as a whole, and edits are
compare-and-swap against the version the editor read.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class MeetingTemplate(Base):
    __tablename__ = "meeting_template"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    # Ordered section names. A list, not newline-joined text: a section
    # name containing a newline must not become two sections.
    sections: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # ``tone``, ``missing``, ``instructions``, ``output_format``,
    # ``table_fields``, ``section_guidance`` (one per section). Validated by
    # :class:`~app.schemas.meeting_library.TemplateOptions` on every write.
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_meeting_template_user_updated", "user_id", "updated_at"),)


class MeetingLibraryPreference(Base):
    """Per-user library settings that are not part of any one item."""

    __tablename__ = "meeting_library_preference"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    # A personal template's UUID or a built-in's id. Not a foreign key for
    # that reason; a default that no longer resolves reads as unset.
    default_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The vocabulary list's own revision: renaming or hiding a vocabulary in
    # the list bumps it, so two tabs editing the list cannot overwrite each
    # other.
    vocabulary_list_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )


class MeetingVocabulary(Base):
    """One vocabulary: the user's personal one (``project_id`` NULL) or one
    project's. Holds the version and the categories; terms are rows of
    their own."""

    __tablename__ = "meeting_vocabulary"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("project.id", ondelete="CASCADE"), nullable=True
    )
    # Bumped by every change to the terms or the categories.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # ``[{"id": str, "name": str}]``, in display order. At most 100.
    categories: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # A display name overriding the project's own, and whether the user has
    # hidden this vocabulary from their list. Neither changes the terms.
    display_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        # One personal vocabulary per user, one per project. Two partial
        # indexes because ``UNIQUE`` treats NULL project ids as distinct.
        Index(
            "uq_meeting_vocabulary_personal",
            "user_id",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_meeting_vocabulary_project",
            "user_id",
            "project_id",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
    )


class TermKind(StrEnum):
    HOTWORD = "HOTWORD"
    REPLACEMENT = "REPLACEMENT"


class MeetingVocabularyTerm(Base):
    __tablename__ = "meeting_vocabulary_term"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    vocabulary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("meeting_vocabulary.id", ondelete="CASCADE"), nullable=False
    )
    # Display order, as the user saved it.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[TermKind] = mapped_column(
        SAEnum(
            TermKind,
            name="meeting_term_kind",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    # The spelling to recognize, or (for a replacement) the misrecognition.
    text: Mapped[str] = mapped_column(String(256), nullable=False)
    # For a replacement: what ``text`` is corrected to.
    replacement: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # A category id from the vocabulary's ``categories``, or NULL.
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Bumped each time the term is saved, so its history can be told apart.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Where the term was learned from — a passage of a meeting, an entry of
    # an imported glossary. Kept as given; displayed, never interpreted.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    origin: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # A term's identity within its vocabulary: saving the same spelling
        # again updates it rather than adding a second.
        UniqueConstraint("vocabulary_id", "kind", "text", name="uq_meeting_vocabulary_term"),
        Index("ix_meeting_vocabulary_term_vocabulary", "vocabulary_id", "position"),
    )
