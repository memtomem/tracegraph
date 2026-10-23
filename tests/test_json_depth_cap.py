"""JSON nesting is capped on the text, so every interpreter accepts and refuses the same input.

Python 3.12 and 3.13's ``json`` refuse a little above ``MAX_JSON_DEPTH`` and 3.14's parses far
deeper, so without an explicit cap the same file was refused on one interpreter and accepted on
another. The cap counts the text, not the parsed value: a value a duplicate key overwrites, or
one the checkpoint serializer revives into an object, still counts.
"""

from __future__ import annotations

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
from tracegraph.sqlite_snapshot import _check_json_depth, _inert_serde, ingest_snapshot

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
            "INSERT INTO writes VALUES (?, ?, ?, ?, 999, 'extra', 'json', ?)",
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
        "INSERT INTO checkpoints SELECT thread_id, 'sub:1', checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, ? FROM checkpoints WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
        with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP)),
    )
    _refused(db)


def test_scan_is_charged_to_the_snapshot_deadline(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        SqliteSaver(conn).setup()
        past = time.monotonic() - 1
        with pytest.raises(ValueError, match="deadline exceeded"):
            _check_json_depth(conn, "t", past)
        # A query that runs long before its first row is interrupted inside SQLite.
        conn.executemany(
            "INSERT INTO writes VALUES ('t', '', ?, 'task', 0, 'c', 'json', '[]')",
            [(f"orphan-{i}",) for i in range(20_000)],
        )
        conn.execute("DELETE FROM checkpoints")
        with pytest.raises(ValueError, match="deadline exceeded") as raised:
            _check_json_depth(conn, "t", past)
        assert isinstance(raised.value.__context__, sqlite3.OperationalError)
    finally:
        conn.close()


def test_an_expired_deadline_is_reported_before_any_value_is_scanned(tmp_path):
    db = _db(tmp_path)
    _execute(db, "UPDATE checkpoints SET metadata = ? WHERE rowid = (SELECT max(rowid) FROM checkpoints)",
             with_key('{"source": "loop", "step": 2}', "x", nested(OVER_CAP)))
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError, match="deadline exceeded"):
            _check_json_depth(conn, "t", time.monotonic() - 1)
        # A thread with no rows at all still reports the expired deadline.
        with pytest.raises(ValueError, match="deadline exceeded"):
            _check_json_depth(conn, "no-such-thread", time.monotonic() - 1)
    finally:
        conn.close()


def test_deadline_passing_during_the_last_scan_is_reported(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        SqliteSaver(conn).setup()
        deadline = time.monotonic() + 3600
        real = sqlite_snapshot.check_text_depth
        scanned = {"n": 0}
        total = sum(1 for q in sqlite_snapshot._JSON_TEXTS for _ in conn.execute(q, ("t",)))

        def scan(text):
            real(text)
            scanned["n"] += 1
            if scanned["n"] == total:  # the clock runs out while the last value is scanned
                monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=lambda: deadline + 1))

        monkeypatch.setattr(sqlite_snapshot, "check_text_depth", scan)
        with pytest.raises(ValueError, match="deadline exceeded"):
            _check_json_depth(conn, "t", deadline)
    finally:
        conn.close()


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

