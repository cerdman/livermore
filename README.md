# Livermore

A local-first AI usage observatory. It reads Codex and Claude Code session logs, can also receive Claude Code's OpenTelemetry events, stores usage metadata in SQLite, exports portable NDJSON, and reports token usage, latency, and estimated API replacement cost. It does not store prompt or response text.

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
