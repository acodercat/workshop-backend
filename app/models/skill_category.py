"""skill_category: how the skills catalog is grouped for browsing.

One row is a tab on the skills page — the strip that follows the pinned
「个人」 tab. Only OFFICIAL skills are grouped: a user's own CUSTOM uploads live
behind 「个人」, are few, and are not the platform's to classify.

A table rather than an enum because the thing it groups is itself
operator-managed: ``scripts/upload_skill.py`` seeds official skills with no
deploy, and a taxonomy that needed a migration to gain a row would be the one
part of the catalog that did.

Separate from ``connector_category`` on purpose. The two vocabularies do not
overlap — a skill is a capability, a Connector is an outside system — and one
shared table would need a scope column that every query had to remember to
filter on, where forgetting is silent. Two tables put that guarantee in the
foreign key instead.
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SkillCategory(Base):
    __tablename__ = "skill_category"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    # The tab's label, and what every locale falls back to. Unique because
    # there is no slug: this is what an operator picks the category by in the
    # console, and two rows reading 「写作」 leave them nothing to choose with.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional ``{locale: name}`` overrides. NULL when the category reads the
    # same everywhere — the column above is what every locale falls back to, so
    # nothing has to special-case an untranslated row.
    # ``none_as_null`` so "no translations" persists as SQL NULL rather than a
    # JSONB ``'null'`` literal, keeping ``IS NULL`` honest.
    name_i18n: Mapped[dict[str, str] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    # Tab visibility, the same word and the same reason as
    # ``mcp_server_catalog.published``: whether a category is offered is the
    # operator's call. Unpublishing hides the tab without deleting the row, so
    # the skills pointing at it keep their foreign key and simply read as
    # uncategorised until it comes back.
    published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    # Left-to-right order of the tabs after the pinned one.
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # The strip is only ever read published-first in display order.
        Index(
            "ix_skill_category_published_sort_order",
            "published",
            "sort_order",
        ),
        UniqueConstraint("name", name="uq_skill_category_name"),
    )

    def __repr__(self) -> str:
        return f"<SkillCategory(id={self.id}, name={self.name!r}, published={self.published})>"
