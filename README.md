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

## The Ledger model

The Ledger is double-entry and append-only, in the `ledger` schema (migration `0002`).

- **Accounts** have a code, a type (asset, liability, revenue, expense) and one currency.
- A **Transaction** is a set of **Entries**. Each Entry is a signed, non-zero integer amount
  in Minor Units (positive is a debit, negative a credit) posted to one Account in that
  Account's currency, with an **Effective Time**, a **Recorded Time** (from the Clock) and an
  **Actor**: a system job (name and version), a Provider Event (Inbox ID), or a person
  (operator ID and approved Proposal ID). The database checks each Actor kind has exactly its
  own fields.
- Entries **sum to zero per currency**: checked in code before posting, and again at commit
  by deferred constraint triggers, which also refuse a Transaction with no Entries.
- **Balances are bitemporal and derived (ADR 0002, ADR 0009).**
  `Journal.balances(account, effective_at, known_at=None)` and
  `GET /accounts/{code}/balances?effective_at=…&known_at=…` (both with a UTC offset;
  `known_at` defaults to now and can't be later) return the **Posted**, **Pending** and
  **Available** Balances, plus the `held` and `unsettled_outflows` behind Available =
  Posted − Holds − unsettled outflows. A row counts when its Effective Time ≤ `effective_at`
  and its Recorded Time ≤ `known_at` (both inclusive), so a backdated Entry changes the
  balance "as known now" but not "as known then". Balances read in the Account's normal
  direction: a Customer Balance (a liability) holding $30 reads `3000`, while the Entries
  stay debit-positive. Nothing is stored or cached: every read is one SQL derivation over an
  index on Entries by (Account, Effective Time, Recorded Time).
- **Pending and Settlement.** A Transaction is posted (the default) or `pending`, fixed when
  it's posted. `POST /transactions/{id}/settlement` appends a **Settlement**, with its own
  Effective Time, Actor and Idempotency Key, from which the pending Entries count as Posted.
  No Entry changes. A Transaction settles once. Reversing an unsettled pending Transaction
  (a decline) posts a pending Reversal that cancels it, and it can't settle afterwards;
  reversing a posted or settled one (a Return) is posted, and can't take effect before the
  original or its Settlement. Settle and reverse take one advisory lock per Transaction, so
  they can't race. **Unsettled outflows** are the in-flight pending Transactions that take
  money out of the Account. Pending inflows aren't Available until they settle.
- **Holds.** `POST /holds` reserves a positive amount on one Account, reducing Available
  without moving money. A Hold ends once: `POST /holds/{id}/release` (no money moves) or
  `POST /holds/{id}/capture` with a Transaction, which is posted in the same commit and must
  take money out of the held Account. A second release or capture, or a release after a
  capture, is a `409`. Holds are append-only too (`holds` and `hold_ends`), and bitemporal
  like Entries. Daily Close comes later.
- **Idempotency:** every Transaction has a unique Idempotency Key and a SHA-256 fingerprint of
  its payload (times compared as instants). The row goes in with `ON CONFLICT DO NOTHING`,
  so concurrent posts of one key post once. The fingerprint covers the Entries, Actor,
  Effective Time and description, so a retry that changes only its description is a
  conflict (ADR 0008). Over HTTP the first post answers `201 Created`;
  a retry with the same payload answers `200 OK` with the original and
  `Idempotent-Replayed: true`; the same key with another payload answers `409 Conflict`.
- **Reversal** (`POST /transactions/{id}/reversal`, with its own Idempotency Key and Actor)
  posts a Transaction that negates every Entry of the original and references it. A unique
  `reverses_id` means a Transaction is reversed at most once (a second attempt is a `409`).
  A Reversal can't itself be reversed: post a new Transaction instead (ADR 0008).
- **Append-only (ADR 0006):** the Ledger API runs as `railbench_app` (`SET ROLE` on every
  connection, `LEDGER_DATABASE_ROLE`), which has `SELECT` and `INSERT` only on Ledger tables.
  A statement trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` from every role, the owner
  included, unless the session set `railbench.migrating = on`, which only `migrations/env.py`
  does (`SET LOCAL`, inside the migration transaction). That setting guards against
  accidents, not against the owner, who can always disable a trigger; the app role has no
  `UPDATE` or `DELETE` grant whatever it sets. Transaction and Entry IDs are unique but not
  gapless: a refused or replayed post can use up an ID.

The API: `POST /accounts`, `GET /accounts/{code}`, `GET /accounts/{code}/balances`,
`POST /transactions`, `GET /transactions/{id}`, `POST /transactions/{id}/reversal`,
`POST /transactions/{id}/settlement`, `POST /holds`, `GET /holds/{id}`,
`POST /holds/{id}/release` and `POST /holds/{id}/capture`. Every write takes an Idempotency
Key and replays or conflicts the same way. Money is a JSON integer: `12.5`, `12.0`,
`"1250"` and `true` are all refused with `422`.

## Payments, the Inbox and the Simulator

An ACH pull funds a Customer Balance end to end (SPEC §4.1–4.3, §6, ADR 0004, ADR 0005,
ADR 0010):

1. `POST /payments` on the Ledger (`rail: ach`, `direction: pull`, a Customer Balance, an
   integer amount, an Idempotency Key) asks the Provider for the Payment through the
   **Provider interface** (`railbench.payments.provider`): create Payment, get Payment,
   verify and read a webhook. `SimulatorProvider` is its HTTP adapter for the Simulator; no
   Ledger or Payments module imports the Simulator (a test checks the imports). The Payment
   is recorded as `created`. Retries replay with `200`; another payload under the same key
   is a `409`.
2. The Simulator submits the pull, settles it at 08:30 New York time on the Banking Day
   after submission (the 16:00 Cutoff pushes a late one a day), and delivers signed
   Provider Events (`payment.submitted`, `payment.settled`) to
   `POST /providers/simulator/events`, retrying non-2xx answers and timeouts after 30 s,
   60 s, … up to an hour apart, 10 attempts at most.
3. The Ledger verifies the HMAC signature (401 if it fails; nothing is stored), stores the
   event raw in the **Inbox**, deduplicates it by event ID, and applies it at most once.
   The ACH state machine (`created → submitted → settled → available`, or `canceled` /
   `declined` before settlement) moves forward only: an event for a state already passed
   is recorded `stale`, an impossible one (a decline after settlement) is recorded as an
   `exception` with no Ledger write, and both show on `GET /payments/{id}`.
4. Booking, through the Journal, with the Provider Event's Inbox ID as Actor:
   **submitted**: a pending Transaction, Clearing Account → Customer Balance (Pending);
   **settled**: its Settlement (now Posted), a Transaction from the Clearing Account to Bank
   Cash (Clearing nets to zero), and a Hold on the Customer Balance;
   **declined/canceled**: a pending Reversal.
5. **Available** is the Ledger's decision, not the Provider's: a job on the Ledger's Clock
   releases the Hold at midnight New York time at the start of the second Banking Day after
   the settlement's Banking Day. A pull settled on Friday 4 September 2026 is Available on
   Wednesday 9 September, after a weekend and Labor Day.

The **Banking Day calendar** (`railbench.common.calendar`) knows weekends and the Federal
Reserve's holidays as it publishes them (a Sunday holiday closes the Monday; a Saturday
holiday doesn't close the Friday). Payments, the Inbox, outcomes and state changes live in
the `payments` schema and are append-only like the Ledger (migration `0004`); replaying the
Inbox into an empty Ledger reproduces it (tested). The Simulator keeps its own mutable state
in the `simulator` schema (migration `0005`), which the Ledger's role can't read.

**The Clock in tests and running services.** Tests run both apps in-process on one
`ManualClock`, each on a real localhost port, so webhooks cross real HTTP; they call
`Simulator.tick()` and `Payments.make_available()` by hand. Running services use the wall
clock and do the same work on timers (`SIMULATOR_TICK_SECONDS`, `LEDGER_TICK_SECONDS`, on in
Compose), so under `make dev` a pull reaches `submitted` within a second but settles on the
real next Banking Day. A clock shared by running services, for demos, comes with
`make night`.

The Ledger API adds `POST /payments`, `GET /payments/{id}` and
`POST /providers/{provider}/events`; the Simulator serves `POST /payments` and
`GET /payments/{id}`.

### Layout

- `src/railbench/common`: settings, the Clock, the Banking Day calendar, webhook signing,
  JSON logging, the database Engine, `/health`
- `src/railbench/ledger`: the Ledger. `journal.py` posts and reads (the one way in),
  `model.py` holds the validated values, `app.py` is the thin HTTP layer
- `src/railbench/payments`: Payments, the Inbox and the ACH state machine, the Provider
  interface and its Simulator adapter; served by the Ledger API
- `src/railbench/simulator`: the Simulator, a separate service (ADR 0005)
- `migrations/`: Alembic. The baseline creates the `ledger`, `payments` (Payments and the
  Inbox), `reconciliation` and `simulator` schemas; `0002` adds the Ledger tables, triggers
  and the `railbench_app` role; `0003` adds Transaction status, Settlements and Holds;
  `0004` adds Payments, the Inbox and state changes; `0005` the Simulator's state.

Nothing reads the system time directly: code takes a `Clock` (`WallClock` by default,
`ManualClock` in tests and Scenarios), and a ruff rule bans `datetime.now()` and friends
everywhere else.
