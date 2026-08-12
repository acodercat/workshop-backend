"""Enqueue a user message for the worker.

One load-bearing function, :func:`queue_user_message`, sequences
the steps in a specific order:

1. Lock the session row and reject if a live generation is in progress
   (:class:`GenerationInProgressError`). The lock serialises concurrent
   enqueue requests for the same session so the second one always sees
   the first's ``running`` status. Apply the one-way LITE → FULL
   ``agent_type`` upgrade here when the send carries one — under the
   same lock, committed atomically with the turn, so the worker's
   claim (which JOINs ``session.agent_type``) runs this very turn in
   the upgraded mode.
2. In one transaction:
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
   - INSERT the user ``message`` row + flip ``session.status`` to
     ``running``;
   - DELETE the per-session output stream so the next turn's events
     don't tail onto stale history. Last, after every fallible step,
     so a failed enqueue never destroys replay history.
3. After the DB commits, ``PUBLISH notify:worker`` so an idle worker
   wakes immediately. Best-effort: a missed publish is recovered by
   the worker's poll fallback.

Conversation history is **not** sent — the worker keeps its own
compacted context file.
"""

import asyncio
import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentTypeDowngradeError
from app.models.mcp_server import MCPServer
from app.models.message import Message, MessageRole
from app.models.recipe import Recipe, RecipeOrigin
from app.models.session import AgentType, Session, SessionStatus
from app.models.session_files import SessionFile, SessionFileOrigin
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.models.skill import Skill, SkillOrigin
from app.schemas.session import FileRef
from app.services import credits, file_storage
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    publish_wake,
    stream_key,
)
from app.services.session import connectors as session_connectors
from app.services.session.exceptions import (
    ConcurrentTaskLimitError,
    FileNotInSessionError,
    FileNotUploadedError,
    GenerationInProgressError,
    RecipeNotInvokableError,
    SessionGoneError,
)
from config import settings

logger = logging.getLogger(__name__)


async def queue_user_message(
    db: AsyncSession,
    redis: aioredis.Redis,
    session: Session,
    *,
    content: str,
    files: list[FileRef],
    skills: list[str] | None = None,
    recipe_id: int | None = None,
    agent_type: AgentType | None = None,
) -> None:
    """Persist the user message + session_turn, flip status, wake worker.

    Order is load-bearing:

    1. ``SELECT ... FOR UPDATE`` the session row to serialise concurrent
       enqueues for the same session, then reject if status is already
       ``running``. PostgreSQL holds the lock until the transaction
       commits, so two racing requests pass through one at a time and
       the second sees the first's ``running`` status. ``agent_type``
       (the send's one-way LITE → FULL mode upgrade; ``None`` = keep)
       is applied here so the flip and the turn commit atomically —
       the worker's claim JOINs ``session.agent_type`` post-commit, so
       this very turn runs in the upgraded mode.
    2. Lazy-sweep this user's open credit reservations into terminal
       state. Settle reads durable ``session_turn.input_tokens`` /
       ``output_tokens`` / ``terminated_at`` written by the worker,
       not the Redis stream. Skipped on legacy sessions without a
       ``user_id``.
    3. INSERT the ``session_turn`` (status=``pending``) and flush so
       its auto-generated id is the ``turn_id`` carried on the new
       reservation.
    4. Reserve credits for this turn — raises
       :class:`~app.errors.InsufficientCreditsError` (→ HTTP 402) if
       the estimate would push the balance negative. The entire tx
       rolls back; no message stored, no worker wake.
    5. INSERT the user ``message`` row, bind uploaded files to the
       session, flip ``session.status='RUNNING'``, then DELETE the
       previous turn's output stream — all before one commit so a
       refetch can't see a half-applied state, and so a 402/skills
       failure above can't destroy replay history for a turn that
       never enqueues.
    6. ``publish_wake`` so an idle worker doesn't have to wait for
       its poll cycle. **After commit** so the worker's claim sees
       the row. Best-effort; poll fallback covers a publish failure.

    ``files`` are bound to the session at step 5; ``skills`` (the
    composer's per-turn selection) is persisted on the ``session_turn``
    row at step 3 and rides along to the worker through the claim
    query — no separate binding needed (skill rows are already
    user-scoped, the worker re-checks ownership at lookup time).

    Sandbox ownership lives on ``sandbox.session_id``. The worker
    resumes the latest non-TERMINATED sandbox for this session, or
    creates a fresh one when none exists.

    Raises:
        GenerationInProgressError: a turn is already streaming.
        SessionGoneError: the session was deleted between the
            endpoint's ownership check and this lock.
        InsufficientCreditsError: user's balance is below the
            estimated max cost.
        ConcurrentTaskLimitError: the user is already at
            ``settings.max_concurrent_running_tasks`` running tasks.
        AgentTypeDowngradeError: ``agent_type`` asked for FULL → LITE.
    """
    session = await _lock_session(db, session.id)
    if session.status == SessionStatus.RUNNING:
        raise GenerationInProgressError("A completion is already in progress for this session")
    _apply_agent_type_upgrade(session, agent_type)

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

    # Drop ``skills`` the user can't see (someone else's CUSTOM, or a
    # hidden SYSTEM row). Visibility is the only gate here: an explicit
    # per-turn pick is its own authorization, so the enable toggle —
    # which governs ambient auto-discovery, not manual selection — is
    # deliberately not consulted.
    accepted_skills = await _filter_invokable_skills(
        db,
        session.user_id,
        skills or [],
    )

    # Snapshot what the session's standing configuration resolves to right now
    # — not a per-turn pick any more: the switch says "from now on", and this
    # is the subset still owned and still connected when the turn ran.
    # Credentials are NOT snapshotted; the worker resolves url + decrypted
    # credential live by id.
    accepted_connectors = await _resolve_mcp_servers(
        db,
        session.user_id,
        await session_connectors.configured_ids(db, session.id),
    )

    # Resolve the picked recipe server-side into a pinned snapshot — the
    # client only sends ``recipe_id``, never the instruction text. A bad
    # id (missing / not visible / disabled) raises rather than silently
    # dropping, since a recipe is a single explicit pick.
    recipe_snapshot = await _resolve_recipe_snapshot(
        db,
        session.user_id,
        recipe_id,
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
        mcp_servers=accepted_connectors,
        recipe=recipe_snapshot,
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
        await _enforce_concurrent_task_limit(db, session.user_id)

    user_msg = Message(
        session_id=session.id,
        role=MessageRole.USER,
        blocks=[{"type": "text", "content": content, "files": file_dicts}],
    )
    session.status = SessionStatus.RUNNING
    db.add(user_msg)
    newly_bound_keys = await _bind_session_files(
        db,
        session_id=session.id,
        user_id=session.user_id,
        files=files,
    )

    # Clear the prior turn's output stream (it lingers post-``agent.stopped``
    # for late SSE replay) here, after every fallible step (see the step
    # list above). ``DELETE`` is a no-op on a missing key; before commit so
    # the stream is clean before the worker (woken post-commit) writes the
    # new turn's first event.
    await redis.delete(stream_key(SESSION_STREAM_PREFIX, session.id))

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
            session.id,
            exc_info=True,
        )

    # Clear the orphan-reaping tag on files this turn just bound (M5), so the
    # bucket lifecycle rule no longer reaps them. Best-effort + post-commit:
    # the binding is already durable, so a tag-clear blip must not fail the
    # send (mirrors publish_wake / stream-delete). A genuinely stuck clear is
    # logged loudly — the ~1-day lifecycle window leaves room to react.
    if settings.oss_orphan_tagging_enabled and newly_bound_keys:
        await _clear_orphan_tags(newly_bound_keys)


async def _clear_orphan_tags(file_keys: list[str]) -> None:
    """Best-effort clear of the orphan tag on newly-bound uploads.

    Runs the sync OSS calls off the event loop, concurrently. Per-key
    failures are logged, never raised — see the call site.
    """
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                file_storage.clear_orphan_tag,
                file_storage.SESSION_FILE_POLICY,
                key,
            )
            for key in file_keys
        ),
        return_exceptions=True,
    )
    for key, result in zip(file_keys, results):
        if isinstance(result, Exception):
            logger.error(
                "failed to clear orphan tag on %s — it may be reaped by the "
                "lifecycle rule; re-clear within the window",
                key,
                exc_info=result,
            )


def _apply_agent_type_upgrade(
    session: Session,
    requested: AgentType | None,
) -> None:
    """Apply the send's one-way LITE → FULL mode upgrade, if any.

    ``None`` (no preference) and same-as-current are no-ops, so resends
    are idempotent. A FULL → LITE request is rejected — only the upgrade
    exists (a downgrade would strand the session's sandbox and files),
    and the UI never offers it, so hitting this is a client programming
    error. Runs on the locked session row, before the turn insert, so
    the flip commits atomically with the turn.
    """
    if requested is None or requested == session.agent_type:
        return
    if requested is not AgentType.FULL:
        raise AgentTypeDowngradeError("A session can only switch from LITE to FULL")
    session.agent_type = requested


async def _enforce_concurrent_task_limit(db: AsyncSession, user_id: int) -> None:
    """Reject if the user is already at their running-task cap.

    Must be called **after** ``reserve_for_turn`` so it runs under the
    reservation's user-row ``FOR UPDATE`` lock: two concurrent enqueues for
    the same user then serialize on that lock, so the count can't overshoot
    (the second sees the first's committed ``RUNNING`` flip). The session
    being enqueued isn't ``RUNNING`` yet, so ``>=`` rejects the would-be
    (limit+1)-th; raising rolls the whole transaction back (turn +
    reservation), leaving no trace.
    """
    running_count = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(
            Session.user_id == user_id,
            Session.status == SessionStatus.RUNNING,
            Session.deleted_at.is_(None),
        )
    )
    # ``or 0``: ``COUNT`` never answers NULL, but ``scalar`` is typed as if
    # any query might return nothing.
    if (running_count or 0) >= settings.max_concurrent_running_tasks:
        raise ConcurrentTaskLimitError(
            f"user {user_id} already has {running_count} running task(s) "
            f"(max {settings.max_concurrent_running_tasks})"
        )


async def _lock_session(db: AsyncSession, session_id: UUID) -> Session:
    """Re-fetch the live session under a row-level lock.

    Returns the locked row whose ``status`` is the authoritative answer
    to "is a generation running for this session"; the lock is held
    until the surrounding transaction commits or rolls back. The
    caller-supplied ``Session`` object may be a stale snapshot (read
    in a different transaction at endpoint dispatch); the lock is what
    makes the subsequent status check race-free.

    ``deleted_at IS NULL`` is part of the locked predicate so a
    ``delete_session`` that committed between the endpoint's
    ``_get_or_404`` and this lock leaves no row to match — we raise
    :class:`SessionGoneError` rather than enqueue a turn (+ user message
    + credit reservation) against a tombstoned session. ``delete_session``
    takes the same row lock before tombstoning, so the two serialize:
    whichever commits first, the other sees the committed state.
    """
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id, Session.deleted_at.is_(None))
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionGoneError(f"session {session_id} no longer exists")
    return session


async def _bind_session_files(
    db: AsyncSession,
    *,
    session_id: UUID,
    user_id: int | None,
    files: list[FileRef],
) -> list[str]:
    """Bind uploaded file keys to this session exactly once.

    Each new row records ``user_id`` (the session owner, ``None`` for
    anonymous sessions) denormalized from the session for per-user queries.

    Returns the keys that were **newly** bound this call (a fresh
    ``session_files`` row), so the caller can clear their orphan-reaping
    tag (M5). Keys already bound to this session are not returned (their
    tag was cleared on first bind).

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
        return []

    rows = await db.execute(
        select(SessionFile.file_key, SessionFile.session_id).where(
            SessionFile.file_key.in_(unique_files.keys())
        )
    )
    existing = {file_key: owner_session_id for file_key, owner_session_id in rows.all()}

    # Classify first (reject cross-session rebinds, skip idempotent
    # same-session ones), then insert only the genuinely new keys.
    to_insert: list[FileRef] = []
    for file_key, file_ref in unique_files.items():
        owner_session_id = existing.get(file_key)
        if owner_session_id is None:
            to_insert.append(file_ref)
        elif owner_session_id != session_id:
            raise FileNotInSessionError(
                f"file_key {file_key!r} is already bound to another session"
            )
        # owner_session_id == session_id → idempotent rebind, skip.
    if not to_insert:
        return []

    # ``size_bytes`` is client-declared and untrusted: a low declaration
    # slips past the per-file cap AND the per-user quota (which SUMs this
    # column). The object provably exists in OSS only now, at bind, so
    # reconcile against its true size here — the one place the persisted
    # value becomes trustworthy. ``head_size`` is blocking oss2; thread it
    # and fan out so N attachments cost ~one HEAD's latency, not N serial.
    sizes = await asyncio.gather(
        *(
            asyncio.to_thread(
                file_storage.head_size,
                file_storage.SESSION_FILE_POLICY,
                file_ref.file_key,
            )
            for file_ref in to_insert
        ),
        return_exceptions=True,
    )

    newly_bound: list[str] = []
    for file_ref, size in zip(to_insert, sizes):
        if isinstance(size, file_storage.FileObjectNotFoundError):
            raise FileNotUploadedError(f"file_key {file_ref.file_key!r} was not uploaded")
        if isinstance(size, BaseException):
            raise size  # unexpected OSS/other error — surface as 500
        if size > file_storage.SESSION_FILE_POLICY.max_bytes:
            raise file_storage.FileTooLargeError(size, file_storage.SESSION_FILE_POLICY.max_bytes)
        db.add(
            SessionFile(
                session_id=session_id,
                user_id=user_id,
                file_key=file_ref.file_key,
                origin=SessionFileOrigin.UPLOAD,
                filename=file_ref.filename,
                mime_type=file_ref.mime_type,
                size_bytes=size,
            )
        )
        newly_bound.append(file_ref.file_key)
    return newly_bound


async def _filter_invokable_skills(
    db: AsyncSession,
    user_id: int | None,
    requested: list[str],
) -> list[dict[str, str | int]]:
    """Return immutable snapshots of the skills the user can invoke this turn.

    Each element is ``{"id", "name", "bundle_key"}`` — pinned to the
    skill row that existed at enqueue time so the worker loads the
    exact bundle bytes the user picked, even if the row is later
    mutated (delete / re-upload / a same-named CUSTOM appearing and
    overriding an OFFICIAL skill). The ``id`` pins the lineage; the
    ``bundle_key`` pins the bytes; ``name`` rides along for log and
    LLM-reminder display.

    A skill is invokable when it's visible to the user: their own
    CUSTOM, or any OFFICIAL skill. The enable toggle is deliberately
    **not** checked — it governs ambient auto-discovery (whether the
    skill's metadata rides in the LLM context every turn, see the
    worker's ``list_enabled``), while an explicit composer pick is its
    own per-turn authorization. Unknown / hidden names are silently
    dropped rather than raising; the user's intent (a free-text
    composer prompt) is still useful even if one chip was stale, and
    surfacing a 4xx mid-conversation would be a worse UX than gracefully
    proceeding without that skill.

    Slug collisions between CUSTOM + OFFICIAL resolve CUSTOM-wins, matching
    the worker repo's ``ORDER BY (user_id IS NULL) ASC`` rule. Order is
    preserved from the request so the worker's reminder lists skills in
    the order the user picked them.
    """
    if not requested or user_id is None:
        return []
    # De-duplicate while preserving order — duplicates would noise both
    # the snapshot list and the LLM reminder.
    requested_unique = list(dict.fromkeys(requested))

    stmt = select(Skill.id, Skill.name, Skill.bundle_key, Skill.user_id).where(
        Skill.name.in_(requested_unique),
        (Skill.user_id == user_id) | (Skill.origin == SkillOrigin.OFFICIAL),
    )
    result = await db.execute(stmt)
    # CUSTOM-wins: keep the row owned by this user when both a CUSTOM
    # and an OFFICIAL skill of the same name pass the filter.
    by_name: dict[str, dict[str, str | int]] = {}
    for skill_id, name, bundle_key, row_user_id in result.all():
        snapshot = {"id": skill_id, "name": name, "bundle_key": bundle_key}
        existing = by_name.get(name)
        if existing is None or row_user_id is not None:
            by_name[name] = snapshot
    return [by_name[name] for name in requested_unique if name in by_name]


async def _resolve_mcp_servers(
    db: AsyncSession,
    user_id: int | None,
    requested: list[int],
) -> list[dict[str, str | int]]:
    """Snapshot ``{"id", "name"}`` for each of the session's live Connectors.

    ``requested`` is the session's configured Connector ids, whose
    ownership was validated when it was written. Liveness cannot be: one
    disconnected since is dropped here rather than raising, so it costs the
    turn that Connector and not the message.

    Credentials are deliberately not snapshotted — the worker re-reads each
    row by id at connect time, so a rotation is picked up and secrets never
    sit on the ``session_turn`` row. ``name`` only keeps a queued row
    legible in psql; the worker must not use it either, or a Connector
    renamed between enqueue and claim would expose its tools under a name
    that no longer exists. (Contrast the skills snapshot, whose
    ``bundle_key`` *is* load-bearing.)
    """
    if not requested or user_id is None:
        return []
    # De-duplicate; a repeated pick would otherwise repeat in the snapshot.
    # Request order is kept only so a queued row reads the way the user
    # picked — the worker connects by its own rule (failed-last), so nothing
    # downstream depends on it.
    requested_unique = list(dict.fromkeys(requested))

    stmt = select(MCPServer.id, MCPServer.name).where(
        MCPServer.id.in_(requested_unique),
        MCPServer.user_id == user_id,
        MCPServer.connected.is_(True),
    )
    result = await db.execute(stmt)
    by_id = {row_id: {"id": row_id, "name": name} for row_id, name in result.all()}
    return [by_id[picked] for picked in requested_unique if picked in by_id]


async def _resolve_recipe_snapshot(
    db: AsyncSession,
    user_id: int | None,
    recipe_id: int | None,
) -> dict[str, str | int] | None:
    """Resolve a picked recipe id into a pinned per-turn snapshot.

    Returns ``{"id", "display_name", "instructions"}`` — the rendered
    instructions captured at enqueue time so a later edit / delete of the
    recipe can't change or strand this queued turn. The worker injects
    ``instructions`` verbatim as an axon system-reminder.

    A recipe is invokable when it's visible to the user (their own
    CUSTOM, or any OFFICIAL) AND enabled. Unlike skills — silently
    filtered, since a stale chip shouldn't block a free-text send — a
    recipe is a single explicit pick, so a missing / hidden / disabled id
    raises :class:`RecipeNotInvokableError` (→ 400) rather than vanishing.

    Returns ``None`` when no recipe was picked.
    """
    if recipe_id is None:
        return None
    stmt = select(Recipe.id, Recipe.display_name, Recipe.instructions).where(
        Recipe.id == recipe_id,
        Recipe.enabled.is_(True),
        (Recipe.user_id == user_id) | (Recipe.origin == RecipeOrigin.OFFICIAL),
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise RecipeNotInvokableError(f"recipe {recipe_id} is not available")
    return {
        "id": row.id,
        "display_name": row.display_name,
        "instructions": row.instructions,
    }
