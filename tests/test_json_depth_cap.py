"""JSON nesting is capped on the text, so every interpreter accepts and refuses the same input.

Python 3.12 and 3.13's ``json`` refuse a little above ``MAX_JSON_DEPTH`` and 3.14's parses far
deeper, so without an explicit cap the same file was refused on one interpreter and accepted on
another. The cap counts the text, not the parsed value: a value a duplicate key overwrites, or
one the checkpoint serializer revives into an object, still counts.
"""

from __future__ import annotations

from contextlib import closing
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from tracegraph import artifact, cli, sqlite_snapshot
from tracegraph.adapters import OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.input_validation import MAX_JSON_DEPTH, check_text_depth, loads
from tracegraph.sqlite_snapshot import _JSON_REFUSED
from tracegraph.sqlite_snapshot import _inert_serde, _scanning_rows, ingest_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
CHECKPOINTS = FIXTURES / "langgraph" / "native-failure.sqlite"


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def with_key(document: str, key: str, value: str) -> str:
    """``document`` (a JSON object) with one more member, written as raw text."""
    compact = json.dumps(json.loads(document))
    return compact[:-1] + f', "{key}": {value}}}'


ARTIFACT = (FIXTURES / "review-candidates" / "source.json").read_text(encoding="utf-8")
OTLP = (FIXTURES / "syncmill" / "compete-winner.otlp.json").read_text(encoding="utf-8")
PHOENIX = (FIXTURES / "phoenix" / "trace-get-v1.8.json").read_text(encoding="utf-8")

# A member of the top-level object holding `n` nested lists sits at depth 1 + n.
AT_CAP = MAX_JSON_DEPTH - 1
OVER_CAP = MAX_JSON_DEPTH


def _otlp_array(value: str) -> str:
    return "[" + with_key(OTLP, "ignored", value) + "]"


def _otlp_jsonl(value: str) -> str:
    return json.dumps(json.loads(OTLP)) + "\n" + with_key(OTLP, "ignored", value) + "\n"


PARSERS = {
    "artifact": (artifact.loads, lambda v: with_key(ARTIFACT, "ignored", v), 1),
    "otlp-object": (OTLPSpanAdapter.from_json, lambda v: with_key(OTLP, "ignored", v), 1),
    "otlp-array": (OTLPSpanAdapter.from_json, _otlp_array, 2),
    "otlp-jsonl": (OTLPSpanAdapter.from_json, _otlp_jsonl, 1),
    "phoenix": (PhoenixExportAdapter.from_json, lambda v: with_key(PHOENIX, "ignored", v), 1),
}


@pytest.mark.parametrize("name", PARSERS)
def test_depth_at_the_cap_is_accepted_and_one_more_is_refused(name):
    parse, build, outer = PARSERS[name]
    parse(build(nested(MAX_JSON_DEPTH - outer)))
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        parse(build(nested(MAX_JSON_DEPTH - outer + 1)))


@pytest.mark.parametrize("name", PARSERS)
def test_object_nesting_counts_like_list_nesting(name):
    parse, build, outer = PARSERS[name]
    deep = '{"a":' * (MAX_JSON_DEPTH - outer + 1) + "1" + "}" * (MAX_JSON_DEPTH - outer + 1)
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        parse(build(deep))


@pytest.mark.parametrize("name", PARSERS)
def test_a_value_a_duplicate_key_overwrites_still_counts(name):
    parse, build, outer = PARSERS[name]
    hidden = nested(MAX_JSON_DEPTH - outer + 1) + ', "ignored": null'
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        parse(build(hidden))


def test_the_cli_refuses_the_same_text(tmp_path):
    source = tmp_path / "deep.json"
    source.write_text(with_key(ARTIFACT, "ignored", nested(OVER_CAP)))
    result = CliRunner().invoke(cli.app, ["analyze", str(source)])
    assert result.exit_code == 2, result.output
    assert "nesting too deep" in result.output


def test_phoenix_cli_output_is_capped(monkeypatch):
    monkeypatch.setattr(cli, "_px_run", lambda command, *, action: nested(MAX_JSON_DEPTH))
    assert cli._px_json(["px"], action="px project list") == json.loads(nested(MAX_JSON_DEPTH))
    monkeypatch.setattr(cli, "_px_run", lambda command, *, action: nested(OVER_CAP + 1))
    with pytest.raises(cli._PxError, match="returned invalid JSON"):
        cli._px_json(["px"], action="px project list")


# --- check_text_depth ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,depth",
    [
        ("1", 0),
        ('"[[["', 0),
        ("[]", 1),
        ('[{"a": [1, {"b": []}]}]', 5),
        ('["\\\\"]', 1),  # an escaped backslash does not escape the closing quote
        ('["\\"[[["]', 1),  # an escaped quote does not close the string
        ('[[["]"]]]', 3),
        ("[" + "[], " * 10_000 + "[]]", 2),
    ],
)
def test_check_text_depth_counts_brackets_outside_strings(text, depth):
    check_text_depth("[" * (MAX_JSON_DEPTH - depth) + text)
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        check_text_depth("[" * (MAX_JSON_DEPTH - depth + 1) + text)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-32"])
def test_bytes_are_decoded_as_json_decodes_them(encoding):
    check_text_depth(nested(MAX_JSON_DEPTH).encode(encoding))
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        check_text_depth(nested(OVER_CAP + 1).encode(encoding))


def test_bytes_are_scanned_as_characters_not_bytes():
    # U+225B is the bytes `"[` in UTF-16-BE: read byte-wise it opens a string that swallows
    # the nesting after it.
    text = '["≛",' + nested(MAX_JSON_DEPTH) + "]"
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        check_text_depth(text.encode("utf-16-be"))


def test_scan_stops_at_the_first_container_past_the_cap():
    # Unbalanced on purpose: the scan raises without reading the rest.
    with pytest.raises(ValueError, match="^JSON nesting too deep$"):
        check_text_depth("[" * (OVER_CAP + 1) + "x" * 1_000_000)


def test_an_unterminated_string_is_left_to_the_parser():
    check_text_depth('["' + "[" * (OVER_CAP + 1))
    with pytest.raises(ValueError):
        loads('["' + "[" * (OVER_CAP + 1))


@pytest.mark.perf
def test_scan_is_linear_on_escaped_quotes_and_long_strings():
    # A string-stripping regex retried from every escaped quote: 8000 pairs took 0.83 s and
    # quadrupled per doubling. These sizes would take minutes that way.
    started = time.perf_counter()
    check_text_depth('"' + '\\"' * 200_000)
    check_text_depth("[" + '"\\"",' * 200_000 + "1]")
    check_text_depth(json.dumps(["y" * 1_000_000]))
    assert time.perf_counter() - started < 5


def test_scan_does_not_recurse():
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        check_text_depth(nested(MAX_JSON_DEPTH))
    finally:
        sys.setrecursionlimit(limit)


# --- SQLite checkpoint snapshots ---------------------------------------------------------


def _db(tmp_path, name: str = "db") -> Path:
    db = tmp_path / f"{name}.sqlite"
    shutil.copy(CHECKPOINTS, db)
    return db


def _execute(db: Path, sql: str, *params) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _json_checkpoint(db: Path, extra: str) -> None:
    """Re-store the newest checkpoint as a JSON body carrying one more member."""
    conn = sqlite3.connect(db)
    try:
        rowid, type_, blob = conn.execute(
            "SELECT rowid, type, checkpoint FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        body = json.dumps(_inert_serde().loads_typed((type_, blob)), default=list)
        conn.execute(
            "UPDATE checkpoints SET type = 'json', checkpoint = ? WHERE rowid = ?",
            (with_key(body, "ignored", extra).encode(), rowid),
        )
        conn.commit()
    finally:
        conn.close()


def _ingest(db: Path):
    return ingest_snapshot(db, "t", error_channel="error")


def _refused(db: Path) -> None:
    with pytest.raises(ValueError, match=r"cannot be decoded \(JSON nesting too deep\)"):
        _ingest(db)


def test_metadata_column_is_capped(tmp_path):
    db = _db(tmp_path)
    newest = "rowid = (SELECT max(rowid) FROM checkpoints)"
    _execute(db, f"UPDATE checkpoints SET metadata = ? WHERE {newest}",
             with_key('{"source": "loop", "step": 2}', "x", nested(AT_CAP)).encode())
    _ingest(db)
    _execute(db, f"UPDATE checkpoints SET metadata = ? WHERE {newest}",
             with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP)))
    _refused(db)
    _execute(db, f"UPDATE checkpoints SET metadata = ? WHERE {newest}",
             with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP)).encode())
    _refused(db)


def test_metadata_duplicate_key_is_capped(tmp_path):
    db = _db(tmp_path)
    _execute(db, "UPDATE checkpoints SET metadata = ? WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
             '{"x": ' + nested(OVER_CAP) + ', "x": null, "source": "loop", "step": 2}')
    _refused(db)


def test_json_checkpoint_body_is_capped(tmp_path):
    db = _db(tmp_path)
    _json_checkpoint(db, nested(AT_CAP))
    _ingest(db)
    db = _db(tmp_path, "over")
    _json_checkpoint(db, nested(OVER_CAP))
    _refused(db)


def test_depth_inside_a_revived_constructor_still_counts(tmp_path):
    db = _db(tmp_path)
    # body, Send and kwargs objects, then the nested lists: one past the cap.
    send = ('{"lc": 2, "type": "constructor", "id": ["langgraph", "types", "Send"], '
            '"kwargs": {"node": "n", "arg": ' + nested(MAX_JSON_DEPTH - 2) + "}}")
    _json_checkpoint(db, send)
    _refused(db)


def _json_write(db: Path, value: str, *, checkpoint_id: str | None = None) -> None:
    conn = sqlite3.connect(db)
    try:
        thread, ns, cid, task = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id FROM writes LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) VALUES (?, ?, ?, ?, 999, 'extra', 'json', ?)",
            (thread, ns, checkpoint_id or cid, task, value.encode()),
        )
        conn.commit()
    finally:
        conn.close()


def test_json_write_value_is_counted_on_its_own(tmp_path):
    # The saver returns writes as a list of triples; that wrapper is not the stored text.
    db = _db(tmp_path)
    _json_write(db, nested(MAX_JSON_DEPTH))
    _ingest(db)
    db = _db(tmp_path, "over")
    _json_write(db, nested(OVER_CAP + 1))
    _refused(db)


def test_a_write_the_saver_never_reads_is_not_scanned(tmp_path):
    db = _db(tmp_path)
    _json_write(db, nested(OVER_CAP + 1), checkpoint_id="no-such-checkpoint")
    baseline = _ingest(_db(tmp_path, "base"))
    assert _ingest(db).model_dump() == baseline.model_dump()


def test_every_namespace_is_scanned(tmp_path):
    db = _db(tmp_path)
    _execute(
        db,
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, metadata) SELECT thread_id, 'sub:1', checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, ? FROM checkpoints WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
        with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP)),
    )
    _refused(db)


DEEP_NEWEST = with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP))
NEWEST = "rowid = (SELECT max(rowid) FROM checkpoints)"


def _expire_after_setup(monkeypatch) -> None:
    """Move the snapshot clock past every deadline once the saver has set up the private copy."""
    real_setup = SqliteSaver.setup

    def setup(self):
        real_setup(self)
        monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=lambda: float("inf")))

    monkeypatch.setattr(SqliteSaver, "setup", setup)


def _never_scanned(monkeypatch) -> list:
    scanned: list = []
    real = sqlite_snapshot.check_text_depth
    monkeypatch.setattr(sqlite_snapshot, "check_text_depth", lambda t: (scanned.append(t), real(t)))
    return scanned


def test_an_expired_deadline_is_reported_before_any_value_is_scanned(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _execute(db, f"UPDATE checkpoints SET metadata = ? WHERE {NEWEST}", DEEP_NEWEST)
    scanned = _never_scanned(monkeypatch)
    _expire_after_setup(monkeypatch)
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        _ingest(db)
    assert scanned == []


def test_a_thread_with_no_rows_still_reports_an_expired_deadline(tmp_path, monkeypatch):
    db = _db(tmp_path)
    with pytest.raises(KeyError, match="no checkpoints"):  # within the deadline
        ingest_snapshot(db, "no-such-thread", error_channel="error")
    _expire_after_setup(monkeypatch)
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        ingest_snapshot(db, "no-such-thread", error_channel="error")


def test_a_deadline_passing_during_an_empty_listing_is_reported(tmp_path, monkeypatch):
    # An empty listing reaches no row to check, and a short query can run long without reaching
    # the progress handler, so the deadline is checked when the adapter finds no checkpoints.
    db = _db(tmp_path)
    real_list = SqliteSaver.list

    def slow_list(self, *args, **kwargs):
        monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=lambda: float("inf")))
        yield from real_list(self, *args, **kwargs)

    monkeypatch.setattr(SqliteSaver, "list", slow_list)
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        ingest_snapshot(db, "no-such-thread", error_channel="error")


def _rows(conn: sqlite3.Connection, factory, sql: str):
    conn.row_factory = factory
    return conn.execute(sql).fetchall()


def test_the_row_check_reports_an_expired_deadline_as_the_deadline():
    with closing(sqlite3.connect(":memory:")) as conn:
        for value in ("'[]'", repr(nested(OVER_CAP + 1))):  # passed before the check
            refusal: list[str] = []
            factory = _scanning_rows(time.monotonic() - 1, refusal)
            with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
                _rows(conn, factory, f"SELECT {value} AS metadata")
            assert refusal == ["SQLite snapshot deadline exceeded"]


def test_a_deadline_passing_during_the_row_check_is_reported_as_the_deadline(monkeypatch):
    deadline = time.monotonic() + 3600
    refusal: list[str] = []
    factory = _scanning_rows(deadline, refusal)
    real = sqlite_snapshot.check_text_depth

    def scan(text):
        real(text)
        monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=lambda: deadline + 1))

    monkeypatch.setattr(sqlite_snapshot, "check_text_depth", scan)
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
            _rows(conn, factory, "SELECT '[]' AS metadata")
    assert refusal == ["SQLite snapshot deadline exceeded"]


def test_the_row_check_keeps_its_refusal_and_shares_it_with_the_serializer():
    deadline = time.monotonic() + 3600
    with closing(sqlite3.connect(":memory:")) as conn:
        serde, refusal = sqlite_snapshot._scanning_serde(deadline)
        factory = _scanning_rows(deadline, refusal)
        deep = repr(nested(OVER_CAP + 1))
        assert _rows(conn, factory, "SELECT '[]' AS metadata, 'x' AS other") == [("[]", "x")]
        with pytest.raises(ValueError, match=r"^checkpoint history cannot be decoded \(JSON nesting too deep\)$"):
            _rows(conn, factory, f"SELECT {deep} AS metadata")
        assert refusal == [_JSON_REFUSED]
        # A caller that swallowed the refusal cannot read on past it, by rows or by decoding.
        with pytest.raises(ValueError, match=r"\(JSON nesting too deep\)$"):
            _rows(conn, factory, "SELECT 1 AS unrelated")
        with pytest.raises(ValueError, match=r"\(JSON nesting too deep\)$"):
            serde.loads_typed(("json", b"[]"))

        # And a refusal from the serializer stops the rows too.
        serde, refusal = sqlite_snapshot._scanning_serde(deadline)
        factory = _scanning_rows(deadline, refusal)
        with pytest.raises(ValueError):
            serde.loads_typed(("json", nested(OVER_CAP + 1)))
        with pytest.raises(ValueError, match=r"\(JSON nesting too deep\)$"):
            _rows(conn, factory, "SELECT '[]' AS metadata")


@pytest.mark.parametrize("name", ["metadata", "METADATA", "MetaData", '"metadata"', "[Metadata]"])
def test_the_metadata_column_is_found_by_any_spelling_sqlite_resolves(name):
    with closing(sqlite3.connect(":memory:")) as conn:
        factory = _scanning_rows(time.monotonic() + 3600, [])
        with pytest.raises(ValueError, match=r"\(JSON nesting too deep\)$"):
            _rows(conn, factory, f"SELECT {nested(OVER_CAP + 1)!r} AS {name}")
        # Only that column: the same text under another name is not metadata.
        _rows(conn, _scanning_rows(time.monotonic() + 3600, []),
              f"SELECT {nested(OVER_CAP + 1)!r} AS metadata_copy")


def _redefine_checkpoints(db: Path, kind: str) -> None:
    """Serve ``checkpoints`` through ``kind`` over the real rows, the newest metadata nested too deep."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"UPDATE checkpoints SET metadata = ? WHERE {NEWEST}", (DEEP_NEWEST,))
        if kind == "declared-upper":
            conn.execute("ALTER TABLE checkpoints RENAME COLUMN metadata TO METADATA")
        else:
            conn.execute("ALTER TABLE checkpoints RENAME TO cp_real")
            columns = ("thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, "
                       "checkpoint, metadata AS MetaData")
            if kind == "view":
                conn.execute(f"CREATE VIEW checkpoints AS SELECT {columns} FROM cp_real")
            else:
                conn.execute(f"CREATE VIEW cp_view AS SELECT rowid, {columns} FROM cp_real")
                conn.execute(
                    "CREATE VIRTUAL TABLE checkpoints USING fts5(thread_id, checkpoint_ns, "
                    "checkpoint_id, parent_checkpoint_id, type, checkpoint, MetaData, "
                    "content='cp_view', content_rowid='rowid')")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["declared-upper", "view", "fts5"])
def test_metadata_served_by_any_checkpoints_object_is_capped(tmp_path, kind):
    db = _db(tmp_path)
    _redefine_checkpoints(db, kind)
    _refused(db)


class _JsonSpy:
    """``json`` for the saver's module, recording the objects ``loads`` is handed."""

    def __init__(self, events: list):
        self._events = events

    def __getattr__(self, name):
        return getattr(json, name)

    def loads(self, text, *args, **kwargs):
        self._events.append(("parsed", text))
        return json.loads(text, *args, **kwargs)


@pytest.mark.parametrize("kind", ["table", "view"])
def test_the_saver_parses_only_the_metadata_object_just_measured(tmp_path, monkeypatch, kind):
    import langgraph.checkpoint.sqlite as saver_module

    db = _db(tmp_path)
    if kind == "view":
        _redefine_checkpoints(db, "view")
        _execute(db, "UPDATE cp_real SET metadata = '{\"source\": \"loop\", \"step\": 2}' "
                     "WHERE rowid = (SELECT max(rowid) FROM cp_real)")
    events: list = []
    real = sqlite_snapshot.check_text_depth

    def measure(text):
        events.append(("measured", text))
        return real(text)

    monkeypatch.setattr(sqlite_snapshot, "check_text_depth", measure)
    monkeypatch.setattr(saver_module, "json", _JsonSpy(events))

    def assert_each_parse_follows_its_measurement():
        parsed = [i for i, (what, _) in enumerate(events) if what == "parsed"]
        assert parsed  # the saver did parse metadata
        for i in parsed:
            last = next(e for e in reversed(events[:i]) if e[0] == "measured")
            assert last[1] is events[i][1]

    _ingest(db)  # the saver's `list`
    assert_each_parse_follows_its_measurement()
    # `get_tuple`, which `get_state` reaches when a graph is supplied.
    events.clear()
    with closing(sqlite3.connect(db)) as conn:
        serde, refusal = sqlite_snapshot._scanning_serde(time.monotonic() + 3600)
        conn.row_factory = _scanning_rows(time.monotonic() + 3600, refusal)
        assert SqliteSaver(conn, serde=serde).get_tuple(
            {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
        )
    assert_each_parse_follows_its_measurement()


# --- JSON bodies and writes are checked where the saver decodes them ---------------------------


def _nocase_threads_with_json_write(path: Path, value: str) -> None:
    """The fixture rewritten under thread ``T`` with a ``COLLATE NOCASE`` thread column.

    Asked for thread ``t``, the saver lists these checkpoints and then reads their writes by the
    stored ``T``. A check that selected writes by the requested ``t`` would miss ``value``.
    """
    src = sqlite3.connect(CHECKPOINTS)
    dst = sqlite3.connect(path)
    try:
        dst.execute(
            "CREATE TABLE checkpoints (thread_id TEXT NOT NULL COLLATE NOCASE, checkpoint_ns TEXT "
            "NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type "
            "TEXT, checkpoint BLOB, metadata BLOB, PRIMARY KEY (thread_id, checkpoint_ns, "
            "checkpoint_id))")
        dst.execute(
            "CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
            "checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel "
            "TEXT NOT NULL, type TEXT, value BLOB, PRIMARY KEY (thread_id, checkpoint_ns, "
            "checkpoint_id, task_id, idx))")
        dst.executemany(
            "INSERT INTO checkpoints VALUES ('T', ?, ?, ?, ?, ?, ?)",
            src.execute("SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, "
                        "checkpoint, metadata FROM checkpoints"))
        dst.executemany(
            "INSERT INTO writes VALUES ('T', ?, ?, ?, ?, ?, ?, ?)",
            src.execute("SELECT checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value "
                        "FROM writes"))
        ns, cid, task = dst.execute(
            "SELECT checkpoint_ns, checkpoint_id, task_id FROM writes LIMIT 1").fetchone()
        dst.execute("INSERT INTO writes VALUES ('T', ?, ?, ?, 999, 'extra', 'json', ?)",
                    (ns, cid, task, value.encode()))
        dst.commit()
    finally:
        src.close()
        dst.close()


def test_a_json_write_reached_through_a_collation_is_still_capped(tmp_path):
    # Before the check moved into the serializer, 3.12 and 3.13 reported RecursionError here and
    # 3.14 parsed the write and reported no checkpoints for `t`.
    db = tmp_path / "nocase.sqlite"
    _nocase_threads_with_json_write(db, nested(OVER_CAP + 1))
    _refused(db)
    result = CliRunner().invoke(
        cli.app, ["ingest", "--sqlite", str(db), "--thread", "t", "--out", str(tmp_path / "a.json")]
    )
    assert result.exit_code == 2, result.output
    assert "JSON nesting too deep" in result.output


def test_the_serializer_caps_json_and_keeps_the_refusal():
    serde, refusal = sqlite_snapshot._scanning_serde(time.monotonic() + 3600)
    serde.loads_typed(("json", nested(MAX_JSON_DEPTH).encode()))  # at the cap: decoded
    assert refusal == []
    with pytest.raises(ValueError, match=r"^checkpoint history cannot be decoded \(JSON nesting too deep\)$"):
        serde.loads_typed(("json", nested(MAX_JSON_DEPTH + 1)))
    assert refusal == ["checkpoint history cannot be decoded (JSON nesting too deep)"]
    # Sticky, and shared with the copies the saver makes.
    clone = serde.with_msgpack_allowlist([("some.module", "Thing")])
    for s in (serde, clone):
        with pytest.raises(ValueError, match=r"\(JSON nesting too deep\)$"):
            s.loads_typed(("json", b"[]"))


def test_a_json_value_that_is_not_text_is_left_to_the_decoder():
    serde, refusal = sqlite_snapshot._scanning_serde(time.monotonic() + 3600)
    with pytest.raises(TypeError, match="JSON object must be str, bytes or bytearray"):
        serde.loads_typed(("json", 12345))
    assert refusal == []


def test_the_json_check_reports_an_expired_deadline_as_the_deadline():
    for value in (b"[]", nested(OVER_CAP + 1)):  # passed before the check: not reported as depth
        serde, refusal = sqlite_snapshot._scanning_serde(time.monotonic() - 1)
        with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
            serde.loads_typed(("json", value))
        assert refusal == ["SQLite snapshot deadline exceeded"]


def test_a_deadline_passing_during_the_json_check_is_reported_as_the_deadline(monkeypatch):
    deadline = time.monotonic() + 3600
    serde, refusal = sqlite_snapshot._scanning_serde(deadline)
    real = sqlite_snapshot.check_text_depth

    def scan(text):
        real(text)
        monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=lambda: deadline + 1))

    monkeypatch.setattr(sqlite_snapshot, "check_text_depth", scan)
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        serde.loads_typed(("json", b"[]"))
    assert refusal == ["SQLite snapshot deadline exceeded"]


def test_undecodable_bytes_keep_the_savers_own_error(tmp_path):
    db = _db(tmp_path)
    _execute(db, "UPDATE checkpoints SET metadata = ? WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
             b"\xff")
    with pytest.raises(ValueError, match=r"cannot be decoded \(UnicodeDecodeError\)"):
        _ingest(db)


def test_a_database_without_tables_still_reads_as_no_checkpoints(tmp_path):
    db = tmp_path / "empty.sqlite"
    sqlite3.connect(db).close()
    with pytest.raises(KeyError, match="no checkpoints"):
        _ingest(db)

