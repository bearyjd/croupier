# PRP-002: Broker Topology — Casino Floor / Vault Split

**Status:** Draft
**Depends on:** PRP-001 (gate pipeline, audit, AGENT.md contract)

## Decision

- **Robinhood Agentic Account = execution venue.** Funded only with sleeve
  capital. All orders flow through Robinhood MCP, gated by Croupier. The
  account's own budget + per-trade notifications are the outer containment.
- **Schwab = vault + data.** Main portfolio; no automation touches it.
  Considered and rejected as the EOD floor: one expired refresh token would
  take out FRESH and DEGRADED together, and a floor whose failures correlate
  with the primary's is not a floor (PRP-004).
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
   - Exit rules still run, using the EOD floor, flagged DEGRADED in the
     audit log. A missed weekly re-auth must never silently disable exits
     over a catalyst weekend — degraded exits beat no exits. This holds only
     while the floor is observed to be serving; see invariant 3.
   - CONFIRM-mode orders surface the data-health status in the report so
     the human decides with eyes open.
3. **The floor is observed, not assumed.** An EOD feed sits beneath the
   Schwab one, but its availability is a fact about a third party and must be
   measured rather than asserted. The account below is of Stooq, the original
   floor, and is kept because the reasoning outlived it.

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

   The floor adapter therefore reports what it has observed: DEGRADED until a
   fetch fails, DEAD thereafter, DEGRADED again once one succeeds. A
   well-formed but empty series is not a refusal — the source answered — and
   an unknown symbol is a fact about the ticker, not about the feed.

   **Resolved 2026-08-29 (PRP-004):** the floor is now Twelve Data, a free
   tier reached with an API key. Stooq is removed rather than kept as a
   secondary — it will not answer a client that declines its proof-of-work, so
   a second dead source would only be somewhere for a future reader to wire
   back to.

   The floor therefore has a credential for the first time, and can now be
   *absent* as well as refusing: with no `TWELVEDATA_API_KEY` there is no
   price source at all. `build_router` returns a router with no fallback and
   health() reports DEAD, because reporting DEGRADED over nothing would be the
   same lie in a new place.

## Components

- `croupier/data/base.py` — MarketData protocol + Quote type + health enum
- `croupier/data/schwab.py` — Market Data-only adapter; token lifecycle
  with explicit staleness reporting (never raises into trading paths)
- `croupier/data/twelvedata.py` — EOD floor (shared lineage with Filature);
  asks for a single bar per ticker, because marking needs the latest close and
  the tier is metered
- `croupier/data/router.py` — health-aware routing: FRESH schwab ->
  DEGRADED EOD -> DEAD (no quotes; everything halts except human orders).
  The fallback is optional: no floor configured is DEAD, not DEGRADED
- venue gate added to the PRP-001 pipeline
- `croupier auth-status` CLI: days until Schwab token expiry, nag output
  suitable for a cron -> ntfy push (homelab pattern)

**Known gap: `croupier journal` does not observe.** `cmd_journal` builds a
fresh `DataRouter` and calls `.health()` on it immediately, with no quote ever
requested — inherited unchanged from the original Stooq adapter, which had the
same shape. So the DEGRADED banner in the journal (the surface `AGENT.md`
tells the trading agent to read `data_health` from) means only "a router was
constructed with something in `TWELVEDATA_API_KEY`", not "this key has
recently answered". A revoked or exhausted key reports DEGRADED, never DEAD,
until something else in the same process happens to call `.quote()` first.
`cmd_mark` does not have this gap — `mark_to_market` calls `router.quote()`
for every position before reading `router.health()`, so that path is
genuinely observed. Fixing `journal` needs somewhere to persist the
last-observed health between processes (a JSON sidecar, a row in the ledger
DB — undecided) and is tracked as a follow-up rather than folded into this
component list.

## Re-auth ergonomics (accepted weekly cost)

Weekly Schwab re-auth is a 60-second browser task. Mitigations: cron nag at
T-24h via ntfy; auth-status in the daily journal; and the DEGRADED path
above so a missed week is an annoyance, not a silent failure.
