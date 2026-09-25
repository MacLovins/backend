from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from leadradar_auth.dependencies import get_current_principal, require_roles
from leadradar_auth.schemas import (
    LoginIn,
    LoginResponse,
    Principal,
    TokenResponse,
    UserCreate,
    UserOut,
    UserUpdate,
)
from leadradar_auth.security import create_access_token
from leadradar_auth.service import AuthService
from leadradar_auth.settings import auth_settings

# In-memory rate limiter: (ip, email) -> list of timestamps
_login_attempts: dict[tuple[str, str], list[datetime]] = defaultdict(list)
RATE_LIMIT_WINDOW = timedelta(minutes=5)
MAX_LOGIN_ATTEMPTS = 10


def _check_rate_limit(ip: str, email: str) -> None:
    now = datetime.now(UTC)
    key = (ip, email.strip().lower())
    attempts = [t for t in _login_attempts[key] if now - t < RATE_LIMIT_WINDOW]
    _login_attempts[key] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again later.",
        )


def _record_attempt(ip: str, email: str) -> None:
    now = datetime.now(UTC)
    key = (ip, email.strip().lower())
    _login_attempts[key].append(now)


def create_auth_router(
    get_session: Callable[[], AsyncIterator[AsyncSession]],
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login", response_model=LoginResponse)
    async def login(
        login_in: LoginIn,
        request: Request,
        response: Response,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> LoginResponse:
        client_ip = request.client.host if request.client else "127.0.0.1"
        _check_rate_limit(client_ip, login_in.email)

        service = AuthService(session)
        result = await service.authenticate(login_in.email, login_in.password)
        if not result:
            _record_attempt(client_ip, login_in.email)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_credentials",
            )

        user, principal = result
        token = create_access_token(principal)

        response.set_cookie(
            key=auth_settings.COOKIE_NAME,
            value=token,
            max_age=auth_settings.ACCESS_TTL_MIN * 60,
            httponly=True,
            secure=auth_settings.COOKIE_SECURE,
            samesite="lax",
            path="/",
        )

        return LoginResponse(
            user=UserOut.model_validate(user),
        )

    @router.post("/token", response_model=TokenResponse)
    async def token_login(
        form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> TokenResponse:
        client_ip = request.client.host if request.client else "127.0.0.1"
        _check_rate_limit(client_ip, form_data.username)

        service = AuthService(session)
        result = await service.authenticate(form_data.username, form_data.password)
        if not result:
            _record_attempt(client_ip, form_data.username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_credentials",
            )

        _, principal = result
        token = create_access_token(principal)
        return TokenResponse(access_token=token, token_type="bearer")

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(response: Response) -> None:
        response.delete_cookie(
            key=auth_settings.COOKIE_NAME,
            path="/",
            httponly=True,
            secure=auth_settings.COOKIE_SECURE,
            samesite="lax",
        )

    @router.get("/me", response_model=UserOut)
    async def get_me(
        principal: Annotated[Principal, Depends(get_current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> UserOut:
        service = AuthService(session)
        user = await service.get_by_id(principal.user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return UserOut.model_validate(user)

    # Admin endpoints
    @router.get(
        "/users",
        response_model=list[UserOut],
        dependencies=[Depends(require_roles("admin"))],
    )
    async def list_users(
        principal: Annotated[Principal, Depends(get_current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[UserOut]:
        service = AuthService(session)
        users = await service.list_users(principal.org_id)
        return [UserOut.model_validate(u) for u in users]

    @router.post(
        "/users",
        response_model=UserOut,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_roles("admin"))],
    )
    async def create_user(
        user_in: UserCreate,
        principal: Annotated[Principal, Depends(get_current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> UserOut:
        service = AuthService(session)
        user_in.org_id = principal.org_id
        try:
            user = await service.create_user(user_in)
            return UserOut.model_validate(user)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            )

    @router.patch(
        "/users/{id}",
        response_model=UserOut,
        dependencies=[Depends(require_roles("admin"))],
    )
    async def update_user(
        id: UUID,
        update_in: UserUpdate,
        principal: Annotated[Principal, Depends(get_current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> UserOut:
        service = AuthService(session)
        user = await service.get_by_id(id)
        if not user or user.org_id != principal.org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        updated = await service.update_user(id, update_in)
        return UserOut.model_validate(updated)

    return router
