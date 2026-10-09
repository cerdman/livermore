#!/usr/bin/env python3
"""Metadata-only, local importer for Codex and Claude Code session usage."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    source TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    model TEXT,
    fresh_input INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    cache_write_unknown INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    output INTEGER NOT NULL,
    reasoning_output INTEGER,
    PRIMARY KEY (source, machine_id, event_id)
);
CREATE INDEX IF NOT EXISTS usage_events_time ON usage_events(occurred_at);
CREATE TABLE IF NOT EXISTS meter_snapshots (
    source TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    plan TEXT,
    meter_name TEXT NOT NULL,
    used_percent REAL NOT NULL,
    window_minutes INTEGER,
    resets_at INTEGER,
    PRIMARY KEY (source, machine_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS meter_snapshots_time ON meter_snapshots(occurred_at);
CREATE TABLE IF NOT EXISTS task_notes (
    machine_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source TEXT,
    session_id TEXT,
    category TEXT,
    outcome TEXT,
    elapsed_minutes REAL,
    interventions INTEGER,
    noted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (machine_id, task_id)
);
CREATE TABLE IF NOT EXISTS subscription_cycles (
    source TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    plan TEXT NOT NULL,
    cycle_start TEXT NOT NULL,
    cycle_end TEXT NOT NULL,
    fee_usd REAL NOT NULL,
    PRIMARY KEY (source, account_alias, cycle_start)
);
"""


def integer(value):
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def identity(*parts):
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def add_usage(db, row):
    db.execute(
        """INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,machine_id,event_id) DO UPDATE SET
          account_alias=excluded.account_alias,
          occurred_at=excluded.occurred_at,
          session_id=excluded.session_id,
          turn_id=excluded.turn_id,
          model=COALESCE(excluded.model,usage_events.model),
          fresh_input=excluded.fresh_input,
          cache_write_5m=excluded.cache_write_5m,
          cache_write_1h=excluded.cache_write_1h,
          cache_write_unknown=excluded.cache_write_unknown,
          cache_read=excluded.cache_read,
          output=excluded.output,
          reasoning_output=excluded.reasoning_output""",
        row,
    )


def read_jsonl(path, *, strict=False):
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                if strict:
                    raise ValueError(f"Invalid JSON at {path}:{line_number}") from error
                continue  # Session files can end with an in-progress line.
            if isinstance(record, dict):
                yield line_number, record


def import_codex(path, db, machine_id, account_alias="personal"):
    session_id = None
    model_by_turn = {}
    current_model = None
    usage_count = 0
    meter_count = 0
    for line_number, record in read_jsonl(path):
        kind = record.get("type")
        payload = record.get("payload") or {}
        timestamp = record.get("timestamp") or ""
        if kind == "session_meta":
            session_id = payload.get("session_id") or payload.get("id") or session_id
        elif kind == "turn_context":
            current_model = payload.get("model") or current_model
            turn_id = payload.get("turn_id")
            if turn_id and current_model:
                model_by_turn[turn_id] = current_model
        elif kind == "token_usage_record":
            usage = payload.get("usage") or {}
            required = {"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "output_tokens"}
            if not required.issubset(usage):
                continue
            total_input = integer(usage.get("input_tokens"))
            cached = integer(usage.get("cached_input_tokens"))
            written = integer(usage.get("cache_write_input_tokens"))
            if cached + written > total_input:
                continue  # Invalid accounting; do not silently price it.
            turn_id = payload.get("turn_id")
            event_id = payload.get("response_id") or identity(path, line_number)
            add_usage(db, (
                "codex", machine_id, account_alias, event_id, timestamp,
                payload.get("session_id") or session_id,
                turn_id, model_by_turn.get(turn_id) or current_model,
                total_input - cached - written, 0, 0, written, cached,
                integer(usage.get("output_tokens")),
                (integer(usage["reasoning_output_tokens"])
                 if "reasoning_output_tokens" in usage else None),
            ))
            usage_count += 1
        elif kind == "event_msg" and payload.get("type") == "token_count":
            limits = payload.get("rate_limits") or {}
            for name in ("primary", "secondary"):
                meter = limits.get(name) or {}
                used = meter.get("used_percent")
                if not isinstance(used, (int, float)):
                    continue
                db.execute(
                    """INSERT OR REPLACE INTO meter_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    ("codex", machine_id, account_alias,
                     identity(path, line_number, name), timestamp,
                     session_id, limits.get("plan_type"),
                     f"{limits.get('limit_id') or 'codex'}:{name}", float(used),
                     meter.get("window_minutes"), meter.get("resets_at")),
                )
                meter_count += 1
    return usage_count, meter_count


def import_claude(path, db, machine_id, account_alias="personal"):
    usage_count = 0
    for line_number, record in read_jsonl(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        required = {"input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"}
        if not required.issubset(usage) or message.get("model") == "<synthetic>":
            continue
        session_id = record.get("sessionId")
        event_id = message.get("id") or record.get("uuid") or identity(path, line_number)
        creation = usage.get("cache_creation") or {}
        write_5m = creation.get("ephemeral_5m_input_tokens")
        write_1h = creation.get("ephemeral_1h_input_tokens")
        write_total = integer(usage.get("cache_creation_input_tokens"))
        known_write = integer(write_5m) + integer(write_1h)
        add_usage(db, (
            "claude", machine_id, account_alias, event_id,
            record.get("timestamp") or "", session_id,
            record.get("parentUuid"), message.get("model"),
            integer(usage.get("input_tokens")), integer(write_5m), integer(write_1h),
            max(0, write_total - known_write), integer(usage.get("cache_read_input_tokens")),
            integer(usage.get("output_tokens")), None,
        ))
        usage_count += 1
    return usage_count, 0


def scan(args):
    with connect(Path(args.db)) as db:
        totals = defaultdict(int)
        for source, root, importer in (
            ("codex", Path(args.codex_root), import_codex),
            ("claude", Path(args.claude_root), import_claude),
        ):
            if not root.exists():
                continue
            files = root.rglob("*.jsonl")
            for path in files:
                if source == "codex" and not {"sessions", "archived_sessions"}.intersection(path.parts):
                    continue
                if source == "claude" and "projects" not in path.parts:
                    continue
                usage, meters = importer(path, db, args.machine_id, args.account_alias)
                totals[f"{source}_files"] += 1
                totals[f"{source}_usage_seen"] += usage
                totals[f"{source}_meters_seen"] += meters
        db.commit()
        for key in sorted(totals):
            print(f"{key}: {totals[key]}")
        count = db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        print(f"unique_usage_events_in_db: {count}")


def load_prices(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("prices", [])


def price_for(prices, source, model, timestamp):
    matches = [p for p in prices if p.get("source") == source and p.get("model") == model
               and p.get("effective_from", "") <= timestamp[:10]
               and (not p.get("effective_to") or timestamp[:10] < p["effective_to"])]
    return max(matches, key=lambda p: p["effective_from"]) if matches else None


def event_cost(row, price):
    fields = (
        ("fresh_input", "fresh_input_per_mtok"),
        ("cache_write_5m", "cache_write_5m_per_mtok"),
        ("cache_write_1h", "cache_write_1h_per_mtok"),
        ("cache_write_unknown", "cache_write_unknown_per_mtok"),
        ("cache_read", "cache_read_per_mtok"),
        ("output", "output_per_mtok"),
    )
    cost = 0.0
    for count_field, price_field in fields:
        tokens = row[count_field]
        if tokens:
            rate = price.get(price_field)
            if not isinstance(rate, (int, float)):
                return None
            cost += tokens * rate / 1_000_000
    return cost


def report(args):
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"No database at {path}; run `scan` first.")
    prices = load_prices(args.prices)
    month = args.month or date.today().strftime("%Y-%m")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM usage_events WHERE substr(occurred_at,1,7)=? ORDER BY occurred_at",
            (month,),
        ).fetchall()
        grouped = defaultdict(lambda: {"events": 0, "tokens": 0, "priced": 0,
                                       "replacement_cost": 0.0})
        for row in rows:
            group = grouped[(row["source"], row["model"] or "unknown")]
            group["events"] += 1
            group["tokens"] += sum(row[key] for key in (
                "fresh_input", "cache_write_5m", "cache_write_1h",
                "cache_write_unknown", "cache_read", "output"))
            price = price_for(prices, row["source"], row["model"], row["occurred_at"])
            cost = event_cost(row, price) if price else None
            if cost is not None:
                group["priced"] += 1
                group["replacement_cost"] += cost
        print(f"Usage for {month} (local metadata only)")
        if not rows:
            print("No imported usage events.")
        for (source, model), data in sorted(grouped.items()):
            coverage = f"{data['priced']}/{data['events']} events priced"
            value = (f"${data['replacement_cost']:10.2f}" if data["priced"]
                     else "unavailable")
            print(f"{source:6} {model:24} {data['events']:5} events  "
                  f"{data['tokens']/1_000_000:10.2f} MTok  "
                  f"{value} API replacement estimate  ({coverage})")
        meters = db.execute(
            """SELECT meter_name, MIN(used_percent), MAX(used_percent), COUNT(*)
               FROM meter_snapshots WHERE substr(occurred_at,1,7)=?
               GROUP BY meter_name ORDER BY meter_name""", (month,),
        ).fetchall()
        for name, low, high, count in meters:
            print(f"meter {name}: observed {low:g}%–{high:g}% across {count} snapshots")
        tasks = db.execute(
            """SELECT outcome, COUNT(*) FROM task_notes
               WHERE substr(noted_at,1,7)=? GROUP BY outcome ORDER BY outcome""",
            (month,),
        ).fetchall()
        for outcome, count in tasks:
            print(f"task notes: {outcome}: {count}")


def note(args):
    if args.outcome not in {"accepted", "reworked", "abandoned", "blocked"}:
        raise SystemExit("outcome must be accepted, reworked, abandoned, or blocked")
    with connect(Path(args.db)) as db:
        db.execute(
            """INSERT INTO task_notes(machine_id,task_id,source,session_id,category,outcome,elapsed_minutes,interventions)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(machine_id,task_id) DO UPDATE SET
               source=excluded.source,session_id=excluded.session_id,
               category=excluded.category,outcome=excluded.outcome,
               elapsed_minutes=excluded.elapsed_minutes,interventions=excluded.interventions,
               noted_at=CURRENT_TIMESTAMP""",
            (args.machine_id, args.task_id, args.source, args.session_id, args.category,
             args.outcome, args.elapsed_minutes, args.interventions),
        )
    print(f"Saved note for {args.task_id}")


def cycle(args):
    if args.cycle_end <= args.cycle_start or args.fee_usd < 0:
        raise SystemExit("Cycle end must follow start and fee must be nonnegative")
    with connect(Path(args.db)) as db:
        db.execute(
            """INSERT OR REPLACE INTO subscription_cycles VALUES (?,?,?,?,?,?)""",
            (args.source, args.account_alias, args.plan,
             args.cycle_start, args.cycle_end, args.fee_usd),
        )
    print(f"Saved {args.plan} cycle starting {args.cycle_start}")


def cycle_report(args):
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"No database at {path}; run `scan` first.")
    prices = load_prices(args.prices)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        cycle_row = db.execute(
            """SELECT * FROM subscription_cycles WHERE source=?
               AND account_alias=? AND cycle_start=?""",
            (args.source, args.account_alias, args.cycle_start),
        ).fetchone()
        if cycle_row is None:
            raise SystemExit("No matching subscription cycle")
        start, end = cycle_row["cycle_start"], cycle_row["cycle_end"]
        rows = db.execute(
            """SELECT * FROM usage_events WHERE account_alias=? AND source=?
               AND substr(occurred_at,1,10)>=? AND substr(occurred_at,1,10)<?""",
            (args.account_alias, args.source, start, end),
        ).fetchall()
        priced, replacement = 0, 0.0
        for row in rows:
            price = price_for(prices, row["source"], row["model"], row["occurred_at"])
            cost = event_cost(row, price) if price else None
            if cost is not None:
                priced += 1
                replacement += cost
        print(f"{cycle_row['plan']} ({args.source}/{args.account_alias}, all machines)")
        print(f"Cycle: {start} to {end}; fee: ${cycle_row['fee_usd']:.2f}")
        print(f"Usage records: {len(rows)}; priced: {priced}")
        if priced == len(rows) and rows:
            ratio = replacement / cycle_row["fee_usd"] if cycle_row["fee_usd"] else None
            print(f"API replacement estimate: ${replacement:.2f}")
            if ratio is not None:
                print(f"Replacement estimate / fee: {ratio:.2f}x")
        else:
            print(f"Partial API replacement estimate: ${replacement:.2f}; ratio withheld")
        meters = db.execute(
            """SELECT meter_name, MAX(used_percent), COUNT(*) FROM meter_snapshots
               WHERE account_alias=? AND source=? AND substr(occurred_at,1,10)>=?
               AND substr(occurred_at,1,10)<? GROUP BY meter_name ORDER BY meter_name""",
            (args.account_alias, args.source, start, end),
        ).fetchall()
        for name, peak, count in meters:
            last = db.execute(
                """SELECT used_percent, occurred_at FROM meter_snapshots WHERE account_alias=?
                   AND source=? AND meter_name=? AND substr(occurred_at,1,10)>=?
                   AND substr(occurred_at,1,10)<? ORDER BY occurred_at DESC LIMIT 1""",
                (args.account_alias, args.source, name, start, end),
            ).fetchone()
            print(f"{name}: peak {peak:g}%, last {last[0]:g}% at {last[1]} ({count} snapshots)")


EXPORT_TABLES = {
    "usage": ("usage_events", (
        "source", "machine_id", "account_alias", "event_id", "occurred_at", "session_id", "turn_id",
        "model", "fresh_input", "cache_write_5m", "cache_write_1h",
        "cache_write_unknown", "cache_read", "output", "reasoning_output")),
    "meter": ("meter_snapshots", (
        "source", "machine_id", "account_alias", "snapshot_id", "occurred_at", "session_id", "plan",
        "meter_name", "used_percent", "window_minutes", "resets_at")),
    "task": ("task_notes", (
        "machine_id", "task_id", "source", "session_id", "category", "outcome",
        "elapsed_minutes", "interventions", "noted_at")),
    "cycle": ("subscription_cycles", (
        "source", "account_alias", "plan", "cycle_start",
        "cycle_end", "fee_usd")),
}


def export(args):
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"No database at {db_path}; run `scan` first.")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    count = 0
    with sqlite3.connect(db_path) as db, temporary.open("w", encoding="utf-8") as handle:
        db.row_factory = sqlite3.Row
        for kind, (table, _fields) in EXPORT_TABLES.items():
            for row in db.execute(f"SELECT * FROM {table}"):
                envelope = {"schema_version": 1, "kind": kind, "data": dict(row)}
                handle.write(json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n")
                count += 1
    temporary.replace(output)
    print(f"Exported {count} metadata events to {output}")


def ingest(args):
    count = 0
    with connect(Path(args.db)) as db:
        for _line, envelope in read_jsonl(Path(args.input), strict=True):
            if envelope.get("schema_version") != 1:
                raise SystemExit("Unsupported or missing schema_version in input")
            kind = envelope.get("kind")
            if kind not in EXPORT_TABLES:
                raise SystemExit(f"Unknown event kind: {kind}")
            data = envelope.get("data") or {}
            table, fields = EXPORT_TABLES[kind]
            if any(field not in data for field in fields):
                raise SystemExit(f"Missing field in {kind} event")
            values = tuple(data[field] for field in fields)
            if kind == "usage":
                add_usage(db, values)
            else:
                placeholders = ",".join("?" for _ in fields)
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", values)
            count += 1
    print(f"Ingested {count} metadata events; duplicate IDs updated in place")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="observatory.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="Import usage metadata from local transcripts")
    scan_parser.add_argument("--codex-root", default=str(Path.home() / ".codex"))
    scan_parser.add_argument("--claude-root", default=str(Path.home() / ".claude"))
    scan_parser.add_argument("--machine-id", required=True, help="Stable, unique alias for this machine")
    scan_parser.add_argument("--account-alias", required=True, help="Stable alias for your subscription account")
    scan_parser.set_defaults(func=scan)
    report_parser = commands.add_parser("report", help="Summarize a calendar month")
    report_parser.add_argument("--month", help="YYYY-MM; defaults to the current month")
    report_parser.add_argument("--prices", help="Versioned pricebook JSON; unknown prices remain unpriced")
    report_parser.set_defaults(func=report)
    note_parser = commands.add_parser("note", help="Tag a task outcome without storing its content")
    note_parser.add_argument("task_id")
    note_parser.add_argument("--source")
    note_parser.add_argument("--session-id")
    note_parser.add_argument("--category")
    note_parser.add_argument("--outcome", required=True)
    note_parser.add_argument("--elapsed-minutes", type=float)
    note_parser.add_argument("--interventions", type=int)
    note_parser.add_argument("--machine-id", required=True)
    note_parser.set_defaults(func=note)
    cycle_parser = commands.add_parser("cycle", help="Record one subscription billing cycle")
    cycle_parser.add_argument("--source", required=True)
    cycle_parser.add_argument("--account-alias", required=True)
    cycle_parser.add_argument("--plan", required=True)
    cycle_parser.add_argument("--cycle-start", required=True, help="YYYY-MM-DD, inclusive")
    cycle_parser.add_argument("--cycle-end", required=True, help="YYYY-MM-DD, exclusive")
    cycle_parser.add_argument("--fee-usd", required=True, type=float)
    cycle_parser.set_defaults(func=cycle)
    cycle_report_parser = commands.add_parser("cycle-report", help="Compare measured use with a plan fee")
    cycle_report_parser.add_argument("--source", required=True)
    cycle_report_parser.add_argument("--account-alias", required=True)
    cycle_report_parser.add_argument("--cycle-start", required=True)
    cycle_report_parser.add_argument("--prices", required=True)
    cycle_report_parser.set_defaults(func=cycle_report)
    export_parser = commands.add_parser("export", help="Write portable, metadata-only NDJSON")
    export_parser.add_argument("--out", required=True)
    export_parser.set_defaults(func=export)
    ingest_parser = commands.add_parser("ingest", help="Import a portable NDJSON export")
    ingest_parser.add_argument("--input", required=True)
    ingest_parser.set_defaults(func=ingest)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
