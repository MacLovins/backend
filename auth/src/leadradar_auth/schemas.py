from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

Role = Literal["admin", "sales"]


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    org_id: UUID
    email: EmailStr
    role: Role
    full_name: str | None = None


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    password: str
    remember_me: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None = None
    role: Role
    is_active: bool
    last_login_at: datetime | None = None


class LoginResponse(BaseModel):
    user: UserOut
    access: str | None = None
    refresh: str | None = None
    role: str | None = None
    token_type: str = "bearer"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str
    full_name: str | None = None
    role: Role = "sales"
    org_id: UUID | None = None


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = None
