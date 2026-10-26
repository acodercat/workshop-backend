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
   - Bind uploaded files, confirming each size with a HEAD against
     OSS — before the reservation, so that round trip never holds the
     user-row lock;
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
   wakes immediately, and announce ``RUNNING`` on the user's
   notification channel. Both best-effort: a missed wake is recovered
   by the worker's poll fallback, a missed announcement by the next
   ``GET /sessions``.

Conversation history is **not** sent — the worker keeps its own
compacted context in ``session_context``.
"""

import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    AgentTypeDowngradeError,
    ConcurrentTaskLimitError,
    FileRetentionFailedError,
    GenerationInProgressError,
    SessionNotFoundError,
)
from app.models.mcp_server import MCPServer
from app.models.message import Message, MessageRole
from app.models.session import AgentType, Session, SessionStatus
from app.models.session_files import SessionFile, SessionFileOrigin
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.schemas.session import FileRef
from app.services import credits, file_storage
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    publish_wake,
    stream_key,
)
from app.services.session import connectors as session_connectors
from app.services.session.files import assert_files_owned_by
from app.services.user import publish_session_status
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
    agent_type: AgentType | None = None,
    reminders: list[str] | None = None,
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
       not the Redis stream.
    3. Bind uploaded files to the session — before the reservation,
       so the OSS size checks never run under the user-row lock.
    4. INSERT the ``session_turn`` (status=``pending``) and flush so
       its auto-generated id is the ``turn_id`` carried on the new
       reservation.
    5. Reserve credits for this turn — raises
       :class:`~app.errors.InsufficientCreditsError` (→ HTTP 402) if
       the estimate would push the balance negative. The entire tx
       rolls back; no message stored, no worker wake.
    6. INSERT the user ``message`` row, flip
       ``session.status='RUNNING'``, then DELETE the
       previous turn's output stream — all before one commit so a
       refetch can't see a half-applied state, and so a 402/skills
       failure above can't destroy replay history for a turn that
       never enqueues.
    7. ``publish_wake`` so an idle worker doesn't have to wait for
       its poll cycle. **After commit** so the worker's claim sees
       the row. Best-effort; poll fallback covers a publish failure.

    ``files`` are bound to the session at step 3; ``skills`` (the
    composer's per-turn selection) is persisted on the ``session_turn``
    row at step 4 and rides along to the worker through the claim
    query — no separate binding needed (skill rows are already
    user-scoped, the worker re-checks ownership at lookup time).
    ``reminders`` are model-facing context strings a server-side caller
    rendered (a scheduled run's briefing); they are stored on the turn as
    given and the worker appends them to the run's system reminders. Never
    pass client-supplied text here.

    Sandbox ownership lives on ``sandbox.session_id``. The worker
    resumes the latest non-TERMINATED sandbox for this session, or
    creates a fresh one when none exists.

    Raises:
        GenerationInProgressError: a turn is already streaming.
        SessionNotFoundError: the session was deleted between the
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
    # doesn't matter.
    await credits.settle_open_for_user(db, session.user_id)

    # Drop ``skills`` the user can't reach: someone else's CUSTOM, a
    # hidden SYSTEM row, or a listing an operator has retired. The user's
    # own toggle is deliberately not consulted — it governs which skills
    # the composer offers, and an explicit per-turn pick is its own
    # authorization however the request came by the name.
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

    # Before the reservation, which locks the user row: binding confirms each
    # file's size with a HEAD against OSS, and that round trip must not hold
    # the lock every other write for this user — their other enqueues, settle,
    # top-ups — waits on. The session lock already serializes this session.
    newly_bound_keys = await _bind_session_files(
        db,
        session_id=session.id,
        user_id=session.user_id,
        files=files,
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
        reminders=reminders or [],
        status=SessionTurnStatus.PENDING,
    )
    db.add(session_turn)
    await db.flush()

    # Reserve credits using the model_id stamped on the session.
    # Raises InsufficientCreditsError if the estimate would push the
    # balance negative — the whole transaction rolls back, including
    # the session_turn we just inserted, so no message is stored and
    # the worker never sees the turn.
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
    # The user's other tabs learn the session is running; the worker
    # announces the terminal the same way.
    await publish_session_status(
        redis,
        user_id=session.user_id,
        session_id=session.id,
        status=SessionStatus.RUNNING,
    )

    # Clear the orphan-reaping tag on files this turn just bound (M5), so the
    # bucket lifecycle rule no longer reaps them. Best-effort + post-commit:
    # the binding is already durable, so a tag-clear blip must not fail the
    # send (mirrors publish_wake / stream-delete). A genuinely stuck clear is
    # logged loudly — the ~1-day lifecycle window leaves room to react.
    try:
        await file_storage.clear_orphan_tags(file_storage.SESSION_FILE_POLICY, newly_bound_keys)
    except FileRetentionFailedError:
        logger.error(
            "session %s: files bound but still tagged for reaping; re-clear within the window",
            session.id,
            exc_info=True,
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
        raise ConcurrentTaskLimitError(settings.max_concurrent_running_tasks)


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
    :class:`SessionNotFoundError` rather than enqueue a turn (+ user message
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
        raise SessionNotFoundError("Session not found.")
    return session


async def _bind_session_files(
    db: AsyncSession,
    *,
    session_id: UUID,
    user_id: int,
    files: list[FileRef],
) -> list[str]:
    """Bind uploaded file keys to this session exactly once.

    Each new row records ``user_id`` (the session owner) denormalized from
    the session for per-user queries.

    Returns the keys that were **newly** bound this call (a fresh
    ``session_files`` row), so the caller can clear their orphan-reaping
    tag (M5). Keys already bound to this session are not returned (their
    tag was cleared on first bind).

    Reusing a file already bound to this same session is idempotent.
    Reusing one bound to a **different owner** is rejected; reusing one of
    your own in another session is allowed, and is how a scheduled task
    re-attaches the same upload to every session it creates.

    That ownership test is load-bearing, and it is the whole reason it is
    spelled out rather than left to the schema. Until ``d8a4f1c6e290`` the
    column-level UNIQUE on ``file_key`` made *any* rebind fail, which
    happened to shut a real door: a share viewer legitimately holds the
    ``file_key`` of a file in the session shared with them (``share.py``
    passes it as a query parameter), so without this they could bind it to
    a session of their own and keep minting download URLs long after the
    share was revoked. Widening the constraint without widening this check
    would have re-opened exactly that.
    """
    # Keep the first FileRef per key — it carries the presentation metadata
    # (filename / mime_type / size_bytes) denormalized onto the row.
    unique_files: dict[str, FileRef] = {}
    for file_ref in files:
        unique_files.setdefault(file_ref.file_key, file_ref)
    if not unique_files:
        return []

    await assert_files_owned_by(db, user_id=user_id, file_keys=unique_files.keys())

    bound_here = set(
        (
            await db.execute(
                select(SessionFile.file_key).where(
                    SessionFile.session_id == session_id,
                    SessionFile.file_key.in_(unique_files.keys()),
                )
            )
        ).scalars()
    )
    to_insert = [ref for key, ref in unique_files.items() if key not in bound_here]
    if not to_insert:
        return []

    # ``size_bytes`` is client-declared and untrusted: a low declaration
    # slips past the per-file cap AND the per-user quota (which SUMs this
    # column). The object provably exists in OSS only now, at bind, so
    # reconcile against its true size here — the one place the persisted
    # value becomes trustworthy. ``head_size`` is blocking oss2; thread it
    # and fan out so N attachments cost ~one HEAD's latency, not N serial.
    sizes = await file_storage.confirmed_sizes(
        file_storage.SESSION_FILE_POLICY, [f.file_key for f in to_insert]
    )

    newly_bound: list[str] = []
    for file_ref, size in zip(to_insert, sizes):
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
    user_id: int,
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

    A skill is invokable when it is live and visible to the user: their
    own CUSTOM, or any OFFICIAL skill, and ``PUBLISHED`` either way. The
    user's own toggle is deliberately **not** checked — it governs which
    skills the composer *offers* (see
    :mod:`app.models.user_skill_preference`), which is a convenience for
    finding them, not an authorization boundary. An explicit pick is its
    own authorization, so naming a skill that was never turned on still
    works: that is what makes a freshly uploaded skill usable, and what
    keeps a stale composer chip from failing a turn.

    This route is now the **only** way an OFFICIAL or CUSTOM skill reaches
    the model — the worker's listing carries the agent-only SYSTEM set and
    these picks, nothing else. The listing's ``status`` *is* therefore
    checked here, and it is the one place it can be: retiring a skill has
    to close this route or it closes none that matter.

    Retirement therefore takes effect here rather than in the worker. A
    turn whose snapshot already names a skill keeps running against the
    ``bundle_key`` pinned here — mid-turn consistency is worth more than
    an immediate stop — so barring the name from *new* snapshots is what
    makes ``RETIRED`` a kill switch.

    Unknown / hidden names are silently dropped rather than raising; the
    user's intent (a free-text composer prompt) is still useful even if
    one chip was stale, and surfacing a 4xx mid-conversation would be a
    worse UX than gracefully proceeding without that skill.

    Slug collisions between CUSTOM + OFFICIAL resolve CUSTOM-wins, matching
    the worker repo's ``ORDER BY (user_id IS NULL) ASC`` rule. Order is
    preserved from the request so the worker's reminder lists skills in
    the order the user picked them.
    """
    if not requested:
        return []
    # De-duplicate while preserving order — duplicates would noise both
    # the snapshot list and the LLM reminder.
    requested_unique = list(dict.fromkeys(requested))

    stmt = select(Skill.id, Skill.name, Skill.bundle_key, Skill.user_id).where(
        Skill.name.in_(requested_unique),
        Skill.status == SkillStatus.PUBLISHED,
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
    user_id: int,
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
    if not requested:
        return []
    # ``requested`` comes from ``session_mcp_server``, whose primary key rules
    # out a duplicate; the worker connects by its own rule (failed-last), so
    # the snapshot's order carries no meaning.
    stmt = select(MCPServer.id, MCPServer.name).where(
        MCPServer.id.in_(requested),
        MCPServer.user_id == user_id,
        MCPServer.connected.is_(True),
    )
    result = await db.execute(stmt)
    return [{"id": row_id, "name": name} for row_id, name in result.all()]
