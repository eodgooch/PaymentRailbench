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
   60 s, … up to an hour apart, 10 attempts at most. It claims due events in a short commit
   and POSTs outside any transaction, so a slow Ledger holds no locks.
3. The Ledger verifies the HMAC signature (401 if it fails; nothing is stored), stores the
   event raw in the **Inbox**, deduplicates it by event ID, and applies it at most once.
   The ACH state machine (`created → submitted → settled → available`, or `canceled` /
   `declined` before settlement) moves forward only: an event for a state already passed
   is recorded `stale`, an impossible one (a decline after settlement, or a `submitted`
   for a Payment that was declined without ever being submitted) is recorded as an
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

Three settings extend SPEC §13: `LEDGER_WEBHOOK_URL` (where the Simulator delivers Provider
Events), `SIMULATOR_TICK_SECONDS` and `LEDGER_TICK_SECONDS` (the timers above; `0` turns
them off). Applying one Provider Event is one database transaction: the Journal runs as a
unit of work on the Inbox's connection, so a step the Ledger refuses leaves no Ledger rows
behind, only the exception.

The Ledger API adds `POST /payments`, `GET /payments/{id}` and
`POST /providers/{provider}/events`; the Simulator serves `POST /payments` and
`GET /payments/{id}` (and, for Returns, `POST /payments/{id}/returns`).

## Auction Closes and Payouts

At an Auction Close sellers get paid (SPEC §4.4, ADR 0004, ADR 0012).
`POST /auction-closes` takes one auction's lots (buyer, seller, hammer price, each Customer
named by their Customer Balance) and the sellers asking for an Instant Payout:

```json
{"auction_id": "fall-auction", "closed_at": "2026-09-03T17:00:00-04:00", "currency": "USD",
 "lots": [{"lot_id": "lot-1", "buyer": "customer:c-avery:usd",
           "seller": "customer:c-bram:usd", "hammer_price": 25000}],
 "instant_payouts": ["customer:c-bram:usd"]}
```

- **Booking, gross.** Each lot is one posted Transaction at the close, by the
  `auction-close` system job: the buyer's Customer Balance pays the hammer price, the
  seller's is credited it less Mintwharf's fee (`SELLER_FEE_BPS`, 10% by default, rounded
  down; fictional), and Fee Revenue takes the fee. A Customer who both bought and sold owes
  their purchases and is owed their sales, lot by lot, and funds their purchases like any
  buyer. `GET /auction-closes/{auction_id}` returns the lots, each Customer's position
  (`owes`, `sold`, `fees`, `owed`) and each Payout with its steps and Payments. An auction
  closes once: a retry answers `200` with `Idempotent-Replayed: true`, other lots `409`.
- **Reserved until it goes out.** Every Payout's amount is held on the seller's Customer
  Balance from the close, so the proceeds are Posted but not Available. The Payout's
  Payment names that Hold, and the Transaction booking the money going out captures it (an
  RTP settlement, an ACH push's submission). If the money stays (an ACH push declined), it's
  Available again: reversed, or the Hold released.
- **Standard Payout** (over ACH): sent when the funds behind it are Available, that is when
  neither the buyers of that seller's lots nor the seller has a negative Available Balance
  (a buyer who funded by ACH: once the Ledger releases their funding's Hold; the seller: once
  their own purchases are paid for). The `payout-release` job then sends an ACH credit push.
  Until then the Payout's `waiting_for` names who it's waiting on.
- **Instant Payout** (over RTP): sent at once, at Mintwharf's risk, if the seller's
  outstanding exposure (their Instant Payouts not declined whose funds aren't Available yet)
  plus this one is within `INSTANT_PAYOUT_LIMIT_CENTS` (inclusive) and the seller doesn't
  owe for their own purchases. Otherwise the whole amount falls back to a Standard Payout. A
  declined RTP moved nothing, and also falls back to Standard. A per-seller lock keeps two
  auctions closing at once from both spending one seller's limit.
- **Pushes in the Ledger** mirror the pull through the Clearing Account for their Rail: an
  ACH push is a pending Transaction from the Customer Balance to the Clearing Account at
  submission (an unsettled outflow, no longer Available), then its Settlement and Bank Cash
  to the Clearing Account; declined, a pending Reversal gives the money back. A settled RTP
  push is two posted Transactions, Customer Balance to the RTP Clearing Account and Bank Cash
  to it, so the Clearing Account nets to zero at once. `POST /payments` only makes pulls.
- **RTP in the Simulator** is credit push only (an RTP pull is a `422`), instant and final:
  it settles, or is declined if planned, the moment it's created, any day at any hour (no
  Cutoff, no Banking Days), with one Provider Event, `payment.settled` or `payment.declined`,
  and no Returns. Its state machine is `created → settled | declined`; anything else is an
  exception. Duplicate and delayed delivery Faults work on RTP events; out-of-order needs a
  later event, which RTP never has. ACH pushes run on the pull's timetable.

A Payout's steps, each at most once, are `reserved`, then `instant` or `standard`, then
`released` (sent over ACH) and, if its ACH Payment ended without moving money, `returned`.
The Ledger's ticker (`LEDGER_TICK_SECONDS`) runs ACH availability and then Payout release.
`SELLER_FEE_BPS` extends SPEC §13. Auction Closes, lots, Payouts and Payout steps are
append-only in the `marketplace` schema (migration `0007`).

## Returns and Recovery Receivable

A settled ACH pull can be sent back with a NACHA return code (SPEC §4.3, ADR 0004,
ADR 0013). The Simulator knows each code's Return Window, measured from the Settlement Date
on the Banking Day calendar (`railbench.common.returns`, checked against nacha.org on
2026-11-27): R01, R02, R03, R04 and R29 by the opening of the second Banking Day after it;
unauthorized consumer debits, R05, R07, R10 and R11, by the opening of the Banking Day after
the 60th calendar day. It returns a pull as planned by a `return` Fault, or now through
`POST /payments/{id}/returns` (`{"code": "R10"}`), emitting `payment.returned` with the code,
and refuses a Return outside its window (`422`), on RTP or a push, or before settlement
(`409`). The Ledger's ACH state machine adds `returned`, from `settled` or `available`,
final; a Return for a Payment never settled is an exception with no Ledger write. Applying
one, in one database transaction with the Provider Event as Actor:

- a posted **Reversal** of the funding (Customer Balance → Clearing Account) and of its sweep
  to Bank Cash, so the money leaves Bank Cash and the Clearing Account still nets to zero;
  nothing is edited;
- the availability Hold, if still in place, is released, so the money is never Available;
- if the Customer Balance is left short (the money was spent, say at an Auction Close), the
  deficit moves to that Customer's **Recovery Receivable** (`recovery-receivable:<Customer
  Balance>`), the Customer Balance goes back to zero, and a **return exception** records the
  exposure in Minor Units, the return code and whether it was a **Late Return** (after the
  funds were Available). `GET /return-exceptions[?customer_account=…]` lists them, and
  `GET /payments/{id}` shows a Payment's.

What a Customer owes in Recovery Receivable counts against them for Payouts: a Standard
Payout behind a returned buyer goes on waiting, and an Instant Payout whose buyer was
returned is back on its seller's limit, until the buyer funds what they owe.

The headline Scenario, `scenarios/late-r10-after-instant-payout.yaml`, is a Late R10 thirty
two days after the buyer's funds settled, when the seller's Instant Payout has long cleared
over RTP; it ends with the buyer's Customer Balance at 0, 300.00 in Recovery Receivable, one
return exception for 30000, Bank Cash down by the 270.00 paid out, the seller untouched,
every Clearing Account at zero, and an Inbox replay that reproduces the Ledger (tested).
`scenarios/duplicate-settlement-after-return.yaml` redelivers a settlement after its Return:
the Inbox recognises it and nothing changes.

## Scenarios and delivery Faults

A **Scenario** is a named, seeded YAML file of Customers, ACH pulls, Auction Closes and
**Faults** (SPEC §6, ADR 0011, ADR 0012); the committed ones are in `scenarios/`. Each pull has an amount in Minor Units and
an ending, `settle`, `decline` or `cancel` (a decline or cancellation happens when the pull
would have settled). Faults are a `kind` plus the Payment and Provider Event they hit:

```yaml
faults:
  - {id: dup, kind: duplicate, payment: avery-funding, event: submitted, times: 2}
  - {id: late, kind: delay, payment: cleo-top-up, event: settled, by: PT6H}
  - {id: swap, kind: out_of_order, payment: cleo-funding, event: submitted, after: settled}
  - {id: r10, kind: return, payment: cleo-funding, code: R10, at: P30D}
  - {id: again, kind: duplicate, payment: cleo-funding, event: settled, after: returned}
random_faults: {rate: 0.5, kinds: [duplicate, delay, out_of_order], max_delay: PT12H}
```

The Simulator injects them on its outbox, over the normal signed webhooks: a duplicate is
the same event ID and body delivered again, a delay holds the event back on the Clock, and an
out-of-order Fault holds an event until a later one of the same Payment has gone. An Auction Close lists its lots
and, per seller, a Payout's `speed` and how its RTP and ACH Payments end
(`scenarios/auction-close-payouts.yaml`); Faults can name a Payout like a Payment, and land
on whichever of its Payments emits the event. Random Faults are drawn from the seed before
the run (for pulls only); Payment and event IDs are derived from
Idempotency Keys, and the runner jumps the ManualClock from one due moment to the next, so a
Scenario and seed replay exactly (tested: two runs, one in a database already holding another
run, give the same manifest, ticks and Ledger; another seed gives other Faults). A webhook
that fails or times out (60 s) fails the run rather than being retried off course.

```sh
make dev
make scenario SCENARIO=scenarios/ach-pull-random-faults.yaml SEED=9
```

runs one Scenario in a throwaway database on that Postgres, with both services in-process on
one ManualClock, prints each Payment's states and Inbox outcomes, and writes the **Fault
Manifest** (every run does) to `reports/manifests/<scenario>-<seed>.json`: each Fault's id, kind, Payment,
event, parameters, when it was injected, a cause code and its true root cause. The Ledger
never reads it: it lives in the `simulator` schema, which the Ledger's role can't read, and
the `ledger` and `payments` packages can't import `railbench.scenarios` (both tested).

Under these Faults the Ledger stays correct: a stateful property test
(`tests/integration/test_ach_payment_properties.py`) delivers a Payment's events, Returns
included, in random orders, again, late and contradicting each other, and checks after every step that
Transactions balance, states only move forward, each event applies once, the Clearing
Account nets to zero, stale and impossible events write nothing, and balances match the
Payment's furthest state; another (`test_return_properties.py`) checks that a Return after
random spending leaves no negative Customer Balance, only the matching Recovery Receivable;
and replaying the Inbox of a run with every Fault kind into an
empty database, from that database alone (the availability job's runs are read from what it
recorded), reproduces the Ledger exactly, Recorded Times included.

### Layout

- `src/railbench/common`: settings, the Clock, the Banking Day calendar, webhook signing,
  JSON logging, the database Engine, `/health`
- `src/railbench/ledger`: the Ledger. `journal.py` posts and reads (the one way in),
  `model.py` holds the validated values, `app.py` is the thin HTTP layer
- `src/railbench/payments`: Payments, the Inbox and the ACH state machine, the Provider
  interface and its Simulator adapter; served by the Ledger API
- `src/railbench/marketplace`: Auction Closes, Payouts and the Payout release job; served by
  the Ledger API
- `src/railbench/simulator`: the Simulator, a separate service (ADR 0005), with ACH and RTP,
  scripted endings and delivery Faults
- `src/railbench/scenarios`: Scenario files, the runner and CLI, the Fault Manifest, the
  Inbox replay and the Ledger summary they're compared by
- `scenarios/`: the committed Scenario files
- `migrations/`: Alembic. The baseline creates the `ledger`, `payments` (Payments and the
  Inbox), `reconciliation` and `simulator` schemas; `0002` adds the Ledger tables, triggers
  and the `railbench_app` role; `0003` adds Transaction status, Settlements and Holds;
  `0004` adds Payments, the Inbox and state changes; `0005` the Simulator's state; `0006`
  its plans, delivery Faults and the Fault Manifest; `0007` pushes and RTP, and the
  `marketplace` schema; `0008` Returns and return exceptions.

Nothing reads the system time directly: code takes a `Clock` (`WallClock` by default,
`ManualClock` in tests and Scenarios), and a ruff rule bans `datetime.now()` and friends
everywhere else.
