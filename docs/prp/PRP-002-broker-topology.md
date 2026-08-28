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
3. **Fallback is always available.** Stooq EOD (free, no auth) is the
   floor: the system can always mark positions daily even with zero broker
   data connectivity.

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
