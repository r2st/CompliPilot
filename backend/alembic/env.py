"""Alembic environment.

The URL comes from the application's settings rather than from ``alembic.ini``,
so ``alembic upgrade head`` and the running API can never disagree about which
database they are talking to.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.database import Base

# Importing the package registers every model on Base.metadata. Without it
# autogenerate sees an empty metadata and proposes dropping the whole schema.
import app.models  # noqa: F401  isort:skip

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate away from things it should not manage.

    ``alembic_version`` is alembic's own bookkeeping table; comparing it
    against the model metadata proposes dropping it on every run.
    """
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without these two, a widened column or a changed default is
            # invisible to autogenerate and the migration silently omits it.
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. Harmless on Postgres, and it means the same
            # migration file runs against the test database.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
