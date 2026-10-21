"""`explain`, `query --explain` and `inspect` say when a causal parent was chosen heuristically (#140).

The capture record says such a choice happened somewhere in the trace, not which edge, so the
caveat is trace-wide. The fan-in line never calls the chain's causes real or tells the reader to
trust it, with or without the codes (#143): an adapter may reconstruct a cause unmarked.
"""

import re

import pytest
from test_heuristic_parent_disclosure import _by_timing, _fallback_only, _plain, _span, _trace
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.adapters import OTLPSpanAdapter
from tracegraph.analysis.diagnose import HEURISTIC_PARENT_FIDELITY_WARNING
from tracegraph.cli import app
from tracegraph.normalize import normalize

runner = CliRunner()
CAVEAT = f"⚠ {HEURISTIC_PARENT_FIDELITY_WARNING}"
EXPLAIN_LOSSY = (
    "Some steps had more than one recorded cause; the single-parent tree view drops some of "
    "them, and this raw chain lists every recorded ancestor."
)
QUERY_LOSSY = (
    "⚠ some steps in this chain had more than one recorded cause — the single-parent tree drops "
    "some of them; this raw chain lists every recorded ancestor."
)
OLD_EXPLAIN_LOSSY = (
    "Some steps had multiple real causes; the single-parent tree view is lossy for them — trust "
    "this raw chain, not the tree."
)
OLD_QUERY_LOSSY = "⚠ some steps in this match had multiple real causes — trust this raw chain, not the tree."
T0 = 1_700_000_000_000_000_000


def _plain_lossy():
    """c has span parent r and a link to a: two declared causes, no heuristic choice. Trace id
    `u`, so it can share a query with the `t` fixtures."""
    spans = [
        _span("r", T0, parent=None),
        _span("a", T0 + 100),
        _span("c", T0 + 200, error=True, links=["a"]),
    ]
    for span in spans:
        span["traceId"] = "u"
        for link in span.get("links", []):
            link["traceId"] = "u"
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    return normalize(OTLPSpanAdapter(doc).ingest("u"))


def _timing_with_failed_root():
    """_by_timing with r failing too, so `query error` also matches a step with no causes."""
    return _trace([
        _span("r", T0, parent=None, error=True),
        _span("a", T0 + 100, attrs=[("graph.node.id", "loop")]),
        _span("b", T0 + 200, attrs=[("graph.node.id", "loop")]),
        _span("c", T0 + 300, attrs=[("graph.node.parent_id", "loop")], error=True, links=["r"]),
    ])


def _timing_lossy_ancestor():
    """c is the lossy step but succeeds; only d, its single-parent child, fails."""
    return _trace([
        _span("r", T0, parent=None),
        _span("a", T0 + 100, attrs=[("graph.node.id", "loop")]),
        _span("b", T0 + 200, attrs=[("graph.node.id", "loop")]),
        _span("c", T0 + 300, attrs=[("graph.node.parent_id", "loop")], links=["r"]),
        _span("d", T0 + 400, parent="c", error=True),
    ])


def _save(tmp_path, name, nt):
    path = tmp_path / f"{name}.json"
    artifact.save(nt, path)
    return str(path)


def _run(*args):
    result = runner.invoke(app, list(args))
    return result, " ".join(result.output.split())


def _backend(name):
    if name == "ladybug":
        pytest.importorskip("ladybug", reason="requires the [cypher] extra")
    return name


def _query_blocks(flat):
    """Each match's text, from its `<trace id>: <labels>` line up to the next match or the footer."""
    body = re.split(r" [tu]: ", flat)[1:]
    body[-1] = body[-1].split(" match(es) across ")[0]
    return body


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_explain_qualifies_a_chain_in_a_trace_with_a_timed_parent(tmp_path, backend):
    path = _save(tmp_path, "timing", _by_timing())
    result, flat = _run("explain", path, "c", "--backend", _backend(backend))
    assert result.exit_code == 0, result.output
    assert flat.count(CAVEAT) == 1 and flat.count(EXPLAIN_LOSSY) == 1
    assert "real causes" not in flat and "trust this raw chain" not in flat


def test_explain_discloses_a_fallback_on_a_chain_that_is_not_lossy(tmp_path):
    path = _save(tmp_path, "fallback", _fallback_only())
    result, flat = _run("explain", path, "c")
    assert result.exit_code == 0, result.output
    assert flat.count(CAVEAT) == 1
    assert "recorded cause" not in flat and "real causes" not in flat


def test_explain_of_a_root_step_shows_no_caveat(tmp_path):
    path = _save(tmp_path, "timing", _by_timing())
    result, flat = _run("explain", path, "r")
    assert result.exit_code == 0, result.output
    assert "no causes" in flat and HEURISTIC_PARENT_FIDELITY_WARNING not in flat


def test_explain_without_the_codes_says_recorded_and_no_caveat(tmp_path):
    lossy = _plain_lossy()
    assert any(s.projection_lossy for s in lossy.steps)
    result, flat = _run("explain", _save(tmp_path, "lossy", lossy), "c")
    assert result.exit_code == 0, result.output
    assert flat.count(EXPLAIN_LOSSY) == 1 and OLD_EXPLAIN_LOSSY not in flat
    assert "real causes" not in flat and HEURISTIC_PARENT_FIDELITY_WARNING not in flat
    result, flat = _run("explain", _save(tmp_path, "plain", _plain()), "c")
    assert result.exit_code == 0, result.output
    assert HEURISTIC_PARENT_FIDELITY_WARNING not in flat and "cause" not in flat


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_query_explain_qualifies_each_match_with_a_chain(tmp_path, backend):
    path = _save(tmp_path, "fallback", _fallback_only())
    result, flat = _run("query", "error", path, "--explain", "--backend", _backend(backend))
    assert result.exit_code == 0, result.output
    blocks = _query_blocks(flat)
    assert len(blocks) == 2 and all(block.count(CAVEAT) == 1 for block in blocks)
    # Neither chain is lossy, so the caveat stands alone.
    assert not any("recorded cause" in block or "real causes" in block for block in blocks)


def test_query_explain_skips_the_caveat_for_a_root_match(tmp_path):
    path = _save(tmp_path, "root", _timing_with_failed_root())
    result, flat = _run("query", "error", path, "--explain")
    assert result.exit_code == 0, result.output
    root, child = _query_blocks(flat)
    assert root.startswith("r ") and "no causes" in root and CAVEAT not in root
    assert "recorded cause" not in root and "real causes" not in root
    assert child.startswith("c ") and child.count(CAVEAT) == 1 and child.count(QUERY_LOSSY) == 1


def test_query_explain_qualifies_only_the_trace_that_carries_the_code(tmp_path):
    timing = _save(tmp_path, "timing", _by_timing())
    lossy = _save(tmp_path, "lossy", _plain_lossy())
    result, flat = _run("query", "error", timing, lossy, "--explain")
    assert result.exit_code == 0, result.output
    blocks = _query_blocks(flat)
    assert len(blocks) == 2
    qualified = [b for b in blocks if CAVEAT in b]
    plain = [b for b in blocks if CAVEAT not in b]
    assert len(qualified) == 1 and qualified[0].count(QUERY_LOSSY) == 1
    assert "real causes" not in qualified[0]
    assert len(plain) == 1 and plain[0].count(QUERY_LOSSY) == 1
    assert OLD_QUERY_LOSSY not in plain[0] and "real causes" not in plain[0]


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_query_explain_reports_fan_in_on_an_ancestor_outside_the_match(tmp_path, backend):
    nt = _timing_lossy_ancestor()
    lossy = {s.step_id for s in nt.steps if s.projection_lossy}
    assert lossy == {"c"}
    path = _save(tmp_path, "ancestor", nt)
    result, flat = _run("query", "error", path, "--explain", "--backend", _backend(backend))
    assert result.exit_code == 0, result.output
    (block,) = _query_blocks(flat)
    assert block.startswith("d ") and block.count(QUERY_LOSSY) == 1 and block.count(CAVEAT) == 1


@pytest.mark.parametrize("make", [_by_timing, _fallback_only], ids=["timing", "fallback"])
def test_inspect_discloses_a_heuristic_parent_once(tmp_path, make):
    result, flat = _run("inspect", _save(tmp_path, "t", make()))
    assert result.exit_code == 0, result.output
    assert flat.count(CAVEAT) == 1
    assert flat.endswith(CAVEAT)


def test_inspect_without_the_codes_shows_no_caveat(tmp_path):
    for name, nt in [("plain", _plain()), ("lossy", _plain_lossy())]:
        result, flat = _run("inspect", _save(tmp_path, name, nt))
        assert result.exit_code == 0, result.output
        assert HEURISTIC_PARENT_FIDELITY_WARNING not in flat
