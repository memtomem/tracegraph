"""Cross-trace pattern matching over the raw causal graph."""

import pytest

from tracegraph.analysis import (
    MAX_GAP,
    PRESETS,
    PathPattern,
    StepPredicate,
    find_matches,
    search,
)
from tracegraph.model import (
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize


def _trace(trace_id: str, *specs):
    """Build a linear trace from (name, kind, status) specs: specs[0] is the root."""
    steps, edges = [], []
    for i, (name, kind, status) in enumerate(specs):
        steps.append(Step(step_id=f"{trace_id}{i}", trace_id=trace_id, seq=i,
                          name=name, kind=kind, status=status))
        if i:
            edges.append(Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}{i}", dst=f"{trace_id}{i-1}"))
    return normalize(RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"),
                              steps=steps, causal_edges=edges))


OK = StepStatus.OK
ERR = StepStatus.ERROR


def _erroring_tool_trace(tid="A"):
    return _trace(tid,
                  ("input", StepKind.CHAIN, OK),
                  ("plan", StepKind.CHAIN, OK),
                  ("call_tool", StepKind.TOOL, ERR),
                  ("respond", StepKind.CHAIN, OK))


def _clean_trace(tid="B"):
    return _trace(tid,
                  ("input", StepKind.CHAIN, OK),
                  ("plan", StepKind.CHAIN, OK),
                  ("call_tool", StepKind.TOOL, OK),
                  ("respond", StepKind.CHAIN, OK))


def test_single_predicate_match():
    nt = _erroring_tool_trace()
    matches = find_matches(nt, PathPattern((StepPredicate(kind=StepKind.TOOL, status=ERR),)))
    assert len(matches) == 1
    assert nt.steps_by_id()[matches[0][0]].name == "call_tool"


def test_multi_step_causal_path_match():
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(name="plan"),
                           StepPredicate(kind=StepKind.TOOL, status=ERR)))
    matches = find_matches(nt, pattern)
    assert len(matches) == 1
    assert [nt.steps_by_id()[s].name for s in matches[0]] == ["plan", "call_tool"]


def test_no_false_match_when_status_differs():
    # clean trace has a TOOL step but it didn't error -> tool-failure must not match
    assert find_matches(_clean_trace(), PRESETS["tool-failure"]) == []


def test_non_contiguous_sequence_does_not_match():
    # plan and respond are not adjacent (call_tool is between) -> no match
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(name="plan"), StepPredicate(name="respond")))
    assert find_matches(nt, pattern) == []


def test_cross_trace_search_filters():
    traces = [_erroring_tool_trace("A"), _clean_trace("B")]
    matches = search(traces, PRESETS["tool-failure"])
    assert {m.trace_id for m in matches} == {"A"}  # only the erroring trace


def test_operational_failure_presets_match_timeout_retry_and_gate():
    timeout = _trace("T", ("attempt:codex", StepKind.AGENT, ERR))
    timeout.steps[0].error_msg = "timeout"
    assert find_matches(timeout, PRESETS["timeout"]) == [["T0"]]

    repeated = _trace(
        "R",
        ("attempt:codex", StepKind.AGENT, ERR),
        ("retry", StepKind.CHAIN, OK),
        ("attempt:codex", StepKind.AGENT, ERR),
    )
    assert find_matches(repeated, PRESETS["repeated-agent-failure"]) == [["R0", "R2"]]

    gate = _trace(
        "G",
        ("attempt:codex", StepKind.AGENT, OK),
        ("gate:pytest", StepKind.TOOL, ERR),
    )
    assert find_matches(gate, PRESETS["gate-failure-after-success"]) == [["G0", "G1"]]


def test_extended_predicate_validation():
    with pytest.raises(ValueError, match="mutually exclusive"):
        StepPredicate(name="gate", name_prefix="gate:")
    with pytest.raises(ValueError, match="non-empty"):
        StepPredicate(error_contains="")


def test_preset_labels_are_readable():
    matches = search([_erroring_tool_trace("A")], PRESETS["plan-then-tool-failure"])
    assert len(matches) == 1
    assert matches[0].labels == ["plan", "call_tool"]


# --- the marquee: "tool X → retry → tool X → failure" (back-references + variable-length gaps) ---


def _custom(tid, specs, edges):
    """Build a NormalizedTrace from explicit (id, name, kind, status) step specs + CAUSED_BY pairs."""
    steps = [
        Step(step_id=f"{tid}{sid}", trace_id=tid, seq=i, name=name, kind=kind, status=status)
        for i, (sid, name, kind, status) in enumerate(specs)
    ]
    es = [Edge(type=EdgeType.CAUSED_BY, src=f"{tid}{s}", dst=f"{tid}{d}") for s, d in edges]
    return normalize(RawTrace(trace=Trace(trace_id=tid, source_kind="x"), steps=steps, causal_edges=es))


def _retry_trace(tid="R", first_status=OK):
    """input → plan → search(first_status) → retry:search → search(ERR) → respond."""
    return _custom(
        tid,
        [
            (0, "input", StepKind.CHAIN, OK),
            (1, "plan", StepKind.CHAIN, OK),
            (2, "search", StepKind.TOOL, first_status),
            (3, "retry:search", StepKind.CHAIN, OK),
            (4, "search", StepKind.TOOL, ERR),
            (5, "respond", StepKind.CHAIN, OK),
        ],
        [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4)],
    )


def test_marquee_matches_same_tool_retried_then_failing():
    nt = _retry_trace()
    matches = find_matches(nt, PRESETS["tool-retry-failure"])
    assert len(matches) == 1
    assert [nt.steps_by_id()[s].name for s in matches[0]] == [
        "search",
        "retry:search",
        "search",
    ]
    # the two distinct tool invocations, not the same step twice
    assert matches[0][0] != matches[0][2]
    assert matches[0] == ["R2", "R3", "R4"]


def test_marquee_status_agnostic_first_call_catches_both_ok_then_fail_and_fail_then_fail():
    # First call OK then failing retry — and first call already failing (doom loop) — both match.
    assert len(find_matches(_retry_trace(first_status=OK), PRESETS["tool-retry-failure"])) == 1
    assert len(find_matches(_retry_trace(first_status=ERR), PRESETS["tool-retry-failure"])) == 1


def test_marquee_does_not_match_when_second_tool_is_a_different_name():
    # The back-reference is the whole point: a DIFFERENT failing tool after 'search' is not a retry.
    nt = _custom(
        "D",
        [
            (0, "input", StepKind.CHAIN, OK),
            (1, "search", StepKind.TOOL, OK),
            (2, "retry:search", StepKind.CHAIN, OK),
            (3, "fetch", StepKind.TOOL, ERR),  # different tool name
        ],
        [(1, 0), (2, 1), (3, 2)],
    )
    assert find_matches(nt, PRESETS["tool-retry-failure"]) == []


def test_marquee_does_not_match_adjacent_same_tool_with_no_retry_gap():
    # Back-to-back same-tool calls without an explicit marker are not a retry.
    nt = _custom(
        "A",
        [
            (0, "search", StepKind.TOOL, OK),
            (1, "search", StepKind.TOOL, ERR),
        ],
        [(1, 0)],
    )
    assert find_matches(nt, PRESETS["tool-retry-failure"]) == []


def test_gap_endpoint_reachable_by_two_paths_yields_one_deduped_match():
    # Diamond: search(OK) → {a, b} → search(ERR). The failing retry is reachable via TWO gap
    # paths (both length 2); the matcher must emit the (s0,s1) pair ONCE (== RETURN DISTINCT).
    nt = _custom(
        "G",
        [
            (0, "search", StepKind.TOOL, OK),
            (1, "a", StepKind.CHAIN, OK),
            (2, "b", StepKind.CHAIN, OK),
            (3, "search", StepKind.TOOL, ERR),
        ],
        [(1, 0), (2, 0), (3, 1), (3, 2)],
    )
    matches = find_matches(nt, PRESETS["tool-repeat-failure-heuristic"])
    assert matches == [["G0", "G3"]]


def test_nameless_back_reference_never_matches():
    # Two nameless TOOL steps: Cypher NULL=NULL is NULL (no match); the matcher mirrors that,
    # so a back-reference on a None name can never be satisfied (no spurious None==None match).
    nt = _custom(
        "N",
        [
            (0, None, StepKind.TOOL, OK),
            (1, "mid", StepKind.CHAIN, OK),
            (2, None, StepKind.TOOL, ERR),
        ],
        [(1, 0), (2, 1)],
    )
    assert find_matches(nt, PRESETS["tool-repeat-failure-heuristic"]) == []


def test_bounded_near_preset_matches_close_retry_but_misses_far_one():
    near = PRESETS["tool-retry-failure-near"]
    unbounded = PRESETS["tool-repeat-failure-heuristic"]
    # close retry: both compatibility heuristics match
    close = _retry_trace("C")
    assert find_matches(close, near) == find_matches(close, unbounded) == [["C2", "C4"]]


def test_unbounded_gap_matches_far_retry_on_deep_linear_trace_without_recursionerror():
    # Deep-linear regression (memory: traces-are-deep-linear; recursion overflows ~498). A
    # 2400-step chain with the first tool near the start and the failing retry near the end:
    # the unbounded gap walks ~2390 hops ITERATIVELY (no stack growth) and matches; the bounded
    # 'near' preset (≤30 hops) correctly misses the far retry.
    n = 2400
    specs = [(i, f"n{i}", StepKind.CHAIN, OK) for i in range(n)]
    specs[5] = (5, "search", StepKind.TOOL, OK)
    specs[n - 5] = (n - 5, "search", StepKind.TOOL, ERR)
    edges = [(i, i - 1) for i in range(1, n)]
    nt = _custom("L", specs, edges)

    far = find_matches(nt, PRESETS["tool-repeat-failure-heuristic"])
    assert far == [[f"L{5}", f"L{n - 5}"]]
    assert find_matches(nt, PRESETS["tool-retry-failure-near"]) == []  # > 30 hops apart


# --- validation: malformed patterns fail loudly, never silently or with a KeyError ---


@pytest.mark.parametrize("bad_ref", [1, 2])  # self (==idx 1) and forward/out-of-range
def test_forward_or_self_back_reference_raises(bad_ref):
    # same_name_as must point to a STRICTLY earlier predicate; self and forward refs raise.
    pattern = PathPattern((StepPredicate(kind=StepKind.TOOL), StepPredicate(same_name_as=bad_ref)))
    with pytest.raises(ValueError, match="strictly earlier"):
        find_matches(_retry_trace(), pattern)


def test_valid_back_reference_to_earlier_predicate_is_accepted():
    # The mirror of the above: same_name_as=0 on predicate 1 is the legal, marquee case.
    pattern = PathPattern((StepPredicate(kind=StepKind.TOOL), StepPredicate(same_name_as=0)))
    find_matches(_retry_trace(), pattern)  # must not raise


def test_gap_lower_bound_below_one_rejected_at_construction():
    with pytest.raises(ValueError, match="lower bound"):
        StepPredicate(gap=(0, 5))


def test_gap_upper_below_lower_rejected_at_construction():
    with pytest.raises(ValueError, match="below lower bound"):
        StepPredicate(gap=(5, 2))


def test_negative_back_reference_rejected_at_construction():
    with pytest.raises(ValueError, match="non-negative"):
        StepPredicate(same_name_as=-1)


def test_unbounded_gap_is_constructible_even_though_it_will_not_compile():
    # The pure-Python matcher (system of record) accepts unbounded gaps; only the compiler refuses.
    p = StepPredicate(kind=StepKind.TOOL, gap=(2, None))
    assert p.gap == (2, None)


def test_unbounded_marquee_is_not_quadratic_on_tool_heavy_deep_linear_trace():
    # Perf regression for the confirmed O(n²) finding: an all-TOOL same-name deep-linear chain
    # (every step matches predicate 0, only the last errors) is the worst case for a forward
    # walk-from-every-start. The endpoint-anchored fast path must keep it ~linear. We assert a
    # generous wall-clock bound (not a micro-benchmark) so it fails loudly if the quadratic
    # behavior ever returns, without being flaky on a slow CI box.
    import time

    n = 6000
    specs = [(i, "search", StepKind.TOOL, OK) for i in range(n)]
    specs[-1] = (n - 1, "search", StepKind.TOOL, ERR)
    nt = _custom("Q", specs, [(i, i - 1) for i in range(1, n)])

    t0 = time.perf_counter()
    matches = find_matches(nt, PRESETS["tool-repeat-failure-heuristic"])
    elapsed = time.perf_counter() - t0
    # Every earlier 'search' tool ≥2 causal hops back pairs with the single failing one. The
    # immediate predecessor (distance 1) is excluded by the gap's lo=2 — so n-2, not n-1.
    assert len(matches) == n - 2
    assert matches[0] == ["Q0", f"Q{n - 1}"]
    assert [f"Q{n - 2}", f"Q{n - 1}"] not in matches  # the adjacent pair is NOT a retry
    assert elapsed < 2.0, f"unbounded marquee took {elapsed:.1f}s on n={n} — quadratic regression?"


def test_ignored_first_predicate_gap_still_uses_unbounded_fast_path(monkeypatch):
    # A first-predicate gap has no predecessor and is contractually ignored. It therefore must
    # not route an otherwise optimizable two-step unbounded pattern through the quadratic
    # general matcher.
    nt = _retry_trace()
    pattern = PathPattern(
        (
            StepPredicate(kind=StepKind.TOOL, gap=(1, None)),
            StepPredicate(
                kind=StepKind.TOOL,
                status=ERR,
                same_name_as=0,
                gap=(2, None),
            ),
        )
    )

    def fail_forward(*args, **kwargs):
        raise AssertionError("ignored first-predicate gap disabled the unbounded fast path")

    monkeypatch.setattr("tracegraph.analysis.patterns._find_matches_forward", fail_forward)
    assert find_matches(nt, pattern) == [["R2", "R4"]]


@pytest.mark.parametrize("seed", range(12))
def test_fast_path_equals_forward_reference_on_random_dags(seed):
    # The endpoint-anchored fast path must return EXACTLY what the general forward matcher
    # (the semantic reference) returns — same matches, same order — on arbitrary DAGs with
    # diamonds, multiple endpoints, varied names, and varied gap lower bounds. This is the
    # guard that the optimization never diverges from the definition.
    import random

    from tracegraph.analysis.patterns import _find_matches_forward

    rng = random.Random(seed)
    n = rng.randint(2, 14)
    names = ["x", "y", None]
    specs = []
    for i in range(n):
        kind = rng.choice([StepKind.TOOL, StepKind.CHAIN])
        status = ERR if rng.random() < 0.4 else OK
        specs.append((i, rng.choice(names), kind, status))
    # random DAG edges: each step caused by 0..2 strictly-earlier steps (cause.seq < effect.seq)
    edges = []
    for i in range(1, n):
        for cand in rng.sample(range(i), k=min(i, rng.randint(1, 2))):
            edges.append((i, cand))
    nt = _custom(f"RND{seed}", specs, edges)

    lo = rng.randint(1, 4)
    pattern = PathPattern(
        (
            StepPredicate(kind=StepKind.TOOL),
            StepPredicate(kind=StepKind.TOOL, status=ERR, same_name_as=0, gap=(lo, None)),
        )
    )
    # find_matches() dispatches to the fast path; compare against the forward reference directly.
    assert find_matches(nt, pattern) == _find_matches_forward(nt, pattern)


def test_str_renders_gap_and_back_reference_readably():
    s = str(PRESETS["tool-retry-failure"])
    assert "{kind=TOOL}" in s
    assert "name^=retry:" in s
    assert "name=#0" in s  # the back-reference to predicate 0
    heuristic = str(PRESETS["tool-repeat-failure-heuristic"])
    assert "→[gap 2..]→" in heuristic
    near = str(PRESETS["tool-retry-failure-near"])
    assert f"→[gap 2..{MAX_GAP}]→" in near
