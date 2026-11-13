# A personal AI usage observatory

## The question worth answering

Measure what happens during **real work**, without generating artificial requests to consume a subscription. The useful output is a time series of usage, equivalent API cost, remaining subscription headroom, and work outcomes. The headline figure should be called **API replacement cost**, not “value”: it answers what the same measured tokens would have cost at list API rates. It does not say whether the work was useful or whether an API client would have used the same tokens.

The more interesting question is: **Which assistant gives me a satisfactory result with the least time, intervention, and scarce subscription capacity for the kinds of tasks I actually do?** That requires joining usage data to lightweight task outcomes. Without outcomes, token totals reward waste.

Some concrete questions worth testing once enough sessions exist:

| Question | Signal to collect | Decision it could change |
| --- | --- | --- |
| Is a higher plan tier worth buying? | Actual limit collisions, delayed tasks, and remaining headroom by billing cycle | Upgrade only if the extra capacity would have changed real work |
| Which model should handle each task type? | Accepted outcomes, elapsed time, interventions, and meter debit by task category | Route easy and hard tasks differently |
| What is the context/retry tax? | Growing input, cache hits, compactions, repeated attempts, and outcome | Start a fresh session or improve instructions when context becomes expensive |
| Did a provider quietly change limits? | Meter movement per normalized token mix across comparable sessions | Detect a policy shift without exhausting the plan |
| Is a subscription better than API use for *my* workflow? | Date-correct API replacement cost, fee, quality, and availability | Keep, downgrade, or cancel a plan |

## Three analyses to build, in order

1. **Usage and headroom.** By day, billing cycle, provider, harness, model, and project: fresh input, cache write, cache read, output, reasoning (where reported), API replacement cost, subscription fee, and the observed five-hour/weekly meter readings. Show the busiest days and how often a limit actually constrained work. An unused limit is optionality, not “lost value.”
2. **Cost per useful outcome.** Add a manual task label and completion result: accepted, reworked, abandoned, or blocked by limits. Record elapsed time, number of user interventions, tests/build results, and optional PR/commit linkage. Report API replacement cost and subscription-meter debit per accepted task, stratified by task type and model. This is the closest thing to personal value.
3. **Change detection.** Track rolling cache-hit rate, tokens per accepted task, output tokens per task, latency, failure/retry rate, and meter debit per normalized token mix. Annotate model, plan, API-price, and harness-version changes. Flag a limit change only when enough natural usage crosses meter increments, and keep uncertainty bands. A meter change, user-behavior change, and model mix change are different explanations.

## Capture strategy

### Across machines: event log, then database

Use a small **versioned NDJSON event format** as the handoff between collectors and analytics:

```text
Codex / Claude / future harness
        -> machine-local adapter
        -> metadata-only NDJSON export
        -> central idempotent ingest
        -> database + dated pricebook + reports
```

Give each machine a stable alias and tag each event with a subscription account alias. Each event carries `(source, machine_id, event_id)` so retransmitting a file or re-running an import cannot double-count it; reports can combine machines that share `(source, account_alias)`. The initial central store can be SQLite with a single ingestion process; Postgres is a natural later choice if multiple machines or users must write concurrently. The NDJSON is the durable interchange format, so a database migration does not require re-reading vendor transcripts. A local adapter should spool events when offline and send the same IDs later. The current prototype exports a complete snapshot; incremental export and authenticated transport can be added after the event semantics settle.

**OTel is useful, but optional.** (Implemented for Claude Code as `observatory serve`; see the [usage guide](usage.md#live-collection-with-opentelemetry-claude-code).) Both [Codex](https://learn.chatgpt.com/docs/config-file/config-advanced) and [Claude Code](https://code.claude.com/docs/en/monitoring-usage) can export OpenTelemetry. It can add latency, retries, tool activity, and live cross-machine collection. It does not give a uniform, complete subscription-value ledger or task outcomes by itself. Normalize selected OTel events into the same NDJSON schema, and deduplicate them against passive transcript events by provider response ID. Keep raw OTel traces in a separate, short-retention store if you need them at all. Claude's documentation warns that prompt text can appear in `user_prompt` events, so filter content before export; Codex supports prompt redaction, and tool results also deserve review. The [OTel GenAI token conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/) are helpful mappings, but the canonical format should preserve source-specific details such as cache-write duration and meter windows.

### Passive first

Read local session records and save only structured metadata. Do not copy prompts, tool output, file contents, or assistant responses into the analytics database by default.

- **Codex:** On this machine, `~/.codex/sessions/.../*.jsonl` contains `token_usage_record` rows with a response ID, turn ID, session ID, and input/cached/cache-write/output/reasoning counts. `turn_context` records the model. `event_msg` `token_count` rows also contain five-hour and weekly percentage snapshots. This is an observed local format, not a guaranteed public storage API; an importer should be versioned, tolerant of missing fields, and retain raw files as the source of truth.
- **Claude Code:** The installed CLI has `--print --output-format stream-json`; the local project transcript includes `assistant.message.usage` with fresh input, cache creation, cache read, and output counts. The one transcript found here is synthetic, so a real session is needed to validate deduplication and final-usage semantics. [Anthropic's CLI reference](https://code.claude.com/docs/en/cli-usage) documents the structured print output.
- **Other harnesses:** Add adapters that map their structured events to the same schema. An adapter must declare which fields are observed, estimated, or unavailable. Do not invent cache or reasoning counts from visible text.

For controlled tasks later, optional wrappers can save `codex exec --json` or Claude Code's `stream-json` output with a task ID. [Official OpenAI documentation](https://developers.openai.com/blog/eval-skills) confirms `codex exec --json` emits structured JSONL events. Wrappers give cleaner task boundaries, but requiring them for all work would miss normal interactive use.

### Canonical event and task records

```text
usage_event:
  source, source_version, machine_id, account_alias, event_id,
  timestamp, session_id, turn_id,
  provider, harness, model, project_id,
  fresh_input_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
  cache_write_unknown_ttl_tokens, cache_read_tokens,
  output_tokens, reasoning_output_tokens, observed_fields, provenance

meter_snapshot:
  timestamp, provider, account_alias, plan, meter_name,
  used_percent, window_minutes, resets_at, provenance

task:
  task_id, started_at, completed_at, project_id, category,
  outcome, user_interventions, optional_commit_or_pr,
  optional_quality_note

price_version:
  provider, model, effective_from, effective_to,
  fresh_input_per_mtok, cache_write_5m_per_mtok,
  cache_write_1h_per_mtok,
  cache_read_per_mtok, output_per_mtok, source

subscription_cycle:
  account_alias, plan, fee, currency, cycle_start, cycle_end
```

Use SQLite locally. Upsert by provider event ID where possible, so rescanning a growing JSONL file is idempotent. Keep a source/version column because Codex and Claude counts have different cache semantics. Include an “unknown” value instead of zero when a field is absent. Local file paths can be reduced to hashes or project aliases in the analytics database.

## Calculations and interpretation

For each observed event, calculate `API replacement cost = Σ(tokens by type × the API rate effective at event time) / 1,000,000`. Reasoning output is already included in output tokens and should not be added twice. For Codex's local `token_usage_record`, cached and cache-write counts appear to be subsets of reported input, so fresh input is the remainder; validate against a native summary. For Claude, [Anthropic documents](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) fresh, cache-creation, and cache-read counts as separate fields and prices five-minute and one-hour cache writes differently. Keep them separate where the transcript reports the duration. If a pricing modifier is unknown, report an estimate with that limitation instead of false precision.

For each billing cycle, show:

- Measured API replacement cost and its coverage fraction (how many events had a known model and price).
- `replacement_cost / subscription_fee`, clearly labeled as a counterfactual ratio.
- Maximum and median meter pressure by window, percentage of days near a limit, and actual limit blocks.
- Number of accepted tasks, median time to completion, and replacement cost per accepted task.

The observed meter cannot usually identify a per-token debit from one natural session because readings are quantized. For change detection, fit meter movement across many sessions using billed token mixes, omit intervals that span resets, and report an interval rather than a point estimate. Compare similar task mixes and model versions. Do not call a rate “free” merely because the meter did not move.

## A practical first release

1. Build read-only Codex and Claude importers, a local SQLite store, and `report --month` output. First validate against a few sessions without persisting their content.
2. Add a versioned price file and subscription-cycle entries. This makes replacement cost reproducible even after list prices change.
3. Add task tagging with a tiny post-task prompt or CLI command: category, outcome, and whether you had to intervene. Make tagging optional; report untagged usage separately.
4. Add meter-history plots and change alerts only after the basic accounting is trustworthy.

The first release is successful if rerunning an import never double-counts events, token totals reconcile with native summaries for sampled sessions, price changes can be replayed by date, and the report makes missing coverage visible. No background daemon, provider credentials, or automated limit probing is necessary for that release.

## Decisions to make before the dashboard

The main product choice is which question gets the front page: **subscription economics**, **task efficiency**, or **change detection**. My recommendation is task efficiency, with economics and headroom as context. It best avoids treating high token consumption as high value. A first dashboard can still include all three, but the data model should make task outcomes first-class from day one.
