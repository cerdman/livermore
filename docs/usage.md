# Usage observatory prototype

`observatory` is a metadata-only first slice of the [design](design.md). It reads local Codex and Claude Code session JSONL, stores token counts and Codex meter snapshots in SQLite, and can export a portable NDJSON file for ingestion on another machine. It does not call model APIs, exhaust a subscription, or persist prompt/response text.

## Quick start

Install the package as shown in the root README, then run these commands from the repository root. Use a stable, unique alias for each machine, and use the same account alias on every machine sharing one subscription.

```sh
observatory --db ./observatory.sqlite3 scan \
  --machine-id macbook --account-alias personal
observatory --db ./observatory.sqlite3 report --month 2026-10
observatory --db ./observatory.sqlite3 export --out ./macbook-usage.ndjson
```

Copy the NDJSON to the machine that owns the central database, then:

```sh
observatory --db ./central.sqlite3 ingest --input ./macbook-usage.ndjson
observatory --db ./central.sqlite3 report --month 2026-10
```

Repeated scans and repeated imports update the same event IDs. The NDJSON contains model names, usage counts, session IDs, meter percentages, and optional task labels, but no conversation or file contents. Treat it as private data. Export currently writes a complete snapshot; later versions can add incremental cursors and encrypted transport.

Record the fee and exact billing dates once in the central database. A cycle report combines every machine tagged with the same account alias:

```sh
observatory --db ./central.sqlite3 cycle \
  --source codex --account-alias personal --plan plus \
  --cycle-start 2026-10-01 --cycle-end 2026-11-01 --fee-usd 20
observatory --db ./central.sqlite3 cycle-report \
  --source codex --account-alias personal --cycle-start 2026-10-01 \
  --prices ./pricebooks/prices.2026-10-08.json
```

The replacement-cost/fee ratio is shown only when all usage records in the cycle have prices. The cycle report also shows the peak and latest observed meter percentage. It does not infer an account's true unused allowance from a meter snapshot.

## Pricebook

API replacement cost appears only for events whose model and date have a matching entry. Copy [prices.example.json](../pricebooks/prices.example.json) to `prices.json` in the repository root, add rates from a dated provider price source, then run:

```sh
observatory --db ./central.sqlite3 report --month 2026-10 --prices ./prices.json
```

Rates are USD per million tokens. `effective_to` is exclusive. `source` is `codex` or `claude`. A nonzero token category without a known rate leaves the whole event unpriced, and the report shows coverage. For cache writes whose duration is unknown, set `cache_write_unknown_per_mtok` only if you have a defensible rate; otherwise leave it absent. Prices are intentionally not built into the program because they change and some requests use special pricing modes.

For a concrete example, [prices.2026-10-08.json](../pricebooks/prices.2026-10-08.json) contains the official GPT-6 Sol **standard, short-context** rates observed on October 8, 2026. It does not cover long-context requests, other processing tiers, tool fees, or other models. Treat its dollar output as an estimate for sessions that fit that price scope.

## Task outcome notes

You can attach a lightweight outcome to a session without recording its content:

```sh
observatory --db ./observatory.sqlite3 note fix-auth-timeout \
  --machine-id macbook --source codex --session-id SESSION_ID \
  --category bugfix --outcome accepted --elapsed-minutes 42 --interventions 2
```

Outcomes are `accepted`, `reworked`, `abandoned`, or `blocked`. The first report counts them. Cost allocation to individual tasks is not yet implemented because a session can contain multiple tasks; it needs explicit task boundaries before such a metric is trustworthy.

## Current limits

- Codex's local session-file schema is not a stable public API. The importer was checked against a local file that contains `token_usage_record` and `token_count` events, and may need adapters for older or future versions.
- The available Claude transcript on this machine contains only synthetic usage. Its importer handles the observed field shape, but needs validation against a real Claude Code session.
- Calendar-month and billing-cycle reports are available. The prototype does not yet calculate reset-adjusted headroom, model-specific limits, or OTel traces.
- The prototype does not collect quality or latency automatically. Task outcome notes are the first step toward those analyses.
