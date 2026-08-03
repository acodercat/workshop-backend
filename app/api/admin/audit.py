"""Reading the audit trail.

``GET /audit-logs`` — newest-first, filtered, paginated.

Read-only, and there is no write route here by construction: rows are
written exclusively by :func:`app.services.admin.audit`, alongside the
change they describe. An endpoint that could append to this table would
be a way to record something that never happened.

``VIEWER``, so support staff can answer "what happened to this account"
without holding the privilege to change anything. The three filter
combinations are the three composite indexes on the table — by operator,
by target, by action — so the common questions are index scans rather
than a walk of the whole log.
"""

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.admin.dependencies import ViewerContext
from app.common.pagination import paginate
from app.models.admin_audit_log import AdminAuditLog
from app.models.staff import Staff
from app.schemas.admin.audit import AuditLogResponse
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services.admin import AuditAction

router = APIRouter()


@router.get("", response_model=PaginatedResponse[AuditLogResponse])
async def list_audit_logs(
    ctx: ViewerContext,
    staff_id: int | None = Query(None, description="Actions by this operator"),
    action: AuditAction | None = Query(None, description="Exact action"),
    target_type: str | None = Query(None, max_length=32, description="Kind of row acted on"),
    target_id: str | None = Query(None, max_length=64, description="Its key"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[AuditLogResponse]:
    """Return one page of recorded operator actions, newest first.

    Filters are ANDed and every one is optional. ``action`` and
    ``target_type`` are validated against the console's vocabulary, so a
    typo is a 422 rather than a silently empty page.

    The count and the page are built from **one** filter list, so the
    total can never describe a different query than the rows.
    """
    filters = []
    if staff_id is not None:
        filters.append(AdminAuditLog.staff_id == staff_id)
    if action is not None:
        filters.append(AdminAuditLog.action == action.value)
    if target_type is not None:
        filters.append(AdminAuditLog.target_type == target_type.value)
    if target_id is not None:
        filters.append(AdminAuditLog.target_id == target_id)

    total = await ctx.db.scalar(
        select(func.count()).select_from(AdminAuditLog).where(*filters)
    )
    # Joined rather than resolved per row: the log is read as "who did
    # this", and N+1 lookups against a directory of a dozen accounts is
    # the wrong shape for a page of fifty rows.
    rows = (
        await ctx.db.execute(
            paginate(
                select(AdminAuditLog, Staff.username)
                .join(Staff, Staff.id == AdminAuditLog.staff_id)
                .where(*filters)
                .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[
            AuditLogResponse(
                id=entry.id,
                staff_id=entry.staff_id,
                staff_username=username,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                reason=entry.reason,
                payload=entry.payload,
                request_id=entry.request_id,
                ip_address=entry.ip_address,
                created_at=entry.created_at,
            )
            for entry, username in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
