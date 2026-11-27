# The Simulator sits behind the same Provider interface a real provider would use

The Ledger talks to the Simulator only through a Provider interface: an HTTP API to create Payments, signed webhooks delivering Provider Events, and daily Settlement Reports. The Simulator is a separate service, not an in-process fake. That keeps the tests honest (duplicates, out-of-order delivery and retries happen over real HTTP) and means adding a real provider is a new adapter, not a Ledger change. The cost is a second service to run in tests and demos.
