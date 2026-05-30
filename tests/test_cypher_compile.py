"""Tests for the backend-neutral PathPattern → openCypher compiler.

No Kùzu dependency here — the compiler is pure stdlib so it stays importable (and verifiable)
in the default install. Equivalence with the pure-Python matcher is tested separately in
test_kuzu_store.py, which is the only place the contract "same input → same matches" must hold.
"""

from __future__ import annotations

import pytest

from tracegraph.analysis import (
    PRESETS,
    CompiledQuery,
    PathPattern,
    StepPredicate,
    compile_to_cypher,
)
from tracegraph.model import StepKind, StepStatus


def test_single_predicate_uses_bare_match_no_relationship():
    # A 1-step pattern is a degenerate path: no edges to traverse, just predicate-match.
    q = compile_to_cypher(PathPattern((StepPredicate(status=StepStatus.ERROR),)))
    assert "CAUSED_BY" not in q.cypher
    assert "MATCH (s0:Step)" in q.cypher
    assert q.result_vars == ("s0",)
    assert q.params == {"s0_status": "error"}
    assert "RETURN s0.step_id AS s0" in q.cypher


def test_multi_step_walks_caused_by_backwards_for_cause_to_effect_order():
    # PathPattern is cause→effect; CAUSED_BY is effect→cause; so MATCH walks <-[:CAUSED_BY]-.
    # If this regresses, results would be the *ancestor* chain instead of the descendant chain
    # — the same string match, but pointed the wrong way through the DAG.
    q = compile_to_cypher(PRESETS["plan-then-tool-failure"])
    assert "(s0:Step)<-[:CAUSED_BY]-(s1:Step)" in q.cypher
    assert q.result_vars == ("s0", "s1")
    assert q.params == {
        "s0_name": "plan",
        "s1_kind": "TOOL",
        "s1_status": "error",
    }


def test_unset_predicate_fields_are_wildcards_no_where_clauses():
    # StepPredicate() with no fields set must produce no WHERE — matches anything.
    q = compile_to_cypher(PathPattern((StepPredicate(),)))
    assert "WHERE" not in q.cypher
    assert q.params == {}


def test_enum_values_are_serialized_as_their_string_value():
    # The on-disk store keeps enums as their ``.value`` (e.g. "TOOL", "error"); the WHERE
    # parameters must match that representation, not the Python repr or enum name.
    q = compile_to_cypher(PathPattern((StepPredicate(kind=StepKind.LLM, status=StepStatus.OK),)))
    assert q.params == {"s0_kind": "LLM", "s0_status": "ok"}


def test_params_are_placeholders_no_string_interpolation():
    # Values must reach the engine as bound parameters, not embedded in the Cypher text
    # (Cypher-injection guard — even though preset names aren't user input today, the
    # contract has to hold for ad-hoc patterns built from external data).
    q = compile_to_cypher(PathPattern((StepPredicate(name="evil' OR true --"),)))
    assert "evil" not in q.cypher
    assert q.params["s0_name"] == "evil' OR true --"
    assert "$s0_name" in q.cypher


def test_empty_pattern_is_caller_error_not_silent_no_match():
    # The pure-Python matcher returns [] for an empty pattern as a convenience; compiling
    # one is unambiguously a bug — fail loudly so callers don't ship a query that matches
    # nothing by accident.
    with pytest.raises(ValueError, match="empty"):
        compile_to_cypher(PathPattern(()))


def test_return_columns_are_ordered_s0_through_sn_minus_one():
    # The equivalence layer below relies on column order = predicate order; if the compiler
    # reorders RETURN aliases, every match would be tuple-misaligned.
    n = 4
    q = compile_to_cypher(PathPattern(tuple(StepPredicate() for _ in range(n))))
    assert q.result_vars == ("s0", "s1", "s2", "s3")
    for i in range(n):
        assert f"s{i}.step_id AS s{i}" in q.cypher


def test_compiled_query_is_frozen_dataclass():
    q = compile_to_cypher(PRESETS["tool-failure"])
    assert isinstance(q, CompiledQuery)
    with pytest.raises(Exception):  # FrozenInstanceError, but dataclass version differs
        q.cypher = "tampered"  # type: ignore[misc]
