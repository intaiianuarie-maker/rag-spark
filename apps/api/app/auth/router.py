"""Auth HTTP routes (binding contract: POST /auth/register|login|logout|refresh).

Tokens are returned in the JSON body only; FastAPI never sets cookies (the
Next.js proxy owns cookies per docs/engineering/frontend-m1-prep.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.auth.schemas import (
    AuthErrorBody,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserPublic,
)
from app.auth.service import AuthService, AuthServiceError

router = APIRouter(prefix="/auth", tags=["auth"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": AuthErrorBody, "description": "Invalid credentials or token."},
    409: {"model": AuthErrorBody, "description": "Registration conflict."},
    422: {"description": "Validation error (input is redacted)."},
    429: {"model": AuthErrorBody, "description": "Login rate limit exceeded."},
    503: {"model": AuthErrorBody, "description": "Auth store unavailable."},
}


def _http_error(exc: AuthServiceError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
        headers=headers,
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserPublic,
    responses=_ERROR_RESPONSES,
    summary="Register a user",
)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    service = AuthService(session)
    try:
        user = await service.register(body.email, body.password)
    except AuthServiceError as exc:
        raise _http_error(exc) from exc
    return UserPublic(id=str(user.id), email=user.email, created_at=user.created_at)


@router.post(
    "/login",
    response_model=TokenPair,
    responses=_ERROR_RESPONSES,
    summary="Log in and receive an access/refresh token pair",
)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    service = AuthService(session)
    try:
        return await service.login(body.email, body.password)
    except AuthServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/refresh",
    response_model=TokenPair,
    responses=_ERROR_RESPONSES,
    summary="Rotate the refresh token and issue a new token pair",
)
async def refresh(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    service = AuthService(session)
    try:
        return await service.refresh(body.refresh_token)
    except AuthServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ERROR_RESPONSES,
    summary="Invalidate the refresh-token family server-side",
)
async def logout(
    body: LogoutRequest,
    session: AsyncSession = Depends(get_session),
) -> Response:
    service = AuthService(session)
    try:
        await service.logout(body.refresh_token)
    except AuthServiceError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
