"""Alembic environment. Migrations are hand-written; there is no autogenerate metadata yet."""

from alembic import context
from sqlalchemy import create_engine

from railbench.common.settings import Settings

config = context.config
url = config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
