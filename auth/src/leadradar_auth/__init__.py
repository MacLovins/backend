"""LeadRadar Auth package - public API."""

from leadradar_auth.dependencies import get_current_principal, require_roles
from leadradar_auth.models import UserAccount, auth_metadata
from leadradar_auth.router import create_auth_router
from leadradar_auth.schemas import (
    LoginIn,
    LoginResponse,
    Principal,
    Role,
    TokenResponse,
    UserCreate,
    UserOut,
    UserUpdate,
)
from leadradar_auth.service import AuthService
from leadradar_auth.settings import AuthSettings, auth_settings

__all__ = [
    "AuthService",
    "AuthSettings",
    "LoginIn",
    "LoginResponse",
    "Principal",
    "Role",
    "TokenResponse",
    "UserAccount",
    "UserCreate",
    "UserOut",
    "UserUpdate",
    "auth_metadata",
    "auth_settings",
    "create_auth_router",
    "get_current_principal",
    "require_roles",
]
