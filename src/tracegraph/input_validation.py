"""Shape checks at untrusted JSON boundaries; diagnostics never echo field values."""

import functools
import json
import re
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

#: Deepest container nesting accepted in a JSON text, on every interpreter. Python 3.12 and
#: 3.13's ``json`` refuse near 10,000 less the C frames already on the stack (9,984 through
#: the CLI, 9,979 under pytest); 3.14's parses as deep as the C stack allows. The margin
#: keeps this cap, not the interpreter, the limit a caller meets.
MAX_JSON_DEPTH = 9000

#: Deepest container nesting accepted on one path of a msgpack value, counted across the
#: extension payloads LangGraph's checkpoint serializer unpacks with a fresh ``unpackb`` call.
#: ormsgpack refuses more than 1022 levels per call, so a value without extensions never
#: reaches this; stacked extensions start a new 1022 each time, and the decoder then runs out
#: of C stack with no Python exception. Measured: 3.12 and 3.13 crash from about 76,000
#: levels on an 8 MiB stack (macOS and Linux), scaling down with the stack (about 97 bytes a
#: level, 1.37 KB an extension). At this cap and ``MAX_MSGPACK_EXT_DEPTH`` together, arrays,
#: maps, deep map keys and every extension code still decoded on a 1 MiB stack.
MAX_MSGPACK_DEPTH = 2048

#: Deepest nesting of msgpack extensions on one path (see ``MAX_MSGPACK_DEPTH``).
MAX_MSGPACK_EXT_DEPTH = 256

# Bytes after a type byte before the payload: (length-field size, extra fixed bytes).
_MSGPACK_SCALAR = {0xC0: 0, 0xC2: 0, 0xC3: 0, 0xCA: 4, 0xCB: 8, 0xCC: 1, 0xCD: 2, 0xCE: 4,
                   0xCF: 8, 0xD0: 1, 0xD1: 2, 0xD2: 4, 0xD3: 8}
_MSGPACK_SIZED = {0xC4: 1, 0xC5: 2, 0xC6: 4, 0xD9: 1, 0xDA: 2, 0xDB: 4}  # bin, str
_MSGPACK_EXT = {0xC7: 1, 0xC8: 2, 0xC9: 4}  # ext8/16/32: length size, then a type byte
_MSGPACK_FIXEXT = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}
_MSGPACK_ARRAY = {0xDC: 2, 0xDD: 4}
_MSGPACK_MAP = {0xDE: 2, 0xDF: 4}
_MSGPACK_TICK = 1 << 16

_TOKEN = re.compile(r'["\[\]{}]')
# Possessive, so an unterminated string fails in one linear pass instead of retrying from
# every escaped quote inside it.
_STRING_REST = re.compile(r'(?:[^"\\]|\\.)*+"', re.S)


def check_text_depth(text: str | bytes) -> None:
    """Refuse a JSON text nested deeper than ``MAX_JSON_DEPTH``, before it is parsed.

    Counted on the text, not the parsed value: a value a duplicate key later overwrites, or
    one a reviver turns into an object, is gone once parsed but was still nested in the
    text. Brackets inside strings do not count. Not a validator: undecodable bytes or an
    unterminated string end the scan, and malformed text is left for ``json.loads`` to report.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = text.decode(json.detect_encoding(text), "surrogatepass")
        except UnicodeDecodeError:
            return  # not text at all: the parser's own error says so
    depth = 0
    pos = 0
    search = _TOKEN.search
    string_rest = _STRING_REST.match
    while (token := search(text, pos)) is not None:
        pos = token.end()
        char = token.group()
        if char == '"':
            rest = string_rest(text, pos)
            if rest is None:
                return
            pos = rest.end()
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting too deep")
        else:
            depth -= 1


def check_msgpack_depth(data: object, tick: Callable[[], None] | None = None) -> None:
    """Refuse a msgpack value nested too deep for LangGraph's decoder, before it is decoded.

    Counts, on each path, every container (map keys included, empty ones too) and every
    extension, descending into the payload of an extension of any code: LangGraph's hook
    unpacks codes 0-7 again, and a newer release may add another. A payload is walked as the
    one object the hook unpacks from it, and bytes after that object are skipped, as the
    decoder skips them. Malformed data inside a payload ends only that extension, because the
    decoder returns ``None`` or a fallback for it and goes on to the next sibling; at the top
    level it ends the scan, and the decoder reports it. A value that is not bytes-like is not
    scanned: ormsgpack refuses it. ``tick`` is called every 65,536 objects so a caller can
    charge a long scan to a deadline. One linear pass with an explicit stack.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return
    buf = bytes(data)
    # Each frame is [objects still to read, payload end]: end -1 for a container; an extension
    # frame reads the single object its payload holds.
    stack: list[list[int]] = []
    limits = [len(buf)]
    containers = exts = 0
    pos = objects = 0
    while True:
        objects += 1
        if tick is not None and objects % _MSGPACK_TICK == 0:
            tick()
        limit = limits[-1]
        children = payload = -1
        malformed = pos >= limit
        if not malformed:
            byte = buf[pos]
            pos += 1
            if byte <= 0x7F or byte >= 0xE0:
                pass
            elif byte <= 0x8F:
                children = 2 * (byte & 0x0F)
            elif byte <= 0x9F:
                children = byte & 0x0F
            elif byte <= 0xBF:
                pos += byte & 0x1F
            elif byte in _MSGPACK_SCALAR:
                pos += _MSGPACK_SCALAR[byte]
            else:
                width = (_MSGPACK_SIZED.get(byte) or _MSGPACK_EXT.get(byte)
                         or _MSGPACK_ARRAY.get(byte) or _MSGPACK_MAP.get(byte))
                if width is not None:
                    if pos + width > limit:
                        malformed = True
                    else:
                        length = int.from_bytes(buf[pos:pos + width], "big")
                        pos += width
                        if byte in _MSGPACK_SIZED:
                            pos += length
                        elif byte in _MSGPACK_EXT:
                            pos += 1  # the extension's type byte
                            payload = length
                        elif byte in _MSGPACK_ARRAY:
                            children = length
                        else:
                            children = 2 * length
                elif byte in _MSGPACK_FIXEXT:
                    pos += 1
                    payload = _MSGPACK_FIXEXT[byte]
                else:  # 0xC1, never used
                    malformed = True
            if pos + max(payload, 0) > limit:
                malformed = True
        if malformed:
            # Discard the innermost extension being read and everything open inside it, then
            # resume after its payload; with no extension open, the decoder fails as a whole.
            while stack and stack[-1][1] < 0:
                stack.pop()
                containers -= 1
            if not stack:
                return
            pos = stack.pop()[1]
            limits.pop()
            exts -= 1
        elif children >= 0:
            containers += 1
            if containers > MAX_MSGPACK_DEPTH:
                raise ValueError("msgpack nesting too deep")
            if children:
                stack.append([children, -1])
                continue
            containers -= 1
        elif payload >= 0:
            exts += 1
            if exts > MAX_MSGPACK_EXT_DEPTH:
                raise ValueError("msgpack nesting too deep")
            if payload:
                stack.append([1, pos + payload])
                limits.append(pos + payload)
                continue
            exts -= 1
        # One object is complete: close every frame it was the last object of.
        while stack:
            frame = stack[-1]
            frame[0] -= 1
            if frame[0]:
                break
            stack.pop()
            if frame[1] < 0:
                containers -= 1
            else:
                pos = frame[1]
                limits.pop()
                exts -= 1
        else:
            return


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON contains the non-standard constant {name}")


def loads(text: str, *, strict: bool = False) -> Any:
    """Parse JSON nested at most ``MAX_JSON_DEPTH`` deep (see ``check_text_depth``).

    ``strict`` also refuses ``NaN``/``Infinity``, which strict parsers reject.
    """
    check_text_depth(text)
    try:
        if strict:
            return json.loads(text, parse_constant=_reject_constant)
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError("JSON nesting too deep") from exc


#: For a span document read by the OTLP or Phoenix adapter; Phoenix reads through OTLP's, so the
#: two share one message.
TRACE_UNREADABLE = "trace document cannot be read: recursion limit exceeded"


def refuses_deep_nesting(message: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Report a ``RecursionError`` raised inside the wrapped entry point as ``ValueError(message)``.

    For library callers that hand over documents they decoded themselves: those are not
    depth-capped, and reading one nested deep enough recurses (formatting a value, or code
    LangGraph runs). The error does not prove the input is deep, since a caller-supplied saver,
    graph or object can recurse on its own, so ``message`` names the limit, not a cause.
    """

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return fn(*args, **kwargs)
            except RecursionError as exc:
                raise ValueError(message) from exc

        return wrapper

    return decorate


def object_field(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value
