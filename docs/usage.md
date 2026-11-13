# Usage observatory prototype

`livermore` (also installed as `observatory`) is a metadata-only first slice of the [design](design.md). It reads local Codex and Claude Code session JSONL, can receive Claude Code's OpenTelemetry events live, stores token counts, request timings, and Codex meter snapshots in SQLite, and can export a portable NDJSON file for ingestion on another machine. It does not call model APIs, exhaust a subscription, or persist prompt/response text.

## Quick start

Install the package as shown in the root README, then run these commands from the repository root. Use a stable, unique alias for each machine, and use the same account alias on every machine sharing one subscription.

```sh
livermore --db ./observatory.sqlite3 scan \
  --machine-id macbook --account-alias personal
livermore --db ./observatory.sqlite3 report --month 2026-10
livermore --db ./observatory.sqlite3 export --out ./macbook-usage.ndjson
```

Copy the NDJSON to the machine that owns the central database, then:

```sh
livermore --db ./central.sqlite3 ingest --input ./macbook-usage.ndjson
livermore --db ./central.sqlite3 report --month 2026-10
```

Repeated scans and repeated imports update the same event IDs. `scan` skips files whose size and modification time have not changed since the last scan; pass `--full` to re-read everything (for example after changing `--account-alias`). Reports accept `--format json`. The NDJSON contains model names, usage counts, session IDs, meter percentages, and optional task labels, but no conversation or file contents. Treat it as private data. Export currently writes a complete snapshot (schema version 2; `ingest` still accepts version 1 files).

## Live collection with OpenTelemetry (Claude Code)

Session logs are the ledger: they cover past sessions and Codex's plan meters. Claude Code's OpenTelemetry events add what the logs lack (request latency, time to first token, API errors and retries, and Claude Code's own cost estimate) and let other machines send usage live instead of exporting files.

Start the receiver on the machine that owns the central database:

```sh
livermore --db ./central.sqlite3 serve
```

It listens on `127.0.0.1:4318` and accepts OTLP/HTTP **JSON** logs at `/v1/logs`. Then, on each machine, add this to the `env` block of `~/.claude/settings.json` (repository-level settings cannot enable telemetry):

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
    "OTEL_RESOURCE_ATTRIBUTES": "livermore.machine_id=macbook,livermore.account_alias=personal"
  }
}
```

Use the same machine and account aliases you pass to `scan`. If a sender cannot set them, start `serve` with `--machine-id` and `--account-alias` defaults; records with neither are rejected and reported back to the sender. Leave `OTEL_METRICS_EXPORTER` unset: the receiver acknowledges and discards metrics and traces, and Claude Code 2.0 rejects the value `none`. The receiver only accepts JSON; it answers `415` to protobuf.

**No double counting.** Claude Code 2.1 includes the provider's `request_id` on each `api_request` event, and its transcripts record the same ID as `requestId`. Usage is keyed on that ID, so a request seen by both `serve` and `scan` is stored once (its `provenance` becomes `otel,transcript`), even if the two paths used different machine aliases. Older Claude Code versions (2.0.x) send no `request_id`; for those, `serve` keeps the timing but records no usage, and logs a one-time warning, because it could not be matched to the transcript.

**Content is dropped.** The receiver reads a fixed list of numeric and identifier attributes from `api_request` and `api_error` events and ignores every other event and attribute. Prompt text, assistant responses, tool inputs, raw API bodies, and identity attributes such as `user.email` are never written, even if a sender turns on `OTEL_LOG_USER_PROMPTS` or similar. API error messages are truncated to 200 characters.

**Across a network.** Bind beyond localhost only with a token:

```sh
LIVERMORE_TOKEN=change-me livermore --db ./central.sqlite3 serve --host 0.0.0.0
```

and add `"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer change-me"` to each sender. The receiver speaks plain HTTP; put it behind TLS (a reverse proxy, Tailscale, or an SSH tunnel) on untrusted networks.

`report` shows request counts, p50/p95 latency, and API errors per model when OTel data exists.

## Docker

The image runs the receiver with its database in a `/data` volume, and can also run any other command:

```sh
docker compose up -d
MACHINE_ID=desktop ACCOUNT_ALIAS=personal docker compose run --rm scan
docker compose run --rm receiver report --month 2026-11
```

The `scan` service mounts `~/.codex` and `~/.claude` read-only. The compose file publishes the receiver on localhost only; see its comments for accepting other machines. The image uses Python 3.14, so compressed Codex sessions are read without extra packages. The database path can also be set with `LIVERMORE_DB`.

Record the fee and exact billing dates once in the central database. A cycle report combines every machine tagged with the same account alias:

```sh
livermore --db ./central.sqlite3 cycle \
  --source codex --account-alias personal --plan plus \
  --cycle-start 2026-10-01 --cycle-end 2026-11-01 --fee-usd 20
livermore --db ./central.sqlite3 cycle-report \
  --source codex --account-alias personal --cycle-start 2026-10-01 \
  --prices ./pricebooks/prices.2026-10-08.json
```

The replacement-cost/fee ratio is shown only when all usage records in the cycle have prices. The cycle report also shows the peak and latest observed meter percentage. It does not infer an account's true unused allowance from a meter snapshot.

## Pricebook

API replacement cost appears only for events whose model and date have a matching entry. Copy [prices.example.json](../pricebooks/prices.example.json) to `prices.json` in the repository root, add rates from a dated provider price source, then run:

```sh
livermore --db ./central.sqlite3 report --month 2026-10 --prices ./prices.json
```

Rates are USD per million tokens. `effective_to` is exclusive. `source` is `codex` or `claude`. A nonzero token category without a known rate leaves the whole event unpriced, and the report shows coverage. For cache writes whose duration is unknown, set `cache_write_unknown_per_mtok` only if you have a defensible rate; otherwise leave it absent. Prices are intentionally not built into the program because they change and some requests use special pricing modes.

For a concrete example, [prices.2026-10-08.json](../pricebooks/prices.2026-10-08.json) contains the official GPT-6 Sol **standard, short-context** rates observed on October 8, 2026. It does not cover long-context requests, other processing tiers, tool fees, or other models. Treat its dollar output as an estimate for sessions that fit that price scope.

## Task outcome notes

You can attach a lightweight outcome to a session without recording its content:

```sh
livermore --db ./observatory.sqlite3 note fix-auth-timeout \
  --machine-id macbook --source codex --session-id SESSION_ID \
  --category bugfix --outcome accepted --elapsed-minutes 42 --interventions 2
```

Outcomes are `accepted`, `reworked`, `abandoned`, or `blocked`. The first report counts them. Cost allocation to individual tasks is not yet implemented because a session can contain multiple tasks; it needs explicit task boundaries before such a metric is trustworthy.

## Current limits

- Codex's local session-file schema is not a stable public API. The importer was checked against local files (plain and `.jsonl.zst`) that contain `token_usage_record` and `token_count` events, and may need adapters for older or future versions.
- The Claude importer and the OTel receiver were checked against real Claude Code 2.1.321 sessions: the transcript `requestId` matched the OTel `request_id`, and token counts agreed.
- Codex OpenTelemetry is not ingested yet. Its documented example exporter uses protobuf, and its documented events do not show a response ID to match transcripts; keep using `scan` for Codex.
- Calendar-month and billing-cycle reports are available. Months and cycle dates are compared against UTC timestamps. The prototype does not yet calculate reset-adjusted headroom, model-specific limits, or OTel traces.
- The prototype does not collect quality or latency automatically. Task outcome notes are the first step toward those analyses.
