from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings

engine = create_async_engine(
    settings.db_uri,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle_seconds,
    pool_timeout=settings.db_pool_timeout_seconds,
    pool_pre_ping=True,
    echo=settings.db_echo,
    echo_pool=settings.db_echo_pool,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def release_connection(db: AsyncSession) -> None:
    """End the read-only prelude's transaction before a third-party round-trip.

    A pooled connection is held from the first query of the request — which is
    the *auth* dependency's, before any handler body runs — until something
    commits. A probe or an OAuth discovery in between leaves it
    ``idle in transaction``: checked out, holding its snapshot, and doing
    nothing, for as long as somebody else's server takes to answer. Forty of
    those and the pool is empty, at which point requests that never touch a
    Connector start failing too.

    So the read is committed and the connection goes back; SQLAlchemy checks
    another one out when the work resumes. ``expire_on_commit=False`` is what
    makes that free here: rows already read keep their values, so the code after
    reads them without a round-trip. (``rollback`` would not do — it expires
    every instance, and the next attribute access would open a transaction again,
    right before the call this exists to make outside one.)

    **Nothing may be pending.** This commits, so a mutation made above it would
    be persisted and then stranded by a probe that fails — where today the whole
    handler is one transaction that rolls back together. A plain ``raise``
    rather than an ``assert``, because ``python -O`` strips assertions: that
    partial write is silent, and this is the only place that can see it coming.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError(
            "release_connection would commit a pending change; move it above "
            "the read it belongs to, or move this call above the change"
        )
    await db.commit()
