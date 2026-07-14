"""Tests for the backend-neutral PathPattern → Cypher compiler.

No LadybugDB dependency here — the compiler is pure stdlib so it stays importable (and verifiable)
in the default install. Equivalence with the pure-Python matcher is tested separately in
test_ladybug_store.py, which is the only place the contract "same input → same matches" must hold.
"""

from __future__ import annotations

import pytest

from tracegraph.analysis import (
    MAX_GAP,
    PRESETS,
    CompiledQuery,
    PathPattern,
    StepPredicate,
    UncompilablePattern,
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


# --- gaps + back-references (the marquee "tool X → retry → tool X → failure") ---


def test_strict_patterns_keep_bare_return_no_distinct():
    # Regression guard: only gap patterns get RETURN DISTINCT. Strict-adjacency patterns can't
    # produce duplicate tuples (no repeated CAUSED_BY edges), so the existing presets' compiled
    # text must NOT gain DISTINCT (it would be a spurious diff and a perf cost).
    for name in ("error", "tool-failure", "plan-then-tool-failure"):
        assert "DISTINCT" not in compile_to_cypher(PRESETS[name]).cypher


def test_bounded_gap_compiles_to_capped_var_length_with_distinct():
    q = compile_to_cypher(PRESETS["tool-retry-failure-near"])
    # capped variable-length span (never exceeds LadybugDB's 30-hop ceiling) ...
    assert f"(s0:Step)<-[:CAUSED_BY*2..{MAX_GAP}]-(s1:Step)" in q.cypher
    # ... DISTINCT so a fan-in graph collapses to one row per endpoint pair ...
    assert q.cypher.startswith("MATCH") and "RETURN DISTINCT" in q.cypher
    # ... the back-reference as a column-equality + NOT-NULL guard (NOT a bound param) ...
    assert "s1.name = s0.name" in q.cypher
    assert "s1.name IS NOT NULL" in q.cypher
    assert "name" not in q.params  # the back-ref is column=column, never interpolated/param'd
    # ... and the local constraints still parameterized exactly as before.
    assert q.params == {"s0_kind": "TOOL", "s1_kind": "TOOL", "s1_status": "error"}
    assert q.result_vars == ("s0", "s1")


def test_unbounded_gap_marquee_preset_refuses_to_compile():
    # The headline preset uses an unbounded gap, which cannot be faithful under the 30-hop cap.
    # Refusing (so the backend falls back to pure-Python) is correct; silently truncating is not.
    with pytest.raises(UncompilablePattern, match="unbounded gap"):
        compile_to_cypher(PRESETS["tool-retry-failure"])


def test_over_cap_bounded_gap_refuses_to_compile():
    pattern = PathPattern(
        (StepPredicate(kind=StepKind.TOOL), StepPredicate(kind=StepKind.TOOL, gap=(2, MAX_GAP + 1)))
    )
    with pytest.raises(UncompilablePattern, match=f"exceeds LadybugDB's {MAX_GAP}-hop"):
        compile_to_cypher(pattern)


def test_uncompilable_pattern_is_a_valueerror_subclass():
    # Existing callers catch ValueError at the load/query boundary; UncompilablePattern must
    # stay within that contract so it can be handled (and the backend can fall back) cleanly.
    assert issubclass(UncompilablePattern, ValueError)


def test_at_cap_bounded_gap_is_accepted():
    # Exactly MAX_GAP hops is the largest faithful bound — it must compile, not refuse.
    pattern = PathPattern(
        (StepPredicate(kind=StepKind.TOOL), StepPredicate(kind=StepKind.TOOL, gap=(1, MAX_GAP)))
    )
    q = compile_to_cypher(pattern)
    assert f"*1..{MAX_GAP}" in q.cypher


def test_first_predicate_gap_is_ignored_without_spurious_distinct():
    pattern = PathPattern(
        (
            StepPredicate(kind=StepKind.TOOL, gap=(1, None)),
            StepPredicate(kind=StepKind.TOOL),
        )
    )
    q = compile_to_cypher(pattern)
    assert "RETURN DISTINCT" not in q.cypher
    assert "RETURN s0.step_id AS s0, s1.step_id AS s1" in q.cypher
