"""Golden contract tests for the syncmill trace fixtures (P0/T1).

These pin the consumer side of the syncmill -> tracegraph contract with ZERO
adapter changes: every fixture must ingest through the stock ``OTLPSpanAdapter``
and produce an exact, byte-stable causal graph. The invariants under test are
the ones the ecosystem plan calls load-bearing: concurrent siblings share no
edges (completion order is never causality), a route fallback is caused by the
prior failure, and a compete ``select`` is a genuine multi-parent fan-in
(``projection_lossy``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from syncmill_otlp_traces import CONSUMED_KEYS, GENERATORS, SYNCMILL_ALLOWLIST

# Redaction contract: no bodies/secrets/local paths in any trace attribute value,
# span name, or status message. The allowlist test already bounds attribute KEYS;
# this scan bounds string VALUES anywhere in the document.
_FORBIDDEN_SUBSTRINGS = ("prompt", "patch", "stdout", "stderr")
_ABS_PATH = re.compile(r"^(/|[A-Za-z]:[\\/])")
_CREDENTIAL_PATTERNS = [
    re.compile(r"://[^/\s]+:[^/\s]+@"),
    re.compile(r"password\s*=", re.IGNORECASE),
    re.compile(r"token\s*=", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
]


def _string_values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _string_values(item)
    elif isinstance(node, str):
        yield node


def _scan(doc) -> list[str]:
    violations = []
    for value in _string_values(doc):
        low = value.lower()
        for marker in _FORBIDDEN_SUBSTRINGS:
            if marker in low:
                violations.append(f"body:{value}")
        if _ABS_PATH.match(value):
            violations.append(f"abs-path:{value}")
        for pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(value):
                violations.append(f"cred:{value}")
    return violations

from tracegraph.adapters import OTLPSpanAdapter
from tracegraph.model import StepKind, StepStatus
from tracegraph.normalize import normalize

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "syncmill"

RUN = "aa00000000000001"
A1, A2, A3 = "bb00000000000001", "bb00000000000002", "bb00000000000003"
SELECT = "cc00000000000001"
G1, G2 = "dd00000000000001", "dd00000000000002"

# name -> exact raw CAUSED_BY edge set (src=effect, dst=cause)
EXPECTED_EDGES = {
    "route-success": {(A1, RUN)},
    "route-fallback": {(A1, RUN), (A2, A1)},
    "compete-winner": {
        (A1, RUN),
        (A2, RUN),
        (A3, RUN),
        (SELECT, A1),  # graph parent: the winner
        (SELECT, A2),  # links: the examined losers
        (SELECT, A3),
    },
    "compete-timeout": {
        (A1, RUN),
        (A2, RUN),
        (A3, RUN),
        (SELECT, A1),
        (SELECT, A2),  # only the examined completer — never the timed-out sibling
    },
    "compete-gate-reject": {
        (A1, RUN),
        (A2, RUN),
        (G1, A1),  # each gate is caused by the attempt it judged
        (G2, A2),
        (SELECT, A2),  # graph parent: the gate-passing candidate
        (SELECT, A1),  # link: the examined, gate-rejected one
    },
    "pipeline-success": {
        ("bb00000000000001", RUN),
        ("bb00000000000002", "bb00000000000001"),
        ("bb00000000000003", "bb00000000000002"),
    },
    "council-success": {
        ("bb00000000000001", RUN), ("bb00000000000002", RUN),
        ("bb00000000000003", "bb00000000000001"),
        ("bb00000000000004", "bb00000000000002"),
        ("bb00000000000005", "bb00000000000003"),
        ("bb00000000000005", "bb00000000000004"),
        ("bb00000000000006", "bb00000000000004"),
        ("bb00000000000006", "bb00000000000003"),
        (SELECT, "bb00000000000005"), (SELECT, "bb00000000000006"),
    },
    "decompose-success": {
        ("bb00000000000001", RUN),
        ("bb00000000000002", "bb00000000000001"),
        ("bb00000000000003", "bb00000000000001"),
        ("bb00000000000004", "bb00000000000001"),
        ("bb00000000000004", "bb00000000000002"),
        ("bb00000000000004", "bb00000000000003"),
        ("bb00000000000005", "bb00000000000001"),
        ("bb00000000000005", "bb00000000000002"),
        ("bb00000000000005", "bb00000000000003"),
        (SELECT, "bb00000000000004"), (SELECT, "bb00000000000005"),
    },
}

TRACE_IDS = {name: "0" * 31 + str(i + 1) for i, name in enumerate(GENERATORS)}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.otlp.json").read_text(encoding="utf-8"))


def _normalized(name: str):
    adapter = OTLPSpanAdapter(_load(name))
    return normalize(adapter.ingest(TRACE_IDS[name]))


@pytest.mark.parametrize("name", sorted(GENERATORS), ids=str)
def test_golden_matches_generator(name: str):
    """Regeneration guard: the committed golden IS the generator's output."""
    expected = json.dumps(GENERATORS[name](), indent=2) + "\n"
    assert (FIXTURES / f"{name}.otlp.json").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("name", sorted(EXPECTED_EDGES), ids=str)
def test_causal_edges_exact(name: str):
    nt = _normalized(name)
    caused = {(e.src, e.dst) for e in nt.edges if e.type.value == "CAUSED_BY"}
    assert caused == EXPECTED_EDGES[name]


@pytest.mark.parametrize("name", ["compete-winner", "compete-timeout", "compete-gate-reject"])
def test_concurrent_siblings_share_no_edges(name: str):
    """Completion order must never look causal: no edge between fan-out siblings."""
    attempts = {A1, A2, A3}
    for src, dst in EXPECTED_EDGES[name]:
        assert not (src in attempts and dst in attempts)
    caused = {(e.src, e.dst) for e in _normalized(name).edges if e.type.value == "CAUSED_BY"}
    assert not {(s, d) for s, d in caused if s in attempts and d in attempts}


@pytest.mark.parametrize(
    ("name", "siblings"),
    [
        ("council-success", {"bb00000000000001", "bb00000000000002"}),
        ("decompose-success", {"bb00000000000002", "bb00000000000003"}),
    ],
)
def test_advanced_strategy_parallel_siblings_share_no_edges(name, siblings):
    caused = {(e.src, e.dst) for e in _normalized(name).edges if e.type.value == "CAUSED_BY"}
    assert not {(s, d) for s, d in caused if s in siblings and d in siblings}


def test_route_fallback_is_caused_by_prior_failure():
    nt = _normalized("route-fallback")
    steps = nt.steps_by_id()
    assert steps[A1].status is StepStatus.ERROR
    assert steps[A1].error_msg == "exit_code=1"
    assert (A2, A1) in {(e.src, e.dst) for e in nt.edges if e.type.value == "CAUSED_BY"}


def test_compete_select_is_lossy_multi_parent():
    """select consumed >1 candidate, so the single-parent tree view must flag it."""
    steps = _normalized("compete-winner").steps_by_id()
    assert steps[SELECT].projection_lossy
    assert steps[SELECT].kind is StepKind.CHAIN


def test_compete_timeout_surfaces_bounded_error():
    steps = _normalized("compete-timeout").steps_by_id()
    assert steps[A3].status is StepStatus.ERROR
    assert steps[A3].error_msg == "timeout"


def test_gate_spans_are_tool_steps():
    steps = _normalized("compete-gate-reject").steps_by_id()
    assert steps[G1].kind is StepKind.TOOL
    assert steps[G1].status is StepStatus.ERROR
    assert steps[G1].error_msg == "gate: pytest -q failed"
    assert steps[G2].status is StepStatus.OK


@pytest.mark.parametrize("name", sorted(GENERATORS), ids=str)
def test_attribute_keys_within_allowlist(name: str):
    """Every span attribute is either a consumed key or on the syncmill.* allowlist."""
    allowed = CONSUMED_KEYS | SYNCMILL_ALLOWLIST
    doc = _load(name)
    for rs in doc["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                for attr in span["attributes"]:
                    assert attr["key"] in allowed, f"{name}: {attr['key']}"


@pytest.mark.parametrize("name", sorted(GENERATORS), ids=str)
def test_no_forbidden_content(name: str):
    """No prompts, patch bodies, secrets, or local absolute paths in any string value."""
    assert _scan(_load(name)) == [], f"{name} violates the redaction contract"


def test_scanner_catches_planted_violations():
    """Adversarial guard: the scanner must FLAG a planted body / secret / abs path in
    an attribute value (incl. uppercase and Windows paths) — else the scan is vacuous."""
    poisoned = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "attempt:codex",
                                "status": {"message": "PROMPT: reveal the system prompt"},
                                "attributes": [
                                    {"key": "syncmill.status", "value": {"stringValue": "C:\\Users\\me\\out"}},
                                    {"key": "note", "value": {"stringValue": "redis://u:p@host"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    found = _scan(poisoned)
    assert any(v.startswith("body:") for v in found)
    assert any(v.startswith("abs-path:") for v in found)
    assert any(v.startswith("cred:") for v in found)


@pytest.mark.parametrize("name", sorted(GENERATORS), ids=str)
def test_span_names_are_diff_stable(name: str):
    """AHU diff labels on step.name — names must carry no run-unique material."""
    doc = _load(name)
    for rs in doc["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                name_ = span["name"]
                assert TRACE_IDS[name] not in name_
                assert span["spanId"] not in name_
                assert "-4000-" not in name_  # no run_id uuids in structural labels
