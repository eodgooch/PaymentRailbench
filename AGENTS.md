# AGENTS.md

PaymentRailbench: a double-entry ledger for Mintwharf, a fictional collectibles marketplace, tested against a payment-rail simulator that injects the failures real sandboxes don't. The build spec is `SPEC.md`. Use the vocabulary in `GLOSSARY.md`; respect the decisions in `docs/adr/`.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `eodgooch/PaymentRailbench`, through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), each label string matching its role. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Ground rules

- AI code paths never write to the Ledger (ADR 0001). Don't add a write tool to the MCP server or give the triage agent a write-capable role.
- No real company's name, branding or fees in product code or demo data. Mintwharf is fictional.
- Never commit secrets. The Anthropic API key lives only in env and GitHub secrets.
- Every commit is GPG-signed (key `ABEC574D0BAE9E68`, `eodgooch@gmail.com`) so GitHub shows it as Verified. Never pass `--no-gpg-sign`.
