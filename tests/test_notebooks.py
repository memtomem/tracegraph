"""The getting-started notebook must run top to bottom against the current CLI.

The notebook carries its own contracts: every ``tg(..., expect=N)`` call asserts the exit code
it teaches, and a few cells assert the structure they demonstrate (the one failing step, the
OTLP fan-in). Executing it here is what keeps the committed tutorial from drifting away from
the CLI. Outputs are not compared byte for byte: LangGraph step ids are checkpoint ids and
change on every run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NOTEBOOK = EXAMPLES / "getting_started_ko.ipynb"
KERNEL_NAME = "tracegraph-notebook-test"


def _private_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a kernelspec bound to this interpreter, visible to this test only.

    The plain ``python3`` name resolves to any user-registered ``python3`` spec before the
    native one, which could run the notebook on another interpreter — one without langgraph,
    or with a different tracegraph installed. JUPYTER_PATH is searched first.
    """
    spec_dir = tmp_path / "jupyter" / "kernels" / KERNEL_NAME
    spec_dir.mkdir(parents=True)
    spec = {
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": KERNEL_NAME,
        "language": "python",
    }
    (spec_dir / "kernel.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jupyter"))
    return KERNEL_NAME


def _code_cells(nb: nbformat.NotebookNode) -> list[nbformat.NotebookNode]:
    cells = [cell for cell in nb.cells if cell.cell_type == "code"]
    assert cells, "the notebook has no code cells"
    return cells


def test_notebook_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nb = nbformat.read(NOTEBOOK, as_version=4)  # an in-memory copy; the file is never rewritten
    cells = _code_cells(nb)
    # nbclient skips a cell tagged `skip-execution` and tolerates an error in one tagged
    # `raises-exception`; either would let a broken cell pass unseen.
    tagged = [cell.metadata.get("tags") for cell in cells if cell.metadata.get("tags")]
    assert not tagged, f"code cells must not carry execution tags: {tagged}"
    for cell in cells:  # the committed counts must not stand in for this run's
        cell.execution_count = None
        cell.outputs = []
    client = NotebookClient(
        nb,
        kernel_name=_private_kernel(tmp_path, monkeypatch),
        timeout=120,
        force_raise_errors=True,
        resources={"metadata": {"path": str(EXAMPLES)}},
    )
    client.execute()
    assert [cell.execution_count for cell in cells] == list(range(1, len(cells) + 1))


def test_committed_notebook_was_run_top_to_bottom() -> None:
    cells = _code_cells(nbformat.read(NOTEBOOK, as_version=4))
    counts = [cell.execution_count for cell in cells]
    assert counts == list(range(1, len(counts) + 1)), (
        "commit the notebook executed in order from a fresh kernel, outputs included"
    )
    errors = [output for cell in cells for output in cell.outputs if output.output_type == "error"]
    assert not errors, "the committed notebook carries an error output"
