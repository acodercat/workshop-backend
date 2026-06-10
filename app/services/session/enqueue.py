"""Enqueue a user message for the worker.

One load-bearing function, :func:`queue_user_message`, sequences
the steps in a specific order:

1. Lock the session row and reject if a live generation is in progress
   (:class:`GenerationInProgressError`). The lock serialises concurrent
   enqueue requests for the same session so the second one always sees
   the first's ``running`` status.
2. In one transaction:
   - DELETE the per-session output stream so the next turn's events
     don't tail onto stale history;
   - Lazy-sweep this user's open credit reservations into terminal
      state (settles from durable ``session_turn`` usage, or refunds
      if usage is missing);
   - INSERT the ``session_turn`` row (status=``pending``) — its
     auto-generated id is the ``turn_id`` carried on the reservation;
   - Reserve credits for this turn (debits ``user.credit_balance``,
     inserts ``credit_ledger`` RESERVE row + ``credit_reservation``);
     raises :class:`~app.errors.InsufficientCreditsError` if the
     estimate would push the balance negative — the entire tx rolls
     back, no message stored;
   - INSERT the user ``message`` row;
   - flip ``session.status`` to ``running``.
3. After the DB commits, ``PUBLISH notify:worker`` so an idle worker
   wakes immediately. Best-effort: a missed publish is recovered by
   the worker's poll fallback.

Conversation history is **not** sent — the worker keeps its own
compacted context file.
"""

import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus
from app.models.session_files import SessionFile, SessionFileOrigin
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.models.skill import Skill, SkillOrigin
from app.models.user_skill_preference import UserSkillPreference
from app.schemas.session import FileRef
from app.services import credits
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    publish_wake,
    stream_key,
)
from app.services.session.exceptions import (
    FileNotInSessionError,
    GenerationInProgressError,
)

logger = logging.getLogger(__name__)


async def queue_user_message(
    db: AsyncSession,
    redis: aioredis.Redis,
    session: Session,
    *,
    content: str,
    files: list[FileRef],
    skills: list[str] | None = None,
) -> None:
    """Persist the user message + session_turn, flip status, wake worker.

    Order is load-bearing:

    1. ``SELECT ... FOR UPDATE`` the session row to serialise concurrent
       enqueues for the same session, then reject if status is already
       ``running``. PostgreSQL holds the lock until the transaction
       commits, so two racing requests pass through one at a time and
       the second sees the first's ``running`` status.
    2. Lazy-sweep this user's open credit reservations into terminal
       state. Settle reads durable ``session_turn.input_tokens`` /
       ``output_tokens`` / ``terminated_at`` written by the worker,
       not the Redis stream. Skipped on legacy sessions without a
       ``user_id``.
    3. DELETE the previous turn's output stream so new events don't
       tail onto stale history.
    4. INSERT the ``session_turn`` (status=``pending``) and flush so
       its auto-generated id is the ``turn_id`` carried on the new
       reservation.
    5. Reserve credits for this turn — raises
       :class:`~app.errors.InsufficientCreditsError` (→ HTTP 402) if
       the estimate would push the balance negative. The entire tx
       rolls back; no message stored, no worker wake.
    6. INSERT the user ``message`` row, bind uploaded files to the
       session, flip ``session.status='RUNNING'`` — one commit so a
       refetch between steps can't see a half-applied state.
    7. ``publish_wake`` so an idle worker doesn't have to wait for
       its poll cycle. **After commit** so the worker's claim sees
       the row. Best-effort; poll fallback covers a publish failure.

    ``files`` are bound to the session at step 6; ``skills`` (the
    composer's per-turn selection) is persisted on the ``session_turn``
    row at step 4 and rides along to the worker through the claim
    query — no separate binding needed (skill rows are already
    user-scoped, the worker re-checks ownership at lookup time).

    Sandbox ownership lives on ``sandbox.session_id``. The worker
    resumes the latest non-TERMINATED sandbox for this session, or
    creates a fresh one when none exists.

    Raises:
        GenerationInProgressError: a turn is already streaming.
        InsufficientCreditsError: user's balance is below the
            estimated max cost.
    """
    session = await _lock_session(db, session.id)
    if session.status == SessionStatus.RUNNING:
        raise GenerationInProgressError(
            "A completion is already in progress for this session"
        )

    # Lazy sweep: settle any of this user's reservations from prior
    # turns whose session is already terminal. Source of truth is
    # ``session_turn.input_tokens`` / ``output_tokens`` (V2 durable
    # storage), so ordering against the Redis stream ``DELETE`` below
    # doesn't matter. Sessions without a ``user_id`` are treated as
    # system / legacy and don't bill; logged at WARNING so the case
    # surfaces if it ever fires in prod (every production path stamps
    # ``user_id`` at session creation).
    if session.user_id is None:
        logger.warning(
            "queue_user_message: session %s has no user_id — skipping credit reserve",
            session.id,
        )
    else:
        await credits.settle_open_for_user(db, session.user_id)
    # Prior stream lingers post-``agent.stopped`` for late SSE replay;
    # clear before the new turn so new events don't tail onto stale
    # history. ``DELETE`` is a no-op on missing keys.
    await redis.delete(stream_key(SESSION_STREAM_PREFIX, session.id))

    # Reject ``skills`` the user can't actually invoke — visible to
    # them AND effective-enabled. Without this check, a client could
    # POST ``skills: ["some-disabled-name"]`` and the worker's
    # ``extra_names`` bypass would happily load it, defeating the
    # toggle's "off means off" semantics.
    accepted_skills = await _filter_invokable_skills(
        db, session.user_id, skills or [],
    )

    file_dicts = [f.model_dump() for f in files]
    # Insert the session_turn first so we have a stable ``turn_id`` to
    # attach to the credit reservation. The flush below populates
    # ``session_turn.id`` without committing.
    session_turn = SessionTurn(
        session_id=session.id,
        query=content,
        files=file_dicts,
        skills=accepted_skills,
        status=SessionTurnStatus.PENDING,
    )
    db.add(session_turn)
    await db.flush()

    # Reserve credits using the model_id stamped on the session.
    # Raises InsufficientCreditsError if the estimate would push the
    # balance negative — the whole transaction rolls back, including
    # the session_turn we just inserted, so no message is stored and
    # the worker never sees the turn.
    if session.user_id is not None:
        await credits.reserve_for_turn(
            db,
            user_id=session.user_id,
            session_id=session.id,
            turn_id=session_turn.id,
            model_id=session.model_id,
        )

    user_msg = Message(
        session_id=session.id,
        role=MessageRole.USER,
        blocks=[{"type": "text", "content": content, "files": file_dicts}],
    )
    session.status = SessionStatus.RUNNING
    db.add(user_msg)
    await _bind_session_files(db, session_id=session.id, files=files)
    await db.commit()  # releases the FOR UPDATE lock + makes session_turn visible

    # Wake any idle worker — fire-and-forget; if it fails the worker's
    # poll fallback (5s) catches the row anyway. Logging only, no raise:
    # the user-visible effect (message stored, session running) already
    # committed.
    try:
        await publish_wake(redis, session.id)
    except Exception:
        logger.warning(
            "publish_wake failed for session %s — worker poll fallback will catch it",
            session.id, exc_info=True,
        )


async def _lock_session(db: AsyncSession, session_id: str) -> Session:
    """Re-fetch the session under a row-level lock.

    Returns the locked row whose ``status`` is the authoritative answer
    to "is a generation running for this session"; the lock is held
    until the surrounding transaction commits or rolls back. The
    caller-supplied ``Session`` object may be a stale snapshot (read
    in a different transaction at endpoint dispatch); the lock is what
    makes the subsequent status check race-free.
    """
    result = await db.execute(
        select(Session).where(Session.id == session_id).with_for_update()
    )
    return result.scalar_one()


async def _bind_session_files(
    db: AsyncSession,
    *,
    session_id: UUID,
    files: list[FileRef],
) -> None:
    """Bind uploaded file keys to this session exactly once.

    Reusing a file already bound to this same session is idempotent.
    Reusing a file bound to a different session is rejected: otherwise
    a caller could reference someone else's ``file_key`` in a message
    and rely on the JSONB copy even though download authorization would
    later fail.
    """
    # Keep the first FileRef per key — it carries the presentation metadata
    # (filename / mime_type / size_bytes) denormalized onto the row.
    unique_files: dict[str, FileRef] = {}
    for file_ref in files:
        unique_files.setdefault(file_ref.file_key, file_ref)
    if not unique_files:
        return

    rows = await db.execute(
        select(SessionFile.file_key, SessionFile.session_id)
        .where(SessionFile.file_key.in_(unique_files.keys()))
    )
    existing = {
        file_key: owner_session_id for file_key, owner_session_id in rows.all()
    }

    for file_key, file_ref in unique_files.items():
        owner_session_id = existing.get(file_key)
        if owner_session_id is None:
            db.add(
                SessionFile(
                    session_id=session_id,
                    file_key=file_key,
                    origin=SessionFileOrigin.UPLOAD,
                    filename=file_ref.filename,
                    mime_type=file_ref.mime_type,
                    size_bytes=file_ref.size_bytes,
                )
            )
            continue
        if owner_session_id == session_id:
            continue
        raise FileNotInSessionError(
            f"file_key {file_key!r} is already bound to another session"
        )


async def _filter_invokable_skills(
    db: AsyncSession, user_id: int | None, requested: list[str],
) -> list[dict[str, str | int]]:
    """Return immutable snapshots of the skills the user can invoke this turn.

    Each element is ``{"id", "name", "bundle_key"}`` — pinned to the
    skill row that existed at enqueue time so the worker loads the
    exact bundle bytes the user picked, even if the row is later
    mutated (delete / re-upload / a same-named CUSTOM appearing and
    overriding a BUILTIN). The ``id`` pins the lineage; the
    ``bundle_key`` pins the bytes; ``name`` rides along for log and
    LLM-reminder display.

    A skill is invokable when it's visible to the user (their own
    CUSTOM, or any BUILTIN) AND its effective enabled state — the
    per-user preference override taking precedence over the skill's
    default — is true. Unknown / hidden / disabled names are silently
    dropped rather than raising; the user's intent (a free-text
    composer prompt) is still useful even if one chip was stale, and
    surfacing a 4xx mid-conversation would be a worse UX than gracefully
    proceeding without that skill.

    Slug collisions between CUSTOM + BUILTIN resolve CUSTOM-wins, matching
    the worker repo's ``ORDER BY (user_id IS NULL) ASC`` rule. Order is
    preserved from the request so the worker's reminder lists skills in
    the order the user picked them.
    """
    if not requested or user_id is None:
        return []
    # De-duplicate while preserving order — duplicates would noise both
    # the snapshot list and the LLM reminder.
    requested_unique = list(dict.fromkeys(requested))

    # Effective enabled = ``COALESCE(pref.enabled, skill.enabled)`` —
    # mirrors the same expression used by the worker repo and the
    # ``GET /skills`` endpoint so all three reads of "is this skill
    # active for this user" agree.
    pref_enabled = UserSkillPreference.enabled
    stmt = (
        select(Skill.id, Skill.name, Skill.bundle_key, Skill.user_id)
        .outerjoin(
            UserSkillPreference,
            and_(
                UserSkillPreference.skill_id == Skill.id,
                UserSkillPreference.user_id == user_id,
            ),
        )
        .where(
            Skill.name.in_(requested_unique),
            (Skill.user_id == user_id) | (Skill.origin == SkillOrigin.BUILTIN),
            (pref_enabled.is_(True)) | (pref_enabled.is_(None) & Skill.enabled),
        )
    )
    result = await db.execute(stmt)
    # CUSTOM-wins: keep the row owned by this user when both a CUSTOM
    # and a BUILTIN of the same name pass the filter.
    by_name: dict[str, dict[str, str | int]] = {}
    for skill_id, name, bundle_key, row_user_id in result.all():
        snapshot = {"id": skill_id, "name": name, "bundle_key": bundle_key}
        existing = by_name.get(name)
        if existing is None or row_user_id is not None:
            by_name[name] = snapshot
    return [by_name[name] for name in requested_unique if name in by_name]
