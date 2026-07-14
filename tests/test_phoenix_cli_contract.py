"""Failure-path tests for the real-Phoenix CLI contract harness."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phoenix_cli_contract.py"


def _launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "fake_px.py"
    launcher.write_text(
        """\
import os
import sys

mode = os.environ["FAKE_PX_MODE"]
if mode == "failure":
    print("SENSITIVE TRACE BODY")
    print("px could not connect", file=sys.stderr)
    raise SystemExit(17)
if mode == "invalid-json":
    print("not-json")
else:
    print("{}")
""",
        encoding="utf-8",
    )
    return launcher


def _run(tmp_path: Path, mode: str, *, optimized: bool = False) -> subprocess.CompletedProcess[str]:
    launcher = _launcher(tmp_path)
    env = {**os.environ, "FAKE_PX_MODE": mode}
    optimize = ["-O"] if optimized else []
    return subprocess.run(
        [sys.executable, *optimize, str(SCRIPT), sys.executable, str(launcher)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )


def test_launcher_failure_reports_stderr_without_leaking_stdout(tmp_path: Path) -> None:
    result = _run(tmp_path, "failure")

    assert result.returncode == 1
    assert "Phoenix CLI contract failed: launcher exited with status 17" in result.stderr
    assert "px could not connect" in result.stderr
    assert "SENSITIVE TRACE BODY" not in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_invalid_json_has_stable_diagnostic(tmp_path: Path) -> None:
    result = _run(tmp_path, "invalid-json")

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Phoenix CLI contract failed: launcher stdout was not valid JSON"
    )
    assert result.stdout == ""


def test_contract_checks_survive_python_optimized_mode(tmp_path: Path) -> None:
    result = _run(tmp_path, "invalid-shape", optimized=True)

    assert result.returncode == 1
    assert result.stderr.strip() == "Phoenix CLI contract failed: expected exactly two traces"
    assert "Traceback" not in result.stderr
