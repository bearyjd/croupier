# Croupier

Multi-strategy trading orchestrator: policy engine + audit trail for an AI
agent trading a Robinhood **Agentic Account** (official MCP server).
Strategies live as "sleeves" (see `croupier/sleeves/`); every order passes a
gate pipeline — restricted-list denylist, sleeve budgets, position caps,
limit-only, venue, data-health, confirm-by-default — and is audited
append-only *before* approval.

AGPL-3.0 · Grepon Labs

- `docs/prp/PRP-001` — architecture + invariants
- `docs/prp/PRP-002` — broker topology (execution venue, data health)
- `croupier/executor/AGENT.md` — the agent's operating contract
- `config/policy.example.yaml` — enforced limits (YAML wins over sleeve prose)
- `croupier/sleeves/README.md` — the sleeve format

Croupier is a **policy engine, not a trading agent**. It holds no broker
credentials and places nothing. The agent connects to its broker itself and
is bound to pass every proposed order through `croupier check` first, placing
only what comes back approved and recording the `approval_id`.

A sleeve's *signal source* is deliberately outside Croupier. One sleeve's
source might be a human reading filings; another's might be a separate
service emitting intents on a schedule. Croupier does not care which — it
requires only that an order carry citable `signal_refs` and pass every gate.
Keeping signal generation out is what lets the engine stay two dependencies
deep and auditable line by line.

## Compliance-by-design
All signals must be public and cited (`signal_refs` is mandatory at the type
level — an intent without provenance cannot be constructed). The denylist
and the append-only audit log exist so an operator can prove, after the
fact, that every trade derived from public information. Populate the
denylist with counsel before the first live trade.

**Live operating configuration is not tracked here.** Sleeve documents
(`croupier/sleeves/*.md`) and the live `config/policy.yaml` /
`config/catalysts.yaml` are gitignored; the repository ships only
`.example.yaml` templates with fictional tickers. A real watchlist, sizing,
or restricted list belongs in a private repository — PRP-001 asks for the
denylist to be *versioned*, which is not the same as public.

## Install

Croupier is a normal editable Python package. Use a virtualenv — most
distro Pythons are marked externally-managed and will refuse a bare
`pip install`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # drop [dev] for runtime only

cp config/policy.example.yaml    config/policy.yaml
cp config/catalysts.example.yaml config/catalysts.yaml
```

`config/policy.yaml` and `config/catalysts.yaml` are **gitignored**; only the
`.example.yaml` files are tracked. Your real budgets, restricted list, and
catalyst windows describe your position — they should not land in a public
repository. `croupier` tells you to run the `cp` above if the file is
missing rather than failing with a bare file-not-found.

Requires Python ≥ 3.12. Runtime deps: `pyyaml`, `httpx`. Dev adds `pytest`
and `ruff`.

## Operator surface

Croupier holds **no broker credentials**. The agent connects to Robinhood
MCP itself and is contractually bound (`croupier/executor/AGENT.md`) to pipe
every proposed order through `croupier check` first, placing only orders
that come back `approved: true` and recording the `approval_id`.

### `croupier check` — gate an order

Reads one order intent as JSON on stdin, writes the verdict as JSON on
stdout. Exit code `0` = approved, `1` = rejected.

```bash
croupier check < examples/order.json
```

```json
{
  "approved": true,
  "approval_id": "8e5dfcc22109ac76",
  "requires_confirm": true,
  "decisions": [
    { "gate": "denylist",     "passed": true, "reason": "not on restricted-list denylist" },
    { "gate": "venue",        "passed": true, "reason": "execution venue robinhood" },
    { "gate": "sleeve_budget","passed": true, "reason": "$2,800 of $10,000 ceiling" },
    { "gate": "position_cap", "passed": true, "reason": "within $3,000 cap" },
    { "gate": "order_type",   "passed": true, "reason": "LIMIT ONLY + thin-liquidity: agent must not convert to market order" },
    { "gate": "data_health",  "passed": true, "reason": "fresh data" },
    { "gate": "mode",         "passed": true, "reason": "CONFIRM: human approval required before placement" }
  ]
}
```

`examples/order.json` is a complete, working payload — copy it as the shape
for real intents:

| Field | Meaning |
|---|---|
| `sleeve` | must exist in `config/policy.yaml` |
| `ticker`, `side`, `qty`, `limit_price` | the order; `side` is `buy`/`sell` |
| `signal_refs` | **mandatory, non-empty** — public refs only (filing IDs, 8-K accession numbers, PR URLs). PRP-001 invariant 2 rejects an intent without them at construction time. |
| `thesis` | one-line rationale, recorded in the audit log |
| `is_etf` | lets an `etf_only` denylist entry pass |
| `venue` | must equal `execution_venue` in policy (`robinhood`) |
| `adv_shares` | average daily volume, for the thin-liquidity flag |
| `data_health` | `fresh` \| `degraded` \| `dead` (from `croupier auth-status`) |
| `auto_spent_today` | AUTO-mode daily spend so far; ignored in CONFIRM |
| `account` | Robinhood MCP snapshot: `total_value`, `cash`, `sleeve_cost_basis`, `position_cost_basis` |

Every check — approved or not — appends a record to `data/audit.jsonl`
and fsyncs it *before* the verdict is returned. No log write, no approval.

### `croupier fill` — report a fill

```bash
echo '{"approval_id":"8e5dfcc22109ac76","ticker":"ACME",
       "side":"buy","qty":1000,"price":2.79}' | croupier fill
```

The fill is joined back to its approval in the audit log: that lookup
supplies the sleeve and proves the position came from an intent the gates
cleared. A fill whose `approval_id` is unknown — or whose ticker/side
contradicts the approval — is still written to the audit log (an ungated
trade is exactly what the trail is for) but refused entry to the ledger, and
the command exits `1`.

### `croupier mark` — daily mark-to-market + drawdown halt

Marks every open position through the data router, advances each sleeve's
equity curve, and halts any sleeve that has drawn down past the ceiling.

```bash
croupier mark
```

Exit `0` normally, `1` when a **new** halt was written — so a cron wrapper
can push on the exit code alone.

Sleeve drawdown is measured on a *time-weighted* return index, not on raw
market value. Buys and sells are external cash flows into the sleeve's book:
raw market value peaks whenever capital is deployed and collapses whenever a
winner is sold, so a naive high-water mark would halt the sleeve for taking
a profit. The index removes flows, which is what "drawdown in this sleeve"
actually means. A position the router cannot quote is carried at cost rather
than dropped to zero, so a data outage cannot manufacture a halt.

State lives in `data/ledger.db` (SQLite):

- `fills` — keyed by `approval_id`. Re-reporting an identical fill is a
  no-op; re-reporting a *different* fill under the same approval_id raises
  rather than overwriting.
- `equity_points` — one row per sleeve per day: market value, net flow,
  TWR index, high-water mark, drawdown.

### Halts

When a sleeve's drawdown exceeds `max_sleeve_drawdown_pct` (25%, from the sleeve's
guardrails), `croupier mark` writes it into
`data/sleeve_state.yaml` and the policy loader merges that **over**
`config/policy.yaml`.

The merge is one-way by construction: state may halt a sleeve that
policy.yaml leaves trading, and can never un-halt, loosen a mode, or invent
a sleeve. A state file claiming `mode: confirm` or `mode: auto` is ignored on
load. Prose cannot loosen code (PRP-001), and neither can runtime state.

> **Operator note:** per `croupier/executor/AGENT.md`, HALTED means *no
> orders, including sells*. A drawdown halt therefore also freezes the
> sleeve's own `-40% → EXIT` rule until a human intervenes. That is the
> documented contract, not an oversight — clearing a halt is a deliberate
> act: review, then delete the sleeve's entry from `data/sleeve_state.yaml`.

### `croupier journal` — the daily operator report

```bash
croupier journal
```

Renders the day and writes the same text to `data/journal/YYYY-MM-DD.md`:
data-feed health (with the PRP-002 DEGRADED/DEAD banner), Schwab re-auth
status, each sleeve's cost basis against its ceiling and its open positions,
orders approved but still waiting on a human `Y`, active halts, and catalyst
freezes.

Sleeve ceilings are percentages of account value and Croupier holds no
broker credentials, so account value comes from the most recent snapshot an
agent supplied with a `check`. The journal always prints how old that
snapshot is rather than implying a live number, and says `unknown` when no
check has ever run.

Exit `0` when there is nothing to act on; `1` when there are pending
confirms, active halts, or a re-auth nag — so a cron wrapper can push on the
exit code alone.

#### Cron + ntfy (homelab Core/.20 pattern)

`croupier journal` is a read-only render, so it is safe to run unattended;
`croupier mark` writes the ledger and may halt a sleeve, so run it first and
let the journal report the result.

```cron
# m  h  dom mon dow   command
  10 17  *   *  1-5   cd /srv/croupier && .venv/bin/croupier mark  >> data/cron.log 2>&1
  15 17  *   *  1-5   cd /srv/croupier && /srv/croupier/bin/journal-push.sh
  0  9   *   *  *     cd /srv/croupier && .venv/bin/croupier auth-status | \
                        /srv/croupier/bin/nag-push.sh
```

`bin/journal-push.sh` — push only when there is something to act on:

```bash
#!/usr/bin/env bash
# Pushes the daily journal to ntfy when croupier journal exits non-zero.
set -uo pipefail
cd /srv/croupier

NTFY_HOST="${NTFY_HOST:?set to your ntfy base URL}"   # e.g. http://ntfy.lan:8080
NTFY_TOPIC="croupier"

out=$(.venv/bin/croupier journal); rc=$?
[ "$rc" -eq 0 ] && exit 0                # nothing pending, stay quiet

curl -fsS \
  -H "Title: Croupier — action needed $(date +%F)" \
  -H "Priority: high" \
  -H "Tags: chart_with_upwards_trend" \
  -d "$out" \
  "$NTFY_HOST/$NTFY_TOPIC" > /dev/null
```

A new drawdown halt makes `croupier mark` exit `1` as well, so the same
wrapper shape works for it. `NTFY_HOST` and `NTFY_TOPIC` are yours to set —
keep the host out of the repo and export it from the cron environment or a
gitignored env file, so the journal's contents and your endpoint never end
up in version control together.

### Sector map

The sector denylist can only act on tickers it can place in a sector, so the
bulk map lives in a CSV rather than inline YAML:

```yaml
ticker_sector_csv: data/seed/ticker_sector.csv   # header row, then ticker,sector
ticker_sector:
  ACME: defense                                  # inline entries win over the CSV
```

Inline entries outrank the file on purpose — a hand-curated mapping is a
deliberate act and should beat a generated one. A missing or malformed CSV is
logged and treated as empty rather than raised: it degrades the *sector*
denylist to whatever is listed inline and leaves the per-ticker denylist and
every other gate untouched.

The same CSV is generated and consumed by the Filature sister project, so a
wrong mapping is fixed in one place.

### Catalyst freezes

`config/catalysts.yaml` lists binary event windows: ticker, event type,
window start/end, and a **public** source URL. From T-5 trading days before
a window opens through the end of it, a **buy** in that ticker requires a
human `Y` regardless of its sleeve's mode — the sleeve's catalyst
calendar protocol, enforced in code.

The freeze is an escalation, not a rejection:

- it can only ever *raise* `requires_confirm`, never lower it — a CONFIRM
  sleeve stays CONFIRM;
- it never gates **sells**. An exit into a catalyst does not need the
  calendar's permission;
- it does not rescue an order some other gate rejected.

Active freezes appear in `croupier journal`, and the `catalyst_freeze` gate
decision is recorded on every check whether or not a window is open.

Trading days are counted as **weekdays**; market holidays are not modelled,
so a holiday inside the run-up shortens the window by a day. Widen
`window_start` for events where that matters.

The freeze boundary is evaluated against the **UTC** date, not US market
time. Near a boundary an evening ET order can be UTC-tomorrow: an order the
night before a window opens is frozen a few hours early, and one late on the
window's last day is still frozen. Both directions err toward asking a
human, which is the safe side.

> **Seeded, not verified.** The shipped entries are transcribed from the
> Tier 1 watchlist in `croupier/sleeves/event_driven.md`, which states
> windows only as prose ("Mono data Fall 2026"). The dates are the
> conservative calendar reading of that prose, not dates read off a filing,
> so every entry carries `verified: false` and the journal marks it
> UNVERIFIED. Confirm each against its public source and flip the flag
> before relying on it. An unverified entry still freezes — erring toward
> asking a human is the safe direction.

### `croupier auth-status` — Schwab data-feed health

```bash
croupier auth-status
# {"schwab_reauth_in_days": 2.31, "expires_at": "...", "nag": false}
```

Exit `1` when tokens are missing or expired. Schwab refresh tokens
hard-expire every 7 days (PRP-002); a lapse degrades the feed to Stooq EOD
rather than stopping exits.

## Development

```bash
ruff check .     # must be clean
pytest -q        # must be green
```

Both run in CI (`.github/workflows/ci.yml`) on Python 3.12–3.14 for every
push to `main` and every PR. Conventions: branch-per-feature
(`feat/<slug>`), conventional commits, AGPL-3.0 headers on new source
files, and green lint + tests before any merge to `main`.

`data/` is gitignored in full — it holds the audit trail, journals, ledger,
and broker tokens, none of which belong in version control.
