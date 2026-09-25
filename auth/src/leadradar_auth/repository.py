from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from leadradar_auth.models import UserAccount


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> UserAccount | None:
        stmt = select(UserAccount).where(UserAccount.email == email.strip().lower())
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_id(self, user_id: UUID) -> UserAccount | None:
        stmt = select(UserAccount).where(UserAccount.id == user_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_org(self, org_id: UUID) -> list[UserAccount]:
        stmt = select(UserAccount).where(UserAccount.org_id == org_id).order_by(UserAccount.created_at.desc())
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def create(self, user: UserAccount) -> UserAccount:
        self.session.add(user)
        await self.session.flush()
        return user

    async def update_last_login(self, user_id: UUID) -> None:
        stmt = update(UserAccount).where(UserAccount.id == user_id).values(last_login_at=datetime.now(UTC))
        await self.session.execute(stmt)
