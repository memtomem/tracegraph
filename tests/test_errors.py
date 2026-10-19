"""Exception descriptions never run caller code that can escape a ValueError boundary."""

from __future__ import annotations

import pytest
from test_langgraph_adapter import _StubSaver, _ck

from tracegraph import artifact
from tracegraph._errors import describe, type_name
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import analyze
from tracegraph.graph_ref import load_graph
from tracegraph.model import NormalizedTrace, RawTrace, Step, StepKind, Trace
from tracegraph.normalize import normalize


class _HostileStr(str):
    def __bool__(self):
        raise RuntimeError("bool")

    def __format__(self, spec):
        raise RuntimeError("format")

    def __add__(self, other):
        raise RuntimeError("add")


class _RaisingNameMeta(type):
    @property
    def __name__(cls):  # type: ignore[override]
        raise RuntimeError("type name")


class _HostileNameMeta(type):
    @property
    def __name__(cls):  # type: ignore[override]
        return _HostileStr("Hostile")


class UnnamedError(RuntimeError, metaclass=_RaisingNameMeta):
    pass


class HostileNameError(RuntimeError, metaclass=_HostileNameMeta):
    pass


class UnprintableError(RuntimeError):
    def __str__(self):
        raise RuntimeError("str")


class ExitingStrError(RuntimeError):
    def __str__(self):
        raise SystemExit(3)


class HostileStrError(RuntimeError):
    def __str__(self):
        return _HostileStr("message")


class InterruptingStrError(RuntimeError):
    def __str__(self):
        raise KeyboardInterrupt


def raised_value_error(call):
    """Run ``call`` and return the ValueError it raised, failing on anything else.

    Not ``pytest.raises``: on a regression pytest would format the escaped exception's chain,
    whose hostile members stop it with an internal error instead of a failure.
    """
    caught = None
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the type is what is asserted
        caught = exc
    if isinstance(caught, ValueError):
        return caught
    # object.__repr__ reads the C-level type name, never a metaclass property.
    leaked = "nothing" if caught is None else object.__repr__(caught)
    del caught
    pytest.fail(f"expected ValueError, got {leaked}")


#: Built inside the test, not passed as a parameter: on a regression pytest would repr a hostile
#: parameter while reporting the failure and stop with an internal error.
_CASES = {
    "ordinary": (lambda: RuntimeError("boom"), "RuntimeError: boom"),
    "system-exit": (lambda: SystemExit(4), "SystemExit: 4"),
    "empty": (lambda: RuntimeError(), "RuntimeError"),
    "raising-str": (lambda: UnprintableError("x"), "UnprintableError"),
    "exiting-str": (lambda: ExitingStrError("x"), "ExitingStrError"),
    "hostile-str": (lambda: HostileStrError("x"), "HostileStrError"),
    "raising-name": (lambda: UnnamedError("x"), "exception: x"),
    "hostile-name": (lambda: HostileNameError("x"), "exception: x"),
}


@pytest.mark.parametrize("case", list(_CASES))
def test_describe_returns_plain_text_for_any_exception(case):
    make, expected = _CASES[case]
    text, leaked = None, None
    try:
        text = describe(make())
    except BaseException as exc:  # noqa: BLE001 - reported without formatting it
        leaked = object.__repr__(exc)
    # Failing outside the handler: inside it the hostile exception stays the failure's context.
    if leaked is not None:
        pytest.fail(f"describe raised {leaked}")
    assert type(text) is str
    assert text == expected


def test_type_name_is_plain_text():
    assert type_name(RuntimeError()) == "RuntimeError"
    assert type(type_name(HostileNameError())) is str


def test_an_interrupt_while_describing_still_propagates():
    with pytest.raises(KeyboardInterrupt):
        describe(InterruptingStrError())


# --- each boundary listed in #109 ---


def _v1(edges):
    return {
        "schema_version": 1,
        "trace": {
            "trace": {"trace_id": "t", "source_kind": "x"},
            "steps": [{"step_id": "a", "trace_id": "t", "seq": 0}],
            "edges": edges,
        },
    }


def _failing(error):
    yield {"type": "CAUSED_BY", "src": "a", "dst": "a"}
    raise error


class _ShapeShifter:
    @property
    def __class__(self):  # type: ignore[override]
        raise RuntimeError("class")

    def __iter__(self):
        return iter([])


class _UnnamedFailure(BaseException, metaclass=_RaisingNameMeta):
    pass


@pytest.mark.parametrize(
    "edges",
    [lambda: _failing(SystemExit(3)), lambda: _failing(_UnnamedFailure()), _ShapeShifter],
    ids=["system-exit", "base-exception", "class-property"],
)
def test_v1_migration_refuses_any_edge_failure_as_value_error(edges):
    source = edges()
    error = raised_value_error(lambda: artifact.from_obj(_v1(source)))
    assert error.__cause__ is not None


def test_v1_migration_lets_an_interrupt_through():
    with pytest.raises(KeyboardInterrupt):
        artifact.from_obj(_v1(_failing(KeyboardInterrupt())))


def test_validated_copy_names_a_hostile_type_safely():
    class Unreadable(NormalizedTrace):
        def model_dump(self, **kwargs):
            raise UnnamedError("dump")

    nt = normalize(RawTrace(trace=Trace(trace_id="t", source_kind="x"),
                            steps=[Step(step_id="a", trace_id="t", seq=0, kind=StepKind.CHAIN)]))
    hostile = Unreadable.model_validate(nt.model_dump())
    for call in (lambda: artifact.dumps(hostile), lambda: analyze(hostile)):
        error = raised_value_error(call)
        assert str(error) == "trace could not be validated: exception"


def test_an_unreadable_checkpoint_listing_is_named_safely():
    class Saver(_StubSaver):
        def list(self, config, **kwargs):
            raise UnnamedError("list")
            yield  # pragma: no cover - makes this a generator

    error = raised_value_error(lambda: LangGraphCheckpointAdapter(Saver([])).ingest("A"))
    assert str(error) == "checkpoint history cannot be decoded (exception)"


def test_a_supplied_graph_that_fails_with_an_unprintable_error_is_refused():
    class Graph:
        def copy(self, update=None):
            return self

        def get_state(self, config):
            raise UnprintableError("state")

    cid = "1f1ae6ea-e7d8-6c28-8001-03afbf35e17c"
    adapter = LangGraphCheckpointAdapter(_StubSaver([_ck(cid, "", 1, None)]), schedule_graph=Graph())
    error = raised_value_error(lambda: adapter.ingest("A"))
    assert str(error).endswith(": UnprintableError")


def test_a_graph_module_that_fails_with_an_unprintable_error_is_refused(tmp_path):
    path = tmp_path / "hostile_graph.py"
    path.write_text(
        "class Unprintable(RuntimeError):\n"
        "    def __str__(self):\n"
        "        raise RuntimeError('str')\n"
        "raise Unprintable('x')\n",
        encoding="utf-8",
    )
    error = raised_value_error(lambda: load_graph(f"{path}:graph"))
    assert str(error).endswith(": Unprintable")
