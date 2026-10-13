"""A checkpoint history whose shape LangGraph never writes is a usage error, never a traceback.

Each tuple is checked, field by field, against the types LangGraph declares for the fields the
adapter reads. Before that check, about half of 64 one-field mutations of a real history
crashed with ``AttributeError``/``TypeError``, and several changed the result silently: a
malformed parent config dropped a declared parent and fabricated a root, and ``step=True``
was read as step 1.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.cli import app
from tracegraph.normalize import normalize
from tracegraph.sqlite_snapshot import _inert_serde, ingest_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "langgraph" / "native-failure.sqlite"
runner = CliRunner()
DELETE = object()


def _read_fixture() -> list:
    # Opening a SqliteSaver runs its setup, which switches the file to WAL mode: read a copy,
    # never the committed fixture itself.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db.sqlite"
        shutil.copy(FIXTURE, db)
        conn = sqlite3.connect(db)
        try:
            return list(SqliteSaver(conn, serde=_inert_serde()).list({"configurable": {"thread_id": "t"}}))
        finally:
            conn.close()


_FIXTURE_TUPLES = _read_fixture()


def _tuples() -> list:
    return copy.deepcopy(_FIXTURE_TUPLES)


class _Saver:
    def __init__(self, tuples):
        self.tuples = tuples

    def list(self, config):
        return iter(self.tuples)


def _ingest(tuples):
    return LangGraphCheckpointAdapter(_Saver(tuples)).ingest("t")


def _set(t, path, value):
    field, *rest = path
    if not rest:
        return t._replace(**{field: value})
    obj = copy.deepcopy(getattr(t, field))
    cur = obj
    for key in rest[:-1]:
        cur = cur[key]
    if value is DELETE:
        cur.pop(rest[-1], None)
    else:
        cur[rest[-1]] = value
    return t._replace(**{field: obj})


def _mutated(index: int, path: tuple, value):
    tuples = _tuples()
    tuples[index] = _set(tuples[index], path, value)
    return tuples


#: The fixture's artifact as tracegraph produced it before this boundary existed (be1930c).
BASELINE_DIGEST = "sha256:000d30f2acd313b3f73cfec9053583feccad4f8f31a0ffea55407748a0c694f8"


def _digest(raw) -> str:
    return "sha256:" + hashlib.sha256(artifact.dumps(normalize(raw)).encode()).hexdigest()
NEWEST = 0  # SqliteSaver lists newest first; this one has a parent

_BOUNDARY_PHRASES = ("malformed", "has no id", "has no thread_id", "has no checkpoint_id", "disagrees",
                     "must be", "not a list")


MALFORMED = [
    (("metadata",), "x"), (("metadata",), 5), (("metadata",), []),
    (("metadata", "source"), []), (("metadata", "source"), {}), (("metadata", "source"), 5),
    (("metadata", "step"), True), (("metadata", "step"), 1.5), (("metadata", "step"), "1"),
    (("metadata", "parents"), []), (("metadata", "parents"), "x"), (("metadata", "parents"), {"": None}),
    (("metadata", "parents"), {"": 5}), (("metadata", "parents"), {5: "id"}),
    (("config",), None), (("config",), []), (("config",), "x"),
    (("config", "configurable"), None), (("config", "configurable", "thread_id"), DELETE),
    (("config", "configurable"), []), (("config", "configurable"), "x"), (("config", "configurable"), 5),
    (("config", "configurable", "checkpoint_ns"), 5), (("config", "configurable", "checkpoint_ns"), []),
    (("config", "configurable", "checkpoint_id"), []), (("config", "configurable", "checkpoint_id"), 5),
    (("config", "configurable", "thread_id"), []),
    (("checkpoint",), []), (("checkpoint",), "x"),
    (("checkpoint", "id"), 5), (("checkpoint", "id"), []), (("checkpoint", "id"), DELETE),
    (("checkpoint", "ts"), 5),
    (("checkpoint", "channel_values"), []), (("checkpoint", "channel_values"), None),
    (("checkpoint", "channel_values"), {7: True}),
    (("checkpoint", "channel_versions"), 5), (("checkpoint", "channel_versions"), None),
    (("checkpoint", "versions_seen"), 5), (("checkpoint", "versions_seen"), None),
    (("checkpoint", "v"), "x"), (("checkpoint", "v"), None), (("checkpoint", "v"), True),
    (("parent_config",), []), (("parent_config",), "x"), (("parent_config",), {}),
    (("parent_config", "configurable"), None), (("parent_config", "configurable"), {}),
    (("parent_config", "configurable", "checkpoint_id"), DELETE),
    (("parent_config", "configurable"), []), (("parent_config", "configurable"), "x"),
    (("parent_config", "configurable", "checkpoint_id"), []),
    (("parent_config", "configurable", "checkpoint_id"), {}),
    (("parent_config", "configurable", "checkpoint_ns"), 5),
    (("pending_writes",), 5), (("pending_writes",), "x"),
]


@pytest.mark.parametrize("path,value", MALFORMED, ids=lambda v: repr(v)[:30])
def test_malformed_shape_is_a_value_error(path, value):
    with pytest.raises(ValueError) as caught:
        _ingest(_mutated(NEWEST, path, value))
    message = str(caught.value)
    assert any(phrase in message for phrase in _BOUNDARY_PHRASES), message
    assert "Input should be" not in message.split(":", 1)[0]  # our wording leads, not pydantic's


def test_malformed_parents_on_a_checkpoint_without_parent_config_is_not_a_root():
    # The root-fabrication path: with no parent config, metadata.parents is the only parent
    # declaration, and a malformed one used to read as "no parent".
    tuples = _tuples()
    t = _set(tuples[NEWEST], ("parent_config",), None)
    tuples[NEWEST] = _set(t, ("metadata", "parents"), [])
    with pytest.raises(ValueError, match="parents"):
        _ingest(tuples)


def test_conflicting_checkpoint_ids_are_rejected():
    tuples = _tuples()
    other = tuples[1].checkpoint["id"]
    tuples[NEWEST] = _set(tuples[NEWEST], ("checkpoint", "id"), other)
    with pytest.raises(ValueError, match="checkpoint_id"):
        _ingest(tuples)


def test_a_tuple_without_tuple_attributes_is_a_value_error():
    from types import SimpleNamespace

    t = _tuples()[NEWEST]
    bare = SimpleNamespace(config=t.config, checkpoint=t.checkpoint, metadata=t.metadata)
    raw = _ingest([bare, *_tuples()[1:]])  # parent_config and pending_writes are optional
    assert raw.steps
    with pytest.raises(ValueError):
        _ingest([SimpleNamespace(config=t.config, metadata=t.metadata), *_tuples()[1:]])


def test_malformed_thread_id_fails_discover_too():
    tuples = _mutated(NEWEST, ("config", "configurable", "thread_id"), [])
    with pytest.raises(ValueError):
        LangGraphCheckpointAdapter(_Saver(tuples)).discover()


@pytest.mark.parametrize("path,value", MALFORMED, ids=lambda v: repr(v)[:30])
def test_cli_reports_each_malformed_shape_as_a_usage_error(tmp_path, monkeypatch, path, value):
    tuples = _mutated(NEWEST, path, value)

    def fake_ingest_snapshot(source, thread, *, error_channel, timeout=30):
        return LangGraphCheckpointAdapter(_Saver(tuples), error_channel=error_channel).ingest(thread)

    monkeypatch.setattr("tracegraph.sqlite_snapshot.ingest_snapshot", fake_ingest_snapshot)
    db = tmp_path / "db.sqlite"
    shutil.copy(FIXTURE, db)
    result = runner.invoke(app, ["ingest", "--sqlite", str(db), "--thread", "t", "-o", str(tmp_path / "a.json")])
    assert result.exit_code == 2, result.output
    assert "cannot ingest checkpoint" in result.output
    message = re.sub(r"[│\s]+", " ", result.output)  # undo the error box's line wrapping
    assert any(phrase in message for phrase in _BOUNDARY_PHRASES), result.output
    assert not (tmp_path / "a.json").exists()


# --- shapes a real saver writes stay accepted ----------------------------------------------


@pytest.mark.parametrize(
    "path,value",
    [
        (("metadata",), None),
        (("metadata", "source"), "some-future-source"),
        (("metadata", "counters_since_delta_snapshot"), {"x": [1, 2]}),
        (("metadata", "parents"), {}),
        (("checkpoint", "updated_channels"), DELETE),
        (("checkpoint", "pending_sends"), []),
        (("pending_writes",), None),
    ],
    ids=lambda v: repr(v)[:30],
)
def test_legitimate_shapes_are_accepted(path, value):
    raw = _ingest(_mutated(NEWEST, path, value))
    assert raw.steps


def test_unknown_source_still_maps_to_loop():
    tuples = [_set(t, ("metadata", "source"), "some-future-source") for t in _tuples()]
    raw = _ingest(tuples)
    checkpoint_steps = [s for s in raw.steps if s.source.value != "task"]
    assert checkpoint_steps and {s.source.value for s in checkpoint_steps} == {"loop"}


def test_a_legacy_checkpoint_layout_is_accepted():
    # v1: no `updated_channels`, Send packets under `pending_sends` (pre-v4 layout).
    tuples = []
    for t in _tuples():
        t = _set(t, ("checkpoint", "v"), 1)
        t = _set(t, ("checkpoint", "updated_channels"), DELETE)
        tuples.append(_set(t, ("checkpoint", "pending_sends"), []))
    raw = _ingest(tuples)
    assert len(raw.steps) == len(_ingest(_tuples()).steps)


def test_valid_history_is_byte_identical():
    assert _digest(_ingest(_tuples())) == BASELINE_DIGEST


# --- pending writes ------------------------------------------------------------------------


def test_malformed_writes_are_ignored_and_disclosed():
    tuples = _tuples()
    bad = ["abc", {"task": "x"}, ("a", "b", "c", "d"), (1, "ch", "v"), ("t", 2, "v")]
    index = next(i for i, t in enumerate(tuples) if t.pending_writes)
    tuples[index] = tuples[index]._replace(pending_writes=[*tuples[index].pending_writes, *bad])
    raw = _ingest(tuples)
    assert _digest(raw) == BASELINE_DIGEST
    assert any(w.startswith("5 pending write(s)") for w in raw.ingest_warnings), raw.ingest_warnings
    assert "abc" not in json.dumps(raw.ingest_warnings)


# --- decoding failures inside the saver ----------------------------------------------------


def _corrupt(tmp_path, sql: str, *params) -> Path:
    db = tmp_path / "db.sqlite"
    shutil.copy(FIXTURE, db)
    conn = sqlite3.connect(db)
    conn.execute(sql, params)
    conn.commit()
    conn.close()
    return db


CORRUPTIONS = [
    ("UPDATE checkpoints SET type = 'no-such-type' WHERE rowid = (SELECT max(rowid) FROM checkpoints)",),
    ("UPDATE checkpoints SET metadata = ? WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
     "[" * 100_000 + "]" * 100_000),
    ("UPDATE checkpoints SET metadata = 7 WHERE rowid = (SELECT max(rowid) FROM checkpoints)",),
    ("UPDATE checkpoints SET type = 'json', checkpoint = ? WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
     b'{"lc": 2, "type": "constructor", "id": 7}'),
]


@pytest.mark.parametrize("corruption", CORRUPTIONS, ids=["serde-type", "deep-json", "int-metadata", "json-envelope"])
def test_undecodable_history_is_a_usage_error(tmp_path, corruption):
    db = _corrupt(tmp_path, *corruption)
    with pytest.raises(ValueError, match="cannot be decoded"):
        ingest_snapshot(db, "t", error_channel="error")
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError, match="cannot be decoded"):
            LangGraphCheckpointAdapter(SqliteSaver(conn, serde=_inert_serde())).discover()
    finally:
        conn.close()
    result = runner.invoke(app, ["ingest", "--sqlite", str(db), "--thread", "t", "-o", str(tmp_path / "a.json")])
    assert result.exit_code == 2, result.output
    assert "cannot ingest checkpoint" in result.output


def test_a_saver_that_cannot_list_threads_is_still_unsupported():
    class NoListing:
        def list(self, config):
            raise NotImplementedError

    with pytest.raises(NotImplementedError, match="does not support listing"):
        LangGraphCheckpointAdapter(NoListing()).discover()
