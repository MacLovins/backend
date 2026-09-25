import asyncio
import os
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from leadradar_auth.schemas import Role, UserCreate
from leadradar_auth.service import AuthService
from leadradar_auth.settings import auth_settings

app = typer.Typer(help="LeadRadar Auth CLI")


def get_async_session_maker():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://leadradar:leadradar@localhost:5432/leadradar",
    )
    engine = create_async_engine(db_url, echo=False)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _create_user(
    email: str,
    password: str,
    role: Role,
    full_name: str | None = None,
    org_id: UUID | None = None,
) -> None:
    session_maker = get_async_session_maker()
    async with session_maker() as session:
        service = AuthService(session)
        user = await service.create_user(
            UserCreate(
                email=email,
                password=password,
                role=role,
                full_name=full_name,
                org_id=org_id or auth_settings.DEFAULT_ORG_ID,
            )
        )
        typer.echo(f"User created: {user.email} (id: {user.id}, role: {user.role})")


@app.command()
def create_user(
    email: str = typer.Option(..., "--email", "-e", help="User email address"),
    password: str = typer.Option(
        None,
        "--password",
        "-p",
        prompt=True,
        hide_input=True,
        help="User password",
    ),
    role: str = typer.Option("sales", "--role", "-r", help="User role (admin or sales)"),
    full_name: str | None = typer.Option(None, "--full-name", "-n", help="Full name"),
    org_id: str | None = typer.Option(None, "--org-id", help="Organization UUID"),
) -> None:
    """Create a new user account."""
    if role not in ("admin", "sales"):
        typer.echo("Error: role must be 'admin' or 'sales'", err=True)
        raise typer.Exit(code=1)

    parsed_org_id = UUID(org_id) if org_id else None
    try:
        asyncio.run(
            _create_user(
                email=email,
                password=password,
                role=role,  # type: ignore
                full_name=full_name,
                org_id=parsed_org_id,
            )
        )
    except Exception as e:
        typer.echo(f"Error creating user: {e}", err=True)
        raise typer.Exit(code=1) from e


if __name__ == "__main__":
    app()
