"""A document the caller decoded never escapes a library entry point as ``RecursionError``.

Decoded input is not depth-capped (JSON text is, see ``test_json_depth_cap.py``), and reading one
nested deep enough recurses: formatting a value into a message, or code LangGraph runs. Each
entry point reports that as the ``ValueError`` it already promises. The message names the limit,
not a cause, since a caller-supplied saver, graph or object can recurse on its own.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter, OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.adapters import otlp_spans, phoenix_export
from tracegraph.input_validation import TRACE_UNREADABLE, refuses_deep_nesting
from tracegraph.sqlite_snapshot import _inert_serde

FIXTURES = Path(__file__).parent / "fixtures"
CHECKPOINTS = FIXTURES / "langgraph" / "native-failure.sqlite"
OTLP = json.loads((FIXTURES / "syncmill" / "compete-winner.otlp.json").read_text(encoding="utf-8"))
PHOENIX = json.loads((FIXTURES / "phoenix" / "trace-get-v1.8.json").read_text(encoding="utf-8"))
ARTIFACT = json.loads((FIXTURES / "review-candidates" / "source.json").read_text(encoding="utf-8"))
CHECKPOINT_UNREADABLE = "checkpoint history cannot be read: recursion limit exceeded"
ARTIFACT_UNREADABLE = "artifact cannot be read: recursion limit exceeded"

# Past every interpreter's recursion limit: formatting a nested list recurses to about 10,000
# levels on 3.12 and 3.13 and about 70,000 on 3.14, which is bounded by the C stack instead.
DEEP = 300_000


def nested(depth: int) -> list:
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


def recurse(*args, **kwargs):
    """A caller callback with a bug of its own: it recurses whatever it is given."""
    return recurse(*args, **kwargs)


def _ingest_all(adapter) -> None:
    for trace_id in adapter.discover():
        adapter.ingest(trace_id)


def _with_exception_message(value) -> dict:
    document = json.loads(json.dumps(OTLP))
    span = document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["status"] = {"code": 2}
    span["events"] = [{"name": "exception", "attributes": [{"key": "exception.message", "value": value}]}]
    return document


# --- the decorator ---------------------------------------------------------------------


def test_only_recursion_is_relabelled():
    @refuses_deep_nesting("limit")
    def raises(exc):
        raise exc

    with pytest.raises(ValueError, match="^limit$") as raised:
        raises(RecursionError())
    assert isinstance(raised.value.__cause__, RecursionError)
    for exc in (ValueError("own"), KeyError("k"), TypeError("bug")):
        with pytest.raises(type(exc)) as passed:
            raises(exc)
        assert passed.value is exc


# --- OTLP and Phoenix --------------------------------------------------------------------


def test_otlp_deep_exception_message_is_a_value_error():
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        _ingest_all(OTLPSpanAdapter(_with_exception_message(nested(DEEP))))


def test_otlp_deep_object_leaf_is_a_value_error():
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        _ingest_all(OTLPSpanAdapter(_with_exception_message(SimpleNamespace(data=nested(DEEP)))))


def test_otlp_moderate_depth_still_ingests():
    raw = [OTLPSpanAdapter(_with_exception_message(nested(9000))).ingest(t)
           for t in OTLPSpanAdapter(OTLP).discover()]
    assert max(len(step.error_msg or "") for step in raw[0].steps) == 18_002


def test_otlp_discover_reports_recursion(monkeypatch):
    adapter = OTLPSpanAdapter(OTLP)
    monkeypatch.setattr(adapter, "_grouped", recurse)
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        adapter.discover()


def test_otlp_from_documents_reports_recursion(monkeypatch):
    monkeypatch.setattr(otlp_spans, "_span_array", recurse)
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        OTLPSpanAdapter.from_documents([OTLP])


def test_phoenix_constructor_reports_recursion(monkeypatch):
    monkeypatch.setattr(phoenix_export, "_trace_objects", recurse)
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        PhoenixExportAdapter(PHOENIX)


def test_phoenix_ingest_reports_recursion(monkeypatch):
    adapter = PhoenixExportAdapter(PHOENIX)
    monkeypatch.setattr(adapter, "_span", recurse)
    with pytest.raises(ValueError, match=f"^{TRACE_UNREADABLE}$"):
        _ingest_all(adapter)


# --- artifacts ---------------------------------------------------------------------------


def test_artifact_deep_schema_version_is_a_value_error():
    # The refusal message formats the version with `!r`, which recurses on a deep list.
    with pytest.raises(ValueError, match=f"^{ARTIFACT_UNREADABLE}$"):
        artifact.from_obj({**ARTIFACT, "schema_version": nested(DEEP)})


def test_artifact_shallow_errors_keep_their_message():
    with pytest.raises(ValueError, match="unsupported artifact schema_version"):
        artifact.from_obj({**ARTIFACT, "schema_version": [[1]]})


# --- checkpoints -------------------------------------------------------------------------


def _fixture_tuples() -> list:
    # Opening a SqliteSaver switches the file to WAL mode: read a copy, never the fixture.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db.sqlite"
        shutil.copy(CHECKPOINTS, db)
        conn = sqlite3.connect(db)
        try:
            return list(SqliteSaver(conn, serde=_inert_serde()).list({"configurable": {"thread_id": "t"}}))
        finally:
            conn.close()


_TUPLES = _fixture_tuples()


class _Saver:
    def __init__(self, tuples):
        self.tuples = tuples

    def list(self, config, **kwargs):
        return iter(self.tuples)


def _adapter(tuples, **kwargs) -> LangGraphCheckpointAdapter:
    return LangGraphCheckpointAdapter(_Saver(tuples), error_channel="error", **kwargs)


def test_deep_error_channel_value_is_a_value_error():
    tuples = copy.copy(_TUPLES)
    cp = tuples[0].checkpoint
    tuples[0] = tuples[0]._replace(
        checkpoint={**cp, "channel_values": {**cp["channel_values"], "error": nested(DEEP)}}
    )
    with pytest.raises(ValueError, match=f"^{CHECKPOINT_UNREADABLE}$"):
        _adapter(tuples).ingest("t")


def test_deep_pending_write_value_is_a_value_error():
    tuples = [
        t._replace(pending_writes=[(w[0], w[1], nested(DEEP)) for w in t.pending_writes])
        if t.pending_writes else t
        for t in _TUPLES
    ]
    with pytest.raises(ValueError, match=f"^{CHECKPOINT_UNREADABLE}$"):
        _adapter(tuples).ingest("t")


def test_a_recursing_saver_gets_the_limit_message_not_a_depth_claim():
    class Recursing:
        list = staticmethod(recurse)

    for call in (lambda a: a.discover(), lambda a: a.ingest("t")):
        with pytest.raises(ValueError) as raised:
            call(LangGraphCheckpointAdapter(Recursing()))
        assert "recursion" in str(raised.value)
        assert "too deep" not in str(raised.value)


def test_a_schedule_graph_returning_a_deep_config_is_a_value_error():
    snapshot = SimpleNamespace(config={"configurable": {"checkpoint_id": nested(DEEP)}}, tasks=())
    graph = SimpleNamespace(copy=lambda update: SimpleNamespace(get_state=lambda config: snapshot), nodes={})
    with pytest.raises(ValueError, match=f"^{CHECKPOINT_UNREADABLE}$"):
        _adapter(list(_TUPLES), schedule_graph=graph).ingest("t")
