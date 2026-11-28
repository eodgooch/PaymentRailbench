"""RTP, Payouts and Auction Closes (SPEC §4.1, §4.4, ADR 0012).

- `payments.payments` now holds pushes too: an ACH pull or push, or an RTP push (RTP is
  credit push only, so an RTP pull is refused here as well as in code). A push may name the
  Hold that reserves its amount (`hold_id`), which the Transaction booking its outflow
  captures.
- `simulator.plans.rail`: the Rail a Scenario planned the Payment for, since what a Payment
  emits depends on it (an RTP Payment emits only its ending).
- A new `marketplace` schema, append-only like the Ledger:
  - `auction_closes`: one row per auction, unique by its ID, with the payload fingerprint
    (a retry must match), its Effective Time, and the seller fee and Instant Payout limit
    it was booked with;
  - `lots`: each lot sold, its buyer and seller (Customer Balances), hammer price and fee,
    and the Ledger Transaction that booked it;
  - `payouts`: what each seller is owed from the auction and whether they asked for an
    Instant Payout;
  - `payout_steps`: what happened to each Payout, at most once per step: `reserved` (a
    Hold on the seller's Customer Balance for the amount, at the close, named here),
    `instant` (sent over RTP), `standard` (to wait until the funds behind it are Available,
    at the close or after a declined RTP), `released` (sent over ACH) and `returned` (its
    ACH Payment ended without moving money, so the Hold was released), each with the
    system job behind it.

The app role gets USAGE on the schema and SELECT and INSERT on its tables, explicitly (no
default privileges, ADR 0008), and each table gets an append-only trigger.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("auction_closes", "lots", "payouts", "payout_steps")

UPGRADE = """
ALTER TABLE payments.payments
    DROP CONSTRAINT payments_rail_check,
    DROP CONSTRAINT payments_direction_check,
    ADD CONSTRAINT payments_rail_check CHECK (rail IN ('ach', 'rtp')),
    ADD CONSTRAINT payments_direction_check CHECK (direction IN ('pull', 'push')),
    ADD CONSTRAINT payments_rtp_is_push_only CHECK (rail <> 'rtp' OR direction = 'push'),
    ADD COLUMN hold_id BIGINT REFERENCES ledger.holds (id),
    ADD CONSTRAINT payments_hold_reserves_a_push CHECK (hold_id IS NULL OR direction = 'push');

ALTER TABLE simulator.plans
    ADD COLUMN rail TEXT NOT NULL DEFAULT 'ach' CHECK (rail IN ('ach', 'rtp'));

CREATE SCHEMA marketplace;

CREATE TABLE marketplace.auction_closes (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auction_id           TEXT NOT NULL UNIQUE CHECK (auction_id ~ '^[a-z0-9][a-z0-9:._-]{0,119}$'),
    payload_fingerprint  TEXT NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    currency             TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3,5}$'),
    closed_at            TIMESTAMPTZ NOT NULL,
    seller_fee_bps       INTEGER NOT NULL CHECK (seller_fee_bps BETWEEN 0 AND 9999),
    instant_payout_limit BIGINT NOT NULL CHECK (instant_payout_limit >= 0),
    recorded_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE marketplace.lots (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auction_close_id BIGINT NOT NULL REFERENCES marketplace.auction_closes (id),
    lot_id           TEXT NOT NULL CHECK (lot_id <> ''),
    buyer            TEXT NOT NULL REFERENCES ledger.accounts (code),
    seller           TEXT NOT NULL REFERENCES ledger.accounts (code),
    hammer_price     BIGINT NOT NULL CHECK (hammer_price > 0),
    fee              BIGINT NOT NULL CHECK (fee >= 0 AND fee < hammer_price),
    transaction_id   BIGINT NOT NULL REFERENCES ledger.transactions (id),
    UNIQUE (auction_close_id, lot_id),
    CHECK (buyer <> seller)
);

CREATE TABLE marketplace.payouts (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auction_close_id BIGINT NOT NULL REFERENCES marketplace.auction_closes (id),
    customer_account TEXT NOT NULL REFERENCES ledger.accounts (code),
    currency         TEXT NOT NULL,
    amount           BIGINT NOT NULL CHECK (amount > 0),
    requested        TEXT NOT NULL CHECK (requested IN ('instant', 'standard')),
    recorded_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (auction_close_id, customer_account)
);

CREATE TABLE marketplace.payout_steps (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payout_id    BIGINT NOT NULL REFERENCES marketplace.payouts (id),
    step         TEXT NOT NULL
                 CHECK (step IN ('reserved', 'instant', 'standard', 'released', 'returned')),
    detail       TEXT NOT NULL DEFAULT '',
    hold_id      BIGINT REFERENCES ledger.holds (id),
    job_name     TEXT NOT NULL CHECK (job_name <> ''),
    job_version  TEXT NOT NULL CHECK (job_version <> ''),
    effective_at TIMESTAMPTZ NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (payout_id, step),
    -- The reservation names its Hold; the other steps act on it.
    CHECK ((step = 'reserved') = (hold_id IS NOT NULL))
);

CREATE FUNCTION marketplace.reject_history_edits() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('railbench.migrating', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'marketplace.% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON marketplace.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION marketplace.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"""
GRANT USAGE ON SCHEMA marketplace TO {APP_ROLE};
GRANT SELECT, INSERT ON {", ".join(f"marketplace.{t}" for t in TABLES)} TO {APP_ROLE};
"""

DOWNGRADE = f"""
REVOKE USAGE ON SCHEMA marketplace FROM {APP_ROLE};
DROP TABLE {", ".join(f"marketplace.{t}" for t in reversed(TABLES))};
DROP FUNCTION marketplace.reject_history_edits();
DROP SCHEMA marketplace;
ALTER TABLE simulator.plans DROP COLUMN rail;
ALTER TABLE payments.payments
    DROP CONSTRAINT payments_hold_reserves_a_push,
    DROP COLUMN hold_id,
    DROP CONSTRAINT payments_rtp_is_push_only,
    DROP CONSTRAINT payments_rail_check,
    DROP CONSTRAINT payments_direction_check,
    ADD CONSTRAINT payments_rail_check CHECK (rail = 'ach'),
    ADD CONSTRAINT payments_direction_check CHECK (direction = 'pull');
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
