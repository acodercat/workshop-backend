"""meeting workspace: settings, library, and recordings

The deployment's choice of who writes minutes (one seeded row,
``ck_meeting_settings_singleton``), the meeting library (summary templates
and vocabularies), and recordings on their way to Meeting. New tables only;
nothing existing changes. See ``app/models/meeting_settings.py``,
``meeting_library.py`` and ``meeting_recording.py``.

Revision ID: d5a1f3c7e9b2
Revises: e4b8a1c6d9f2
Create Date: 2026-11-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5a1f3c7e9b2"
down_revision: Union[str, Sequence[str], None] = "e4b8a1c6d9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SOURCE = sa.Enum("auto", "meeting", "system", name="minutes_source")
_TERM_KIND = sa.Enum("HOTWORD", "REPLACEMENT", name="meeting_term_kind")
_KIND = sa.Enum("LIVE", "UPLOAD", name="meeting_recording_kind")
_STATUS = sa.Enum("RECORDING", "ARCHIVING", "ARCHIVED", "FAILED", name="meeting_recording_status")
_ARCHIVE = sa.Enum("NOT_REQUESTED", "REQUESTED", "DONE", name="meeting_archive_state")
_ERROR = sa.Enum(
    "meeting_session_expired",
    "meeting_rate_limited",
    "meeting_refused",
    "meeting_service_unavailable",
    "no_speech",
    "meeting_archive_unknown",
    "recording_media_missing",
    name="meeting_recording_error",
)
_TARGET = sa.Enum("auto", "zh-HK", "zh-CN", "en-US", name="meeting_caption_target")


def upgrade() -> None:
    op.create_table(
        "meeting_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", _SOURCE, nullable=False),
        sa.Column("model", sa.String(length=200), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name="ck_meeting_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO meeting_settings (id, source) VALUES (1, 'auto')")

    op.create_table(
        "meeting_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("sections", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "options", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meeting_template_user_updated", "meeting_template", ["user_id", "updated_at"]
    )

    op.create_table(
        "meeting_library_preference",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("default_template_id", sa.String(length=64), nullable=True),
        sa.Column("vocabulary_list_version", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "meeting_vocabulary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "categories", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("display_name", sa.String(length=40), nullable=True),
        sa.Column("hidden", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_meeting_vocabulary_personal",
        "meeting_vocabulary",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_meeting_vocabulary_project",
        "meeting_vocabulary",
        ["user_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "meeting_vocabulary_term",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vocabulary_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", _TERM_KIND, nullable=False),
        sa.Column("text", sa.String(length=256), nullable=False),
        sa.Column("replacement", sa.String(length=256), server_default="", nullable=False),
        sa.Column(
            "aliases", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("origin", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["vocabulary_id"], ["meeting_vocabulary.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vocabulary_id", "kind", "text", name="uq_meeting_vocabulary_term"),
    )
    op.create_index(
        "ix_meeting_vocabulary_term_vocabulary",
        "meeting_vocabulary_term",
        ["vocabulary_id", "position"],
    )

    op.create_table(
        "meeting_recording",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("identity_session_id", sa.Uuid(), nullable=True),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("meeting_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("target", _TARGET, nullable=True),
        sa.Column("units", sa.Integer(), server_default="0", nullable=False),
        sa.Column("samples", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("media_key", sa.String(length=512), nullable=True),
        sa.Column("media_name", sa.String(length=255), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("archive_state", _ARCHIVE, server_default="NOT_REQUESTED", nullable=False),
        sa.Column("archived_meeting_id", sa.String(length=128), nullable=True),
        sa.Column("error", _ERROR, nullable=True),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["identity_session_id"], ["identity_session.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_recording_user", "meeting_recording", ["user_id", "created_at"])
    op.create_index(
        "uq_meeting_recording_media_key",
        "meeting_recording",
        ["media_key"],
        unique=True,
        postgresql_where=sa.text("media_key IS NOT NULL"),
    )
    op.create_index(
        "ix_meeting_recording_archiving",
        "meeting_recording",
        ["lease_until"],
        postgresql_where=sa.text("status = 'ARCHIVING'"),
    )
    op.create_table(
        "meeting_recording_window",
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("pcm", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["recording_id"], ["meeting_recording.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recording_id", "seq"),
    )
    op.create_table(
        "meeting_recording_line",
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("translation", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["recording_id"], ["meeting_recording.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recording_id", "seq"),
    )


def downgrade() -> None:
    op.drop_table("meeting_recording_line")
    op.drop_table("meeting_recording_window")
    op.drop_table("meeting_recording")
    op.drop_table("meeting_vocabulary_term")
    op.drop_table("meeting_vocabulary")
    op.drop_table("meeting_library_preference")
    op.drop_table("meeting_template")
    op.drop_table("meeting_settings")
    bind = op.get_bind()
    for enum in (_ARCHIVE, _STATUS, _KIND, _ERROR, _TARGET, _TERM_KIND, _SOURCE):
        enum.drop(bind, checkfirst=False)
