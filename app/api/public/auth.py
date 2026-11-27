"""Sign-in, sign-out, and the account requests around them.

Accounts belong to the identity provider (Meeting); every endpoint here
relays to it through :class:`~app.services.identity.provider.MeetingIdentityProvider`,
and none of them touches a password beyond passing it on. What the backend adds is
the local half: the account row a provider identity maps to, and the
session a successful sign-in opens.

All failure paths raise ``AppError`` subclasses (:mod:`app.errors`) that
bubble up to ``app_error_handler`` — ``status_code`` and ``error_type``
live on each subclass, so this layer carries no reason → status table.
"""

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_identity_provider
from app.auth import Principal, authenticate_session
from app.rate_limit import limiter
from app.schemas.auth import (
    AccessTokenResponse,
    LoggedInResponse,
    PasswordResetRequest,
    RegisterRequest,
    VerificationCodeRequest,
)
from app.services.identity.provider import MeetingIdentityProvider
from app.services.identity.signin import sign_in, sign_out
from config import settings

router = APIRouter()


@router.post("/auth/access-tokens", response_model=AccessTokenResponse)
@limiter.limit(settings.rate_limit_login)
async def login_for_access_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    provider: MeetingIdentityProvider = Depends(get_identity_provider),
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Sign in with the account's identity-provider credentials.

    The OAuth2 password-grant shape: ``username`` is whatever the provider
    accepts as an account name (for Meeting, the email). The first
    successful sign-in for an identity creates its local account.
    """
    signed_in = await sign_in(db, provider, account=form_data.username, password=form_data.password)
    return AccessTokenResponse(access_token=signed_in.access_token, token_type="bearer")


@router.delete("/auth/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    db: AsyncSession = Depends(get_db),
    provider: MeetingIdentityProvider = Depends(get_identity_provider),
    principal: Principal = Depends(authenticate_session),
):
    """Sign out: revoke this session, then the provider's side of it.

    Only the session the request's own token names — other devices stay
    signed in. The token is dead once this returns, whether or not the
    provider could be reached.
    """
    await sign_out(db, provider, session_id=principal.session_id)


@router.post("/auth/verification-codes", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_register_code)
async def create_verification_code(
    request: Request,
    body: VerificationCodeRequest,
    provider: MeetingIdentityProvider = Depends(get_identity_provider),
):
    """Have the provider send a verification code to ``email``.

    The provider sends it and the provider checks it; nothing about the
    code is stored here.
    """
    await provider.send_verification_code(
        email=body.email, purpose=body.purpose, invite_code=body.invite_code
    )


@router.post(
    "/users",
    response_model=LoggedInResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_register)
async def create_user(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    provider: MeetingIdentityProvider = Depends(get_identity_provider),
):
    """Register at the provider, then sign in to the new account.

    Two steps on purpose. Registration creates the provider's account and
    nothing else; the sign-in that follows is the *same* one every
    returning user goes through, and is what creates the local account.
    So there is one way an account comes to exist here, not two that have
    to be kept equivalent — and if the second step fails, the provider
    account still exists and signing in normally completes the job.
    """
    await provider.register(
        email=body.email,
        password=body.password,
        code=body.code,
        name=body.name,
        invite_code=body.invite_code,
    )
    signed_in = await sign_in(db, provider, account=body.email, password=body.password)
    return LoggedInResponse(
        access_token=signed_in.access_token,
        token_type="bearer",
        user=signed_in.user,
    )


@router.post("/auth/password-resets", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_register)
async def reset_password(
    request: Request,
    body: PasswordResetRequest,
    provider: MeetingIdentityProvider = Depends(get_identity_provider),
):
    """Set a new password at the provider, proven by a ``recover`` code.

    Signs nobody in and ends no session here: sessions opened with the
    old password are the provider's to invalidate, and each one stops
    working at its next re-confirmation if the provider does.
    """
    await provider.reset_password(email=body.email, password=body.password, code=body.code)
