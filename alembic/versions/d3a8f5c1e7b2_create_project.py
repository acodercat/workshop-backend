"""projects: project + project_file, and session.project_id

A project groups a user's sessions under shared instructions and files;
see ``app/models/project.py`` and ``app/models/project_file.py``.

The two new tables are empty, so they are built in the ordinary
transaction. ``session`` is not, and the two changes to it are kept off its
write path:

- ``project_id`` is nullable with no default — a catalog-only change. Its
  foreign key is added ``NOT VALID`` (a brief lock, no scan), then
  validated in a transaction of its own: ``VALIDATE CONSTRAINT`` scans
  under ``SHARE UPDATE EXCLUSIVE``, which blocks no reads or writes, but
  only once the ``ADD``'s stronger lock has been released by a commit.
- ``ix_session_project_id_updated_at`` is built ``CONCURRENTLY``, which
  also has to run outside the wrapping transaction.

Both therefore sit in the ``autocommit_block`` at the end, which commits
everything before it first.

Revision ID: d3a8f5c1e7b2
Revises: 0e2330db080c
Create Date: 2026-11-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3a8f5c1e7b2"
down_revision: Union[str, Sequence[str], None] = "0e2330db080c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SESSION_FK = "session_project_id_fkey"
_SESSION_INDEX = "ix_session_project_id_updated_at"


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("instructions", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_user_id_updated_at",
        "project",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "project_file",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("file_key", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "origin",
            sa.Enum("UPLOAD", "SESSION_FILE", "AGENT", name="project_file_origin"),
            nullable=False,
        ),
        sa.Column("origin_session_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(origin = 'UPLOAD') = (origin_session_id IS NULL)",
            name="ck_project_file_origin_session_consistent",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "filename",
            "version",
            name="uq_project_file_project_id_filename_version",
        ),
    )
    op.create_index("ix_project_file_user_id", "project_file", ["user_id"])
    op.create_index("ix_project_file_file_key", "project_file", ["file_key"])

    op.add_column("session", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        _SESSION_FK,
        "session",
        "project",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
        postgresql_not_valid=True,
    )

    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE session VALIDATE CONSTRAINT {_SESSION_FK}")
        op.create_index(
            _SESSION_INDEX,
            "session",
            ["project_id", sa.text("updated_at DESC")],
            postgresql_where=sa.text("project_id IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _SESSION_INDEX,
            table_name="session",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_constraint(_SESSION_FK, "session", type_="foreignkey")
    op.drop_column("session", "project_id")

    op.drop_index("ix_project_file_file_key", table_name="project_file")
    op.drop_index("ix_project_file_user_id", table_name="project_file")
    op.drop_table("project_file")
    sa.Enum(name="project_file_origin").drop(op.get_bind())

    op.drop_index("ix_project_user_id_updated_at", table_name="project")
    op.drop_table("project")
