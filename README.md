# Livermore

A local-first AI usage observatory. It reads Codex and Claude Code session logs, stores usage metadata in SQLite, exports portable NDJSON, and reports token usage and estimated API replacement cost. It does not store prompt or response text.

## Start here

The project uses Python 3.8 or newer and has no runtime dependencies. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run this from the repository root:

```sh
uv tool install .
observatory --help
```

If your shell cannot find `observatory`, run `uv tool update-shell` and open a new terminal.

See the [usage guide](docs/usage.md) for scan, export, ingest, billing-cycle, and pricing examples. The [design](docs/design.md) explains the data model, and the [source-model analysis](docs/reverse-engineering.md) documents the research behind it.

## Project layout

```text
src/livermore/observatory.py  CLI and data model
tests/                       Tests
docs/                        Usage, design, and source analysis
pricebooks/                  Example and dated price data
```

The original third-party article and figures are kept locally in `references/` and excluded from Git. The analysis identifies those source files so someone with access can check its evidence.

## Test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
