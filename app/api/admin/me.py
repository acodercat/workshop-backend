"""The signed-in operator's own account.

- ``GET /me``           — who am I (the console restores its session with this).
- ``PUT /me/password``  — rotate my own password.

Separate from :mod:`app.api.admin.staff`, which is the management view
of *other people's* accounts: these two routes act on the caller
themselves.
"""

from fastapi import APIRouter, Response, status

from app.api.admin.dependencies import StaffContext
from app.auth import verify_password
from app.errors import IncorrectPasswordError, SamePasswordError
from app.schemas.admin.staff import StaffPasswordChange, StaffProfileResponse
from app.services.admin import staff as staff_service

router = APIRouter(tags=["me"])


@router.get("/me", response_model=StaffProfileResponse)
async def read_me(ctx: StaffContext) -> StaffProfileResponse:
    """Return the signed-in operator.

    The console calls this on boot to restore its session. The dependency
    already resolved the account, so this costs no query.
    """
    return StaffProfileResponse.model_validate(ctx.staff)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(ctx: StaffContext, body: StaffPasswordChange) -> Response:
    """Rotate the caller's own password.

    Sudo-gated on ``current_password``, mirroring the customer-facing
    email/phone change flows: holding a live token is not on its own
    enough to rotate the credential behind it.

    **This signs the caller out**, here and everywhere else — the token
    version bumps, so the token that authorized this request is dead by
    the time the response lands. That is the point of the rotation, and
    the console is expected to route back to sign-in on the 204.
    """
    if not await verify_password(body.current_password, ctx.staff.password_hash):
        raise IncorrectPasswordError("Current password is incorrect.")
    if body.new_password == body.current_password:
        raise SamePasswordError("New password must differ from the current one.")

    await staff_service.set_password(ctx.db, ctx.staff, body.new_password)
    await ctx.db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
