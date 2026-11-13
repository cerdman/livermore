"""Metadata-only, local importer for Codex and Claude Code session usage."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from . import __version__

SCHEMA_VERSION = 1

# Version 0 tables. Migrations below bring any database, new or old, to SCHEMA_VERSION.
BASE_SCHEMA = """
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


def _migrate_v1(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(usage_events)")}
    if "request_id" not in columns:
        db.execute("ALTER TABLE usage_events ADD COLUMN request_id TEXT")
    if "provenance" not in columns:
        db.execute("ALTER TABLE usage_events ADD COLUMN provenance TEXT")
    # Version 0 stored Claude's parentUuid as a turn ID. It never identified a turn.
    db.execute("UPDATE usage_events SET turn_id=NULL WHERE source='claude'")
    db.executescript("""
    CREATE INDEX IF NOT EXISTS usage_events_request ON usage_events(source, request_id);
    CREATE INDEX IF NOT EXISTS meter_snapshots_natural
        ON meter_snapshots(source, machine_id, occurred_at, meter_name);
    CREATE TABLE IF NOT EXISTS request_metrics (
        source TEXT NOT NULL,
        machine_id TEXT NOT NULL,
        account_alias TEXT NOT NULL,
        metric_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        session_id TEXT,
        request_id TEXT,
        event_name TEXT NOT NULL,
        model TEXT,
        duration_ms REAL,
        ttft_ms REAL,
        status_code INTEGER,
        attempt INTEGER,
        error TEXT,
        provider_cost_usd REAL,
        PRIMARY KEY (source, machine_id, metric_id)
    );
    CREATE INDEX IF NOT EXISTS request_metrics_time ON request_metrics(occurred_at);
    CREATE TABLE IF NOT EXISTS scan_files (
        machine_id TEXT NOT NULL,
        path_hash TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        PRIMARY KEY (machine_id, path_hash)
    );
    """)


MIGRATIONS = {1: _migrate_v1}


def migrate(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SystemExit(f"Database schema {version} is newer than this livermore "
                         f"({SCHEMA_VERSION}); upgrade livermore.")
    db.executescript(BASE_SCHEMA)
    for target in range(version + 1, SCHEMA_VERSION + 1):
        MIGRATIONS[target](db)
        db.execute(f"PRAGMA user_version={target}")
    db.commit()


def connect(path: Path, *, check_same_thread=True):
    """Open and migrate a database. The caller must close it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=check_same_thread)
    db.row_factory = sqlite3.Row
    try:
        migrate(db)
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def open_db(path, *, must_exist=False):
    """Yield a migrated connection; commit on success, roll back on error, always close."""
    path = Path(path)
    if must_exist and not path.exists():
        raise SystemExit(f"No database at {path}; run `scan` first.")
    with closing(connect(path)) as db:
        with db:
            yield db


def integer(value):
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def identity(*parts):
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


USAGE_FIELDS = (
    "source", "machine_id", "account_alias", "event_id", "occurred_at", "session_id",
    "turn_id", "model", "fresh_input", "cache_write_5m", "cache_write_1h",
    "cache_write_unknown", "cache_read", "output", "reasoning_output", "request_id",
    "provenance",
)


def _merge_provenance(*values):
    labels = {label for value in values if value for label in value.split(",") if label}
    return ",".join(sorted(labels)) or None


def add_usage(db, row):
    """Upsert one usage event.

    A provider request ID identifies the same request whichever path reported it
    (transcript scan, OTel, or NDJSON ingest). When one is present, the row that first
    recorded that request keeps its key, so the request is counted once.
    """
    row = {field: row.get(field) for field in USAGE_FIELDS}
    if row["request_id"]:
        found = db.execute(
            "SELECT machine_id, event_id FROM usage_events WHERE source=? AND request_id=? "
            "LIMIT 1", (row["source"], row["request_id"])).fetchone()
        if found and (found[0], found[1]) != (row["machine_id"], row["event_id"]):
            # An older row may still sit under this event's own key without a request ID.
            db.execute("DELETE FROM usage_events WHERE source=? AND machine_id=? AND event_id=?",
                       (row["source"], row["machine_id"], row["event_id"]))
            row["machine_id"], row["event_id"] = found[0], found[1]
    existing = db.execute(
        "SELECT provenance FROM usage_events WHERE source=? AND machine_id=? AND event_id=?",
        (row["source"], row["machine_id"], row["event_id"])).fetchone()
    row["provenance"] = _merge_provenance(existing and existing[0], row["provenance"])
    placeholders = ",".join("?" for _ in USAGE_FIELDS)
    db.execute(
        f"""INSERT INTO usage_events ({",".join(USAGE_FIELDS)}) VALUES ({placeholders})
        ON CONFLICT(source,machine_id,event_id) DO UPDATE SET
          account_alias=excluded.account_alias,
          occurred_at=excluded.occurred_at,
          session_id=COALESCE(excluded.session_id,usage_events.session_id),
          turn_id=COALESCE(excluded.turn_id,usage_events.turn_id),
          model=COALESCE(excluded.model,usage_events.model),
          fresh_input=excluded.fresh_input,
          cache_write_5m=excluded.cache_write_5m,
          cache_write_1h=excluded.cache_write_1h,
          cache_write_unknown=excluded.cache_write_unknown,
          cache_read=excluded.cache_read,
          output=excluded.output,
          reasoning_output=COALESCE(excluded.reasoning_output,usage_events.reasoning_output),
          request_id=COALESCE(excluded.request_id,usage_events.request_id),
          provenance=excluded.provenance""",
        tuple(row[field] for field in USAGE_FIELDS),
    )


METER_FIELDS = (
    "source", "machine_id", "account_alias", "snapshot_id", "occurred_at", "session_id",
    "plan", "meter_name", "used_percent", "window_minutes", "resets_at",
)


def add_meter(db, row):
    """Insert a meter snapshot, replacing any snapshot of the same meter at the same time."""
    db.execute(
        """DELETE FROM meter_snapshots WHERE source=? AND machine_id=? AND occurred_at=?
           AND meter_name=? AND session_id IS ?""",
        (row["source"], row["machine_id"], row["occurred_at"], row["meter_name"],
         row["session_id"]))
    db.execute(
        f"INSERT OR REPLACE INTO meter_snapshots ({','.join(METER_FIELDS)}) "
        f"VALUES ({','.join('?' for _ in METER_FIELDS)})",
        tuple(row.get(field) for field in METER_FIELDS))


REQUEST_FIELDS = (
    "source", "machine_id", "account_alias", "metric_id", "occurred_at", "session_id",
    "request_id", "event_name", "model", "duration_ms", "ttft_ms", "status_code", "attempt",
    "error", "provider_cost_usd",
)


def add_request_metric(db, row):
    db.execute(
        f"INSERT OR REPLACE INTO request_metrics ({','.join(REQUEST_FIELDS)}) "
        f"VALUES ({','.join('?' for _ in REQUEST_FIELDS)})",
        tuple(row.get(field) for field in REQUEST_FIELDS))


def _zstd_decompressor():
    try:
        from compression import zstd  # Python 3.14+
        return lambda data: zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        return None
    return lambda data: zstandard.ZstdDecompressor().decompressobj().decompress(data)


def zstd_available():
    return _zstd_decompressor() is not None


def _open_text(path):
    path = Path(path)
    if path.suffix == ".zst":
        decompress = _zstd_decompressor()
        if decompress is None:
            raise RuntimeError("zstd support needs Python 3.14+ or `pip install zstandard`")
        return io.StringIO(decompress(path.read_bytes()).decode("utf-8", errors="replace"))
    return path.open(encoding="utf-8", errors="replace")


def read_jsonl(path, *, strict=False):
    with _open_text(path) as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                if strict:
                    raise ValueError(f"Invalid JSON at {path}:{line_number}") from error
                continue  # Session files can end with an in-progress line.
            if isinstance(record, dict):
                yield line_number, record


def _session_file_name(path):
    """File name without compression suffix, so archiving a session keeps its IDs."""
    name = Path(path).name
    return name[:-4] if name.endswith(".zst") else name


def import_codex(path, db, machine_id, account_alias="personal"):
    session_id = None
    model_by_turn = {}
    current_model = None
    usage_count = 0
    meter_count = 0
    file_key = _session_file_name(path)
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
            response_id = payload.get("response_id")
            add_usage(db, {
                "source": "codex", "machine_id": machine_id, "account_alias": account_alias,
                "event_id": response_id or identity(file_key, line_number),
                "occurred_at": timestamp,
                "session_id": payload.get("session_id") or session_id,
                "turn_id": turn_id,
                "model": model_by_turn.get(turn_id) or current_model,
                "fresh_input": total_input - cached - written,
                "cache_write_5m": 0, "cache_write_1h": 0, "cache_write_unknown": written,
                "cache_read": cached,
                "output": integer(usage.get("output_tokens")),
                "reasoning_output": (integer(usage["reasoning_output_tokens"])
                                     if "reasoning_output_tokens" in usage else None),
                "request_id": response_id,
                "provenance": "transcript",
            })
            usage_count += 1
        elif kind == "event_msg" and payload.get("type") == "token_count":
            limits = payload.get("rate_limits") or {}
            for name in ("primary", "secondary"):
                meter = limits.get(name) or {}
                used = meter.get("used_percent")
                if not isinstance(used, (int, float)):
                    continue
                add_meter(db, {
                    "source": "codex", "machine_id": machine_id,
                    "account_alias": account_alias,
                    "snapshot_id": identity(file_key, line_number, name),
                    "occurred_at": timestamp, "session_id": session_id,
                    "plan": limits.get("plan_type"),
                    "meter_name": f"{limits.get('limit_id') or 'codex'}:{name}",
                    "used_percent": float(used),
                    "window_minutes": meter.get("window_minutes"),
                    "resets_at": meter.get("resets_at"),
                })
                meter_count += 1
    return usage_count, meter_count


def import_claude(path, db, machine_id, account_alias="personal"):
    usage_count = 0
    file_key = _session_file_name(path)
    for line_number, record in read_jsonl(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        required = {"input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"}
        if not required.issubset(usage) or message.get("model") == "<synthetic>":
            continue
        # One API response is written as several lines (one per content block) that share
        # message.id; the upsert keeps the last, most complete usage.
        event_id = message.get("id") or record.get("uuid") or identity(file_key, line_number)
        creation = usage.get("cache_creation") or {}
        write_5m = creation.get("ephemeral_5m_input_tokens")
        write_1h = creation.get("ephemeral_1h_input_tokens")
        write_total = integer(usage.get("cache_creation_input_tokens"))
        known_write = integer(write_5m) + integer(write_1h)
        add_usage(db, {
            "source": "claude", "machine_id": machine_id, "account_alias": account_alias,
            "event_id": event_id, "occurred_at": record.get("timestamp") or "",
            "session_id": record.get("sessionId"), "turn_id": None,
            "model": message.get("model"),
            "fresh_input": integer(usage.get("input_tokens")),
            "cache_write_5m": integer(write_5m), "cache_write_1h": integer(write_1h),
            "cache_write_unknown": max(0, write_total - known_write),
            "cache_read": integer(usage.get("cache_read_input_tokens")),
            "output": integer(usage.get("output_tokens")), "reasoning_output": None,
            "request_id": record.get("requestId"),
            "provenance": "transcript",
        })
        usage_count += 1
    return usage_count, 0


SCAN_SOURCES = (
    ("codex", "codex_root", ("sessions", "archived_sessions"), import_codex),
    ("claude", "claude_root", ("projects",), import_claude),
)


def _session_files(base):
    for pattern in ("*.jsonl", "*.jsonl.zst"):
        yield from base.rglob(pattern)


def scan(args):
    totals = defaultdict(int)
    can_decompress = zstd_available()
    with open_db(args.db) as db:
        for source, root_attr, subdirs, importer in SCAN_SOURCES:
            root = Path(getattr(args, root_attr))
            for subdir in subdirs:
                base = root / subdir
                if not base.is_dir():
                    continue
                for path in sorted(_session_files(base)):
                    if path.suffix == ".zst" and not can_decompress:
                        totals[f"{source}_compressed_skipped"] += 1
                        continue
                    stat = path.stat()
                    path_hash = identity(path.resolve())
                    seen = db.execute(
                        "SELECT size, mtime_ns FROM scan_files WHERE machine_id=? "
                        "AND path_hash=?", (args.machine_id, path_hash)).fetchone()
                    if not args.full and seen and tuple(seen) == (stat.st_size,
                                                                  stat.st_mtime_ns):
                        totals[f"{source}_files_unchanged"] += 1
                        continue
                    usage, meters = importer(path, db, args.machine_id, args.account_alias)
                    db.execute("INSERT OR REPLACE INTO scan_files VALUES (?,?,?,?)",
                               (args.machine_id, path_hash, stat.st_size, stat.st_mtime_ns))
                    db.commit()
                    totals[f"{source}_files"] += 1
                    totals[f"{source}_usage_seen"] += usage
                    totals[f"{source}_meters_seen"] += meters
        for key in sorted(totals):
            print(f"{key}: {totals[key]}")
        if totals.get("codex_compressed_skipped"):
            print("note: archived .jsonl.zst sessions need Python 3.14+ or "
                  "`pip install zstandard`")
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


TOKEN_FIELDS = ("fresh_input", "cache_write_5m", "cache_write_1h", "cache_write_unknown",
                "cache_read", "output")


def percentile(values, fraction):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def build_report(db, month, prices):
    rows = db.execute(
        "SELECT * FROM usage_events WHERE substr(occurred_at,1,7)=? ORDER BY occurred_at",
        (month,),
    ).fetchall()
    grouped = defaultdict(lambda: {"events": 0, "tokens": 0, "priced_events": 0,
                                   "replacement_cost_usd": 0.0})
    for row in rows:
        group = grouped[(row["source"], row["model"] or "unknown")]
        group["events"] += 1
        group["tokens"] += sum(row[key] for key in TOKEN_FIELDS)
        price = price_for(prices, row["source"], row["model"], row["occurred_at"])
        cost = event_cost(row, price) if price else None
        if cost is not None:
            group["priced_events"] += 1
            group["replacement_cost_usd"] += cost
    usage = []
    for (source, model), data in sorted(grouped.items()):
        if not data["priced_events"]:
            data["replacement_cost_usd"] = None
        usage.append({"source": source, "model": model, **data})
    meters = [
        {"meter": name, "min_percent": low, "max_percent": high, "snapshots": count}
        for name, low, high, count in db.execute(
            """SELECT meter_name, MIN(used_percent), MAX(used_percent), COUNT(*)
               FROM meter_snapshots WHERE substr(occurred_at,1,7)=?
               GROUP BY meter_name ORDER BY meter_name""", (month,))
    ]
    latency = defaultdict(lambda: {"requests": 0, "durations": [], "errors": 0,
                                   "provider_cost_usd": 0.0})
    for row in db.execute(
            """SELECT source, model, event_name, duration_ms, provider_cost_usd
               FROM request_metrics WHERE substr(occurred_at,1,7)=?""", (month,)):
        group = latency[(row["source"], row["model"] or "unknown")]
        if row["event_name"] == "api_error":
            group["errors"] += 1
            continue
        group["requests"] += 1
        if row["duration_ms"] is not None:
            group["durations"].append(row["duration_ms"])
        if row["provider_cost_usd"] is not None:
            group["provider_cost_usd"] += row["provider_cost_usd"]
    requests = []
    for (source, model), data in sorted(latency.items()):
        durations = data.pop("durations")
        requests.append({
            "source": source, "model": model, **data,
            "p50_ms": percentile(durations, 0.5) if durations else None,
            "p95_ms": percentile(durations, 0.95) if durations else None,
        })
    tasks = {outcome: count for outcome, count in db.execute(
        """SELECT outcome, COUNT(*) FROM task_notes
           WHERE substr(noted_at,1,7)=? GROUP BY outcome ORDER BY outcome""", (month,))}
    return {"month": month, "usage": usage, "meters": meters, "requests": requests,
            "task_outcomes": tasks}


def report(args):
    prices = load_prices(args.prices)
    month = args.month or date.today().strftime("%Y-%m")
    with open_db(args.db, must_exist=True) as db:
        data = build_report(db, month, prices)
    if getattr(args, "format", "text") == "json":
        print(json.dumps(data, indent=2))
        return
    print(f"Usage for {month} (local metadata only)")
    if not data["usage"]:
        print("No imported usage events.")
    for item in data["usage"]:
        coverage = f"{item['priced_events']}/{item['events']} events priced"
        cost = item["replacement_cost_usd"]
        value = f"${cost:10.2f}" if cost is not None else "unavailable"
        print(f"{item['source']:6} {item['model']:24} {item['events']:5} events  "
              f"{item['tokens']/1_000_000:10.2f} MTok  "
              f"{value} API replacement estimate  ({coverage})")
    for meter in data["meters"]:
        print(f"meter {meter['meter']}: observed {meter['min_percent']:g}%–"
              f"{meter['max_percent']:g}% across {meter['snapshots']} snapshots")
    for item in data["requests"]:
        timing = (f"p50 {item['p50_ms']:.0f} ms, p95 {item['p95_ms']:.0f} ms"
                  if item["p50_ms"] is not None else "no timings")
        print(f"requests {item['source']}/{item['model']}: {item['requests']} ({timing}), "
              f"{item['errors']} API errors")
    for outcome, count in data["task_outcomes"].items():
        print(f"task notes: {outcome}: {count}")


def note(args):
    if args.outcome not in {"accepted", "reworked", "abandoned", "blocked"}:
        raise SystemExit("outcome must be accepted, reworked, abandoned, or blocked")
    with open_db(args.db) as db:
        db.execute(
            """INSERT INTO task_notes(machine_id,task_id,source,session_id,category,outcome,
               elapsed_minutes,interventions)
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
    with open_db(args.db) as db:
        db.execute(
            """INSERT OR REPLACE INTO subscription_cycles VALUES (?,?,?,?,?,?)""",
            (args.source, args.account_alias, args.plan,
             args.cycle_start, args.cycle_end, args.fee_usd),
        )
    print(f"Saved {args.plan} cycle starting {args.cycle_start}")


def build_cycle_report(db, source, account_alias, cycle_start, prices):
    cycle_row = db.execute(
        """SELECT * FROM subscription_cycles WHERE source=?
           AND account_alias=? AND cycle_start=?""",
        (source, account_alias, cycle_start),
    ).fetchone()
    if cycle_row is None:
        raise SystemExit("No matching subscription cycle")
    start, end = cycle_row["cycle_start"], cycle_row["cycle_end"]
    rows = db.execute(
        """SELECT * FROM usage_events WHERE account_alias=? AND source=?
           AND substr(occurred_at,1,10)>=? AND substr(occurred_at,1,10)<?""",
        (account_alias, source, start, end),
    ).fetchall()
    priced, replacement = 0, 0.0
    for row in rows:
        price = price_for(prices, row["source"], row["model"], row["occurred_at"])
        cost = event_cost(row, price) if price else None
        if cost is not None:
            priced += 1
            replacement += cost
    complete = bool(rows) and priced == len(rows)
    fee = cycle_row["fee_usd"]
    meters = []
    for name, peak, count in db.execute(
            """SELECT meter_name, MAX(used_percent), COUNT(*) FROM meter_snapshots
               WHERE account_alias=? AND source=? AND substr(occurred_at,1,10)>=?
               AND substr(occurred_at,1,10)<? GROUP BY meter_name ORDER BY meter_name""",
            (account_alias, source, start, end)):
        last = db.execute(
            """SELECT used_percent, occurred_at FROM meter_snapshots WHERE account_alias=?
               AND source=? AND meter_name=? AND substr(occurred_at,1,10)>=?
               AND substr(occurred_at,1,10)<? ORDER BY occurred_at DESC LIMIT 1""",
            (account_alias, source, name, start, end),
        ).fetchone()
        meters.append({"meter": name, "peak_percent": peak, "last_percent": last[0],
                       "last_at": last[1], "snapshots": count})
    return {
        "plan": cycle_row["plan"], "source": source, "account_alias": account_alias,
        "cycle_start": start, "cycle_end": end, "fee_usd": fee,
        "usage_records": len(rows), "priced_records": priced,
        "replacement_cost_usd": replacement, "complete": complete,
        "replacement_to_fee": (replacement / fee) if complete and fee else None,
        "meters": meters,
    }


def cycle_report(args):
    prices = load_prices(args.prices)
    with open_db(args.db, must_exist=True) as db:
        data = build_cycle_report(db, args.source, args.account_alias, args.cycle_start,
                                  prices)
    if getattr(args, "format", "text") == "json":
        print(json.dumps(data, indent=2))
        return
    print(f"{data['plan']} ({data['source']}/{data['account_alias']}, all machines)")
    print(f"Cycle: {data['cycle_start']} to {data['cycle_end']}; fee: ${data['fee_usd']:.2f}")
    print(f"Usage records: {data['usage_records']}; priced: {data['priced_records']}")
    if data["complete"]:
        print(f"API replacement estimate: ${data['replacement_cost_usd']:.2f}")
        if data["replacement_to_fee"] is not None:
            print(f"Replacement estimate / fee: {data['replacement_to_fee']:.2f}x")
    else:
        print(f"Partial API replacement estimate: ${data['replacement_cost_usd']:.2f}; "
              "ratio withheld")
    for meter in data["meters"]:
        print(f"{meter['meter']}: peak {meter['peak_percent']:g}%, last "
              f"{meter['last_percent']:g}% at {meter['last_at']} "
              f"({meter['snapshots']} snapshots)")


EXPORT_SCHEMA_VERSION = 2

# kind: (table, fields every version carries, fields added in a later version)
EXPORT_TABLES = {
    "usage": ("usage_events", USAGE_FIELDS[:15], USAGE_FIELDS[15:]),
    "meter": ("meter_snapshots", METER_FIELDS, ()),
    "task": ("task_notes", (
        "machine_id", "task_id", "source", "session_id", "category", "outcome",
        "elapsed_minutes", "interventions", "noted_at"), ()),
    "cycle": ("subscription_cycles", (
        "source", "account_alias", "plan", "cycle_start",
        "cycle_end", "fee_usd"), ()),
    "request": ("request_metrics", REQUEST_FIELDS, ()),
}
KINDS_BY_VERSION = {1: {"usage", "meter", "task", "cycle"},
                    2: set(EXPORT_TABLES)}


def export(args):
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    count = 0
    with open_db(args.db, must_exist=True) as db, \
            temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for kind, (table, required, optional) in EXPORT_TABLES.items():
            fields = required + optional
            for row in db.execute(f"SELECT {','.join(fields)} FROM {table}"):
                envelope = {"schema_version": EXPORT_SCHEMA_VERSION, "kind": kind,
                            "data": dict(row)}
                handle.write(json.dumps(envelope, separators=(",", ":"), sort_keys=True)
                             + "\n")
                count += 1
    temporary.replace(output)
    print(f"Exported {count} metadata events to {output}")


def ingest(args):
    count = 0
    with open_db(args.db) as db:
        for _line, envelope in read_jsonl(Path(args.input), strict=True):
            version = envelope.get("schema_version")
            if version not in KINDS_BY_VERSION:
                raise SystemExit("Unsupported or missing schema_version in input")
            kind = envelope.get("kind")
            if kind not in KINDS_BY_VERSION[version]:
                raise SystemExit(f"Unknown event kind: {kind}")
            data = envelope.get("data") or {}
            table, required, optional = EXPORT_TABLES[kind]
            if any(field not in data for field in required):
                raise SystemExit(f"Missing field in {kind} event")
            row = {field: data.get(field) for field in required + optional}
            if kind == "usage":
                add_usage(db, row)
            elif kind == "meter":
                add_meter(db, row)
            else:
                fields = required + optional
                db.execute(
                    f"INSERT OR REPLACE INTO {table} ({','.join(fields)}) "
                    f"VALUES ({','.join('?' for _ in fields)})",
                    tuple(row[field] for field in fields))
            count += 1
    print(f"Ingested {count} metadata events; duplicate IDs updated in place")


def serve(args):
    from . import otel

    otel.serve(args)


def build_parser():
    parser = argparse.ArgumentParser(prog="observatory", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", default=os.environ.get("LIVERMORE_DB", "observatory.sqlite3"),
                        help="SQLite database path (default: $LIVERMORE_DB or "
                             "./observatory.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="Import usage metadata from local transcripts")
    scan_parser.add_argument("--codex-root", default=str(Path.home() / ".codex"))
    scan_parser.add_argument("--claude-root", default=str(Path.home() / ".claude"))
    scan_parser.add_argument("--machine-id", required=True,
                             help="Stable, unique alias for this machine")
    scan_parser.add_argument("--account-alias", required=True,
                             help="Stable alias for your subscription account")
    scan_parser.add_argument("--full", action="store_true",
                             help="Re-read every file, even ones unchanged since the last scan")
    scan_parser.set_defaults(func=scan)
    report_parser = commands.add_parser("report", help="Summarize a calendar month")
    report_parser.add_argument("--month", help="YYYY-MM; defaults to the current month")
    report_parser.add_argument("--prices",
                               help="Versioned pricebook JSON; unknown prices remain unpriced")
    report_parser.add_argument("--format", choices=("text", "json"), default="text")
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
    cycle_report_parser = commands.add_parser("cycle-report",
                                              help="Compare measured use with a plan fee")
    cycle_report_parser.add_argument("--source", required=True)
    cycle_report_parser.add_argument("--account-alias", required=True)
    cycle_report_parser.add_argument("--cycle-start", required=True)
    cycle_report_parser.add_argument("--prices", required=True)
    cycle_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    cycle_report_parser.set_defaults(func=cycle_report)
    export_parser = commands.add_parser("export", help="Write portable, metadata-only NDJSON")
    export_parser.add_argument("--out", required=True)
    export_parser.set_defaults(func=export)
    ingest_parser = commands.add_parser("ingest", help="Import a portable NDJSON export")
    ingest_parser.add_argument("--input", required=True)
    ingest_parser.set_defaults(func=ingest)
    serve_parser = commands.add_parser(
        "serve", help="Receive Claude Code OpenTelemetry logs (OTLP/HTTP JSON)")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=4318)
    serve_parser.add_argument(
        "--machine-id", help="Used when a sender does not set livermore.machine_id")
    serve_parser.add_argument(
        "--account-alias", help="Used when a sender does not set livermore.account_alias")
    serve_parser.add_argument(
        "--token", help="Require 'Authorization: Bearer TOKEN'; defaults to $LIVERMORE_TOKEN")
    serve_parser.add_argument("--verbose", action="store_true", help="Log every request")
    serve_parser.set_defaults(func=serve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:  # e.g. `observatory report | head`
        sys.stderr.close()


if __name__ == "__main__":
    main()
