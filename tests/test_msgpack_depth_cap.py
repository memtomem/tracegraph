"""msgpack nesting is capped before LangGraph's decoder sees it.

The checkpoint serializer unpacks every extension payload with a new ``ormsgpack.unpackb`` call.
ormsgpack refuses more than 1022 levels per call, so stacked extensions nest without bound until
the C stack overflows and the process dies with no Python exception. ``check_msgpack_depth``
walks the bytes first and refuses a path with more than ``MAX_MSGPACK_DEPTH`` containers or
``MAX_MSGPACK_EXT_DEPTH`` extensions.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ormsgpack
import pytest
from langgraph.checkpoint.serde import jsonplus

from tracegraph import input_validation, sqlite_snapshot
from tracegraph.input_validation import (
    MAX_MSGPACK_DEPTH,
    MAX_MSGPACK_EXT_DEPTH,
    check_msgpack_depth,
)
from tracegraph.sqlite_snapshot import _inert_serde, _scanning_serde, ingest_snapshot

CHECKPOINTS = Path(__file__).parent / "fixtures" / "langgraph" / "native-failure.sqlite"
REFUSED = "msgpack nesting too deep"


def ext(code: int, payload: bytes) -> bytes:
    """An ext32 of ``code`` (negative codes as their signed byte)."""
    return b"\xc9" + struct.pack(">I", len(payload)) + struct.pack(">b", code) + payload


def constructor(code: int, arg: bytes) -> bytes:
    """A code 0-5 extension as LangGraph writes it: ``[module, name, arg]``."""
    return ext(code, ormsgpack.packb(["m", "n", "PLACEHOLDER_"]).replace(b"\xacPLACEHOLDER_", arg))


def arrays(depth: int, inner: bytes = b"\xc0") -> bytes:
    return b"\x91" * depth + inner


def stacked(levels: int, per_level: int, codes=(1,), *, outer: bool = True) -> bytes:
    """``levels`` nested extensions, each holding ``per_level`` nested arrays (the advisory)."""
    inner = b"\xc0"
    for i in range(levels):
        code = codes[i % len(codes)]
        body = arrays(per_level, inner)
        inner = ext(code, body) if code in (6, 7) or code > 7 or code < 0 else constructor(code, body)
    return arrays(per_level, inner) if outer else inner


def ext_chain(levels: int, code: int = 7) -> bytes:
    """``levels`` extensions each directly holding the next: no containers at all."""
    inner = b"\xc0"
    for _ in range(levels):
        inner = ext(code, inner)
    return inner


def refused(data) -> bool:
    try:
        check_msgpack_depth(data)
    except ValueError as exc:
        assert str(exc) == REFUSED
        return True
    return False


# Over-cap values used as a sibling or child: one by containers, one by extensions.
DEEP = arrays(MAX_MSGPACK_DEPTH)  # MAX + 1 containers counting the outer wrapper below
EXT_DEEP = ext_chain(MAX_MSGPACK_EXT_DEPTH + 1)


# --- the caps ----------------------------------------------------------------------------


def test_container_depth_at_the_cap_passes_and_one_more_is_refused():
    assert not refused(arrays(MAX_MSGPACK_DEPTH - 1, b"\x90"))  # last one empty: MAX in all
    assert refused(arrays(MAX_MSGPACK_DEPTH, b"\x90"))


def test_containers_are_counted_across_extension_payloads():
    # c1 levels hold their own [module, name, arg] array plus `per` arrays.
    per = 7
    levels = (MAX_MSGPACK_DEPTH - per) // (per + 1)
    assert levels * (per + 1) + per <= MAX_MSGPACK_DEPTH
    assert not refused(stacked(levels, per))
    assert refused(stacked(levels + 1, per))


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5, 6, 7, 42, -1])
def test_extension_depth_at_the_cap_passes_and_one_more_is_refused(code):
    # Every code is descended, including ones the installed hook does not unpack.
    assert not refused(ext_chain(MAX_MSGPACK_EXT_DEPTH, code))
    assert refused(ext_chain(MAX_MSGPACK_EXT_DEPTH + 1, code))


def test_a_value_at_both_caps_at_once_passes():
    # 256 constructor extensions, each payload its own array plus seven: 2048 and 256 exactly.
    at_both = stacked(MAX_MSGPACK_EXT_DEPTH, 7, outer=False)
    assert not refused(at_both)
    assert refused(b"\x91" + at_both)
    assert refused(ext(7, at_both))


def test_mixed_codes_count_as_one_chain():
    assert not refused(stacked(MAX_MSGPACK_EXT_DEPTH, 0, codes=(1, 7, 3)))
    assert refused(stacked(MAX_MSGPACK_EXT_DEPTH + 1, 0, codes=(1, 7, 3)))


def test_the_advisory_payload_is_refused():
    assert refused(stacked(100, 1000))
    assert refused(stacked(100, 1000, codes=(7,)))
    assert refused(stacked(100, 1000, codes=(1, 7)))


def test_map_keys_and_values_both_count():
    key_path = b"\x81" + arrays(MAX_MSGPACK_DEPTH - 1, b"\x90") + b"\xc0"  # {deep key: nil}
    assert refused(key_path)
    assert not refused(b"\x81" + arrays(MAX_MSGPACK_DEPTH - 2, b"\x90") + b"\xc0")
    value_path = b"\x81\xc0" + arrays(MAX_MSGPACK_DEPTH - 1, b"\x90")  # {nil: deep value}
    assert refused(value_path)
    # A map's second pair is still read: two children per entry.
    assert refused(b"\x82\xc0\xc0\xc0" + arrays(MAX_MSGPACK_DEPTH - 1, b"\x90"))


def test_siblings_do_not_add_up():
    wide = b"\xdc" + struct.pack(">H", 300) + ext_chain(MAX_MSGPACK_EXT_DEPTH - 1) * 300
    assert not refused(wide)
    flat = b"\xdc" + struct.pack(">H", 3000) + b"\x90" * 3000
    assert not refused(flat)


def test_a_deep_value_after_many_siblings_is_still_found():
    many = b"\xdd" + struct.pack(">I", 200_001) + b"\x01" * 200_000 + DEEP
    assert refused(many)


# --- header families: each is read as exactly one object -------------------------------

FAMILIES = {
    "positive-fixint": b"\x05",
    "negative-fixint": b"\xff",
    "nil": b"\xc0",
    "false": b"\xc2",
    "true": b"\xc3",
    "float32": b"\xca" + b"\x91" * 4,
    "float64": b"\xcb" + b"\x91" * 8,
    "uint8": b"\xcc\x91",
    "uint16": b"\xcd" + b"\x91" * 2,
    "uint32": b"\xce" + b"\x91" * 4,
    "uint64": b"\xcf" + b"\x91" * 8,
    "int8": b"\xd0\x91",
    "int16": b"\xd1" + b"\x91" * 2,
    "int32": b"\xd2" + b"\x91" * 4,
    "int64": b"\xd3" + b"\x91" * 8,
    "fixstr": b"\xa3\x91\x91\x91",
    "str8": b"\xd9\x03\x91\x91\x91",
    "str16": b"\xda\x00\x03\x91\x91\x91",
    "str32": b"\xdb\x00\x00\x00\x03\x91\x91\x91",
    "bin8": b"\xc4\x03\x91\x91\x91",
    "bin16": b"\xc5\x00\x03\x91\x91\x91",
    "bin32": b"\xc6\x00\x00\x00\x03\x91\x91\x91",
    "fixarray": b"\x92\x01\x02",
    "array16": b"\xdc\x00\x02\x01\x02",
    "array32": b"\xdd\x00\x00\x00\x02\x01\x02",
    "fixmap": b"\x81\x01\x02",
    "map16": b"\xde\x00\x01\x01\x02",
    "map32": b"\xdf\x00\x00\x00\x01\x01\x02",
    "fixext1": b"\xd4\x07\x01",
    "fixext2": b"\xd5\x07\xa1x",
    "fixext4": b"\xd6\x07\xa3xyz",
    "fixext8": b"\xd7\x07\xa7abcdefg",
    "fixext16": b"\xd8\x07\xafabcdefghijklmno",
    "ext8": b"\xc7\x01\x07\xc0",
    "ext16": b"\xc8\x00\x01\x07\xc0",
    "ext32": b"\xc9\x00\x00\x00\x01\x07\xc0",
}


@pytest.mark.parametrize("family", FAMILIES)
def test_each_header_is_read_as_one_object(family, monkeypatch):
    # With a cap of 3, `[X, [[[]]]]` is 4 deep and `[X, []]` is 2: misreading X's length
    # would shift the parse and change one of the two outcomes.
    monkeypatch.setattr(input_validation, "MAX_MSGPACK_DEPTH", 3)
    value = FAMILIES[family]
    assert refused(b"\x92" + value + b"\x91\x91\x90")
    assert not refused(b"\x92" + value + b"\x90")


def test_container_looking_bytes_in_strings_and_binaries_do_not_count():
    body = b"\x91" * (MAX_MSGPACK_DEPTH + 10)
    assert not refused(b"\xc5" + struct.pack(">H", len(body)) + body)
    assert not refused(b"\xda" + struct.pack(">H", len(body)) + body)


def test_a_value_that_is_not_bytes_like_is_not_scanned():
    for value in (None, 1, arrays(MAX_MSGPACK_DEPTH + 5).decode("latin-1")):
        check_msgpack_depth(value)
    for kind in (bytearray, memoryview):
        assert refused(kind(arrays(MAX_MSGPACK_DEPTH + 5)))


# --- malformed data: contained to the extension that holds it ----------------------------


def test_malformed_top_level_data_ends_the_scan_where_the_decoder_fails_too():
    for value in (b"\xc1", b"\x92\xc1" + DEEP, b"\x92\x01", b"\xd9\x10abc"):
        assert not refused(value)
        with pytest.raises(ValueError):
            ormsgpack.unpackb(value)


MALFORMED_EXTS = {
    "never-used-byte": ext(8, b"\xc1"),
    "empty-payload": ext(1, b""),
    "array-longer-than-payload": ext(7, b"\x92\x01"),
    "length-past-payload": ext(7, b"\xd9\x10abc"),
    "header-past-payload": ext(7, b"\xdc\x00"),
}


@pytest.mark.parametrize("bad", MALFORMED_EXTS)
@pytest.mark.parametrize("deep", [DEEP, EXT_DEEP], ids=["containers", "extensions"])
def test_a_malformed_extension_does_not_hide_the_next_sibling(bad, deep):
    # The decoder returns None or a fallback for the bad extension and reads the sibling.
    assert refused(b"\x92" + MALFORMED_EXTS[bad] + deep)
    # The same one level down, inside another extension's payload.
    assert refused(ext(7, b"\x92" + MALFORMED_EXTS[bad] + deep))


@pytest.mark.parametrize("deep", [DEEP, EXT_DEEP], ids=["containers", "extensions"])
def test_a_deep_child_before_a_malformed_suffix_is_refused(deep):
    # The decoder descends into the child before it reaches the bad byte.
    assert refused(b"\x92" + deep + b"\xc1")
    assert refused(ext(7, b"\x92" + deep + b"\xc1"))


def test_recovery_keeps_the_depth_of_the_enclosing_path(monkeypatch):
    monkeypatch.setattr(input_validation, "MAX_MSGPACK_DEPTH", 4)
    # Three arrays, a bad extension, then a sibling two deep: 3 + 2 = 5 > 4.
    assert refused(b"\x91\x91\x92" + MALFORMED_EXTS["never-used-byte"] + b"\x91\x90")
    assert not refused(b"\x91\x91\x92" + MALFORMED_EXTS["never-used-byte"] + b"\x90")
    monkeypatch.setattr(input_validation, "MAX_MSGPACK_EXT_DEPTH", 2)
    bad_inside = ext(7, b"\x92" + MALFORMED_EXTS["never-used-byte"] + ext(7, b"\xc0"))
    assert refused(ext(7, bad_inside))  # 3 extensions on the sibling's path
    assert not refused(bad_inside)


def test_bytes_after_a_payloads_object_are_not_decoded_and_not_counted():
    for deep in (DEEP, EXT_DEEP):
        value = ext(7, b"\xc0" + deep)
        assert not refused(value)
        assert _inert_serde().loads_typed(("msgpack", value)) is not None  # no crash, decoded
        # Inside a container the walk resumes after the payload, at the next sibling.
        value = b"\x92" + ext(7, b"\xc0" + deep) + b"\x01"
        assert not refused(value)
        assert _inert_serde().loads_typed(("msgpack", value))[1] == 1


def test_an_unfinished_payload_does_not_read_into_the_next_sibling():
    # The extension's three arrays end at its payload; the sibling is read at the outer
    # array's depth (1 + MAX - 1), not under them.
    assert not refused(b"\x92" + ext(7, b"\x91\x91\x91") + arrays(MAX_MSGPACK_DEPTH - 2, b"\x90"))
    assert refused(b"\x92" + ext(7, b"\x91\x91\x91") + arrays(MAX_MSGPACK_DEPTH - 1, b"\x90"))


# --- the walk itself -----------------------------------------------------------------------


def test_the_walk_does_not_recurse():
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        assert not refused(stacked(MAX_MSGPACK_EXT_DEPTH, 0))
        assert refused(arrays(MAX_MSGPACK_DEPTH + 1))
    finally:
        sys.setrecursionlimit(limit)


def test_tick_runs_during_a_long_walk_and_can_stop_it():
    calls = []
    value = ormsgpack.packb(list(range(200_000)))
    check_msgpack_depth(value, lambda: calls.append(1))
    assert len(calls) == 200_001 // 65_536

    def stop():
        raise ValueError("deadline")

    with pytest.raises(ValueError, match="^deadline$"):
        check_msgpack_depth(value, stop)


def _random_tree(rng: random.Random, budget: list[int], depth: int = 0):
    """A nested value as (packed bytes, containers on its deepest path, extensions on it)."""
    budget[0] -= 1
    kind = rng.random() if budget[0] > 0 and depth < 60 else 0.0
    if kind < 0.35:
        return rng.choice([b"\xc0", b"\x07", b"\xa2hi", b"\xcb" + b"\x00" * 8]), 0, 0
    if kind < 0.6:
        items = [_random_tree(rng, budget, depth + 1) for _ in range(rng.randint(0, 3))]
        body = b"".join(i[0] for i in items)
        return (bytes([0x90 | len(items)]) + body, 1 + max((i[1] for i in items), default=0),
                max((i[2] for i in items), default=0))
    if kind < 0.8:
        pairs = [(_random_tree(rng, budget, depth + 1), _random_tree(rng, budget, depth + 1))
                 for _ in range(rng.randint(0, 2))]
        flat = [x for pair in pairs for x in pair]
        body = b"".join(i[0] for i in flat)
        return (bytes([0x80 | len(pairs)]) + body, 1 + max((i[1] for i in flat), default=0),
                max((i[2] for i in flat), default=0))
    inner, containers, exts = _random_tree(rng, budget, depth + 1)
    code = rng.choice([1, 7, 42])
    if code == 1:
        return constructor(1, inner), containers + 1, exts + 1
    return ext(code, inner), containers, exts + 1


def test_random_values_are_refused_exactly_past_each_cap(monkeypatch):
    rng = random.Random(8)
    for _ in range(400):
        value, containers, exts = _random_tree(rng, [rng.randint(1, 80)])
        monkeypatch.setattr(input_validation, "MAX_MSGPACK_DEPTH", max(containers, 1))
        monkeypatch.setattr(input_validation, "MAX_MSGPACK_EXT_DEPTH", max(exts, 1))
        assert not refused(value), value
        if containers:
            monkeypatch.setattr(input_validation, "MAX_MSGPACK_DEPTH", containers - 1)
            assert refused(value), value
            monkeypatch.setattr(input_validation, "MAX_MSGPACK_DEPTH", containers)
        if exts:
            monkeypatch.setattr(input_validation, "MAX_MSGPACK_EXT_DEPTH", exts - 1)
            assert refused(value), value


def test_the_installed_serializer_has_the_extension_codes_this_was_measured_against():
    # Not a safety check: every code is descended. A new code means re-measuring the caps.
    codes = {v for k, v in vars(jsonplus).items() if k.startswith("EXT_") and isinstance(v, int)}
    assert codes == set(range(8))


# --- `ingest --sqlite` ---------------------------------------------------------------------


def _db(tmp_path, name: str = "db") -> Path:
    db = tmp_path / f"{name}.sqlite"
    shutil.copy(CHECKPOINTS, db)
    return db


def _set_body(db: Path, value: bytes | str, *, ns: str | None = None) -> None:
    conn = sqlite3.connect(db)
    try:
        if ns is None:
            conn.execute("UPDATE checkpoints SET type = 'msgpack', checkpoint = ? "
                         "WHERE rowid = (SELECT max(rowid) FROM checkpoints)", (value,))
        else:
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "parent_checkpoint_id, type, checkpoint, metadata) SELECT thread_id, ?, "
                "checkpoint_id, parent_checkpoint_id, 'msgpack', ?, metadata FROM checkpoints "
                "WHERE rowid = (SELECT max(rowid) FROM checkpoints)", (ns, value))
        conn.commit()
    finally:
        conn.close()


def _add_write(db: Path, value: bytes, *, checkpoint_id: str | None = None, idx: int = 999) -> None:
    conn = sqlite3.connect(db)
    try:
        thread, ns, cid, task = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id FROM writes LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, "
            "type, value) VALUES (?, ?, ?, ?, ?, 'extra', 'msgpack', ?)",
            (thread, ns, checkpoint_id or cid, task, idx, value),
        )
        conn.commit()
    finally:
        conn.close()


def _ingest(db: Path):
    return ingest_snapshot(db, "t", error_channel="error")


def _refused_ingest(db: Path) -> None:
    with pytest.raises(ValueError, match=r"^checkpoint history cannot be decoded \(msgpack "
                                         r"nesting too deep\)$"):
        _ingest(db)


BOMBS = {
    "code-1": stacked(100, 1000),
    "code-7": stacked(100, 1000, codes=(7,)),
    "mixed": stacked(100, 1000, codes=(1, 7)),
}


@pytest.mark.parametrize("where", ["body", "write"])
@pytest.mark.parametrize("bomb", BOMBS)
def test_the_cli_refuses_a_crafted_database_instead_of_crashing(tmp_path, where, bomb):
    db = _db(tmp_path)
    (_set_body if where == "body" else _add_write)(db, BOMBS[bomb])
    res = subprocess.run(
        [sys.executable, "-c", "from tracegraph.cli import main; main()",
         "ingest", "--sqlite", str(db), "--thread", "t", "--out", str(tmp_path / "a.json")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "msgpack nesting too deep" in res.stderr
    assert "Traceback" not in res.stderr
    assert not (tmp_path / "a.json").exists()


def test_a_write_at_both_caps_still_ingests(tmp_path):
    per = 7
    levels = (MAX_MSGPACK_DEPTH - per) // (per + 1)
    db = _db(tmp_path)
    _add_write(db, stacked(levels, per))
    _add_write(db, ext_chain(MAX_MSGPACK_EXT_DEPTH), idx=998)
    _add_write(db, stacked(MAX_MSGPACK_EXT_DEPTH, 7, outer=False), idx=997)
    assert _ingest(db).steps


def test_a_write_the_saver_never_reads_is_not_scanned(tmp_path):
    db = _db(tmp_path)
    _add_write(db, BOMBS["code-1"], checkpoint_id="no-such-checkpoint")
    baseline = _ingest(_db(tmp_path, "base"))
    assert _ingest(db).model_dump() == baseline.model_dump()


def test_every_namespace_is_scanned(tmp_path):
    db = _db(tmp_path)
    _set_body(db, BOMBS["code-7"], ns="sub:1")
    _refused_ingest(db)


def test_a_text_value_keeps_the_savers_own_error(tmp_path):
    db = _db(tmp_path)
    _set_body(db, arrays(MAX_MSGPACK_DEPTH + 5).decode("latin-1"))  # stored as TEXT
    with pytest.raises(ValueError, match=r"cannot be decoded \(ValueError\)"):
        _ingest(db)


def test_json_is_checked_before_msgpack_and_keeps_its_message(tmp_path):
    db = _db(tmp_path)
    _add_write(db, BOMBS["code-1"])
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE checkpoints SET metadata = ? WHERE rowid = "
                     "(SELECT max(rowid) FROM checkpoints)", ("[" * 9001 + "]" * 9001,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match=r"^checkpoint history cannot be decoded \(JSON nesting "
                                         r"too deep\)$"):
        _ingest(db)


# --- the scan sits where the saver decodes ----------------------------------------------


def _schema_with_nocase_threads(path: Path, write: bytes) -> None:
    """The fixture rewritten under thread ``T`` with a ``COLLATE NOCASE`` thread column.

    Asked for thread ``t``, the saver lists these checkpoints and then reads their writes by the
    stored ``T``. A scan that selected writes by the requested ``t`` would miss ``write``.
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
        dst.execute("INSERT INTO writes VALUES ('T', ?, ?, ?, 999, 'extra', 'msgpack', ?)",
                    (ns, cid, task, write))
        dst.commit()
    finally:
        src.close()
        dst.close()


def test_a_write_reached_through_a_collation_is_still_scanned(tmp_path):
    db = tmp_path / "nocase.sqlite"
    _schema_with_nocase_threads(db, BOMBS["code-1"])
    res = subprocess.run(
        [sys.executable, "-c", "from tracegraph.cli import main; main()",
         "ingest", "--sqlite", str(db), "--thread", "t", "--out", str(tmp_path / "a.json")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "msgpack nesting too deep" in res.stderr


def test_a_copy_of_the_serializer_shares_the_refusal():
    serde, refusal = _scanning_serde(time.monotonic() + 3600)
    clone = serde.with_msgpack_allowlist([("some.module", "Thing")])
    assert clone is not serde and type(clone) is type(serde)
    with pytest.raises(ValueError, match=r"\(msgpack nesting too deep\)$"):
        clone.loads_typed(("msgpack", BOMBS["code-7"]))
    assert refusal == ["checkpoint history cannot be decoded (msgpack nesting too deep)"]
    # Every later decode, on either copy, is refused too.
    with pytest.raises(ValueError, match="msgpack nesting too deep"):
        serde.loads_typed(("msgpack", ormsgpack.packb([1])))


def test_the_serializer_still_decodes_and_keeps_its_allowlist():
    serde, refusal = _scanning_serde(time.monotonic() + 3600)
    assert serde.loads_typed(("msgpack", ormsgpack.packb({"a": [1, 2]}))) == {"a": [1, 2]}
    assert serde.loads_typed(("json", b'{"a": 1}')) == {"a": 1}
    # An unregistered constructor still comes back inert rather than being called.
    payload = constructor(1, ormsgpack.packb(["arg"]))
    assert serde.loads_typed(("msgpack", payload)) == ["arg"]
    assert refusal == []


def test_a_refusal_the_adapter_swallows_is_still_reported(tmp_path, monkeypatch):
    real = sqlite_snapshot.LangGraphCheckpointAdapter.ingest

    def swallowing(self, thread):
        try:
            self._saver.serde.loads_typed(("msgpack", BOMBS["code-1"]))
        except ValueError:
            pass
        return real(self, thread)

    monkeypatch.setattr(sqlite_snapshot.LangGraphCheckpointAdapter, "ingest", swallowing)
    _refused_ingest(_db(tmp_path))


def test_a_refusal_swallowed_after_the_last_decode_is_still_reported(tmp_path, monkeypatch):
    # Nothing decodes after the failure, so only the check once the adapter returns sees it.
    real = sqlite_snapshot.LangGraphCheckpointAdapter.ingest

    def swallowing_last(self, thread):
        raw = real(self, thread)
        try:
            self._saver.serde.loads_typed(("msgpack", BOMBS["code-1"]))
        except ValueError:
            pass
        return raw

    monkeypatch.setattr(sqlite_snapshot.LangGraphCheckpointAdapter, "ingest", swallowing_last)
    _refused_ingest(_db(tmp_path))


# --- the snapshot deadline ---------------------------------------------------------------


def _clock_after(monkeypatch, *, when) -> None:
    """Make ``sqlite_snapshot``'s clock read past the deadline once ``when`` returns true."""
    real = time.monotonic
    start = real()

    def monotonic():
        return start + 10_000 if when() else real()

    monkeypatch.setattr(sqlite_snapshot, "time", SimpleNamespace(monotonic=monotonic))


def test_a_long_msgpack_walk_is_charged_to_the_deadline(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _add_write(db, ormsgpack.packb(list(range(200_000))))
    ticks = {"n": 0}
    real = sqlite_snapshot.check_msgpack_depth

    def counting(value, tick=None):
        def counted():
            ticks["n"] += 1
            tick()
        real(value, counted)

    monkeypatch.setattr(sqlite_snapshot, "check_msgpack_depth", counting)
    _clock_after(monkeypatch, when=lambda: ticks["n"] > 0)
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        _ingest(db)
    assert ticks["n"] == 1  # stopped at the first tick, inside the walk


def test_nesting_found_while_the_deadline_passes_is_reported_as_nesting(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _add_write(db, BOMBS["code-1"])
    seen = {"bomb": False}
    real = sqlite_snapshot.check_msgpack_depth

    def scan(value, tick=None):
        # The clock runs out as the crafted value's walk starts; the walk finds the depth
        # before its first tick.
        seen["bomb"] = seen["bomb"] or value == BOMBS["code-1"]
        real(value, tick)

    monkeypatch.setattr(sqlite_snapshot, "check_msgpack_depth", scan)
    _clock_after(monkeypatch, when=lambda: seen["bomb"])
    _refused_ingest(db)


def test_an_expired_deadline_is_reported_before_a_value_is_walked():
    serde, refusal = _scanning_serde(time.monotonic() - 1)
    # Deep first: without the check before the walk, it would report depth instead.
    for value in (arrays(MAX_MSGPACK_DEPTH + 1), ormsgpack.packb([1])):
        with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
            serde.loads_typed(("msgpack", value))
    assert refusal == ["SQLite snapshot deadline exceeded"]


def test_a_deadline_passing_during_a_short_walk_is_reported_once_it_ends(tmp_path, monkeypatch):
    # Too short to reach a tick inside the walk, and malformed, so the decoder would fail too.
    db = _db(tmp_path)
    _add_write(db, b"\xc1")
    walked = {"done": False}
    real = sqlite_snapshot.check_msgpack_depth

    def scan(value, tick=None):
        real(value, tick)
        walked["done"] = walked["done"] or value == b"\xc1"

    monkeypatch.setattr(sqlite_snapshot, "check_msgpack_depth", scan)
    _clock_after(monkeypatch, when=lambda: walked["done"])
    with pytest.raises(ValueError, match="^SQLite snapshot deadline exceeded$"):
        _ingest(db)
