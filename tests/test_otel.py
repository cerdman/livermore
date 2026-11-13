import gzip
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from livermore import observatory, otel

CANARY = "SENSITIVE_PROMPT_CANARY"


def kv(key, value):
    if isinstance(value, bool):
        wrapped = {"boolValue": value}
    elif isinstance(value, int):
        wrapped = {"intValue": str(value)}
    elif isinstance(value, float):
        wrapped = {"doubleValue": value}
    else:
        wrapped = {"stringValue": value}
    return {"key": key, "value": wrapped}


def record(name, attrs, nanos="1791460800000000000"):
    """A log record in the shape Claude Code 2.1 sends (event name in body and attribute)."""
    attrs = {"session.id": "claude-session", "event.name": name,
             "event.timestamp": "2026-10-08T12:00:00.000Z", "user.email": CANARY, **attrs}
    return {"timeUnixNano": nanos, "observedTimeUnixNano": nanos,
            "body": {"stringValue": f"claude_code.{name}"},
            "attributes": [kv(key, value) for key, value in attrs.items()]}


def api_request(request_id="req-1", **overrides):
    attrs = {"prompt.id": "prompt-1", "model": "model-b", "input_tokens": 10,
             "output_tokens": 20, "cache_read_tokens": 100, "cache_creation_tokens": 50,
             "cache_creation_5m_tokens": 50, "cache_creation_1h_tokens": 0,
             "cost_usd": 0.001, "duration_ms": 1500, "ttft_ms": 900,
             "request_id": request_id, **overrides}
    return record("api_request", {k: v for k, v in attrs.items() if v is not None})


def payload(*records, machine="desktop", account="personal", service="claude-code"):
    resource = [kv("service.name", service), kv("service.version", "2.1.321")]
    if machine:
        resource.append(kv(otel.MACHINE_ATTRIBUTE, machine))
    if account:
        resource.append(kv(otel.ACCOUNT_ATTRIBUTE, account))
    return {"resourceLogs": [{"resource": {"attributes": resource}, "scopeLogs": [
        {"scope": {"name": "com.anthropic.claude_code.events"},
         "logRecords": list(records)}]}]}


def transcript_line(message_id="message-1", request_id="req-1"):
    return {"type": "assistant", "sessionId": "claude-session", "requestId": request_id,
            "timestamp": "2026-10-08T12:00:01Z", "message": {
                "id": message_id, "model": "model-b", "usage": {
                    "input_tokens": 10, "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 100, "output_tokens": 20,
                    "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                       "ephemeral_1h_input_tokens": 0}}}}


class OtelTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.db_path = self.root / "db.sqlite3"
        # A cleanup, not tearDown, so it runs after servers registered later are closed.
        self.addCleanup(self._directory.cleanup)

    def ingest(self, data, **defaults):
        with observatory.open_db(self.db_path) as db:
            return otel.ingest_logs(db, data, **defaults)

    def rows(self, table):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def import_transcript(self, *lines, machine="desktop"):
        path = self.root / "projects" / "p" / "s.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        with observatory.open_db(self.db_path) as db:
            observatory.import_claude(path, db, machine, "personal")


class IngestLogsTests(OtelTestCase):
    def test_api_request_becomes_usage_and_metric(self):
        user_prompt = record("user_prompt", {"prompt": CANARY, "prompt_length": 30})
        stats = self.ingest(payload(user_prompt, api_request()))
        self.assertEqual((stats["usage"], stats["requests"], stats["ignored"]), (1, 1, 1))
        usage = self.rows("usage_events")
        self.assertEqual(len(usage), 1)
        row = usage[0]
        self.assertEqual((row["machine_id"], row["account_alias"], row["request_id"],
                          row["turn_id"], row["provenance"]),
                         ("desktop", "personal", "req-1", "prompt-1", "otel"))
        self.assertEqual((row["fresh_input"], row["cache_write_5m"], row["cache_read"],
                          row["output"]), (10, 50, 100, 20))
        metric = self.rows("request_metrics")[0]
        self.assertEqual((metric["duration_ms"], metric["ttft_ms"],
                          metric["provider_cost_usd"]), (1500, 900, 0.001))

    def test_content_and_identity_attributes_are_never_stored(self):
        self.ingest(payload(record("user_prompt", {"prompt": CANARY}),
                            api_request(), record("api_error", {"error": "rate limited",
                                                                "status_code": 429})))
        with closing(sqlite3.connect(self.db_path)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(CANARY, dump)

    def test_numbers_sent_as_strings_are_parsed(self):
        # Claude Code 2.0 sends every attribute as a string.
        stringly = api_request()
        for item in stringly["attributes"]:
            value = item["value"]
            if "intValue" in value or "doubleValue" in value:
                item["value"] = {"stringValue": str(next(iter(value.values())))}
        self.ingest(payload(stringly))
        self.assertEqual(self.rows("usage_events")[0]["cache_read"], 100)

    def test_retransmission_is_idempotent(self):
        self.ingest(payload(api_request()))
        self.ingest(payload(api_request()))
        self.assertEqual(len(self.rows("usage_events")), 1)
        self.assertEqual(len(self.rows("request_metrics")), 1)

    def test_otel_then_transcript_counts_once(self):
        self.ingest(payload(api_request()))
        self.import_transcript(transcript_line())
        usage = self.rows("usage_events")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["provenance"], "otel,transcript")
        self.assertEqual(usage[0]["turn_id"], "prompt-1")  # kept from OTel

    def test_transcript_then_otel_counts_once_even_across_machine_ids(self):
        self.import_transcript(transcript_line(), machine="desktop")
        self.ingest(payload(api_request(), machine="other-name"))
        usage = self.rows("usage_events")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["machine_id"], "desktop")
        self.assertEqual(usage[0]["provenance"], "otel,transcript")

    def test_old_row_without_request_id_is_merged_on_rescan(self):
        # A version-0 row (no request_id), then OTel, then a rescan of the transcript.
        self.import_transcript(transcript_line(request_id=None))
        self.ingest(payload(api_request()))
        self.assertEqual(len(self.rows("usage_events")), 2)
        self.import_transcript(transcript_line())
        self.assertEqual(len(self.rows("usage_events")), 1)

    def test_api_request_without_request_id_keeps_timing_only(self):
        stats = self.ingest(payload(api_request(request_id=None)))
        self.assertEqual((stats["usage"], stats["requests"]), (0, 1))
        self.assertTrue(any("request_id" in message for message in stats["messages"]))
        self.assertEqual(self.rows("usage_events"), [])

    def test_api_error_is_a_metric(self):
        self.ingest(payload(record("api_error", {"error": "x" * 500, "status_code": 529,
                                                 "attempt": 2, "duration_ms": 30})))
        metric = self.rows("request_metrics")[0]
        self.assertEqual((metric["event_name"], metric["status_code"], metric["attempt"]),
                         ("api_error", 529, 2))
        self.assertEqual(len(metric["error"]), otel.MAX_ERROR_CHARS)

    def test_missing_machine_is_rejected_unless_defaulted(self):
        stats = self.ingest(payload(api_request(), machine=None, account=None))
        self.assertEqual(stats["rejected"], 1)
        stats = self.ingest(payload(api_request(), machine=None, account=None),
                            machine_id="fallback", account_alias="personal")
        self.assertEqual(stats["usage"], 1)
        self.assertEqual(self.rows("usage_events")[0]["machine_id"], "fallback")

    def test_other_services_are_ignored(self):
        stats = self.ingest(payload(api_request(), service="codex_cli_rs"))
        self.assertEqual((stats["ignored"], stats["usage"]), (1, 0))

    def test_timestamp_falls_back_to_unix_nanos(self):
        request = api_request()
        request["attributes"] = [item for item in request["attributes"]
                                 if item["key"] != "event.timestamp"]
        self.ingest(payload(request))
        self.assertEqual(self.rows("usage_events")[0]["occurred_at"],
                         "2026-10-08T12:00:00.000Z")

    def test_report_shows_latency(self):
        self.ingest(payload(api_request()))
        with observatory.open_db(self.db_path) as db:
            data = observatory.build_report(db, "2026-10", [])
        self.assertEqual(data["requests"][0]["p50_ms"], 1500)

    def test_export_round_trip_keeps_request_metrics(self):
        self.ingest(payload(api_request()))
        out = self.root / "out.ndjson"
        central = self.root / "central.sqlite3"
        with redirect_stdout(io.StringIO()):
            observatory.main(["--db", str(self.db_path), "export", "--out", str(out)])
            observatory.main(["--db", str(central), "ingest", "--input", str(out)])
        with closing(sqlite3.connect(central)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM request_metrics").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT request_id FROM usage_events").fetchone()[0],
                             "req-1")


class ServerTests(OtelTestCase):
    def start(self, **options):
        server = otel.Receiver(("127.0.0.1", 0), self.db_path, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        return server.server_address[1]

    def post(self, port, path, body, headers=None, chunked=False):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            if chunked:
                connection.request("POST", path, body=iter([body[:10], body[10:]]),
                                   headers=headers, encode_chunked=True)
            else:
                connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_logs_over_http_plain_chunked_and_gzip(self):
        port = self.start()
        body = json.dumps(payload(api_request("req-1"))).encode()
        self.assertEqual(self.post(port, "/v1/logs", body)[0], 200)
        body = json.dumps(payload(api_request("req-2"))).encode()
        self.assertEqual(self.post(port, "/v1/logs", body, chunked=True)[0], 200)
        body = gzip.compress(json.dumps(payload(api_request("req-3"))).encode())
        status, _ = self.post(port, "/v1/logs", body, {"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.rows("usage_events")), 3)

    def test_rejections(self):
        port = self.start(token="secret")
        body = json.dumps(payload(api_request())).encode()
        auth = {"Authorization": "Bearer secret"}
        self.assertEqual(self.post(port, "/v1/logs", body)[0], 401)
        self.assertEqual(self.post(port, "/v1/logs", body,
                                   {"Authorization": "Bearer wrong"})[0], 401)
        with redirect_stderr(io.StringIO()):
            status, _ = self.post(port, "/v1/logs", b"\x0a\x00",
                                  {**auth, "Content-Type": "application/x-protobuf"})
            self.assertEqual(status, 415)
            self.assertEqual(self.post(port, "/v1/logs", b"{nope", auth)[0], 400)
            self.assertEqual(self.post(port, "/v1/other", body, auth)[0], 404)
            self.assertEqual(self.post(port, "/v1/metrics", body, auth)[0], 200)
        self.assertEqual(self.rows("usage_events"), [])
        status, response = self.post(port, "/v1/logs", body, auth)
        self.assertEqual((status, response), (200, {}))

    def test_partial_success_reports_rejected_records(self):
        port = self.start()
        body = json.dumps(payload(api_request(), machine=None)).encode()
        with redirect_stderr(io.StringIO()):
            status, response = self.post(port, "/v1/logs", body)
        self.assertEqual(status, 200)
        self.assertEqual(response["partialSuccess"]["rejectedLogRecords"], "1")

    def test_healthz(self):
        port = self.start()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/healthz")
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
