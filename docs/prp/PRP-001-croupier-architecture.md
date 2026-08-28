# PRP-001: Croupier — Multi-Strategy Trading Orchestrator

**Status:** Draft
**License:** AGPL-3.0 · Grepon Labs
**Relationship:** Filature (congressional-disclosure signals) becomes a
strategy sleeve. See `croupier/sleeves/README.md` for the sleeve format.

## Problem

Multiple trading strategies ("sleeves") need shared execution against a
Robinhood Agentic Account (official MCP server) with hard, code-enforced
policy: budgets, position caps, confirm-gating, a restricted-list denylist, and
a complete audit trail proving every trade derived from public signals.

## Role split (the core design decision)

Croupier is a **policy engine**, not the agent. The trading agent (Claude
Code session, scheduled agent, etc.) connects to the Robinhood MCP server
directly, but is instructed to pass every proposed order through
`croupier check` first and to place ONLY orders that return APPROVED, with
the returned `approval_id` recorded. Croupier never holds Robinhood
credentials; the Agentic Account's own budget + per-trade notifications are
the outer containment layer, Croupier's gates are the inner one.

```
strategy sleeves -> OrderIntent -> GATE PIPELINE -> APPROVED/REJECTED (+audit)
                                        |
   [denylist] [sleeve budget] [position cap] [order type] [confirm mode]
                                        |
                    agent places approved orders via Robinhood MCP
                    fills reported back -> `croupier fill` -> ledger
```

## Invariants (never override, mirror the sleeve doc's HARD LIMITS)

1. **Every intent is audited** — inputs, gate decisions, and rationale are
   appended to an immutable log BEFORE the approval is returned. No log
   write, no approval.
2. **Public-signal provenance** — every OrderIntent must reference a
   `signal_ref` (filing ID, press release URL, 8-K accession number, etc.).
   Intents without provenance are rejected by the pipeline, categorically.
3. **Restricted-list denylist** — config-driven list of tickers/sectors marked
   NO_TRADE or ETF_ONLY. Deny rules win over every other gate. The denylist
   file is versioned in git so its history is itself auditable.
4. **Sleeve budgets are ceilings, not targets** — enforced at cost basis
   against the account snapshot the agent supplies with each check.
5. **Confirm-by-default** — every sleeve starts in CONFIRM mode (human
   approves each order). AUTO mode is per-sleeve, requires an explicit
   config change, and is capped: AUTO orders ≤ $X per order, ≤ $Y per day
   (config).
6. **Limit orders only** below liquidity thresholds (typical sleeve rule:
   never market orders under $10/share or ADV < 1M).

## Sleeve format

Each sleeve = one markdown doc (see `croupier/sleeves/README.md`) + one
YAML config entry (budget, mode, caps) + optionally a signal module (as in
Filature/PRP-002). The doc is the agent's operating instructions; the YAML
is what Croupier enforces. Where doc and YAML disagree, YAML wins — prose
cannot loosen code. Live sleeve docs are gitignored.

## Compliance-by-design (non-negotiable)

Croupier assumes an operator who must be able to prove, after the fact, that
every trade derived from public information and that restricted holdings were
never touched. All signals are public and cited (invariant 2), the denylist
covers any sector or ticker the operator is restricted from trading
(invariant 3), and the audit log exports as-is for compliance review.

Populate the denylist with counsel before the first live trade, and revisit
it whenever those restrictions change. Note that invariant 3 asks for the
denylist to be versioned so its history is auditable — "versioned" does not
mean "public". If your restricted list would itself disclose something, keep
`config/policy.yaml` in a private repository.

## Phases

- **P1:** gate pipeline + audit log + denylist + `croupier check`/`fill`
  CLI + first sleeve in CONFIRM mode, paper account.
- **P2:** Filature sleeve integration (its intents flow through the same
  pipeline); daily reconciliation of agent-reported fills vs. account
  snapshot; drawdown halt (25% sleeve drawdown -> freeze).
- **P3:** AUTO mode for at most one proven sleeve, small caps, after ethics
  review. Separate PRP.
