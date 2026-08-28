# Sleeves

A sleeve is one strategy: a Markdown operating document read by the agent,
plus one entry in `config/policy.yaml` that Croupier actually enforces.
Where the document and the YAML disagree, **the YAML wins** — prose cannot
loosen code (PRP-001).

Live sleeve documents are **not tracked in this repository**. They contain
an operator's watchlists, sizing, and theses, which are nobody else's
business; `.gitignore` excludes `croupier/sleeves/*.md` apart from this
file. Keep yours locally, or in a private repository alongside your real
`config/policy.yaml`.

## What a sleeve document contains

A sleeve document is free-form, but the pipeline expects it to establish:

- **Hard limits** — sleeve budget, per-position cap, correlated-cluster cap,
  loss tolerance. These must be mirrored into `config/policy.yaml`, which is
  what the gates read.
- **Entry and exit rules** — including the conditions under which the agent
  proposes an order at all.
- **A catalyst protocol** — dated binary events belong in
  `config/catalysts.yaml`, where the freeze gate can act on them.
- **A reporting format** — how a proposed order is presented to the human
  for confirmation.
- **Guardrails** — notably a drawdown ceiling, enforced by `croupier mark`
  via `max_sleeve_drawdown_pct`.

## Adding one

1. Write the document here (it will be gitignored).
2. Add a matching sleeve entry to `config/policy.yaml`. It starts in
   `confirm` mode: every order needs a human `Y`.
3. Add any dated binary events to `config/catalysts.yaml`.
