from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from dealership_agent.config import get_settings
from dealership_agent.db.base import Base
from dealership_agent.db.models import (  # noqa: F401  (registers tables on Base.metadata)
    Customer,
    Escalation,
    Order,
    OrderStatusHistory,
    TestDriveBooking,
    Vehicle,
    VehicleEmbedding,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The DB URL is intentionally NOT read from alembic.ini. Migrations run as
# the owner/superuser role (DATABASE_MIGRATION_URL) - they need privileges
# the app role must never have, such as CREATE ROLE and ENABLE ROW LEVEL
# SECURITY. See docs/adr and db/rls.py.
_settings = get_settings()


def _migration_url() -> str:
    if not _settings.database_migration_url:
        raise RuntimeError(
            "DATABASE_MIGRATION_URL is not set. Migrations must run as the "
            "owner/superuser role, not the app role."
        )
    return _settings.database_migration_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = create_engine(_migration_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
