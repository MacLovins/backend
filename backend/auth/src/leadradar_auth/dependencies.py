from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from leadradar_auth.schemas import Principal, Role
from leadradar_auth.security import decode_access_token
from leadradar_auth.settings import auth_settings

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_principal(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Principal:
    token: str | None = None

    # 1. Try cookie first (primary for web SPA)
    cookie_token = request.cookies.get(auth_settings.COOKIE_NAME)
    if cookie_token:
        token = cookie_token
    elif bearer and bearer.credentials:
        token = bearer.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(token)
        return Principal(
            user_id=UUID(payload["sub"]),
            org_id=UUID(payload["org"]),
            email=payload["email"],
            role=payload["role"],
            full_name=payload.get("name"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_roles(*allowed_roles: Role):
    async def role_checker(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if principal.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation not permitted for role '{principal.role}'",
            )
        return principal

    return role_checker
