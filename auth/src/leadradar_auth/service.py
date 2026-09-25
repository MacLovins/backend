from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from leadradar_auth.models import UserAccount
from leadradar_auth.repository import UserRepository
from leadradar_auth.schemas import Principal, UserCreate, UserUpdate
from leadradar_auth.security import DUMMY_HASH, hash_password, verify_password
from leadradar_auth.settings import auth_settings


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    async def authenticate(self, email: str, password: str) -> tuple[UserAccount, Principal] | None:
        normalized_email = email.strip().lower()
        user = await self.repo.get_by_email(normalized_email)

        if user is None:
            # Prevent timing attacks by computing dummy verification
            verify_password(password, DUMMY_HASH)
            return None

        if not user.is_active:
            return None

        if not verify_password(password, user.password_hash):
            return None

        await self.repo.update_last_login(user.id)
        await self.session.commit()

        principal = Principal(
            user_id=user.id,
            org_id=user.org_id,
            email=user.email,
            role=user.role,  # type: ignore
            full_name=user.full_name,
        )
        return user, principal

    async def create_user(self, user_in: UserCreate) -> UserAccount:
        normalized_email = user_in.email.strip().lower()
        existing = await self.repo.get_by_email(normalized_email)
        if existing:
            raise ValueError(f"User with email '{normalized_email}' already exists")

        user = UserAccount(
            org_id=user_in.org_id or auth_settings.DEFAULT_ORG_ID,
            email=normalized_email,
            password_hash=hash_password(user_in.password),
            full_name=user_in.full_name,
            role=user_in.role,
            is_active=True,
        )
        created = await self.repo.create(user)
        await self.session.commit()
        return created

    async def get_by_id(self, user_id: UUID) -> UserAccount | None:
        return await self.repo.get_by_id(user_id)

    async def list_users(self, org_id: UUID) -> list[UserAccount]:
        return await self.repo.list_by_org(org_id)

    async def update_user(self, user_id: UUID, user_update: UserUpdate) -> UserAccount | None:
        user = await self.repo.get_by_id(user_id)
        if not user:
            return None

        if user_update.full_name is not None:
            user.full_name = user_update.full_name
        if user_update.role is not None:
            user.role = user_update.role
        if user_update.is_active is not None:
            user.is_active = user_update.is_active
        if user_update.password is not None:
            user.password_hash = hash_password(user_update.password)

        await self.session.commit()
        await self.session.refresh(user)
        return user
