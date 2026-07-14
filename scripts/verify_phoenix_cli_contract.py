"""Exercise the pinned Phoenix CLI against a tiny read-only HTTP contract server.

The normal test suite uses a fake ``px`` executable to keep core CI hermetic. This script is
for the separate Node contract job: it runs the real published CLI and verifies the exact
``trace list --format raw`` properties that one-command diagnosis relies on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse


TRACE_NEWEST = "a" * 32
TRACE_FAILED = "b" * 32


class _ContractViolation(RuntimeError):
    """A stable, user-facing Phoenix CLI contract failure."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _ContractViolation(message)


def _span(trace_id: str, span_id: str, *, start: str, status: str) -> dict:
    return {
        "context": {"trace_id": trace_id, "span_id": span_id},
        "name": f"contract-{status.lower()}",
        "span_kind": "CHAIN",
        "parent_id": None,
        "start_time": start,
        "end_time": start,
        "status_code": status,
        "status_message": "",
        "attributes": {},
        "events": [],
    }


SPANS = [
    _span(TRACE_FAILED, "2" * 16, start="2026-07-14T00:00:00.000Z", status="ERROR"),
    _span(TRACE_NEWEST, "1" * 16, start="2026-07-14T00:01:00.000Z", status="OK"),
]


class _PhoenixHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        self.requests.append(("GET", path))
        if path == "/v1/projects/a1/spans":
            self._json({"data": SPANS, "next_cursor": None})
            return
        if path in {
            "/v1/projects/a1/trace_annotations",
            "/v1/projects/a1/span_annotations",
        }:
            self._json({"data": [], "next_cursor": None})
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("pass the pinned px launcher, e.g. npx --yes @arizeai/phoenix-cli@1.8.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PhoenixHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    command = [
        *argv,
        "trace",
        "list",
        "--endpoint",
        endpoint,
        "--project",
        "a1",
        "--limit",
        "20",
        "--include-annotations",
        "--format",
        "raw",
        "--no-progress",
    ]
    try:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() or "<empty>"
            raise _ContractViolation(
                f"launcher exited with status {exc.returncode}; stderr: {stderr}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise _ContractViolation("launcher timed out after 60 seconds") from exc
        except OSError as exc:
            raise _ContractViolation(f"could not start launcher: {exc}") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    try:
        traces = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _ContractViolation("launcher stdout was not valid JSON") from exc
    _require(isinstance(traces, list) and len(traces) == 2, "expected exactly two traces")
    _require(
        [trace.get("traceId") for trace in traces] == [TRACE_NEWEST, TRACE_FAILED],
        "trace ordering or traceId schema changed",
    )
    _require(
        [trace.get("status") for trace in traces] == ["OK", "ERROR"],
        "trace status schema changed",
    )
    _require(
        all(isinstance(trace.get("spans"), list) and trace["spans"] for trace in traces),
        "trace spans are missing or empty",
    )
    _require(
        {path for method, path in _PhoenixHandler.requests if method == "GET"}
        == {
            "/v1/projects/a1/spans",
            "/v1/projects/a1/trace_annotations",
            "/v1/projects/a1/span_annotations",
        },
        "Phoenix API request contract changed",
    )
    print("Phoenix CLI trace-list schema contract passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except _ContractViolation as exc:
        print(f"Phoenix CLI contract failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
