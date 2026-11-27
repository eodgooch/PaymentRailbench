# Build the core Ledger on Postgres; consider buying reconciliation and payment operations

We build the Ledger ourselves on Postgres instead of adopting TigerBeetle, Modern Treasury Ledgers, Formance, Blnk, Twisp or Fragment. Three requirements drove it. First, no product we checked fully supports bitemporal balances ("effective at T, as known at T′"). Modern Treasury keys balances on effective time only, and Formance stores both timestamps but doesn't document querying by both. Second, none makes an Actor on every Entry a required, enforced field. Third, lending states (Accrual, Charge-off, Cash Advance recovery) would end up in metadata. Building also keeps the data in Postgres on the team's own stack.

At a real marketplace the call would be split. Buy **Modern Treasury** if payment operations and reconciliation from one vendor matter more than lock-in. Self-host **Formance** (closest on time handling) or **Blnk** (simpler holds, built-in reconciliation) on RDS for open source on the same stack. Use **TigerBeetle** only if write throughput becomes the bottleneck. Auction-close bursts are small by its standards, and it's a separate database to operate. The pragmatic path is to build the core Ledger and buy at the edges.

## Considered Options

Comparison as of November 2026. ✓ = confirmed in the vendor's docs; (i) = inferred and should be re-checked before relying on it.

| | Hosting / license | Holds and states | Time model | Reconciliation |
|---|---|---|---|---|
| TigerBeetle | Own database, Apache-2.0 (i) | Pending transfers with post/void/timeout ✓ | Single cluster timestamp; no backdating ✓ | None |
| Modern Treasury Ledgers | SaaS, priced by quote ✓ | Pending, posted, available ✓ | Effective time; recorded time only on versions ✓ | Yes, in the wider suite ✓ |
| Formance | Open source, runs on Postgres ✓ | No native holds (i) | Two timestamps; querying by both not documented ✓ | Enterprise Edition ✓ |
| Blnk | Open source, Apache-2.0 ✓ | Inflight transactions ✓ | Historical balances ✓; effective date (i) | Rules-based matching ✓ |
| Twisp | SaaS, usage-priced ✓ | Settled, pending, encumbered ✓ | Point-in-time ✓; effective dating not documented | Exception journals ✓ |
| Fragment | SaaS, GraphQL | (i) | (i) | (i) |
