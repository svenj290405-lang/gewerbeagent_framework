"""Alembic environment config fuer Gewerbeagent Framework."""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Framework-Imports: settings + alle Modelle registrieren
from config.settings import settings
from core.database.base import Base
from core.models import OAuthToken, Tenant, ToolConfig  # noqa: F401

# Alembic-Config-Objekt
config = context.config

# DB-URL aus unseren Settings uebernehmen (nicht aus alembic.ini)
config.set_main_option("sqlalchemy.url", settings.database_url)

# Logging aus alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata aller Modelle — das ist was Alembic mit der DB vergleicht
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Migration ohne aktive DB-Verbindung (generiert nur SQL-Scripts)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Migration mit aktiver async DB-Verbindung."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online-Modus: echte DB-Verbindung nutzen."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
