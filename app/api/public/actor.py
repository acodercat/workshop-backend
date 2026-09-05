"""User-owned actor (数字员工) CRUD.

An actor is the persistent identity a session can run under: a name, a
human-facing ``description``, and a model-facing ``instructions``
charter (see ``app/models/actor.py``). This surface manages identity
only — tasks, files, and memory attach to the actor through their own
tables and endpoints.

Every actor is owned (``user_id`` NOT NULL); there is no platform tier
and therefore, unlike skills / recipes, no visibility union — a caller
sees exactly their own live actors. Deletion is a soft delete: the row
tombstones (task history keeps billing/audit weight) and the name is
freed for reuse by the partial unique index.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.actor import Actor
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.actor import ActorCreate, ActorResponse, ActorUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["actors"])

# Per-user cap on live actors — bounds account-level abuse. Deliberately
# below the recipe/skill cap (100): an actor is heavier than a prompt
# scaffold (it accrues sessions, files, and — later — a workspace), and
# soft-deleted rows don't count, so the cap is on *live* head-count.
_MAX_ACTORS_PER_USER = 50


@router.post("/actors", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_actor(
    request: Request,
    body: ActorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ActorResponse:
    """Create an actor owned by the caller.

    403 at the per-user cap; 409 if the caller already has a live actor
    with this name.
    """
    # Race-acceptable like the recipe cap: two concurrent creates could
    # each pass and put the user one over. Not worth a row lock for a
    # soft policy limit. Only live rows count — deleting an actor frees
    # its quota slot along with its name.
    used = await db.scalar(
        select(func.count(Actor.id)).where(
            Actor.user_id == current_user.id,
            Actor.deleted_at.is_(None),
        )
    )
    if used is not None and used >= _MAX_ACTORS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor limit reached ({_MAX_ACTORS_PER_USER})",
        )

    # Friendly-path duplicate check; the racing INSERT that slips past it
    # lands on ``uq_actor_user_id_name`` and the global IntegrityError
    # handler returns the same 409.
    await _reject_duplicate_name(db, current_user.id, body.name)

    actor = Actor(
        user_id=current_user.id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
    )
    db.add(actor)
    await db.commit()
    await db.refresh(actor)
    logger.info(
        "actor created: id=%s user_id=%s name=%r",
        actor.id,
        current_user.id,
        actor.name,
    )
    return ActorResponse.model_validate(actor)


@router.get("/actors")
async def list_actors(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[ActorResponse]:
    """Return the caller's live actors, most recently touched first.

    No pagination: the per-user cap bounds the result well under one
    page (the recipe-list posture, not the session-list one).
    """
    stmt = (
        select(Actor)
        .where(
            Actor.user_id == current_user.id,
            Actor.deleted_at.is_(None),
        )
        .order_by(Actor.updated_at.desc())
    )
    result = await db.execute(stmt)
    return [ActorResponse.model_validate(actor) for actor in result.scalars().all()]


@router.get("/actors/{actor_id}")
async def get_actor(
    actor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ActorResponse:
    """Return one live actor owned by the caller."""
    actor = await _get_or_404(db, actor_id, current_user.id)
    return ActorResponse.model_validate(actor)


@router.patch("/actors/{actor_id}")
async def update_actor(
    actor_id: uuid.UUID,
    body: ActorUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ActorResponse:
    """Edit an actor's identity fields.

    409 on a rename that collides with another live actor of the
    caller's. Already-queued turns are unaffected by any edit: each
    carries its enqueue-time actor snapshot.
    """
    actor = await _get_or_404(db, actor_id, current_user.id)

    # Collect non-null fields and apply them in a single UPDATE — avoids
    # the read-modify-write race where two concurrent PATCHes each load
    # the row, mutate one field, and the second commit wipes the first.
    updates = {
        field: value
        for field, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if "name" in updates and updates["name"] != actor.name:
        await _reject_duplicate_name(db, current_user.id, updates["name"])
    if updates:
        await db.execute(sql_update(Actor).where(Actor.id == actor.id).values(**updates))
        await db.commit()
        await db.refresh(actor)
    return ActorResponse.model_validate(actor)


@router.delete("/actors/{actor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_actor(
    actor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Soft-delete an actor.

    Tombstone, not a row delete — the actor's task history carries
    billing/audit weight (the ``session.deleted_at`` posture). The
    partial unique index frees the name for a future actor. The avatar
    object (if any) is kept: the row still references it, and orphan
    cleanup belongs to the avatar flow, not here.
    """
    actor = await _get_or_404(db, actor_id, current_user.id)
    actor_name = actor.name
    # ``func.now()`` — the tombstone is cut on the database clock, like
    # every other timestamp in this schema (``delete_session`` does the
    # same).
    await db.execute(sql_update(Actor).where(Actor.id == actor.id).values(deleted_at=func.now()))
    await db.commit()
    logger.info(
        "actor deleted: id=%s user_id=%s name=%r",
        actor_id,
        current_user.id,
        actor_name,
    )


async def _get_or_404(
    db: AsyncSession,
    actor_id: uuid.UUID,
    user_id: int,
) -> Actor:
    """Fetch a live actor owned by ``user_id``.

    Ownership and ``deleted_at IS NULL`` are folded into the lookup (the
    ``session._get_or_404`` rule), so someone else's actor and a deleted
    one are the same indistinguishable 404 and ids don't leak.
    """
    stmt = select(Actor).where(
        Actor.id == actor_id,
        Actor.user_id == user_id,
        Actor.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    actor = result.scalar_one_or_none()
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="actor not found",
        )
    return actor


async def _reject_duplicate_name(
    db: AsyncSession,
    user_id: int,
    name: str,
) -> None:
    """409 if ``user_id`` already has a live actor named ``name``.

    Best-effort friendliness on top of ``uq_actor_user_id_name`` — the
    index stays the guarantee; this exists to say *which* conflict
    happened instead of the handler's generic envelope.
    """
    exists = await db.scalar(
        select(Actor.id).where(
            Actor.user_id == user_id,
            Actor.name == name,
            Actor.deleted_at.is_(None),
        )
    )
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An actor named {name!r} already exists",
        )
