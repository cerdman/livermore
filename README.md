# Livermore

A local-first AI usage observatory. It reads Codex and Claude Code session logs, can also receive Claude Code's OpenTelemetry events, stores usage metadata in SQLite, exports portable NDJSON, and reports token usage, latency, and estimated API replacement cost. It does not store prompt or response text.

## What it's good for

You pay a flat monthly fee for Codex or Claude Code, and the logs that would tell you whether it's worth it are spread across several machines. Livermore collects them so you can answer questions like these:

- **Is my subscription paying for itself?** Record a billing cycle and its fee, and `cycle-report` prices every token you used at dated API rates. If $20 a month bought $140 of API usage, you'll see that ratio. If half the usage has no matching price, the report says so instead of guessing.
- **Which models eat the budget?** Monthly reports total requests, tokens and replacement cost for each tool and model. Input, output, cache-read and cache-write tokens are stored and priced separately, so cheap cache reads aren't billed at full input rates.
- **Is it me, or is it slow today?** With the OpenTelemetry receiver running, every Claude Code request records time to first token, total duration and errors. Reports show p50 and p95 latency, so "it feels sluggish" becomes a number you can compare month to month.
- **How much do I use across all my machines?** Each machine exports a small NDJSON file of metadata, and one central database ingests them all. The same request counts once whether it arrived through a transcript, live telemetry or a re-import.
- **Did the agent actually help?** Tag a session with `note` to record whether it was accepted, reworked, abandoned or blocked, plus how long it took and how many times you stepped in. That's your own record of results, kept next to what the session cost.

**Doesn't OpenTelemetry already do this?** Partly, and that's why Livermore can receive it. But telemetry only covers what happens while a collector is listening, and it doesn't cover Codex's local history. Session logs fill in everything from before you turned telemetry on. Telemetry adds what the logs don't carry: latency, errors and the provider's own cost figures. Livermore merges both sources into one record per request.

**Private by default.** It stores counts, timestamps, model names and IDs, never prompt or response text. Scanned file paths are stored only as hashes. The receiver listens on localhost unless you tell it otherwise. Exports are safe to copy between machines because there is no conversation text in them to leak.

## Start here

The project uses Python 3.10 or newer and has no required runtime dependencies. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run this from the repository root:

```sh
uv tool install ".[zstd]"
observatory --help
```

The `zstd` extra lets Python 3.10–3.13 read the compressed `.jsonl.zst` files Codex keeps for older sessions; Python 3.14 reads them without it. Without either, `scan` reports how many it skipped. If your shell cannot find `observatory`, run `uv tool update-shell` and open a new terminal.

See the [usage guide](docs/usage.md) for scan, OpenTelemetry, Docker, export, ingest, billing-cycle, and pricing examples. The [design](docs/design.md) explains the data model, and the [source-model analysis](docs/reverse-engineering.md) documents the research behind it.

## Project layout

```text
src/livermore/observatory.py  CLI, importers, reports, and data model
src/livermore/otel.py         OTLP/HTTP JSON receiver (`observatory serve`)
tests/                       Tests
docs/                        Usage, design, and source analysis
pricebooks/                  Example and dated price data
```

The original third-party article and figures are kept locally in `references/` and excluded from Git. The analysis identifies those source files so someone with access can check its evidence.

## Test

```sh
uv run python -m unittest discover -s tests -v
uvx ruff check src tests
```

CI runs both on Linux, macOS, and Windows with Python 3.10 and 3.14, and builds the Docker image.

## License

MIT. See [LICENSE](LICENSE).
