import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from livermore import __version__, observatory


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def run_cli(*argv):
    output = io.StringIO()
    with redirect_stdout(output):
        observatory.main(list(argv))
    return output.getvalue()


def claude_line(message_id, request_id, output_tokens, **extra):
    """A transcript line in the shape Claude Code 2.1 writes."""
    return {"type": "assistant", "sessionId": "claude-session", "uuid": f"uuid-{output_tokens}",
            "parentUuid": "parent-uuid", "isSidechain": False, "requestId": request_id,
            "timestamp": "2026-10-08T12:00:00Z", "message": {
                "id": message_id, "model": "model-b", "usage": {
                    "input_tokens": 10, "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 100, "output_tokens": output_tokens,
                    "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                       "ephemeral_1h_input_tokens": 0}}}, **extra}


CODEX_SESSION = [
    {"type": "session_meta", "payload": {"id": "session-1",
                                          "content": "SENSITIVE_PROMPT_CANARY"}},
    {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "model-a"}},
    {"type": "token_usage_record", "timestamp": "2026-10-08T12:00:00Z",
     "payload": {"response_id": "response-1", "turn_id": "turn-1",
                 "usage": {"input_tokens": 1000, "cached_input_tokens": 700,
                           "cache_write_input_tokens": 0, "output_tokens": 60,
                           "reasoning_output_tokens": 20}}},
    {"type": "event_msg", "timestamp": "2026-10-08T12:00:01Z",
     "payload": {"type": "token_count", "rate_limits": {
         "limit_id": "codex", "plan_type": "plus",
         "primary": {"used_percent": 12, "window_minutes": 300,
                     "resets_at": 1790000000}}}},
]


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self):
        # Fails on Windows if any test leaves a SQLite connection open.
        self._directory.cleanup()

    def query(self, db_path, sql, *params):
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, params).fetchall()


class ImportTests(TemporaryTest):
    def test_codex_import_is_idempotent_and_keeps_meter_data(self):
        source = self.root / "sessions" / "example.jsonl"
        write_lines(source, CODEX_SESSION)
        db_path = self.root / "local.sqlite3"
        with observatory.open_db(db_path) as db:
            observatory.import_codex(source, db, "laptop")
            observatory.import_codex(source, db, "laptop")
        usage = self.query(db_path, "SELECT * FROM usage_events")
        meters = self.query(db_path, "SELECT * FROM meter_snapshots")
        self.assertEqual((len(usage), len(meters)), (1, 1))
        self.assertEqual(usage[0]["fresh_input"], 300)
        self.assertEqual(usage[0]["cache_read"], 700)
        self.assertEqual(usage[0]["request_id"], "response-1")
        self.assertEqual(usage[0]["provenance"], "transcript")

        export_path = self.root / "portable.ndjson"
        central = self.root / "central.sqlite3"
        with redirect_stdout(io.StringIO()):
            observatory.export(SimpleNamespace(db=db_path, out=export_path))
            observatory.ingest(SimpleNamespace(db=central, input=export_path))
            observatory.ingest(SimpleNamespace(db=central, input=export_path))
        self.assertNotIn("SENSITIVE_PROMPT_CANARY", export_path.read_text())
        self.assertEqual(len(self.query(central, "SELECT * FROM usage_events")), 1)
        self.assertEqual(len(self.query(central, "SELECT * FROM meter_snapshots")), 1)

        # Codex response IDs are global, so a second machine reporting the same response
        # (for example a synced home directory) does not double-count it.
        with observatory.open_db(central) as db:
            observatory.import_codex(source, db, "second-machine")
        self.assertEqual(len(self.query(central, "SELECT * FROM usage_events")), 1)

    def test_archived_codex_session_keeps_meter_ids(self):
        live = self.root / "sessions" / "rollout.jsonl"
        archived = self.root / "archived_sessions" / "rollout.jsonl"
        write_lines(live, CODEX_SESSION)
        write_lines(archived, CODEX_SESSION)
        db_path = self.root / "db.sqlite3"
        with observatory.open_db(db_path) as db:
            observatory.import_codex(live, db, "laptop")
            observatory.import_codex(archived, db, "laptop")
        self.assertEqual(len(self.query(db_path, "SELECT * FROM meter_snapshots")), 1)

    def test_claude_current_transcript_shape(self):
        source = self.root / "projects" / "project" / "example.jsonl"
        # One response written as several lines that share message.id and requestId.
        write_lines(source, [claude_line("message-1", "req-1", 5),
                             claude_line("message-1", "req-1", 20),
                             claude_line("message-2", "req-2", 7, isSidechain=True)])
        db_path = self.root / "local.sqlite3"
        with observatory.open_db(db_path) as db:
            observatory.import_claude(source, db, "desktop")
        rows = self.query(db_path, "SELECT * FROM usage_events ORDER BY event_id")
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual((row["fresh_input"], row["cache_write_5m"], row["cache_read"],
                          row["output"]), (10, 50, 100, 20))
        self.assertEqual(row["request_id"], "req-1")
        self.assertIsNone(row["turn_id"])  # parentUuid is not a turn ID
        price = {"fresh_input_per_mtok": 4, "cache_write_5m_per_mtok": 5,
                 "cache_read_per_mtok": 0.2, "output_per_mtok": 20}
        self.assertAlmostEqual(observatory.event_cost(row, price), 0.00071)

    def test_synthetic_and_incomplete_claude_lines_are_skipped(self):
        source = self.root / "projects" / "p" / "s.jsonl"
        synthetic = claude_line("m", "r", 1)
        synthetic["message"]["model"] = "<synthetic>"
        partial = claude_line("m2", "r2", 1)
        del partial["message"]["usage"]["cache_read_input_tokens"]
        write_lines(source, [synthetic, partial, {"type": "user"}])
        with observatory.open_db(self.root / "db.sqlite3") as db:
            self.assertEqual(observatory.import_claude(source, db, "m"), (0, 0))

    def test_read_jsonl_tolerates_in_progress_last_line(self):
        path = self.root / "file.jsonl"
        path.write_text('{"a": 1}\n\n{"b": 2}\n{"trunc', encoding="utf-8")
        self.assertEqual([n for n, _ in observatory.read_jsonl(path)], [1, 3])
        with self.assertRaises(ValueError):
            list(observatory.read_jsonl(path, strict=True))


class ScanTests(TemporaryTest):
    def make_homes(self):
        codex = self.root / "codex"
        claude = self.root / "claude"
        write_lines(codex / "sessions" / "2026" / "a.jsonl", CODEX_SESSION)
        write_lines(claude / "projects" / "p" / "s.jsonl", [claude_line("m", "r", 3)])
        # Files outside the session folders must be ignored, even with similar names.
        write_lines(claude / "plugins" / "projects" / "x.jsonl", [claude_line("x", "x", 9)])
        write_lines(claude / "history.jsonl", [claude_line("h", "h", 9)])
        return codex, claude

    def scan(self, codex, claude, *extra):
        return run_cli("--db", str(self.root / "db.sqlite3"), "scan", "--machine-id", "m",
                       "--account-alias", "a", "--codex-root", str(codex),
                       "--claude-root", str(claude), *extra)

    def test_scan_reads_only_session_folders_and_skips_unchanged_files(self):
        codex, claude = self.make_homes()
        first = self.scan(codex, claude)
        self.assertIn("claude_files: 1", first)
        self.assertIn("codex_files: 1", first)
        self.assertIn("unique_usage_events_in_db: 2", first)
        second = self.scan(codex, claude)
        self.assertIn("claude_files_unchanged: 1", second)
        self.assertNotIn("claude_files:", second)
        forced = self.scan(codex, claude, "--full")
        self.assertIn("claude_files: 1", forced)
        self.assertIn("unique_usage_events_in_db: 2", forced)

    def test_compressed_archives_are_counted_when_unreadable(self):
        codex, claude = self.make_homes()
        archive = codex / "archived_sessions" / "old.jsonl.zst"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"not really zstd")
        output = self.scan(codex, claude)
        if observatory.zstd_available():
            self.assertIn("codex_files_unreadable: 1", output)
        else:
            self.assertIn("codex_compressed_skipped: 1", output)
        # The good files are still imported either way.
        self.assertIn("unique_usage_events_in_db: 2", output)


class MigrationTests(TemporaryTest):
    def test_version_zero_database_is_upgraded(self):
        db_path = self.root / "old.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.executescript(observatory.BASE_SCHEMA)
            db.execute("INSERT INTO usage_events VALUES "
                       "('claude','m','a','msg-1','2026-10-08T00:00:00Z','s','parent','x',"
                       "1,0,0,0,0,1,NULL)")
            db.commit()
        with observatory.open_db(db_path) as db:
            pass
        row = self.query(db_path, "SELECT * FROM usage_events")[0]
        self.assertIsNone(row["turn_id"])
        self.assertIn("request_id", row.keys())
        version = self.query(db_path, "PRAGMA user_version")[0][0]
        self.assertEqual(version, observatory.SCHEMA_VERSION)

    def test_newer_database_is_refused(self):
        db_path = self.root / "new.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute(f"PRAGMA user_version={observatory.SCHEMA_VERSION + 1}")
        with self.assertRaises(SystemExit):
            with observatory.open_db(db_path):
                pass

    def test_reports_require_existing_database(self):
        with self.assertRaises(SystemExit):
            run_cli("--db", str(self.root / "missing.sqlite3"), "report")
        self.assertFalse((self.root / "missing.sqlite3").exists())


class PricingTests(unittest.TestCase):
    PRICES = [
        {"source": "codex", "model": "m", "effective_from": "2026-01-01",
         "effective_to": "2026-06-01", "fresh_input_per_mtok": 1, "output_per_mtok": 1},
        {"source": "codex", "model": "m", "effective_from": "2026-06-01",
         "effective_to": None, "fresh_input_per_mtok": 2, "output_per_mtok": 2},
    ]

    def test_price_selection_respects_exclusive_end(self):
        self.assertEqual(observatory.price_for(self.PRICES, "codex", "m",
                                               "2026-05-31T23:59:59Z")["output_per_mtok"], 1)
        self.assertEqual(observatory.price_for(self.PRICES, "codex", "m",
                                               "2026-06-01T00:00:00Z")["output_per_mtok"], 2)
        self.assertIsNone(observatory.price_for(self.PRICES, "codex", "m", "2025-12-31"))
        self.assertIsNone(observatory.price_for(self.PRICES, "claude", "m", "2026-07-01"))

    def test_missing_rate_for_nonzero_category_leaves_event_unpriced(self):
        row = {field: 0 for field in observatory.TOKEN_FIELDS}
        row.update(fresh_input=10, cache_read=5)
        self.assertIsNone(observatory.event_cost(row, self.PRICES[0]))
        row["cache_read"] = 0
        self.assertAlmostEqual(observatory.event_cost(row, self.PRICES[0]), 0.00001)

    def test_percentile_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(observatory.percentile(values, 0.5), 50)
        self.assertEqual(observatory.percentile(values, 0.95), 95)
        self.assertEqual(observatory.percentile([7], 0.95), 7)


class ReportTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.db = self.root / "db.sqlite3"
        write_lines(self.root / "sessions" / "s.jsonl", CODEX_SESSION)
        with observatory.open_db(self.db) as db:
            observatory.import_codex(self.root / "sessions" / "s.jsonl", db, "laptop")
        self.prices = self.root / "prices.json"
        self.prices.write_text(json.dumps({"prices": [{
            "source": "codex", "model": "model-a", "effective_from": "2026-10-01",
            "fresh_input_per_mtok": 2, "cache_read_per_mtok": 0.2, "output_per_mtok": 10}]}))

    def test_month_report_json(self):
        data = json.loads(run_cli("--db", str(self.db), "report", "--month", "2026-10",
                                  "--prices", str(self.prices), "--format", "json"))
        self.assertEqual(data["usage"][0]["events"], 1)
        self.assertAlmostEqual(data["usage"][0]["replacement_cost_usd"], 0.00134)
        self.assertEqual(data["meters"][0]["max_percent"], 12)
        text = run_cli("--db", str(self.db), "report", "--month", "2026-10")
        self.assertIn("unavailable API replacement estimate", text)

    def test_cycle_report_full_and_partial_coverage(self):
        run_cli("--db", str(self.db), "cycle", "--source", "codex", "--account-alias",
                "personal", "--plan", "plus", "--cycle-start", "2026-10-01",
                "--cycle-end", "2026-11-01", "--fee-usd", "20")
        args = ("--db", str(self.db), "cycle-report", "--source", "codex",
                "--account-alias", "personal", "--cycle-start", "2026-10-01")
        text = run_cli(*args, "--prices", str(self.prices))
        self.assertIn("Usage records: 1; priced: 1", text)
        self.assertIn("all machines", text)
        empty = self.root / "empty.json"
        empty.write_text('{"prices": []}')
        data = json.loads(run_cli(*args, "--prices", str(empty), "--format", "json"))
        self.assertFalse(data["complete"])
        self.assertIsNone(data["replacement_to_fee"])

    def test_note_and_invalid_cycle(self):
        run_cli("--db", str(self.db), "note", "t1", "--machine-id", "laptop",
                "--outcome", "accepted")
        self.assertEqual(len(self.query(self.db, "SELECT * FROM task_notes")), 1)
        with self.assertRaises(SystemExit):
            run_cli("--db", str(self.db), "note", "t2", "--machine-id", "x", "--outcome", "meh")
        with self.assertRaises(SystemExit):
            run_cli("--db", str(self.db), "cycle", "--source", "codex", "--account-alias",
                    "a", "--plan", "p", "--cycle-start", "2026-11-01",
                    "--cycle-end", "2026-10-01", "--fee-usd", "1")


class IngestTests(TemporaryTest):
    def ingest_lines(self, *envelopes):
        path = self.root / "in.ndjson"
        write_lines(path, envelopes)
        observatory.ingest(SimpleNamespace(db=self.root / "db.sqlite3", input=path))

    def usage_data(self):
        fields = observatory.USAGE_FIELDS[:15]
        data = {field: 0 for field in fields}
        data.update(source="codex", machine_id="m", account_alias="a", event_id="e",
                    occurred_at="2026-10-01T00:00:00Z", session_id=None, turn_id=None,
                    model="x", reasoning_output=None)
        return data

    def test_version_one_export_is_still_accepted(self):
        with redirect_stdout(io.StringIO()):
            self.ingest_lines({"schema_version": 1, "kind": "usage", "data": self.usage_data()})
        self.assertEqual(len(self.query(self.root / "db.sqlite3",
                                        "SELECT * FROM usage_events")), 1)

    def test_bad_input_is_rejected(self):
        cases = [
            {"schema_version": 3, "kind": "usage", "data": self.usage_data()},
            {"schema_version": 1, "kind": "request", "data": {}},
            {"schema_version": 2, "kind": "bogus", "data": {}},
            {"schema_version": 2, "kind": "usage", "data": {"source": "codex"}},
        ]
        for envelope in cases:
            with self.subTest(envelope=envelope), self.assertRaises(SystemExit):
                self.ingest_lines(envelope)
        path = self.root / "broken.ndjson"
        path.write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            observatory.ingest(SimpleNamespace(db=self.root / "db.sqlite3", input=path))


class CliTests(unittest.TestCase):
    def test_version(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            observatory.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(__version__, output.getvalue())


if __name__ == "__main__":
    unittest.main()
