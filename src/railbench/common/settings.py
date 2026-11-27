"""Configuration from the environment (SPEC §13). Names match `.env.example`."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from railbench.common.db import LEDGER_APP_ROLE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://railbench:railbench@localhost:5432/railbench"
    ledger_readonly_database_url: str = ""
    # The Ledger API runs every connection as this role (SET ROLE): SELECT and INSERT only on
    # the Ledger (migration 0002). The DATABASE_URL user must be a member of it. Empty or
    # unset to None: connect as the DATABASE_URL user itself.
    ledger_database_role: str | None = LEDGER_APP_ROLE
    simulator_url: str = "http://localhost:8100"
    webhook_signing_secret: SecretStr = SecretStr("change-me")
    anthropic_api_key: SecretStr | None = None
    triage_model: str = ""
    triage_cost_cap_night_usd: int = 1
    triage_cost_cap_eval_usd: int = 10
    instant_payout_limit_cents: int = 500_000
    cash_advance_bps: int = 6_000
    loan_apr_bps: int = 1_200

    log_level: str = "INFO"
    ledger_port: int = 8000
    simulator_port: int = 8100
