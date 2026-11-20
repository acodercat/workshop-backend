"""How a user has arranged their sidebar: the order of its pinned items and
of its projects.

A preference about positions only. Whether a project or session is pinned
is its own ``pinned_at``; these lists only say where the owner dragged
things to. An entry naming something deleted, or no longer pinned, is
harmless: the client places what exists and skips the rest, and its next
save leaves the stale entry out. So nothing that deletes or unpins writes
here.

One row per user, created by their first save. A user without one sees
the default order: pinned items most recently pinned first, projects most
recently updated first.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, ForeignKey, Integer, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SidebarLayout(Base):
    __tablename__ = "sidebar_layout"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The pinned group, top to bottom: ``{"kind": "project" | "session",
    # "id": "<uuid>"}`` — projects and sessions sort together there.
    pin_order: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # The projects group, top to bottom, by project id.
    project_order: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<SidebarLayout(user_id={self.user_id})>"
