"""The database Engine and the Postgres schemas each part of Railbench owns."""

import re
from typing import Any

from sqlalchemy import Engine, create_engine, event

LEDGER_SCHEMA = "ledger"
PAYMENTS_SCHEMA = "payments"  # Payments and the Inbox of Provider Events
RECONCILIATION_SCHEMA = "reconciliation"
SIMULATOR_SCHEMA = "simulator"

SCHEMAS = (LEDGER_SCHEMA, PAYMENTS_SCHEMA, RECONCILIATION_SCHEMA, SIMULATOR_SCHEMA)

LEDGER_APP_ROLE = "railbench_app"  # SELECT and INSERT only on the Ledger (migration 0002)

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def make_engine(database_url: str, role: str | None = None) -> Engine:
    """An Engine whose every connection runs as `role`, if given (`SET ROLE`).

    The login user must be a member of `role` (or a superuser, as in local development).
    """
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    if role is not None:
        if not _ROLE_NAME.match(role):
            raise ValueError(f"not a plain Postgres role name: {role!r}")

        @event.listens_for(engine, "connect")
        def set_role(dbapi_connection: Any, _record: Any) -> None:
            # Autocommit, so the SET outlives the pool's reset-on-return rollback.
            autocommit = dbapi_connection.autocommit
            dbapi_connection.autocommit = True
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f"SET ROLE {role}")
            dbapi_connection.autocommit = autocommit

    return engine
