"""Exercise SyncMill explicit retry causality through a real Phoenix server and px."""

from __future__ import annotations

import argparse
import json
import re
import hashlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class E2EFailure(RuntimeError):
    pass


def _verify_analysis(path: Path, trace_id: str, *, baseline: bool, baseline_digest: str | None = None) -> None:
    from jsonschema import Draft202012Validator

    report = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/analysis-report.schema.json").read_text())
    if list(Draft202012Validator(schema).iter_errors(report)):
        raise E2EFailure("analysis report failed its public schema")
    if report["trace_id"] != trace_id or report["privacy_profile"] != "safe-v1":
        raise E2EFailure("analysis selected an unexpected trace or privacy profile")
    retries = [p for p in report["patterns"] if p["pattern_id"] == "tool-retry-failure" and p["pattern_version"] == 2]
    candidates = {f["step"]["step_id"] for f in report["primary_failures"]}
    candidates.update(s["step_id"] for s in report["propagated_failures"])
    if not report["primary_failures"]:
        raise E2EFailure("analysis lost all primary investigation candidates")
    display = [report["source_kind"], report["metrics"]["cost_currency"]]
    for finding in report["primary_failures"]:
        display.extend(s["name"] for s in [finding["step"], *finding["causal_steps"]])
    display.extend(s["name"] for s in report["propagated_failures"])
    display.extend(label for p in report["patterns"] for label in p["labels"])
    display.extend(value for item in report["metrics"]["evaluations"] for value in (item["name"], item["label"]))
    if any(value is not None and not re.fullmatch(r"[A-Za-z0-9_.:/#@-]{1,200}", value) for value in display):
        raise E2EFailure("analysis report contains unfiltered display text")
    if not retries or not any(len(p["step_ids"]) == 3 and p["step_ids"][-1] in candidates for p in retries):
        raise E2EFailure("analysis lost the retry path or failing tool candidate")
    comparison = report["comparison"]
    if baseline and (comparison is None or not any(c["after"] == "error" for c in comparison["behavior_changes"])):
        raise E2EFailure("analysis lost baseline behavior regression")
    if baseline_digest is not None and (comparison is None or comparison["baseline_digest"] != baseline_digest):
        raise E2EFailure("analysis compared an unexpected baseline")
    if not baseline and comparison is not None:
        raise E2EFailure("analysis unexpectedly included a baseline")


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode not in expected:
        stderr = result.stderr.strip() or "<empty>"
        raise E2EFailure(
            f"command {command[0]!r} exited {result.returncode}; stderr: {stderr}"
        )
    return result


def _wait_http(endpoint: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint}/health", timeout=3) as response:
                if 200 <= response.status < 300:
                    return
                last = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(1)
    raise E2EFailure(f"Phoenix did not become healthy: {last}")


def _canary(
    syncmill_python: Path,
    syncmill_dir: Path,
    output_dir: Path,
    mode: str,
    endpoint: str,
    project: str,
) -> dict:
    result = _run(
        [
            str(syncmill_python),
            str(syncmill_dir / "examples" / "explicit_retry_canary.py"),
            "--mode",
            mode,
            "--output-dir",
            str(output_dir),
            "--repo-root",
            str(syncmill_dir),
            "--exporter",
            "otlp-http",
            "--endpoint",
            endpoint,
            "--project",
            project,
        ],
        cwd=syncmill_dir,
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise E2EFailure(f"{mode} canary did not return its body-free receipt") from exc
    trace_path = Path(payload["trace_path"])
    document = json.loads(trace_path.read_text(encoding="utf-8"))
    spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    payload["trace_id"] = spans[0]["traceId"]
    return payload


def _poll_px_trace(trace_id: str, project: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run(
            [
                "px",
                "trace",
                "get",
                trace_id,
                "--project",
                project,
                "--format",
                "raw",
                "--no-progress",
                "--include-annotations",
            ],
            expected=(0, 1),
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        time.sleep(2)
    raise E2EFailure(f"Phoenix did not expose trace {trace_id} before the deadline")


def _tracegraph(*args: str, expected: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-m", "tracegraph.cli", *args],
        expected=expected,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--project", default="tracegraph-syncmill-e2e")
    parser.add_argument("--syncmill-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--syncmill-sha", required=True)
    parser.add_argument("--px-version", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    syncmill_dir = args.syncmill_dir.resolve()
    work_dir = args.work_dir.resolve()
    evidence = work_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    syncmill_python = syncmill_dir / ".venv" / "bin" / "python"
    syncmill_cli = syncmill_dir / ".venv" / "bin" / "syncmill"
    if not syncmill_python.is_file() or not syncmill_cli.is_file():
        raise E2EFailure("SyncMill virtual environment is not installed")

    actual_px = _run(["px", "--version"]).stdout.strip()
    if actual_px != args.px_version:
        raise E2EFailure(f"expected px {args.px_version}, got {actual_px}")
    _wait_http(args.endpoint)
    _run(
        [
            "px",
            "profile",
            "create",
            "tracegraph-e2e",
            "--endpoint",
            args.endpoint,
            "--project",
            args.project,
            "--activate",
        ]
    )

    baseline = _canary(
        syncmill_python,
        syncmill_dir,
        work_dir / "baseline-run",
        "baseline",
        args.endpoint,
        args.project,
    )
    retry = _canary(
        syncmill_python,
        syncmill_dir,
        work_dir / "retry-run",
        "retry-failure",
        args.endpoint,
        args.project,
    )
    baseline_export = _poll_px_trace(baseline["trace_id"], args.project)
    from tracegraph import artifact
    from tracegraph.adapters import PhoenixExportAdapter
    from tracegraph.normalize import normalize
    baseline_nt = normalize(PhoenixExportAdapter(baseline_export).ingest(baseline["trace_id"]))
    baseline_digest = "sha256:" + hashlib.sha256(artifact.dumps(baseline_nt).encode()).hexdigest()
    _poll_px_trace(retry["trace_id"], args.project)

    doctor = _tracegraph("phoenix", "doctor", "--project", args.project)
    if "READY" not in doctor.stdout:
        raise E2EFailure("tracegraph phoenix doctor did not report READY")

    auto_report = evidence / "phoenix-auto-analysis.json"
    auto = _tracegraph(
        "phoenix",
        "diagnose",
        "--project",
        args.project,
        "--json-out",
        str(auto_report),
    )
    if "selected latest failed Phoenix trace" not in auto.stdout:
        raise E2EFailure("zero-argument diagnosis did not select the latest failed trace")
    if "tool-retry-failure@v2" not in auto.stdout:
        raise E2EFailure("Phoenix diagnosis did not preserve explicit retry causality")

    _verify_analysis(auto_report, retry["trace_id"], baseline=False)

    phoenix_artifact = evidence / "phoenix-retry-normalized.json"
    explicit_report = evidence / "phoenix-baseline-analysis.json"
    explicit = _tracegraph(
        "phoenix",
        "diagnose",
        retry["trace_id"],
        "--project",
        args.project,
        "--baseline",
        baseline["trace_id"],
        "--save-artifact",
        str(phoenix_artifact),
        "--json-out",
        str(explicit_report),
    )
    if "tool-retry-failure@v2" not in explicit.stdout or "Compared with baseline" not in explicit.stdout:
        raise E2EFailure("explicit Phoenix diagnosis did not include retry v2 and baseline comparison")

    _verify_analysis(explicit_report, retry["trace_id"], baseline=True, baseline_digest=baseline_digest)

    baseline_artifact = evidence / "baseline-local.json"
    retry_artifact = evidence / "retry-local.json"
    _tracegraph(
        "ingest-otlp",
        "--file",
        baseline["trace_path"],
        "--out",
        str(baseline_artifact),
    )
    _tracegraph(
        "ingest-otlp",
        "--file",
        retry["trace_path"],
        "--out",
        str(retry_artifact),
    )
    diff = _tracegraph("diff", str(baseline_artifact), str(retry_artifact), expected=(1,))
    if "NOT IDENTICAL" not in diff.stdout:
        raise E2EFailure("baseline/retry diff did not report the expected structural change")

    candidates = evidence / "review-candidates.json"
    _tracegraph(
        "export-review-candidates",
        "tool-retry-failure",
        str(retry_artifact),
        "--out",
        str(candidates),
    )
    candidate_payload = json.loads(candidates.read_text(encoding="utf-8"))
    rows = candidate_payload.get("candidates", [])
    if len(rows) != 1 or rows[0].get("pattern_version") != 2:
        raise E2EFailure("local full-fidelity trace did not produce one retry v2 candidate")

    board_env = {
        **os.environ,
        "SYNCMILL_BOARD__ENABLED": "true",
        "SYNCMILL_BOARD__DB_PATH": str(work_dir / "board.db"),
    }
    first = _run(
        [str(syncmill_cli), "board", "import-review-candidates", str(candidates)],
        cwd=syncmill_dir,
        env=board_env,
    )
    second = _run(
        [str(syncmill_cli), "board", "import-review-candidates", str(candidates)],
        cwd=syncmill_dir,
        env=board_env,
    )
    listing = _run(
        [str(syncmill_cli), "board", "list", "--state", "review"],
        cwd=syncmill_dir,
        env=board_env,
    )
    if "imported 1" not in first.stdout or "skipped 1" not in second.stdout:
        raise E2EFailure("review-candidate import was not repeat-safe")
    if "human-required" not in listing.stdout or "tool-retry-failure@v2" not in listing.stdout:
        raise E2EFailure("review candidate was not retained as a human-required board item")

    summary = {
        "kind": "tracegraph.phoenix-syncmill-e2e",
        "phoenix_endpoint": "loopback",
        "project": args.project,
        "px_version": args.px_version,
        "syncmill_sha": args.syncmill_sha,
        "baseline_trace_id": baseline["trace_id"],
        "retry_trace_id": retry["trace_id"],
        "diagnosis": "tool-retry-failure@v2",
        "diff": "NOT IDENTICAL",
        "review_candidates": 1,
        "board_import": {"imported": 1, "skipped_on_repeat": 1, "human_required": True},
    }
    (evidence / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Phoenix + SyncMill E2E passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (E2EFailure, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Phoenix + SyncMill E2E failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
