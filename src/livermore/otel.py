"""OTLP/HTTP JSON receiver for Claude Code telemetry.

Only a fixed list of numeric and identifier attributes is read from each log record.
Everything else, including any prompt, response, tool input, or request body that a
sender was configured to include, is dropped before anything is written.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import sys
import threading
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .observatory import add_request_metric, add_usage, connect, identity

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_ERROR_CHARS = 200

# Resource attributes a sender sets with OTEL_RESOURCE_ATTRIBUTES.
MACHINE_ATTRIBUTE = "livermore.machine_id"
ACCOUNT_ATTRIBUTE = "livermore.account_alias"


def any_value(value):
    """Decode an OTLP JSON AnyValue."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:  # int64 is a JSON string in OTLP/JSON
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "arrayValue" in value:
        return [any_value(item) for item in (value["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in value:
        return attributes((value["kvlistValue"] or {}).get("values"))
    return None


def attributes(items):
    result = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = any_value(item.get("value"))
    return result


def number(value):
    """A nonnegative number from an attribute that may arrive as a string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value >= 0 and value == value:
        return value
    return None


def tokens(value):
    parsed = number(value)
    return int(parsed) if parsed is not None else 0


def text(value, limit=None):
    if value is None or isinstance(value, (dict, list)):
        return None
    value = str(value)
    return value[:limit] if limit else value


def _nanos_to_iso(nanos):
    try:
        seconds = int(nanos) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def occurred_at(record, attrs):
    stamp = attrs.get("event.timestamp")
    if isinstance(stamp, str) and len(stamp) >= 10:
        return stamp
    return (_nanos_to_iso(record.get("timeUnixNano"))
            or _nanos_to_iso(record.get("observedTimeUnixNano")))


def event_name(record, attrs):
    name = record.get("eventName") or attrs.get("event.name")
    if not name:
        body = any_value(record.get("body"))
        name = body if isinstance(body, str) else None
    if not isinstance(name, str):
        return None
    for prefix in ("claude_code.", "codex."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def source_for(resource, record, attrs):
    service = resource.get("service.name")
    raw = record.get("eventName") or attrs.get("event.name") or any_value(record.get("body"))
    if service == "claude-code" or (isinstance(raw, str) and raw.startswith("claude_code.")):
        return "claude"
    return None


def _usage_row(source, machine_id, account_alias, attrs, timestamp):
    request_id = text(attrs.get("request_id"))
    write_total = tokens(attrs.get("cache_creation_tokens"))
    write_5m = tokens(attrs.get("cache_creation_5m_tokens"))
    write_1h = tokens(attrs.get("cache_creation_1h_tokens"))
    return {
        "source": source, "machine_id": machine_id, "account_alias": account_alias,
        "event_id": request_id, "occurred_at": timestamp,
        "session_id": text(attrs.get("session.id")), "turn_id": text(attrs.get("prompt.id")),
        "model": text(attrs.get("model")),
        "fresh_input": tokens(attrs.get("input_tokens")),
        "cache_write_5m": write_5m, "cache_write_1h": write_1h,
        "cache_write_unknown": max(0, write_total - write_5m - write_1h),
        "cache_read": tokens(attrs.get("cache_read_tokens")),
        "output": tokens(attrs.get("output_tokens")), "reasoning_output": None,
        "request_id": request_id, "provenance": "otel",
    }


def _metric_row(source, machine_id, account_alias, name, attrs, timestamp):
    request_id = text(attrs.get("request_id"))
    session_id = text(attrs.get("session.id"))
    if name == "api_request" and request_id:
        metric_id = identity(name, request_id)
    else:
        metric_id = identity(name, request_id, session_id, timestamp,
                             attrs.get("event.sequence"), attrs.get("attempt"))
    status = number(attrs.get("status_code"))
    attempt = number(attrs.get("attempt"))
    return {
        "source": source, "machine_id": machine_id, "account_alias": account_alias,
        "metric_id": metric_id, "occurred_at": timestamp, "session_id": session_id,
        "request_id": request_id, "event_name": name, "model": text(attrs.get("model")),
        "duration_ms": number(attrs.get("duration_ms")),
        "ttft_ms": number(attrs.get("ttft_ms")),
        "status_code": int(status) if status is not None else None,
        "attempt": int(attempt) if attempt is not None else None,
        "error": text(attrs.get("error"), MAX_ERROR_CHARS) if name == "api_error" else None,
        "provider_cost_usd": number(attrs.get("cost_usd")),
    }


def ingest_logs(db, payload, *, machine_id=None, account_alias=None):
    """Store usage and request metrics from an ExportLogsServiceRequest. Returns counts."""
    stats = {"usage": 0, "requests": 0, "ignored": 0, "rejected": 0}
    reasons = set()
    if not isinstance(payload, dict):
        raise ValueError("OTLP logs payload must be a JSON object")
    for resource_logs in payload.get("resourceLogs") or []:
        resource = attributes(((resource_logs or {}).get("resource") or {}).get("attributes"))
        machine = text(resource.get(MACHINE_ATTRIBUTE)) or machine_id
        account = text(resource.get(ACCOUNT_ATTRIBUTE)) or account_alias
        for scope_logs in (resource_logs or {}).get("scopeLogs") or []:
            for record in (scope_logs or {}).get("logRecords") or []:
                if not isinstance(record, dict):
                    stats["rejected"] += 1
                    continue
                attrs = attributes(record.get("attributes"))
                name = event_name(record, attrs)
                source = source_for(resource, record, attrs)
                if source is None or name not in {"api_request", "api_error"}:
                    stats["ignored"] += 1
                    continue
                if not machine or not account:
                    stats["rejected"] += 1
                    reasons.add(f"set {MACHINE_ATTRIBUTE} and {ACCOUNT_ATTRIBUTE} in "
                                "OTEL_RESOURCE_ATTRIBUTES, or start serve with "
                                "--machine-id and --account-alias")
                    continue
                timestamp = occurred_at(record, attrs)
                if not timestamp:
                    stats["rejected"] += 1
                    reasons.add("log record has no timestamp")
                    continue
                add_request_metric(db, _metric_row(source, machine, account, name, attrs,
                                                   timestamp))
                stats["requests"] += 1
                if name == "api_request":
                    if not text(attrs.get("request_id")):
                        # Without the provider's request ID this cannot be matched to the
                        # transcript copy of the same request, so it would double-count.
                        reasons.add("api_request without request_id: usage not recorded "
                                    "(timing kept); upgrade Claude Code")
                        continue
                    add_usage(db, _usage_row(source, machine, account, attrs, timestamp))
                    stats["usage"] += 1
    stats["messages"] = sorted(reasons)
    return stats


def decode_body(body, encoding):
    encoding = (encoding or "identity").strip().lower()
    if encoding in {"", "identity"}:
        return body
    if encoding not in {"gzip", "deflate"}:
        raise ValueError(f"unsupported Content-Encoding: {encoding}")
    wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
    decompressor = zlib.decompressobj(wbits)
    data = decompressor.decompress(body, MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES or decompressor.unconsumed_tail:
        raise OverflowError("decompressed body too large")
    return data


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path, *, machine_id=None, account_alias=None,
                 token=None, verbose=False):
        self.db = connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self.machine_id = machine_id
        self.account_alias = account_alias
        self.token = token
        self.verbose = verbose
        self.warned = set()
        super().__init__(address, Handler)

    def server_close(self):
        super().server_close()
        self.db.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "livermore-otlp"
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 - signature from BaseHTTPRequestHandler
        if self.server.verbose:
            super().log_message(format, *args)

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.server.token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())

    def _read_body(self):
        # The JavaScript OTLP exporter (Claude Code) sends chunked bodies.
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline(1024).split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(1024).strip():
                        pass  # trailers, up to the blank line
                    return b"".join(chunks)
                total += size
                if size < 0 or total > MAX_BODY_BYTES:
                    raise OverflowError
                chunks.append(self.rfile.read(size))
                self.rfile.readline(1024)
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            raise OverflowError
        return self.rfile.read(length)

    def _warn_once(self, message):
        if message not in self.server.warned:
            self.server.warned.add(message)
            print(f"livermore: {message}", file=sys.stderr, flush=True)

    def do_GET(self):  # noqa: N802
        if self.path in {"/healthz", "/health"}:
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/v1/logs", "/v1/metrics", "/v1/traces"}:
            self._reply(404, {"message": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"message": "missing or wrong bearer token"})
            return
        try:
            body = self._read_body()
        except OverflowError:
            self._reply(413, {"message": "body too large"})
            return
        except ValueError:
            self._reply(400, {"message": "malformed request body"})
            return
        if path != "/v1/logs":
            # Accept so a sender with metrics or traces on does not retry forever.
            self._warn_once(f"ignoring {path}; only /v1/logs is stored")
            self._reply(200, {})
            return
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "json" not in content_type:
            self._warn_once("rejected a non-JSON OTLP request; set "
                            "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json")
            self._reply(415, {"message": "only OTLP/HTTP JSON is supported; "
                                         "set the exporter protocol to http/json"})
            return
        try:
            payload = json.loads(decode_body(body, self.headers.get("Content-Encoding")))
        except OverflowError:
            self._reply(413, {"message": "body too large"})
            return
        except (ValueError, zlib.error) as error:
            self._reply(400, {"message": f"invalid OTLP JSON: {error}"})
            return
        try:
            with self.server.lock, self.server.db:
                stats = ingest_logs(self.server.db, payload,
                                    machine_id=self.server.machine_id,
                                    account_alias=self.server.account_alias)
        except ValueError as error:
            self._reply(400, {"message": str(error)})
            return
        for message in stats["messages"]:
            self._warn_once(message)
        if self.server.verbose:
            print(f"livermore: stored {stats['usage']} usage, {stats['requests']} request "
                  f"metrics; ignored {stats['ignored']}, rejected {stats['rejected']}",
                  file=sys.stderr, flush=True)
        response = {}
        if stats["rejected"]:
            response["partialSuccess"] = {"rejectedLogRecords": str(stats["rejected"]),
                                          "errorMessage": "; ".join(stats["messages"])}
        self._reply(200, response)


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(args):
    token = args.token or os.environ.get("LIVERMORE_TOKEN") or None
    server = Receiver((args.host, args.port), args.db, machine_id=args.machine_id,
                      account_alias=args.account_alias, token=token, verbose=args.verbose)
    if not token and not _is_loopback(args.host):
        print("livermore: warning: listening beyond localhost without --token",
              file=sys.stderr)
    host, port = server.server_address[:2]
    print(f"livermore: receiving OTLP/HTTP JSON logs at http://{host}:{port}/v1/logs "
          f"into {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
