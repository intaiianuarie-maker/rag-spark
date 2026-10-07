"""Auth service: registration, login, refresh rotation, logout.

Implements the M1-001a contract:
- argon2 password hashing (never logged/returned);
- JWT access/refresh pair in the JSON body (no cookies from FastAPI);
- Redis-backed refresh-token families so logout invalidates server-side;
- login rate limiting (sixth rapid attempt throttled);
- unauthenticated failures return generic messages (no user enumeration).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.adapters import redis_store as store
from app.auth.config import get_auth_settings
from app.auth.models import User
from app.auth.passwords import hash_password, verify_password
from app.auth.schemas import TokenPair
from app.auth.sessions import create_session, revoke_session, session_is_active
from app.auth.tokens import (
    REFRESH_TOKEN_TYPE,
    AuthTokenError,
    decode_token,
    issue_access_token,
    issue_refresh_token,
    new_family_id,
)

logger = logging.getLogger("knowledge_os.api.auth")

GENERIC_CREDENTIALS_MESSAGE = "Invalid email or password."
GENERIC_REGISTER_CONFLICT_MESSAGE = "Unable to register with the provided email."
GENERIC_TOKEN_MESSAGE = "Invalid or expired token."
RATE_LIMIT_MESSAGE = "Too many login attempts. Try again later."
AUTH_UNAVAILABLE_MESSAGE = "Authentication temporarily unavailable."

# Verified against when the account does not exist, to keep login timing
# comparable and avoid a user-enumeration side channel.
_DUMMY_HASH = hash_password("dummy-password-for-timing-only")


class AuthServiceError(Exception):
    """Typed error mapped to an HTTP response by the router."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_auth_settings()

    async def register(self, email: str, password: str) -> User:
        email = normalize_email(email)
        if await self._get_user_by_email(email) is not None:
            raise AuthServiceError(409, "email_taken", GENERIC_REGISTER_CONFLICT_MESSAGE)

        user = User(email=email, password_hash=hash_password(password))
        self._session.add(user)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AuthServiceError(
                409, "email_taken", GENERIC_REGISTER_CONFLICT_MESSAGE
            ) from exc
        await self._session.refresh(user)
        return user

    async def login(self, email: str, password: str) -> TokenPair:
        email = normalize_email(email)
        await self._enforce_login_rate_limit(email)

        user = await self._get_user_by_email(email)
        if user is None:
            # Constant-cost dummy verify: same failure path/timing as a bad password.
            verify_password(_DUMMY_HASH, password)
            raise self._invalid_credentials()
        if not verify_password(user.password_hash, password):
            raise self._invalid_credentials()
        if not user.is_active:
            raise self._invalid_credentials()

        return await self._issue_pair(str(user.id), new_family_id())

    async def refresh(self, refresh_token: str) -> TokenPair:
        try:
            payload = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except AuthTokenError as exc:
            raise self._token_error(exc) from exc

        jti = str(payload["jti"])
        user_id = str(payload["sub"])
        family_id = payload.get("fid")

        try:
            record = await store.get_refresh_token(jti)
        except store.AuthStoreUnavailable as exc:
            raise self._unavailable() from exc

        if record is None:
            # Validly signed but unknown jti => rotated/reused/revoked token.
            await self._best_effort_revoke(family_id)
            raise self._token_error(AuthTokenError("invalid_token"))

        if (
            not isinstance(family_id, str)
            or record.get("fid") != family_id
            or record.get("user_id") != user_id
        ):
            await self._best_effort_revoke(family_id)
            raise self._token_error(AuthTokenError("invalid_token"))

        user = await self._get_user_by_id(user_id)
        if user is None or not user.is_active:
            await self._best_effort_revoke(family_id)
            raise self._token_error(AuthTokenError("invalid_token"))

        try:
            session_active = await session_is_active(family_id)
        except store.AuthStoreUnavailable as exc:
            raise self._unavailable() from exc
        if not session_active:
            await self._best_effort_revoke(family_id)
            raise self._token_error(AuthTokenError("invalid_token"))

        new_refresh = issue_refresh_token(user_id, family_id, session_id=family_id)
        access = issue_access_token(user_id, session_id=family_id)
        try:
            await store.rotate_refresh_token(
                jti,
                new_refresh.jti,
                user_id,
                family_id,
                self._settings.refresh_token_ttl_seconds,
            )
        except store.AuthStoreUnavailable as exc:
            raise self._unavailable() from exc

        return TokenPair(
            access_token=access.token,
            refresh_token=new_refresh.token,
            expires_in=access.expires_in,
        )

    async def logout(self, refresh_token: str) -> None:
        """Invalidate the refresh-token family server-side.

        Idempotent and non-oracular: a malformed/expired/rotated token still
        yields success so callers cannot probe token validity.
        """
        try:
            payload = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except AuthTokenError:
            return

        family_id = payload.get("fid")
        if not isinstance(family_id, str) or not family_id:
            return
        try:
            await store.revoke_family(family_id)
            await revoke_session(family_id)
        except store.AuthStoreUnavailable as exc:
            raise self._unavailable() from exc

    # ---- internals -------------------------------------------------------

    async def _issue_pair(self, user_id: str, family_id: str) -> TokenPair:
        access = issue_access_token(user_id, session_id=family_id)
        refresh = issue_refresh_token(user_id, family_id, session_id=family_id)
        try:
            await create_session(family_id, user_id)
            await store.store_refresh_token(
                refresh.jti,
                user_id,
                family_id,
                self._settings.refresh_token_ttl_seconds,
            )
        except store.AuthStoreUnavailable as exc:
            raise self._unavailable() from exc
        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=access.expires_in,
        )

    async def _enforce_login_rate_limit(self, email: str) -> None:
        try:
            allowed, retry_after = await store.hit_rate_limit(
                email,
                self._settings.login_rate_limit_max_attempts,
                self._settings.login_rate_limit_window_seconds,
            )
        except store.AuthStoreUnavailable:
            # Fail open but loudly: rate limiting must not take auth down.
            logger.warning(
                "login_rate_limit_unavailable",
                extra={"event": "login_rate_limit_unavailable"},
            )
            return
        if not allowed:
            raise AuthServiceError(
                429, "rate_limited", RATE_LIMIT_MESSAGE, retry_after=retry_after
            )

    async def _get_user_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def _get_user_by_id(self, user_id: str) -> User | None:
        try:
            parsed = uuid.UUID(user_id)
        except ValueError:
            return None
        result = await self._session.execute(select(User).where(User.id == parsed))
        return result.scalar_one_or_none()

    async def _best_effort_revoke(self, family_id: object) -> None:
        if not isinstance(family_id, str) or not family_id:
            return
        try:
            await store.revoke_family(family_id)
        except store.AuthStoreUnavailable:
            logger.warning(
                "refresh_family_revoke_failed",
                extra={"event": "refresh_family_revoke_failed"},
            )

    @staticmethod
    def _invalid_credentials() -> AuthServiceError:
        return AuthServiceError(401, "invalid_credentials", GENERIC_CREDENTIALS_MESSAGE)

    @staticmethod
    def _token_error(exc: AuthTokenError) -> AuthServiceError:
        return AuthServiceError(401, exc.code, GENERIC_TOKEN_MESSAGE)

    @staticmethod
    def _unavailable() -> AuthServiceError:
        return AuthServiceError(503, "auth_unavailable", AUTH_UNAVAILABLE_MESSAGE)
