"""Synthetic syncmill OTLP exports pinning the ecosystem trace contract (P0/T1).

syncmill's planned FileTracer emits the three attributes tracegraph already
consumes — ``openinference.span.kind``, ``graph.node.id``, ``graph.node.parent_id`` —
plus an additive ``syncmill.*`` allowlist tracegraph ignores, so ingestion needs
zero adapter changes. These generators are the single source of truth for the
golden fixtures under ``tests/fixtures/syncmill/``; the contract rules they encode
(see the ecosystem implementation design, §2.3):

* span ``name`` is a stable structural label (agent ids / phases / indices — never
  uuids or timestamps), because AHU ``diff`` labels default to ``name or kind``;
* ``parentSpanId`` is flat containment under the run root, never causality;
* ``graph.node.parent_id`` carries the ONE logical cause; span ``links`` carry the
  additional consumed causes (fan-in) — e.g. a compete ``select`` is parented on the
  winner and linked to every other candidate it examined;
* concurrent siblings fan out from a common parent and share NO edges with each
  other: completion order must never look causal;
* error states ride OTLP ``status`` with a bounded message (``timeout``,
  ``exit_code=N``, ``gate: <summary>``) — never raw agent output.

Nothing here imports tracegraph; it just emits the OTLP/JSON shape the exporter will.
"""

from __future__ import annotations

import json
from pathlib import Path

_BASE_NANO = 1_700_000_000_000_000_000

# The syncmill.* attribute allowlist (implementation design §2.4). Tests scan
# fixtures against this exact set; the FileTracer must never emit outside it.
SYNCMILL_ALLOWLIST = frozenset(
    {
        "syncmill.schema_version",
        "syncmill.run_id",
        "syncmill.strategy",
        "syncmill.phase",
        "syncmill.agent_id",
        "syncmill.attempt",
        "syncmill.round",
        "syncmill.status",
        "syncmill.exit_code",
        "syncmill.gate.passed",
        "syncmill.worktree_slot",
        "syncmill.winner",
        "syncmill.files_changed_count",
    }
)

CONSUMED_KEYS = frozenset(
    {"openinference.span.kind", "graph.node.id", "graph.node.parent_id"}
)


def _attr(key: str, value: str | int | bool) -> dict:
    if isinstance(value, bool):  # bool first — bool is an int subclass
        wrapped: dict = {"boolValue": value}
    elif isinstance(value, int):
        wrapped = {"intValue": str(value)}  # protobuf-JSON encodes int64 as string
    else:
        wrapped = {"stringValue": value}
    return {"key": key, "value": wrapped}


def _span(
    trace_id: str,
    span_id: str,
    name: str,
    kind: str,
    *,
    parent_span: str | None,
    node_id: str,
    node_parent: str | None,
    start_ns: int,
    end_ns: int,
    syncmill: dict[str, str | int | bool],
    status: dict | None = None,
    links: list[str] | None = None,
) -> dict:
    attrs = [_attr("openinference.span.kind", kind), _attr("graph.node.id", node_id)]
    if node_parent is not None:
        attrs.append(_attr("graph.node.parent_id", node_parent))
    for key, value in syncmill.items():
        attrs.append(_attr(f"syncmill.{key}", value))
    span: dict = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(_BASE_NANO + start_ns),
        "endTimeUnixNano": str(_BASE_NANO + end_ns),
        "attributes": attrs,
    }
    if parent_span is not None:
        span["parentSpanId"] = parent_span
    if status is not None:
        span["status"] = status
    if links is not None:
        span["links"] = [{"traceId": trace_id, "spanId": sid} for sid in links]
    return span


def _document(spans: list[dict]) -> dict:
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _run_span(
    trace_id: str, run_id: str, strategy: str, *, end_ns: int, status: str = "completed"
) -> dict:
    return _span(
        trace_id,
        "aa00000000000001",
        "syncmill.run",
        "CHAIN",
        parent_span=None,
        node_id="run",
        node_parent=None,
        start_ns=0,
        end_ns=end_ns,
        syncmill={
            "schema_version": 1,
            "run_id": run_id,
            "strategy": strategy,
            "agent_id": "supervisor",
            "status": status,
        },
    )


_RUN = "aa00000000000001"


def _trace_id(index: int) -> str:
    """A valid 16-byte OTLP trace id, stable beyond single-digit fixtures."""
    return f"{index:032x}"


# Single source for fixture filtering and generation. Names, not GENERATORS
# insertion order, own their trace ids so registry reordering is harmless.
TRACE_IDS = {
    "route-success": _trace_id(1),
    "route-fallback": _trace_id(2),
    "compete-winner": _trace_id(3),
    "compete-timeout": _trace_id(4),
    "compete-gate-reject": _trace_id(5),
    "pipeline-success": _trace_id(6),
    "council-success": _trace_id(7),
    "decompose-success": _trace_id(8),
}


def route_success() -> dict:
    """route: the first agent completes — one attempt caused by the run."""
    trace_id, run_id = TRACE_IDS["route-success"], "00000000-0000-4000-8000-000000000001"
    return _document(
        [
            _run_span(trace_id, run_id, "route", end_ns=60_000),
            _span(
                trace_id,
                "bb00000000000001",
                "attempt:codex",
                "AGENT",
                parent_span=_RUN,
                node_id="attempt:codex:0",
                node_parent="run",
                start_ns=1_000,
                end_ns=50_000,
                syncmill={
                    "schema_version": 1,
                    "run_id": run_id,
                    "phase": "attempt",
                    "agent_id": "codex",
                    "attempt": 0,
                    "status": "completed",
                    "exit_code": 0,
                    "winner": True,
                    "files_changed_count": 2,
                },
            ),
        ]
    )


def route_fallback() -> dict:
    """route: first agent fails, the fallback attempt is CAUSED BY that failure."""
    trace_id, run_id = TRACE_IDS["route-fallback"], "00000000-0000-4000-8000-000000000002"
    return _document(
        [
            _run_span(trace_id, run_id, "route", end_ns=120_000),
            _span(
                trace_id,
                "bb00000000000001",
                "attempt:codex",
                "AGENT",
                parent_span=_RUN,
                node_id="attempt:codex:0",
                node_parent="run",
                start_ns=1_000,
                end_ns=40_000,
                status={"code": "STATUS_CODE_ERROR", "message": "exit_code=1"},
                syncmill={
                    "schema_version": 1,
                    "run_id": run_id,
                    "phase": "attempt",
                    "agent_id": "codex",
                    "attempt": 0,
                    "status": "failed",
                    "exit_code": 1,
                },
            ),
            _span(
                trace_id,
                "bb00000000000002",
                "attempt:claude",
                "AGENT",
                parent_span=_RUN,
                node_id="attempt:claude:1",
                node_parent="attempt:codex:0",  # fallback = caused by the prior failure
                start_ns=41_000,
                end_ns=110_000,
                syncmill={
                    "schema_version": 1,
                    "run_id": run_id,
                    "phase": "attempt",
                    "agent_id": "claude",
                    "attempt": 1,
                    "status": "completed",
                    "exit_code": 0,
                    "winner": True,
                    "files_changed_count": 1,
                },
            ),
        ]
    )


def _compete_attempt(
    trace_id: str,
    run_id: str,
    span_id: str,
    agent: str,
    slot: int,
    *,
    end_ns: int,
    status: dict | None = None,
    syncmill_status: str = "completed",
    winner: bool = False,
) -> dict:
    syncmill: dict[str, str | int | bool] = {
        "schema_version": 1,
        "run_id": run_id,
        "phase": "attempt",
        "agent_id": agent,
        "attempt": 0,
        "status": syncmill_status,
        "worktree_slot": slot,
    }
    if syncmill_status == "completed":
        syncmill["exit_code"] = 0
        syncmill["files_changed_count"] = slot + 1
    if winner:
        syncmill["winner"] = True
    return _span(
        trace_id,
        span_id,
        f"attempt:{agent}",
        "AGENT",
        parent_span=_RUN,
        node_id=f"attempt:{agent}",
        node_parent="run",  # common parent: completion order is never causal
        start_ns=1_000,
        end_ns=end_ns,
        status=status,
        syncmill=syncmill,
    )


def _select_span(
    trace_id: str,
    run_id: str,
    winner_node: str,
    examined_links: list[str],
    *,
    start_ns: int,
    end_ns: int,
) -> dict:
    return _span(
        trace_id,
        "cc00000000000001",
        "select",
        "CHAIN",
        parent_span=_RUN,
        node_id="select",
        node_parent=winner_node,  # the promoted single cause: the winning attempt
        start_ns=start_ns,
        end_ns=end_ns,
        links=examined_links,  # every other candidate _select_winner examined
        syncmill={
            "schema_version": 1,
            "run_id": run_id,
            "phase": "select",
            "agent_id": "supervisor",
            "status": "completed",
        },
    )


def compete_winner() -> dict:
    """compete: three concurrent attempts fan out from the run; select is parented
    on the winner and linked to both examined losers (multi-parent -> lossy)."""
    trace_id, run_id = TRACE_IDS["compete-winner"], "00000000-0000-4000-8000-000000000003"
    return _document(
        [
            _run_span(trace_id, run_id, "compete", end_ns=100_000),
            _compete_attempt(
                trace_id, run_id, "bb00000000000001", "codex", 0, end_ns=80_000, winner=True
            ),
            # claude finishes FIRST — completion order must not become causality
            _compete_attempt(trace_id, run_id, "bb00000000000002", "claude", 1, end_ns=50_000),
            _compete_attempt(
                trace_id, run_id, "bb00000000000003", "kimi-code", 2, end_ns=70_000
            ),
            _select_span(
                trace_id,
                run_id,
                "attempt:codex",
                ["bb00000000000002", "bb00000000000003"],
                start_ns=81_000,
                end_ns=90_000,
            ),
        ]
    )


def compete_timeout() -> dict:
    """compete: one sibling times out; select examined only the completers."""
    trace_id, run_id = TRACE_IDS["compete-timeout"], "00000000-0000-4000-8000-000000000004"
    return _document(
        [
            _run_span(trace_id, run_id, "compete", end_ns=200_000),
            _compete_attempt(
                trace_id, run_id, "bb00000000000001", "codex", 0, end_ns=90_000, winner=True
            ),
            _compete_attempt(trace_id, run_id, "bb00000000000002", "claude", 1, end_ns=60_000),
            _compete_attempt(
                trace_id,
                run_id,
                "bb00000000000003",
                "kimi-code",
                2,
                end_ns=180_000,
                status={"code": "STATUS_CODE_ERROR", "message": "timeout"},
                syncmill_status="timeout",
            ),
            _select_span(
                trace_id,
                run_id,
                "attempt:codex",
                ["bb00000000000002"],
                start_ns=181_000,
                end_ns=190_000,
            ),
        ]
    )


def compete_gate_reject() -> dict:
    """compete: the priority winner fails its quality gate; select falls to the
    next gate-passing candidate. Gate spans are TOOL steps caused by their attempt."""
    trace_id, run_id = TRACE_IDS["compete-gate-reject"], "00000000-0000-4000-8000-000000000005"
    gate_common = {"schema_version": 1, "run_id": run_id, "phase": "gate"}
    return _document(
        [
            _run_span(trace_id, run_id, "compete", end_ns=150_000),
            _compete_attempt(trace_id, run_id, "bb00000000000001", "codex", 0, end_ns=80_000),
            _compete_attempt(trace_id, run_id, "bb00000000000002", "claude", 1, end_ns=70_000),
            _span(
                trace_id,
                "dd00000000000001",
                "gate:codex",
                "TOOL",
                parent_span=_RUN,
                node_id="gate:codex",
                node_parent="attempt:codex",
                start_ns=81_000,
                end_ns=95_000,
                status={"code": "STATUS_CODE_ERROR", "message": "gate: pytest -q failed"},
                syncmill={
                    **gate_common,
                    "agent_id": "codex",
                    "status": "failed",
                    "gate.passed": False,
                },
            ),
            _span(
                trace_id,
                "dd00000000000002",
                "gate:claude",
                "TOOL",
                parent_span=_RUN,
                node_id="gate:claude",
                node_parent="attempt:claude",
                start_ns=96_000,
                end_ns=110_000,
                syncmill={
                    **gate_common,
                    "agent_id": "claude",
                    "status": "completed",
                    "gate.passed": True,
                },
            ),
            _select_span(
                trace_id,
                run_id,
                "attempt:claude",
                ["bb00000000000001"],
                start_ns=111_000,
                end_ns=120_000,
            ),
        ]
    )


def _advanced_attempt(
    trace_id: str, run_id: str, span_id: str, name: str, node_id: str,
    node_parent: str, agent: str, phase: str, start_ns: int, end_ns: int,
    *, links: list[str] | None = None, winner: bool = False,
) -> dict:
    attrs: dict[str, str | int | bool] = {
        "schema_version": 1, "run_id": run_id, "phase": phase,
        "agent_id": agent, "status": "completed", "exit_code": 0,
    }
    if winner:
        attrs["winner"] = True
        attrs["files_changed_count"] = 2
    return _span(
        trace_id, span_id, name, "AGENT", parent_span=_RUN, node_id=node_id,
        node_parent=node_parent, start_ns=start_ns, end_ns=end_ns,
        syncmill=attrs, links=links,
    )


def pipeline_success() -> dict:
    """pipeline: sequential stages form a strict causal chain."""
    trace_id, run_id = TRACE_IDS["pipeline-success"], "00000000-0000-4000-8000-000000000006"
    return _document([
        _run_span(trace_id, run_id, "pipeline", end_ns=100_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000001", "stage:0:codex",
                          "stage:0:codex", "run", "codex", "stage", 1_000, 30_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000002", "stage:1:claude",
                          "stage:1:claude", "stage:0:codex", "claude", "stage",
                          31_000, 60_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000003", "stage:2:kimi-code",
                          "stage:2:kimi-code", "stage:1:claude", "kimi-code", "stage",
                          61_000, 90_000, winner=True),
    ])


def council_success() -> dict:
    """council: agent-local chains plus explicit cross-member synthesis fan-in."""
    trace_id, run_id = TRACE_IDS["council-success"], "00000000-0000-4000-8000-000000000007"
    spans = [_run_span(trace_id, run_id, "council", end_ns=150_000)]
    specs = [
        ("bb00000000000001", "propose:codex", "run", "codex", "propose", 1_000, 25_000, None),
        ("bb00000000000002", "propose:claude", "run", "claude", "propose", 1_000, 20_000, None),
        ("bb00000000000003", "critique:codex:0", "propose:codex", "codex", "critique", 26_000, 50_000, None),
        ("bb00000000000004", "critique:claude:0", "propose:claude", "claude", "critique", 21_000, 45_000, None),
        ("bb00000000000005", "synthesize:codex", "critique:codex:0", "codex", "synthesize", 51_000, 85_000, ["bb00000000000004"]),
        ("bb00000000000006", "synthesize:claude", "critique:claude:0", "claude", "synthesize", 46_000, 80_000, ["bb00000000000003"]),
    ]
    for sid, node, parent, agent, phase, start, end, links in specs:
        spans.append(_advanced_attempt(trace_id, run_id, sid, node, node, parent,
                                       agent, phase, start, end, links=links))
    spans.append(_select_span(trace_id, run_id, "synthesize:codex",
                              ["bb00000000000006"], start_ns=86_000, end_ns=95_000))
    return _document(spans)


def decompose_success() -> dict:
    """decompose: subtasks fan out from plan; synthesis consumes completed subtasks."""
    trace_id, run_id = TRACE_IDS["decompose-success"], "00000000-0000-4000-8000-000000000008"
    spans = [
        _run_span(trace_id, run_id, "decompose", end_ns=150_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000001", "plan:codex", "plan",
                          "run", "codex", "plan", 1_000, 25_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000002", "subtask:0:claude",
                          "subtask:0", "plan", "claude", "subtask", 26_000, 55_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000003", "subtask:1:kimi-code",
                          "subtask:1", "plan", "kimi-code", "subtask", 26_000, 60_000),
        _advanced_attempt(trace_id, run_id, "bb00000000000004", "synthesize:codex",
                          "synthesize:codex", "plan", "codex", "synthesize", 61_000,
                          90_000, links=["bb00000000000002", "bb00000000000003"]),
        _advanced_attempt(trace_id, run_id, "bb00000000000005", "synthesize:claude",
                          "synthesize:claude", "plan", "claude", "synthesize", 61_000,
                          85_000, links=["bb00000000000002", "bb00000000000003"]),
        _select_span(trace_id, run_id, "synthesize:codex", ["bb00000000000005"],
                     start_ns=91_000, end_ns=100_000),
    ]
    return _document(spans)


GENERATORS = {
    "route-success": route_success,
    "route-fallback": route_fallback,
    "compete-winner": compete_winner,
    "compete-timeout": compete_timeout,
    "compete-gate-reject": compete_gate_reject,
    "pipeline-success": pipeline_success,
    "council-success": council_success,
    "decompose-success": decompose_success,
}


def write_goldens(directory: Path) -> list[Path]:
    """Regenerate the committed golden fixtures (tests assert byte-identity)."""
    written = []
    directory.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS.items():
        path = directory / f"{name}.otlp.json"
        path.write_text(json.dumps(generator(), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    root = Path(__file__).resolve().parent.parent
    for path in write_goldens(root / "tests" / "fixtures" / "syncmill"):
        print(f"wrote {path}")
