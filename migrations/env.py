"""Alembic environment. Migrations are hand-written; there is no autogenerate metadata yet.

Every migration runs in one transaction with `railbench.migrating = on` (SET LOCAL, so it ends
with the transaction). That setting is the only way past the Ledger's append-only trigger
(migration 0002). It is a guard against accidents, not against the owner: the app role has
no UPDATE or DELETE grant whatever it sets, and an owner can always disable a trigger.
"""

from alembic import context
from sqlalchemy import create_engine, text

from railbench.common.settings import Settings

config = context.config
url = config.get_main_option("sqlalchemy.url") or Settings().database_url

MIGRATING = "SET LOCAL railbench.migrating = 'on'"


def run_migrations_offline() -> None:
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.execute(MIGRATING)
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            connection.execute(text(MIGRATING))
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
