# PRP-002: Broker Topology — Casino Floor / Vault Split

**Status:** Draft
**Depends on:** PRP-001 (gate pipeline, audit, AGENT.md contract)

## Decision

- **Robinhood Agentic Account = execution venue.** Funded only with sleeve
  capital. All orders flow through Robinhood MCP, gated by Croupier. The
  account's own budget + per-trade notifications are the outer containment.
- **Schwab = vault + data.** Main portfolio; no automation touches it.
  Optionally, a Schwab Developer app registered with **Market Data product
  only** (no Accounts & Trading product) supplies quotes/streams. Read-only
  by construction: the app cannot place orders even if the agent tried.

## Invariants

1. **Single execution venue.** OrderIntents carry `venue`; a venue gate
   rejects any order not addressed to the configured execution broker.
   Schwab is never a valid order venue in this topology.
2. **Data health gates automation.** Schwab refresh tokens hard-expire
   every 7 days (manual browser re-auth). If the data feed goes stale:
   - AUTO sleeves are treated as HALTED for *entries* (no new buys on
     stale data).
   - Exit rules still run, using the Stooq EOD fallback, flagged DEGRADED
     in the audit log. A missed weekly re-auth must never silently disable
     exits over a catalyst weekend — degraded exits beat no exits.
   - CONFIRM-mode orders surface the data-health status in the report so
     the human decides with eyes open.
3. **The floor is observed, not assumed.** Stooq EOD (free, no auth) is the
   fallback beneath the Schwab feed, but its availability is a fact about a
   third party and must be measured rather than asserted.

   This invariant originally read "Fallback is always available… the system
   can always mark positions daily". On 2026-08-29 that stopped being true:
   Stooq began refusing plain HTTP clients, answering with HTML rather than
   CSV — a 404 to a bare client, or a 200 carrying a JavaScript proof-of-work
   page to a browser-shaped one. Neither is an error, so every quote returned
   None while the adapter still reported DEGRADED.

   That combination is worse than an outage, because DEGRADED and DEAD mean
   different things here. DEGRADED permits exits on EOD prices; DEAD permits
   nothing without explicit human instruction. A source that can price nothing
   while reporting DEGRADED invites exits against prices that do not exist,
   carries every position at cost so the drawdown halt cannot fire, and prints
   a reassuring banner in the journal while doing it.

   `StooqMarketData.health()` therefore reports what it has observed: DEGRADED
   until a fetch fails, DEAD thereafter, DEGRADED again once one succeeds. A
   well-formed but empty series is not a refusal — the source answered.

   **Consequence for planning:** there is currently no working free EOD floor
   for this host or for Core/.20, both of which Stooq refuses. Choosing a
   replacement is an amendment to PRP-001 invariant 4 ("free sources only",
   which admits several alternatives) and a deployment decision if the
   replacement needs an API key.

## Components

- `croupier/data/base.py` — MarketData protocol + Quote type + health enum
- `croupier/data/schwab.py` — Market Data-only adapter; token lifecycle
  with explicit staleness reporting (never raises into trading paths)
- `croupier/data/stooq.py` — EOD fallback (shared lineage with Filature)
- `croupier/data/router.py` — health-aware routing: FRESH schwab ->
  DEGRADED stooq -> DEAD (no quotes; everything halts except human orders)
- venue gate added to the PRP-001 pipeline
- `croupier auth-status` CLI: days until Schwab token expiry, nag output
  suitable for a cron -> ntfy push (homelab pattern)

## Re-auth ergonomics (accepted weekly cost)

Weekly Schwab re-auth is a 60-second browser task. Mitigations: cron nag at
T-24h via ntfy; auth-status in the daily journal; and the DEGRADED path
above so a missed week is an annoyance, not a silent failure.
