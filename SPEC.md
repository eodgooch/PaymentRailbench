# PaymentRailbench: Build Spec

> **For coding agents:** Build in the milestones in §11, in order. At the end of each milestone, all checks pass, the README is updated, and you stop for review. Use the vocabulary in `GLOSSARY.md` and respect `docs/adr/`. Never give an AI code path write access to the Ledger (ADR 0001). Never commit secrets. When a real-world fact in this spec (NACHA return windows, rail limits, model names and prices, library APIs) disagrees with current official sources, follow the sources and say what changed.

---

## 1. Purpose

A focused portfolio project for ledger and payments engineering roles. It is a double-entry Ledger for **Mintwharf**, a fictional collectibles marketplace (buy, sell, consign, vault and borrow against trading cards). The Ledger runs against a payment-rail **Simulator** that produces the failures real sandboxes don't. It delivers five artifacts:

1. **A rail Simulator** that injects late returns, duplicate callbacks, partial reversals (Provider Adjustments), webhooks that arrive twice and out of order, and settlement after the window closed. The Ledger is run against it.
2. **An overnight triage agent** that works the reconciliation Exception Queue and produces a ranked list with a Proposed Cause for each Break cluster, plus a measured accuracy.
3. **An MCP server over the Ledger**, so "where did this discrepancy come from?" is a plain-language question.
4. **Adversarial tests** for the Payment, Loan and Cash Advance state machines (e.g. "the repayment that lands mid-default", "the return that hits after the payout cleared").
5. **A one-page essay**, "What not to hand to a model in financial code", where each claim links to the code that enforces it.

**Restraint is a requirement.** Postgres, Python and one small web page. No queues, no microservices beyond the Simulator, no Kubernetes. When in doubt, pick the simpler design and note the trade-off.

### Hard rules
- **Fictional only.** Mintwharf and every Customer are invented. No real company's name, branding or fee schedule appears in code, data or UI. The README may say which job postings inspired the project.
- **AI is read-only** on the Ledger (ADR 0001).
- **Money is integers.** Every amount is an integer in a currency's Minor Units. No floats anywhere money flows (ADR 0003).
- **History is never edited.** Corrections are Reversals (ADR 0006 explains why there's no hash chain).

---

## 2. Stack

| Layer | Choice |
|---|---|
| Language | **Python 3.12**, `uv`, `ruff`, `mypy --strict` |
| Ledger API and Simulator | **FastAPI**, **SQLAlchemy 2.x** (Core is fine for the Ledger), **Alembic**, **Pydantic v2** |
| Database | **PostgreSQL 16**. One database, separate schemas for the Ledger, the Inbox/payments, reconciliation and the Simulator |
| MCP server | Python, the official MCP SDK |
| Triage agent | Claude via the Anthropic API; its only tools are the MCP server's. Check current models and prices before choosing (§7.3) |
| Activity page | **React + TypeScript** (Vite), one page, read-only |
| Tests | `pytest`, `hypothesis` (including stateful tests), `testcontainers` |
| Local | Docker Compose + `Makefile` |
| CI and demo | GitHub Actions; a scheduled workflow publishes the nightly report to GitHub Pages (§9) |

---

## 3. Ledger

### 3.1 Accounts
Each Account has a code, a type (asset, liability, revenue, expense) and **one currency**.

- **Customer Balance** (liability), one per Customer per currency. All of a Customer's money flows through it.
- **Clearing Account** (asset), one per Provider × Rail × currency. Money the Provider reported but hasn't settled.
- **Bank Cash** (asset), per currency.
- **Fee Revenue** (revenue).
- **Loans Receivable**, **Advances Receivable**, **Recovery Receivable** (assets).
- **Interest Income**, **Recovery Income** (revenue); **Credit Losses** (expense).
- **Conversion** accounts for USD ↔ USDC (ADR 0003).

### 3.2 Transactions and Entries
- A Transaction is a set of Entries that **sum to zero per currency**. This is enforced in code and by a deferred constraint trigger.
- Each Entry has an amount (signed integer Minor Units), an Account, an **Effective Time**, a **Recorded Time**, and an **Actor**. The Actor is one of: system job (name + version), Provider Event (Inbox ID), or person (operator ID + approved Proposal ID). Actor is NOT NULL.
- Each Transaction has an Idempotency Key (unique). Re-posting the same key with the same payload returns the original; with a different payload it fails with a conflict.
- **Append-only:** the app role has `SELECT` and `INSERT` only on Ledger tables. A trigger rejects `UPDATE`/`DELETE` from every role outside migrations.
- **Reversal:** a new Transaction that negates every Entry of the original and references it. A Transaction can be reversed once.

### 3.3 Balances and Holds
- **Posted Balance**, **Pending Balance** and **Available Balance** = posted − Holds − unsettled outflows. Balances are always derived, never stored as editable columns. A rebuildable cache is fine if a test checks it against the derivation.
- **Hold:** placed, released or captured. It reduces Available Balance without moving money.
- **Bitemporal queries (ADR 0002):** `balance(account, effective_at, known_at)`. `known_at` defaults to now.

### 3.4 Daily Close
`close-day <date>` locks a Banking Day once Reconciliation for it has no unresolved Breaks above a threshold, or has an explicit override recorded with an Actor. After that, no Entry may have an Effective Time inside that day. Corrections take effect on the next open day and record `relates_to_day`.

---

## 4. Payments, Inbox and state machines

### 4.1 Payment
One movement of money on one Rail through one Provider. States:
- **ACH:** `created → submitted → settled → available`, plus `returned` (from settled or available) and `canceled` / `declined` before settlement.
- **RTP:** `created → settled` (instant, final, credit-push only; no pulls, no returns), plus `declined`.
- **Wire** (milestone 4): `created → submitted → settled` (final).
- **Stablecoin** (milestone 4): `created → broadcast → confirmed (N confirmations) → final`, plus `reorged` (back to broadcast) and `failed`.

### 4.2 Inbox
- Every inbound Provider Event is verified (signature), stored **raw** in the Inbox and deduplicated by Provider event ID before anything else happens.
- Applying an event moves the Payment's state machine **forward only**. An event for a state already passed is recorded and does nothing. An **impossible** transition (e.g. `returned` for a Payment never settled) raises an exception record, never a crash and never a Ledger write.
- Replaying the Inbox from the beginning must reproduce the Ledger exactly. This is tested.

### 4.3 ACH availability and returns (ADR 0004)
- Pending at origination, posted at settlement, Available 2 Banking Days later.
- Returns carry a NACHA return code. The Simulator knows each code's Return Window (verify current NACHA rules when building; at minimum R01, R02, R03 within 2 Banking Days and R10 within 60 calendar days).
- **Late Return:** posted as a Reversal of the funding. If the Customer Balance goes negative, the deficit moves to Recovery Receivable, and an exception is raised with the dollar exposure.

### 4.4 Payouts
- **Standard Payout:** released when the funds behind it are Available.
- **Instant Payout:** sent over RTP immediately at Auction Close, at Mintwharf's risk, within a per-Customer risk limit (config). Exceeding the limit falls back to a Standard Payout.

### 4.5 Provider Adjustment
After settlement, a Provider claws back part of a settled amount (fee correction, partial recall of a Payout batch). It's booked against the original Payment. If no matching expectation exists, Reconciliation flags it.

---

## 5. Lending and Cash Advances

### 5.1 Loan
- States: `approved → disbursed → current ⇄ delinquent → defaulted → charged_off`, and `repaid → collateral_released`.
- **Delinquent** at 1+ days past due; **Defaulted** at 90+ days past due.
- **Payment Waterfall:** fees, then interest, then principal.
- A payment while Defaulted reduces what's owed but **doesn't cure** the default unless it brings the Loan fully current.
- A payment after Charge-off is **Recovery Income**.
- **Accrual:** fixed annual rate, one accrual Entry per Loan per Banking Day, posted by a daily job. Rounding rule documented and tested.
- Collateral can't be released while any amount is owed.

### 5.2 Cash Advance
- A percentage (config) of a Consignment's stated value (an input, not a model), paid to the seller upfront.
- Recovered from sale proceeds at Auction Close. A **Shortfall** moves to Recovery Receivable.
- If the seller withdraws the Consignment, the advance is due immediately.
- An advance can be reversed if its funding Payment fails.

---

## 6. Simulator

A separate FastAPI service (ADR 0005) that plays every Provider.

- **API:** create Payment (with an idempotency key), get Payment.
- **Webhooks:** signed Provider Events with event IDs, delivered with retries and backoff.
- **Settlement Reports:** one per Provider per Banking Day, as CSV, listing what settled (and Provider Adjustments).
- **Clock:** a controllable clock shared by the Simulator and the Ledger in tests and demos. It has a **Banking Day calendar** (weekends and Fed holidays) and per-Provider **Cutoff** times.
- **Scenarios:** YAML files describing Payments and Faults. Every run is deterministic from a seed.
- **Faults:** duplicate event, out-of-order delivery, delayed delivery, Late Return (any code, any time inside its window), settlement after Cutoff (lands on the next Banking Day in the report), Provider Adjustment, missing report line, report line with a wrong amount, and (milestone 4) stablecoin reorg.
- **Fault manifest:** every run writes a manifest of injected Faults with their true root cause. The triage evaluation scores against it (§7.4). The Ledger and the agent never read it.

---

## 7. Reconciliation, MCP server and triage agent

### 7.1 Reconciliation
- **Two-way:** the Ledger (the Payments behind each Clearing Account) against each Provider's Settlement Report, per Banking Day. Three-way with a bank statement is a stretch goal.
- Deterministic matching rules only. Every disagreement becomes a **Break** with one of the **Break Types**:
  - missing in Ledger
  - missing at Provider
  - amount mismatch
  - timing mismatch
  - duplicate
  - status mismatch
- Each Break stores its evidence (Ledger rows, report lines, Inbox events), dollar exposure and age.
- Open Breaks form the **Exception Queue**. Resolving a Break requires an Actor and either a linked correcting Transaction or a reason.

### 7.2 MCP server (read-only, ADR 0001)
It connects with a `SELECT`-only role. The fixed set of tools (no free-form SQL):
- `trace_payment(payment_id)`: Inbox events → state changes → Entries, in order
- `explain_balance(account, effective_at, known_at)`: the balance and the Entries that make it up
- `diff_balance(account, t1, t2)`: what changed and why
- `list_breaks(filters)` / `explain_break(break_id)`: evidence and related Payments
- `find_entries_by_actor(actor)`
- `scenario_summary(run_id)`: the Night at a glance

Tool outputs never include raw bank account numbers. Customers appear by ID and fictional name.

### 7.3 Triage agent
- It runs after Reconciliation and uses **only** the MCP server's tools.
- It clusters related Breaks and ranks them by **dollar exposure, then age**. Each cluster gets a **Proposed Cause** (more specific than the Break Type, e.g. "settlement after Cutoff on the eve of a Fed holiday") and a typed **Proposal** chosen from a fixed list of correcting actions, with a confidence level.
- Output: a Markdown report plus structured JSON stored in the database.
- `approve <proposal_id>` (CLI) is the only way a Proposal takes effect. Deterministic code builds and posts the Transaction, with the approver as Actor.
- **Cost caps:** a hard cap per Night (default $1) and per eval run (default $10), enforced from token counts before each call. When the cap is reached, the run stops and says so in the report.
- Use the current recommended Claude model, checked against Anthropic's docs when building. Record the model ID and cost in every report.

### 7.4 Evaluation
`make eval N=20` runs N seeded Nights with varied Faults and scores the agent against each fault manifest. It reports:
- cause accuracy (top-1)
- confidently-wrong rate (high confidence, wrong cause)
- Proposals that would have been correct
- cost per Night and model ID

The table goes in the README and the nightly report.

---

## 8. Activity page

One read-only React/TypeScript page that reads straight off the Ledger API:
- a Customer's running balance (Posted, Pending, Available)
- which Payment method funded what
- a full reconciliation of a single Auction Close for a Customer who both bought and sold
- a point-in-time toggle: "as known then" vs. "as known now"

Keep it plain, accessible and phone-friendly. No design system.

---

## 9. The Night and the published report

- `make night [SEED=…]` runs, on simulated time: an Auction Close with ~300 Payouts across Rails (a mix of Standard and Instant), buyer ACH pulls, a Loan repayment and a Cash Advance settlement, the seeded Faults, the following Banking Days until the Return Windows that matter have passed, Reconciliation, and (if an API key is set) the triage agent.
- Output: a static HTML report covering the Night summary, Ledger checks (balanced, Clearing Accounts net to zero, invariants), the Exception Queue, the agent's ranked list, and the eval table.
- A scheduled GitHub Action runs a Night every night and publishes the report to **GitHub Pages**. The agent step runs only when the `ANTHROPIC_API_KEY` secret is set. Cost caps apply.
- The README links the latest report and a short recording of asking the MCP server "where did this discrepancy come from?".

---

## 10. Adversarial tests

- **Stateful property tests** (`hypothesis.stateful`) drive the Payment, Loan and Cash Advance state machines with random event sequences, checking these invariants after every step:
  - every Transaction balances per currency
  - Clearing Accounts net to zero once everything they hold has settled
  - a negative Customer Balance always has a matching Recovery Receivable
  - replaying the Inbox reproduces the Ledger
  - the Loan state matches its days-past-due and the Payment Waterfall
  - nothing takes effect inside a closed day
- **Named adversarial Scenarios**, each a committed Scenario file with an expected outcome:
  - repayment that lands mid-default
  - partial payment after Charge-off
  - Late Return after an Instant Payout cleared
  - duplicate settlement event after a Reversal
  - Cutoff on the eve of a Fed holiday
  - Provider Adjustment on a closed day
  - consignment withdrawn after a Cash Advance
- **Model-generated cases:** generated offline with Claude, reviewed by a person, committed as Scenario files with the prompt that produced them recorded in the file. They're never generated during a test run.
- Coverage: ≥ 90% on the Ledger, the state machines and Reconciliation.

---

## 11. Milestones

Each milestone ends with all checks green, the README updated, and a stop for review.

**M1: v1 for outreach (target ~2 weeks)**
- Repo skeleton, Compose, Makefile, CI (lint, types, tests), pre-commit with `gitleaks`.
- The Ledger (§3), including bitemporal balances, Holds, Reversals, idempotency, append-only grants and trigger, and Daily Close.
- Simulator with **ACH and RTP**, clock and calendar, webhooks, Settlement Reports, Scenarios and the fault manifest.
- Inbox and Payment state machines; ACH availability; Late Returns; Standard and Instant Payouts.
- Two-way Reconciliation, Break Types and the Exception Queue.
- **Thin MCP server** with `trace_payment`, `explain_balance` and `explain_break`.
- `make night` with the headline Scenario (Late Return after an Instant Payout), and the HTML report.
- The nightly GitHub Pages workflow (§9), since its report is the outreach link. The agent step joins in M2.
- ✅ Invariant property tests pass; the headline Scenario ends with the right Recovery Receivable and an exception; the MCP server answers "where did this discrepancy come from?" for that Break.

**M2: Triage agent and evaluation**
- The full MCP tool set, the triage agent with cost caps, Proposals with CLI approval, `make eval`, and the agent step added to the nightly workflow.
- ✅ The eval table is published; the agent never writes (a test proves its role can't).

**M3: Lending and Cash Advances**
- §5 in full, with stateful tests and the named adversarial Scenarios.
- ✅ All named Scenarios pass; Accrual rounding is documented and tested.

**M4: More rails**
- Wire and stablecoin (USDC as its own currency, confirmations, reorg Fault), and conversion Transactions.
- ✅ A Night with all four Rails reconciles; a reorg Scenario is handled without a manual correcting Entry.

**M5: Activity page, essay and README**
- §8, the essay in `docs/` (each claim linked to the enforcing code), and the final README.
- ✅ The activity page shows one Auction Close reconciled for a buyer-and-seller Customer and toggles between "as known then" and "as known now".

---

## 12. README (final)

1. What this is, in one paragraph, and which job postings inspired it
2. The latest nightly report and the MCP recording
3. The Ledger model: accounts, bitemporal balances, Daily Close, Reversals (with the ADRs)
4. The Simulator and its Faults
5. Reconciliation and the Exception Queue
6. The triage agent and its eval table
7. Lending and Cash Advances, with the adversarial Scenarios
8. "What not to hand to a model in financial code" (link)
9. Build vs. buy (ADR 0007)
10. Running it locally in three commands

---

## 13. Configuration

```
DATABASE_URL=postgresql+psycopg://railbench:railbench@localhost:5432/railbench
LEDGER_READONLY_DATABASE_URL=     # SELECT-only role, used by the MCP server and agent
SIMULATOR_URL=http://localhost:8100
WEBHOOK_SIGNING_SECRET=change-me
ANTHROPIC_API_KEY=                # optional; agent steps skip when unset
TRIAGE_MODEL=                     # set from current Anthropic docs
TRIAGE_COST_CAP_NIGHT_USD=1
TRIAGE_COST_CAP_EVAL_USD=10
INSTANT_PAYOUT_LIMIT_CENTS=500000
CASH_ADVANCE_BPS=6000
LOAN_APR_BPS=1200
```
