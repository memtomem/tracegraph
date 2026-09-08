"""Check an installed wheel from outside the checkout, using only synthetic data.

Run with the Python interpreter of a fresh wheel environment (no editable install):
    /tmp/wheel-env/bin/python scripts/verify_wheel.py --extra core
The harness itself needs only the standard library. JSON schemas remain repo-vendored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class SmokeFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    # Keep validation active with python -O as well.
    if not condition:
        raise SmokeFailure(message)


def verify(extra: str) -> dict:
    repo = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "FORCE_COLOR", "PY_COLORS"}}
    env.update(TERM="dumb", NO_COLOR="1", COLUMNS="200", PYTHONNOUSERSITE="1")

    with tempfile.TemporaryDirectory(prefix="tracegraph-wheel-") as directory:
        work = Path(directory).resolve()
        require(not work.is_relative_to(repo), "smoke directory must be outside checkout")

        def run(command: list[str], expected: int = 0) -> str:
            result = subprocess.run(command, cwd=work, env=env, capture_output=True,
                                    text=True, timeout=60, check=False)
            require(result.returncode == expected,
                    f"{command[0]} exited {result.returncode}, expected {expected}: "
                    f"{result.stderr[-2000:]}")
            return result.stdout

        installed = json.loads(run([sys.executable, "-I", "-c", """
import importlib.util, json, sysconfig
from pathlib import Path
import tracegraph
print(json.dumps({
    'module': str(Path(tracegraph.__file__).resolve()),
    'purelib': sysconfig.get_path('purelib'),
    'scripts': sysconfig.get_path('scripts'),
    'ladybug': importlib.util.find_spec('ladybug') is not None,
}))
"""]))
        module = Path(installed["module"])
        require(module.is_relative_to(Path(installed["purelib"]).resolve())
                and not module.is_relative_to(repo), "tracegraph is not loaded from installed wheel")
        require(installed["ladybug"] == (extra == "cypher"), "optional dependency gate failed")
        executable = Path(installed["scripts"]) / ("tracegraph.exe" if os.name == "nt" else "tracegraph")

        def cli(*args: str, expected: int = 0) -> str:
            return run([str(executable), *args], expected)

        cli("--help")
        # The v1 fixture must retain its exact bytes: candidate identity hashes the
        # queried file, not the v2 serialization produced by migration on load.
        fixtures = repo / "tests" / "fixtures" / "review-candidates"
        source = (fixtures / "source.json").read_bytes()
        (work / "source.json").write_bytes(source)
        cli("validate", "source.json")
        cli("inspect", "source.json")
        cli("explain", "source.json", "s2")
        cli("export-review-candidates", "tool-retry-failure", "source.json", "--out", "candidates.json")
        require((work / "candidates.json").read_bytes() == (fixtures / "v1.json").read_bytes(),
                "candidate golden bytes changed")

        for name, failed in (("baseline", False), ("failure", True)):
            spans = [{"traceId": name, "spanId": "first", "name": "demo::tool",
                      "status": {"code": 1},
                      "attributes": {"openinference.span.kind": "TOOL"}}]
            if failed:
                spans.extend([
                    {"traceId": name, "spanId": "retry", "parentSpanId": "first",
                     "name": "retry:demo::tool", "status": {"code": 1},
                     "attributes": {"openinference.span.kind": "CHAIN"}},
                    {"traceId": name, "spanId": "last", "parentSpanId": "retry",
                     "name": "demo::tool", "status": {"code": 2, "message": "PRIVATE BODY"},
                     "attributes": {"openinference.span.kind": "TOOL"}},
                ])
            (work / f"{name}.otlp.json").write_text(
                json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}),
                encoding="utf-8")
            cli("ingest-otlp", "--file", f"{name}.otlp.json", "--out", f"{name}.json")
        cli("validate", "baseline.json", "failure.json")
        cli("diff", "failure.json", "failure.json")
        cli("diff", "baseline.json", "failure.json", expected=1)
        cli("analyze", "failure.json", "--baseline", "baseline.json", "--json-out", "analysis.json")
        report_text = (work / "analysis.json").read_text(encoding="utf-8")
        report = json.loads(report_text)
        require(report["schema_version"] == 2 and report["privacy_profile"] == "safe-v1",
                "analysis contract changed")
        require(report["trace_id"] == "failure" and report["error_count"] == 1,
                "wrong trace or missing failure")
        require([item["step"]["step_id"] for item in report["primary_failures"]] == ["last"],
                "wrong failure candidate")
        require(any(item["pattern_id"] == "tool-retry-failure" and item["pattern_version"] == 2
                    and item["step_ids"] == ["first", "retry", "last"] for item in report["patterns"]),
                "missing explicit retry")
        baseline_digest = "sha256:" + hashlib.sha256((work / "baseline.json").read_bytes()).hexdigest()
        require(report["comparison"]["baseline_digest"] == baseline_digest,
                "wrong baseline digest")
        require(not report["comparison"]["topology_identical"]
                and any(item["after"] == "error" for item in report["comparison"]["behavior_changes"]),
                "baseline regression was lost")
        require("PRIVATE BODY" not in report_text, "raw error leaked into report")
        memory_matches = cli("query", "tool-retry-failure", "--explain", "failure.json")
        require("failure:" in memory_matches, "query lost the matching trace")
        cli("query", "tool-retry-failure", "baseline.json", expected=1)
        if extra == "cypher":
            require(cli("query", "tool-retry-failure", "--backend", "ladybug", "--explain", "failure.json")
                    == memory_matches, "backend match order or explanation differs")
            run([sys.executable, "-I", "-c", """
from tracegraph import artifact
from tracegraph.store.ladybug import LadybugStore
nt = artifact.load('failure.json')
with LadybugStore.from_trace(nt) as store:
    if artifact.dumps(store.trace()) != artifact.dumps(nt):
        raise RuntimeError('Ladybug artifact round-trip changed bytes')
"""])
        require((work / "source.json").read_bytes() == source, "source fixture was modified")
        return {"result": "PASS", "extra": extra, "module": str(module),
                "candidate_golden": "byte-identical", "baseline_digest": baseline_digest,
                "backend_parity": "PASS" if extra == "cypher" else "NOT_RUN"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra", choices=("core", "cypher"), required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.extra), sort_keys=True))
    except (SmokeFailure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Wheel smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
