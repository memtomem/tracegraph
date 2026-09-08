"""Guard against treating crashes as successful negative CLI outcomes."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def verifier():
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_wheel.py"
    spec = importlib.util.spec_from_file_location("wheel_verifier", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("marker", ["NOT IDENTICAL", "no matches"])
@pytest.mark.parametrize("mode", ["valid", "empty", "wrong", "traceback", "stdout_traceback", "wrong_exit"])
def test_negative_outcome_needs_output_and_no_crash(verifier, marker, mode):
    # Real child exit 1 is shared by an intentional CLI result and an unhandled
    # exception. Even a marker printed before the exception must not mask it.
    script = {
        "valid": "print(marker); raise SystemExit(1)",
        "empty": "raise SystemExit(1)",
        "wrong": "print('unexpected ' + marker); raise SystemExit(1)",
        "traceback": "print(marker); raise RuntimeError('synthetic failure')",
        "stdout_traceback": "print(marker); print('Traceback (most recent call last):'); raise SystemExit(1)",
        "wrong_exit": "print(marker)",
    }[mode]
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import sys; marker=sys.argv[1]; " + script, marker],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if mode == "valid":
        assert verifier.checked_output(result, expected=1, expected_line=marker).strip() == marker
    else:
        with pytest.raises(verifier.SmokeFailure):
            verifier.checked_output(result, expected=1, expected_line=marker)


def test_nonzero_expectation_cannot_omit_output_contract(verifier):
    result = subprocess.CompletedProcess(["fake-cli"], 1, stdout="", stderr="")
    with pytest.raises(verifier.SmokeFailure, match="requires an expected output"):
        verifier.checked_output(result, expected=1)
