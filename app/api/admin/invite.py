"""Invite codes and redemptions — the console's view of the front door.

- ``GET  /invite-codes?origin=&q=``  — every code, operator and referral.
- ``POST /invite-codes``             — issue a ``SYSTEM`` code.
- ``GET  /invite-redemptions``       — who joined on whose code.

Registration is invite-gated, so this is the growth funnel: issuing is
the only way to let someone in, and the redemption list is the referral
trail behind every commission.

Issuing replaces day-to-day use of ``scripts/create_invite_code.py`` and
goes through :func:`app.services.auth.invite.create_invite_code` — the
same funnel registration uses, so code generation, the origin/owner
consistency check and collision retry are not re-implemented here.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.admin.dependencies import StaffContext
from app.common.pagination import paginate
from app.models.invite_code import InviteCode, InviteCodeOrigin
from app.models.invite_redemption import InviteRedemption
from app.models.user import User
from app.schemas.admin.invite import (
    InviteCodeCreate,
    InviteCodeResponse,
    InviteRedemptionResponse,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services.admin import AuditAction, audit
from app.services.auth import invite as invite_service

router = APIRouter(tags=["invites"])


def _to_code_response(code: InviteCode, owner_username: str | None) -> InviteCodeResponse:
    return InviteCodeResponse(
        id=code.id,
        code=code.code,
        origin=code.origin,
        owner_user_id=code.owner_user_id,
        owner_username=owner_username,
        max_uses=code.max_uses,
        used_count=code.used_count,
        enabled=code.enabled,
        note=code.note,
        expires_at=code.expires_at,
        created_at=code.created_at,
    )


@router.get("/invite-codes", response_model=PaginatedResponse[InviteCodeResponse])
async def list_invite_codes(
    ctx: StaffContext,
    origin: InviteCodeOrigin | None = Query(None),
    q: str | None = Query(None, min_length=1, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[InviteCodeResponse]:
    """Return one page of invite codes, newest first.

    ``?origin=SYSTEM`` is the operator view — the codes the platform
    issued, as opposed to the per-user referral codes registration mints
    automatically and which outnumber them.
    """
    filters = []
    if origin is not None:
        filters.append(InviteCode.origin == origin)
    if q is not None:
        pattern = f"%{q}%"
        filters.append(sa.or_(InviteCode.code.ilike(pattern), InviteCode.note.ilike(pattern)))

    total = await ctx.db.scalar(select(func.count()).select_from(InviteCode).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(InviteCode, User.username)
                .outerjoin(User, User.id == InviteCode.owner_user_id)
                .where(*filters)
                .order_by(InviteCode.created_at.desc(), InviteCode.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[_to_code_response(code, username) for code, username in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post(
    "/invite-codes",
    response_model=InviteCodeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite_code(ctx: StaffContext, body: InviteCodeCreate) -> InviteCodeResponse:
    """Issue a ``SYSTEM`` invite code.

    The generated code lands in the audit payload: an invite is an
    access grant, and "who let this account in" should be answerable
    from the trail without joining back through the redemption.
    """
    async with audit(
        ctx,
        action=AuditAction.INVITE_CREATE,
        target_type="invite_code",
    ) as draft:
        code = await invite_service.create_invite_code(
            ctx.db,
            origin=InviteCodeOrigin.SYSTEM,
            max_uses=body.max_uses,
            note=body.note,
        )
        draft.target_id = code.id
        draft.payload = {"code": code.code, "max_uses": body.max_uses}

    await ctx.db.refresh(code)
    return _to_code_response(code, None)


@router.get(
    "/invite-redemptions",
    response_model=PaginatedResponse[InviteRedemptionResponse],
)
async def list_invite_redemptions(
    ctx: StaffContext,
    referrer_user_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[InviteRedemptionResponse]:
    """Return one page of redemptions, newest first.

    Two joins onto ``user`` — invitee and referrer — so the row reads as
    the relationship it records rather than a pair of ids.
    ``?referrer_user_id=`` is one person's referral tree, one level
    deep, which is the only depth commission is paid on.
    """
    invitee = aliased(User)
    referrer = aliased(User)

    filters = []
    if referrer_user_id is not None:
        filters.append(InviteRedemption.referrer_user_id == referrer_user_id)

    total = await ctx.db.scalar(select(func.count()).select_from(InviteRedemption).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(InviteRedemption, InviteCode, invitee.username, referrer.username)
                .join(InviteCode, InviteCode.id == InviteRedemption.invite_code_id)
                .join(invitee, invitee.id == InviteRedemption.invitee_user_id)
                .outerjoin(referrer, referrer.id == InviteRedemption.referrer_user_id)
                .where(*filters)
                .order_by(InviteRedemption.created_at.desc(), InviteRedemption.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[
            InviteRedemptionResponse(
                id=redemption.id,
                code=code.code,
                origin=code.origin,
                invitee_user_id=redemption.invitee_user_id,
                invitee_username=invitee_username,
                referrer_user_id=redemption.referrer_user_id,
                referrer_username=referrer_username,
                created_at=redemption.created_at,
            )
            for redemption, code, invitee_username, referrer_username in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
