# PaymentRailbench

A double-entry Ledger for Mintwharf, a fictional collectibles marketplace, tested against a
payment-rail Simulator that injects the failures real sandboxes don't. See `SPEC.md`,
`GLOSSARY.md` and `docs/adr/`.

## Running it locally

You need Docker and [uv](https://docs.astral.sh/uv/).

```sh
make dev    # Postgres 16, migrations, the Ledger API (:8000) and the Simulator (:8100); waits until healthy
make test   # unit tests, plus integration tests against a throwaway Postgres (testcontainers)
make lint   # ruff check, ruff format --check, mypy --strict
```

`make down` stops the stack and deletes its data. `make hooks` installs the pre-commit hooks
(gitleaks and ruff). Configuration comes from the environment; `.env.example` lists every
setting. If a port is already taken, override it: `make dev LEDGER_HOST_PORT=8001`.

Both services answer `GET /health` with their database connectivity, and return 503 when the
database is unreachable. Logs are JSON, one object per line.

### Layout

- `src/railbench/common`: settings, the Clock, JSON logging, the database Engine, `/health`
- `src/railbench/ledger`: the Ledger API
- `src/railbench/simulator`: the Simulator, a separate service (ADR 0005)
- `migrations/`: Alembic. The baseline creates the `ledger`, `payments` (Payments and the
  Inbox), `reconciliation` and `simulator` schemas.

Nothing reads the system time directly: code takes a `Clock` (`WallClock` by default,
`ManualClock` in tests and Scenarios), and a ruff rule bans `datetime.now()` and friends
everywhere else.
