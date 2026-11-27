# AI is read-only on the Ledger

The MCP server and the triage agent connect with a database role that has `SELECT` only, and the MCP server offers a fixed set of read-only tools with no free-form SQL. The agent can cluster Breaks, rank them and propose a cause and a correcting action, but a Proposal only takes effect when a person approves it and deterministic code posts it. Models are good at explaining and ranking and bad at being accountable for money; putting the boundary in database grants means a prompt injection or a confused agent can't move a cent, whatever the prompt says.
