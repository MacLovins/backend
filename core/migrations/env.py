import asyncio
from logging.config import fileConfig

from alembic import context
import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from leadradar_auth.models import auth_metadata
from leadradar_core.db.models import core_metadata
from leadradar_core.settings import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set database URL from leadradar_core settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = [core_metadata, auth_metadata]


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and object.schema not in ("core", "auth"):
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=include_object,
        version_table_schema="core",
    )

    with context.begin_transaction():
        context.execute("CREATE SCHEMA IF NOT EXISTS core;")
        context.execute("CREATE SCHEMA IF NOT EXISTS auth;")
        context.execute("CREATE SCHEMA IF NOT EXISTS langgraph;")
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Ensure schemas exist before Alembic creates the version table
    connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS core;"))
    connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS auth;"))
    connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS langgraph;"))
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=include_object,
        version_table_schema="core",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.DATABASE_URL

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
