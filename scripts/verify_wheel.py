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


def checked_output(
    result: subprocess.CompletedProcess[str], *, expected: int = 0,
    expected_line: str | None = None,
) -> str:
    require(expected == 0 or expected_line is not None,
            "nonzero success requires an expected output line")
    require(result.returncode == expected,
            f"{result.args[0]} exited {result.returncode}, expected {expected}: "
            f"{result.stderr[-2000:]}")
    require("Traceback" not in result.stdout and "Traceback" not in result.stderr,
            "command produced a traceback")
    if expected_line is not None:
        require(expected_line in {line.strip() for line in result.stdout.splitlines()},
                f"command did not report {expected_line!r}")
    return result.stdout


def verify(extra: str, expected_version: str | None = None) -> dict:
    repo = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "FORCE_COLOR", "PY_COLORS"}}
    env.update(TERM="dumb", NO_COLOR="1", COLUMNS="200", PYTHONNOUSERSITE="1")

    with tempfile.TemporaryDirectory(prefix="tracegraph-wheel-") as directory:
        work = Path(directory).resolve()
        require(not work.is_relative_to(repo), "smoke directory must be outside checkout")

        def run(command: list[str], expected: int = 0, expected_line: str | None = None) -> str:
            result = subprocess.run(command, cwd=work, env=env, capture_output=True,
                                    text=True, timeout=60, check=False)
            return checked_output(result, expected=expected, expected_line=expected_line)

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

        def cli(*args: str, expected: int = 0, expected_line: str | None = None) -> str:
            return run([str(executable), *args], expected, expected_line)

        cli("--help")
        # The distribution is `agent-tracegraph` while the import package and command are
        # `tracegraph`. If that lookup is ever pointed at the wrong name the failure is
        # silent — importlib raises PackageNotFoundError and __init__ reports a dev version
        # for a perfectly good install — so both surfaces are checked against the tag.
        reported = cli("--version").strip()
        imported = run([sys.executable, "-I", "-c",
                        "import tracegraph; print(tracegraph.__version__)"]).strip()
        require(reported == imported, f"CLI reports {reported!r}, import reports {imported!r}")
        require(reported != "0.0.0.dev0",
                "version lookup failed: the distribution name in __init__ does not match the wheel")
        if expected_version is not None:
            require(reported == expected_version,
                    f"installed version {reported!r} is not the expected {expected_version!r}")
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
        cli("diff", "baseline.json", "failure.json", expected=1, expected_line="NOT IDENTICAL")
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
        cli("query", "tool-retry-failure", "baseline.json", expected=1, expected_line="no matches")
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
        # Native LangGraph failures, from a committed checkpoint DB. Only the `langgraph`
        # *namespace* exists here (langgraph-checkpoint ships `langgraph.checkpoint`);
        # `langgraph.types` and the graph library itself are absent, which is the real
        # deployment shape and the one where Send packets cannot be revived.
        checkpoints = repo / "tests" / "fixtures" / "langgraph" / "native-failure.sqlite"
        (work / "checkpoints.sqlite").write_bytes(checkpoints.read_bytes())
        cli("ingest", "--sqlite", "checkpoints.sqlite", "--thread", "t", "--out", "native.json")
        native = json.loads((work / "native.json").read_text(encoding="utf-8"))
        require(native["schema_version"] == 3,
                "an artifact carrying derived task steps must declare the version that reads it")
        derived = [s for s in native["trace"]["steps"] if s["source"] == "task"]
        require([s["name"] for s in derived] == ["call_tool"],
                "native failure lost its node attribution outside the checkout")
        require(derived[0]["status"] == "error" and native["trace"]["trace"]["status"] == "error",
                "native failure did not reach the trace status")
        # The same build still writes v2 for content that needs no new reader.
        require(json.loads((work / "failure.json").read_text(encoding="utf-8"))["schema_version"] == 2,
                "envelope version must follow content, not the build")

        require((work / "source.json").read_bytes() == source, "source fixture was modified")
        return {"result": "PASS", "extra": extra, "module": str(module),
                "version": reported,
                "native_failure": "attributed",
                "candidate_golden": "byte-identical", "baseline_digest": baseline_digest,
                "backend_parity": "PASS" if extra == "cypher" else "NOT_RUN"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra", choices=("core", "cypher"), required=True)
    parser.add_argument("--expected-version",
                        help="Fail unless the installed distribution reports exactly this version.")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.extra, args.expected_version), sort_keys=True))
    except (SmokeFailure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Wheel smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
