# AI usage observatory

A local-first, metadata-only prototype for tracking coding-agent usage across machines and harnesses. It imports Codex and Claude Code session logs, records token categories and usage-meter snapshots in SQLite, and exports portable NDJSON for ingestion elsewhere. It can estimate API-equivalent usage value from a versioned price file and record completed work and subscription cycles. It does not generate traffic to exhaust a subscription.

Start with the [setup and command guide](OBSERVATORY_README.md), then see the [design](OBSERVATORY_DESIGN.md) and [source-model analysis](REVERSE_ENGINEERING.md).

The original third-party article and its figures are intentionally not included in this repository. The analysis names those local source files so its evidence can be checked by someone who has access to them.
