# PaymentRailbench

A double-entry ledger for Mintwharf, a fictional collectibles marketplace, tested against a simulated payment provider that produces the failures real sandboxes don't.

## Marketplace

**Mintwharf**:
The fictional marketplace whose money flows the ledger records. Customers buy and sell cards at auction, consign them, take cash advances and borrow against them.

**Customer**:
A person who buys, sells or borrows on Mintwharf. One Customer can be a buyer and a seller in the same auction.
_Avoid_: User, account holder

**Auction Close**:
The moment an auction ends and the money owed between buyers, sellers and Mintwharf becomes due, producing a batch of Payouts.

**Lot**:
One card sold at an auction, to one buyer, for its Hammer Price.
_Avoid_: Item, listing

**Hammer Price**:
The price a Lot sells for at the Auction Close, before Mintwharf's fee.

## Ledger

**Ledger**:
Mintwharf's system of record for money: append-only, double-entry, and the only source of every Balance.
_Avoid_: Database, books (informally fine)

**Account**:
A named bucket in the Ledger that Entries post to, with a type: asset, liability, revenue or expense.
_Avoid_: Wallet, user account

**Customer Balance**:
The liability Account holding what Mintwharf owes one Customer. All of a Customer's money flows through it.
_Avoid_: Wallet

**Clearing Account**:
An asset Account per Provider and Rail holding money in flight: reported by the Provider but not yet settled. It nets to zero once everything settles.
_Avoid_: Suspense account, in-transit

**Bank Cash**:
The asset Account for settled money in Mintwharf's bank.

**Recovery Receivable**:
The asset Account for money a Customer owes Mintwharf after a Late Return or a Shortfall left their Customer Balance negative.

**Transaction**:
One business event recorded as a set of Entries that sum to zero in each currency.
_Avoid_: Payment (a Payment is a different thing)

**Entry**:
One signed amount, in integer minor units of one currency, posted to one Account within a Transaction, stamped with its Actor.
_Avoid_: Line item, posting (as a noun)

**Actor**:
Who or what created an Entry: a system job, a Provider Event, or a person approving a Proposal.

**Reversal**:
A Transaction that exactly negates an earlier one. The only way to correct the Ledger.
_Avoid_: Void, undo, delete

**Settlement**:
The record that a pending Transaction's money has stopped moving. From its Effective Time, the Transaction's Entries count toward the Posted Balance instead of the Pending Balance. No Entry changes.
_Avoid_: Clearing (that's the Clearing Account, a different thing)

**Hold**:
A reservation that reduces an Account's Available Balance without moving money, later released (no money moves) or captured (a Transaction moves it).
_Avoid_: Freeze, lock

**Posted Balance**:
The sum of an Account's settled Entries.

**Pending Balance**:
The sum of an Account's Entries for money in motion that hasn't settled.

**Available Balance**:
What a Customer can use now: the Posted Balance minus Holds and minus unsettled outflows.
_Avoid_: Balance (on its own, without saying which)

**Effective Time**:
When the event behind an Entry happened economically.
_Avoid_: Value date (fine in conversation), transaction date

**Recorded Time**:
When the Ledger learned of an Entry. A balance "as known on Tuesday" counts only Entries recorded by then.
_Avoid_: Created at (ambiguous)

**Daily Close**:
Locking a Banking Day once it's reconciled. Nothing new can take effect inside a closed day; later corrections take effect on the next open day and name the closed day they relate to.
_Avoid_: End of day, EOD

## Lending and advances

**Loan**:
Money lent to a Customer against a vaulted card as Collateral, repaid with interest over time.
_Avoid_: Credit line, advance

**Collateral**:
The card a Loan is secured by. It can't leave the vault until the Loan is repaid and the Collateral is released.

**Loan State**:
Where a Loan is in its life: approved, disbursed, current, delinquent, defaulted, charged off, or repaid.

**Delinquent**:
A Loan with any payment past due.

**Defaulted**:
A Loan 90 or more days past due. A payment reduces what's owed but doesn't cure the default unless it brings the Loan fully current.

**Charge-off**:
Writing a Defaulted Loan's remaining balance off as a loss. Money received afterwards is Recovery Income, not repayment.
_Avoid_: Write-off (same thing; pick one)

**Recovery Income**:
Money collected on a Loan after Charge-off.

**Payment Waterfall**:
The order a Loan payment is applied in: fees first, then interest, then principal.
_Avoid_: Allocation (on its own)

**Accrual**:
Interest earned on a Loan for one Banking Day, posted daily.

**Consignment**:
A card a seller hands Mintwharf to sell at auction.

**Cash Advance**:
Money paid to a seller upfront, as a percentage of a Consignment's stated value, recovered from the sale proceeds at Auction Close. Due immediately if the seller withdraws the Consignment.
_Avoid_: Loan (a Loan is a separate product)

**Shortfall**:
The amount by which sale proceeds fall short of a Cash Advance. It moves to Recovery Receivable.

## Payments and rails

**Rail**:
A payment network with its own timing, finality and failure modes: ACH, RTP, wire or stablecoin.
_Avoid_: Method, channel

**Provider**:
An outside company that moves money on one or more Rails and reports back by Provider Event and Settlement Report. In this project, the Simulator plays every Provider.
_Avoid_: PSP (fine in conversation), gateway, processor

**Payment**:
One movement of money on one Rail through one Provider, with its own state machine.
_Avoid_: Transfer, transaction

**Payout**:
What Mintwharf owes a seller from one Auction Close, and the sending of it to the seller. It goes out as a Payment; an Instant Payout declined on its instant Rail goes out again as a Standard Payout, by a second Payment.
_Avoid_: Disbursement (that's a Loan's), withdrawal

**Standard Payout**:
A Payout released only once the funds behind it are Available.

**Instant Payout**:
A Payout sent over an instant Rail before the funds behind it are Available, at Mintwharf's risk and within the Customer's risk limit.

**Provider Event**:
A message from a Provider about a Payment (for example submitted, settled, returned), stored raw in the Inbox before being applied.
_Avoid_: Webhook (that's the delivery mechanism, not the thing)

**Inbox**:
The raw, deduplicated record of every Provider Event as received.

**Return**:
A Provider Event saying a settled ACH Payment has been sent back, with a NACHA return code (for example R01, R10).
_Avoid_: Bounce, chargeback (chargebacks are a card concept)

**Late Return**:
A Return that arrives after the funds were already Available, so Mintwharf may have paid them out.

**Return Window**:
How long after settlement a Return can still arrive. It depends on the return code.

**Provider Adjustment**:
A Provider clawing back part of an already settled amount after the fact (for example a fee correction or a partial recall of a Payout batch).
_Avoid_: Partial return (ACH Returns are all-or-nothing)

**Cutoff**:
The time of day after which a Provider counts a Payment toward the next banking day.

**Banking Day**:
A day the Federal Reserve is open. Weekends and Fed holidays aren't Banking Days.

## Reconciliation

**Settlement Report**:
A Provider's daily statement of what it settled, which the Ledger is reconciled against.
_Avoid_: Bank statement (that's a different, v2 source)

**Reconciliation**:
Matching the Ledger against a Settlement Report and recording every disagreement as a Break.

**Break**:
A disagreement found by Reconciliation, with a Break Type, its evidence and its age.
_Avoid_: Discrepancy (fine in prose), mismatch (on its own)

**Break Type**:
The kind of disagreement: missing in Ledger, missing at Provider, amount mismatch, timing mismatch, duplicate, or status mismatch.

**Exception Queue**:
The list of open Breaks waiting for a person to resolve.

**Proposed Cause**:
The triage agent's explanation of why a Break happened, more specific than its Break Type.

**Proposal**:
A correcting action suggested for a Break, which only takes effect when a person approves it and deterministic code posts it.
_Avoid_: Fix, auto-correction

## Simulation

**Simulator**:
The stand-in Provider that moves money on every Rail and injects Faults.

**Fault**:
A deliberate failure the Simulator injects, such as a duplicate Provider Event, out-of-order delivery, a Late Return, settlement after Cutoff, or a Provider Adjustment.

**Scenario**:
A named, seeded script of Payments and Faults that replays exactly the same way every run.

**Fault Manifest**:
The record a Scenario run writes of every Fault it injected, with its true root cause. The triage evaluation scores against it; the Ledger never reads it.

**Night**:
A Scenario that runs an Auction Close with its Payouts and Faults, then Reconciliation, producing the Exception Queue.

## Currency

**Minor Unit**:
The smallest unit of a currency, in which every amount is stored as an integer (cents for USD, millionths for USDC).

**USDC**:
A stablecoin treated as its own currency, never as USD. Moving between them always takes an explicit conversion Transaction.
