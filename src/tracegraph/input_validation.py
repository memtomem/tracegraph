"""Shape checks at untrusted JSON boundaries; diagnostics never echo field values."""

import json
import re
from typing import Any

#: Deepest container nesting accepted in a JSON text, on every interpreter. Python 3.12 and
#: 3.13's ``json`` refuse near 10,000 less the C frames already on the stack (9,984 through
#: the CLI, 9,979 under pytest); 3.14's parses as deep as the C stack allows. The margin
#: keeps this cap, not the interpreter, the limit a caller meets.
MAX_JSON_DEPTH = 9000

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


def object_field(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value
