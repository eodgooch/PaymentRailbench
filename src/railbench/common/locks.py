"""Every advisory lock space Railbench takes, in one place, so no two kinds of lock collide.

The app role has no UPDATE, so nothing takes row locks; work that must not race takes a
transaction-scoped Postgres advisory lock instead. The Journal uses the one-key form, keyed
by a Transaction's id. Everything else uses the two-key form, `(space, key)`, with a space
from `LockSpace`: Postgres keeps the one-key and two-key forms apart, so they never collide.
A lock that needs several keys takes them in sorted order, so two takers can't deadlock.
"""

from enum import IntEnum

from sqlalchemy import Connection, func, select

INT4_MAX = 2_147_483_647


class LockSpace(IntEnum):
    PAYMENT = 4  # applying a Provider Event to one Payment (Payments)
    AUCTION = 5  # closing one auction (marketplace)
    PAYOUT = 6  # moving one Payout on a step (marketplace)
    SELLER = 7  # one Customer's Instant Payout exposure (marketplace)


def lock(connection: Connection, space: LockSpace, key: int) -> None:
    """Hold `(space, key)` until the current transaction ends."""
    connection.execute(select(func.pg_advisory_xact_lock(int(space), key % INT4_MAX)))


def lock_name(connection: Connection, space: LockSpace, name: str) -> None:
    """Hold `(space, a hash of name)` until the current transaction ends. Two names can share
    a hash; that only makes them wait for each other."""
    connection.execute(select(func.pg_advisory_xact_lock(int(space), func.hashtext(name))))
