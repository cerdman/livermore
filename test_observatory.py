import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import observatory


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ObservatoryTests(unittest.TestCase):
    def test_codex_import_is_idempotent_and_keeps_meter_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sessions" / "example.jsonl"
            write_lines(source, [
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
            ])
            db_path = root / "local.sqlite3"
            with observatory.connect(db_path) as db:
                observatory.import_codex(source, db, "laptop")
                observatory.import_codex(source, db, "laptop")
                usage = db.execute("SELECT * FROM usage_events").fetchall()
                meters = db.execute("SELECT * FROM meter_snapshots").fetchall()
                self.assertEqual((len(usage), len(meters)), (1, 1))
                self.assertEqual(usage[0][8], 300)  # fresh input
                self.assertEqual(usage[0][12], 700)  # cache read

            export_path = root / "portable.ndjson"
            observatory.export(SimpleNamespace(db=db_path, out=export_path))
            self.assertNotIn("SENSITIVE_PROMPT_CANARY", export_path.read_text())
            central = root / "central.sqlite3"
            observatory.ingest(SimpleNamespace(db=central, input=export_path))
            observatory.ingest(SimpleNamespace(db=central, input=export_path))
            with observatory.connect(central) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM meter_snapshots").fetchone()[0], 1)
                observatory.import_codex(source, db, "second-machine")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 2)
            observatory.cycle(SimpleNamespace(
                db=central, source="codex", account_alias="personal", plan="plus",
                cycle_start="2026-10-01", cycle_end="2026-11-01", fee_usd=20))
            prices = root / "prices.json"
            prices.write_text(json.dumps({"prices": [{
                "source": "codex", "model": "model-a", "effective_from": "2026-10-01",
                "fresh_input_per_mtok": 2, "cache_read_per_mtok": 0.2,
                "output_per_mtok": 10}]}))
            output = io.StringIO()
            with redirect_stdout(output):
                observatory.cycle_report(SimpleNamespace(
                    db=central, source="codex", account_alias="personal",
                    cycle_start="2026-10-01", prices=prices))
            self.assertIn("Usage records: 2; priced: 2", output.getvalue())
            self.assertIn("all machines", output.getvalue())

    def test_claude_cache_categories_and_price(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "projects" / "project" / "example.jsonl"
            first = {"type": "assistant", "sessionId": "claude-session",
                     "timestamp": "2026-10-08T12:00:00Z", "message": {
                         "id": "message-1", "model": "model-b", "usage": {
                             "input_tokens": 10, "cache_creation_input_tokens": 50,
                             "cache_read_input_tokens": 100, "output_tokens": 5,
                             "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                                "ephemeral_1h_input_tokens": 0}}}}
            final = json.loads(json.dumps(first))
            final["message"]["usage"]["output_tokens"] = 20
            write_lines(source, [first, final])
            with observatory.connect(root / "local.sqlite3") as db:
                observatory.import_claude(source, db, "desktop")
                db.row_factory = __import__("sqlite3").Row
                row = db.execute("SELECT * FROM usage_events").fetchone()
                self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 1)
                self.assertEqual((row["fresh_input"], row["cache_write_5m"],
                                  row["cache_read"], row["output"]), (10, 50, 100, 20))
                price = {"fresh_input_per_mtok": 4, "cache_write_5m_per_mtok": 5,
                         "cache_read_per_mtok": 0.2, "output_per_mtok": 20}
                self.assertAlmostEqual(observatory.event_cost(row, price), 0.00071)


if __name__ == "__main__":
    unittest.main()
