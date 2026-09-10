"""Structural regression diff over the **derived** ``TREE_PARENT`` forest.

Two runs are compared as rooted trees. Isomorphism uses the Aho–Hopcroft–Ullman (AHU)
canonical form — each node's canonical value is its label plus the *sorted* canonical values
of its children — which is exact on a forest (the whole reason we project the
multi-parent causal graph down to a single-parent tree for diffing).

The public canonical values remain **nested tuples**, not strings: ``(label, (child_canon, ...))``. Tuples
preserve structure, so — unlike string concatenation — a label containing
``(``, ``)`` or ``,`` can never forge a false match.

Diff and equality intern (label, sorted child IDs) in a table shared by both trees,
avoiding recursive tuple hashing/comparison. Sorting adds O(d log d) per sibling set.

Per the layer contract this reads ``TREE_PARENT`` only. ``CAUSED_BY`` (the raw layer) is for
``explain``/RCA, never for structural diff.

Note on ``diff``: the boolean ``identical`` is exact (AHU). The "diverges at …" explanation is
a readable *heuristic* localization (it pairs unmatched children only on structural/label
evidence, never positionally), not a proven minimal tree-edit script; on trees with several
changed siblings it may point at a plausible-but-not-unique node, but it will not invent a
correspondence between two genuinely unrelated siblings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Callable

from tracegraph.model import EdgeType, NormalizedTrace, Step

#: How a node is identified for structural comparison. Default = node name, falling back to
#: kind. Pass ``structure_only`` to compare pure topology (shape) ignoring labels.
LabelFn = Callable[[Step], str]

#: A collision-free canonical value: ``(label, (sorted child canons...))``.
Canon = tuple


def default_label(step: Step) -> str:
    return step.name or step.kind.value


def structure_only(step: Step) -> str:  # noqa: ARG001 - intentionally ignores the node
    return ""


def _children(nt: NormalizedTrace) -> dict[str, list[str]]:
    """parent_id -> [child_id, ...] from TREE_PARENT (which points child -> parent)."""
    out: dict[str, list[str]] = {}
    for e in nt.edges_of(EdgeType.TREE_PARENT):
        out.setdefault(e.dst, []).append(e.src)
    return out


def _roots(nt: NormalizedTrace, children: dict[str, list[str]]) -> list[str]:
    has_parent = {e.src for e in nt.edges_of(EdgeType.TREE_PARENT)}
    return [s.step_id for s in nt.steps if s.step_id not in has_parent]


def _require_full_coverage(covered: dict[str, object], nt: NormalizedTrace, side: str) -> None:
    """Reject a derived layer whose nodes are not all reachable from a root.

    ``_roots`` treats "has no TREE_PARENT edge" as "is a root", so a cycle in the derived
    layer (or an edge to a missing step) leaves a whole component with no root and therefore
    unvisited. Left unchecked, those nodes never enter the canon table and the comparison
    silently runs on a *subset* of the tree — two traces can then be reported identical while
    one of them contains an entire component the other lacks. Silent truncation is exactly
    what this project refuses to do, so this is an error, not a smaller answer.
    """
    if len(covered) != len(nt.steps):
        missing = sorted({s.step_id for s in nt.steps} - set(covered))
        raise ValueError(
            f"{side}: {len(missing)} step(s) are unreachable from any TREE_PARENT root "
            f"(first: {missing[0]!r}); the derived layer is not a forest, so a structural "
            "comparison would silently ignore them. Run validate_tree() on the artifact."
        )


def _compare(left: Canon, right: Canon) -> int:
    # Tuple-compatible lexical order without Python's recursive tuple comparison.
    stack = [(left, right)]
    while stack:
        a, b = stack.pop()
        if a is b:
            continue
        if isinstance(a, str):
            if a != b:
                return -1 if a < b else 1
            continue
        common = min(len(a), len(b))
        if len(a) != len(b):
            stack.append(("" if len(a) < len(b) else "x", "x" if len(a) < len(b) else ""))
        stack.extend((a[i], b[i]) for i in range(common - 1, -1, -1))
    return 0


def _intern(steps, children, roots, label, table):
    result = {}
    stack = [(root, False) for root in roots]
    while stack:
        node, ready = stack.pop()
        if ready:
            signature = (label(steps[node]), tuple(sorted(result[c] for c in children.get(node, []))))
            result[node] = table.setdefault(signature, len(table))
        else:
            stack.append((node, True))
            stack.extend((child, False) for child in children.get(node, []))
    return result


def _subtree_canons(
    steps: dict[str, Step], children: dict[str, list[str]], roots: list[str], label: LabelFn
) -> dict[str, Canon]:
    """Canonical AHU tuple per node id (iterative post-order over the whole forest).

    Iterative on purpose: agent traces are deep and near-linear, so a recursive post-order
    overflows Python's recursion limit at ~500 nodes. The two-phase stack visits each node
    once and computes its canon only after every child's canon already exists.
    """
    canon: dict[str, Canon] = {}
    for root in roots:
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            nid, ready = stack.pop()
            if ready:
                kids = tuple(sorted((canon[c] for c in children.get(nid, [])), key=cmp_to_key(_compare)))
                canon[nid] = (label(steps[nid]), kids)
            else:
                stack.append((nid, True))
                for c in children.get(nid, []):
                    stack.append((c, False))
    return canon


def canonical(nt: NormalizedTrace, label: LabelFn = default_label) -> Canon:
    """Sorted nested root tuples; use is_isomorphic for recursion-safe deep equality."""
    children = _children(nt)
    steps = nt.steps_by_id()
    roots = _roots(nt, children)
    canon = _subtree_canons(steps, children, roots, label)
    _require_full_coverage(canon, nt, "trace")
    return tuple(sorted((canon[r] for r in roots), key=cmp_to_key(_compare)))


def is_isomorphic(a: NormalizedTrace, b: NormalizedTrace, label: LabelFn = default_label) -> bool:
    table = {}
    ca, cb = _children(a), _children(b)
    ra, rb = _roots(a, ca), _roots(b, cb)
    aa = _intern(a.steps_by_id(), ca, ra, label, table)
    bb = _intern(b.steps_by_id(), cb, rb, label, table)
    _require_full_coverage(aa, a, "A")
    _require_full_coverage(bb, b, "B")
    return sorted(aa[r] for r in ra) == sorted(bb[r] for r in rb)


@dataclass
class TreeDiff:
    identical: bool
    changes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # truthy when there IS a difference
        return not self.identical


def diff(a: NormalizedTrace, b: NormalizedTrace, label: LabelFn = default_label, *, display_label: LabelFn | None = None) -> TreeDiff:
    """Compare two runs structurally. ``identical`` is exact; ``changes`` is a heuristic localization.

    Aligns children by canonical subtree (so identical subtrees match regardless of order),
    descends through *corresponding* mismatched pairs (paired only on real structural/label
    evidence, never positionally) to pinpoint the deepest divergence, and reports genuinely
    unmatched subtrees as present-only-in-A / present-only-in-B. Iterative throughout, so it
    handles the deep-linear traces LangGraph produces without overflowing the stack.
    """
    sa, sb = a.steps_by_id(), b.steps_by_id()
    ca, cb = _children(a), _children(b)
    ra, rb = _roots(a, ca), _roots(b, cb)
    table = {}
    canA = _intern(sa, ca, ra, label, table)
    canB = _intern(sb, cb, rb, label, table)
    _require_full_coverage(canA, a, "A")
    _require_full_coverage(canB, b, "B")
    show = display_label or label

    def render(node, steps, children):
        parts = []
        stack = [(node, False)]
        while stack:
            item, literal = stack.pop()
            if literal:
                parts.append(item)
                continue
            parts.append(show(steps[item]) or "·")
            kids = children.get(item, [])
            if kids:
                parts.append("(")
                stack.append((")", True))
                for i in range(len(kids) - 1, -1, -1):
                    stack.append((kids[i], False))
                    if i:
                        stack.append((", ", True))
        return "".join(parts)

    def path_text(path):
        parts = []
        while path is not None:
            path, name = path
            parts.append(name)
        return " > ".join(reversed(parts))
    # Same comparison as is_isomorphic(), reusing the canons computed above.
    if tuple(sorted(canA[r] for r in ra)) == tuple(sorted(canB[r] for r in rb)):
        return TreeDiff(identical=True)

    changes: list[str] = []

    def align(a_ids: list[str], b_ids: list[str]) -> tuple[list[str], list[str]]:
        """Pop A/B ids whose canonical subtrees match; return the leftovers on each side."""
        bucket: dict[int, list[str]] = {}
        for k in b_ids:
            bucket.setdefault(canB[k], []).append(k)
        a_only: list[str] = []
        for k in a_ids:
            pool = bucket.get(canA[k])
            if pool:
                pool.pop()  # identical subtree on both sides -> matched, nothing to report
            else:
                a_only.append(k)
        b_only = [k for pool in bucket.values() for k in pool]
        return a_only, b_only

    def pair_leftovers(
        a_ids: list[str], b_ids: list[str]
    ) -> tuple[list[tuple[str, str]], list[str], list[str]]:
        """Pair unmatched subtrees that genuinely correspond, so "diverges at" never asserts
        a false A<->B correspondence between unrelated siblings.

        Correspondence requires real evidence — identical *non-empty* child structure (a
        relabel of this node, surfaced as "diverges at"), then identical root label (a node
        with changed descendants, localized by descending). Everything else is honest
        add/remove. There is deliberately NO positional fallback: one unmatched leaf per side
        with nothing in common is an add+remove, not a relabel — and "one leftover each" is
        not itself evidence (the parent may simply have had several children, all but one of
        which aligned away), so we never pair on sibling position alone. That positional
        guess was the bug this replaced.
        """
        pairs: list[tuple[str, str]] = []
        a_left, b_left = list(a_ids), list(b_ids)

        def greedy(key_a: Callable[[str], object], key_b: Callable[[str], object]) -> None:
            nonlocal a_left, b_left
            index: dict[object, list[str]] = {}
            for k in b_left:
                kb = key_b(k)
                if kb is not None:
                    index.setdefault(kb, []).append(k)
            still_a, used_b = [], set()
            for k in a_left:
                ka = key_a(k)
                pool = index.get(ka) if ka is not None else None
                if pool:
                    bk = pool.pop()
                    used_b.add(bk)
                    pairs.append((k, bk))
                else:
                    still_a.append(k)
            a_left = still_a
            b_left = [k for k in b_left if k not in used_b]

        def child_canons(
            canon_map: dict[str, int], child_map: dict[str, list[str]], k: str
        ) -> object:
            kids = child_map.get(k, [])
            return tuple(sorted(canon_map[c] for c in kids)) if kids else None

        greedy(  # identical non-empty child structure == a relabel of this node
            lambda k: child_canons(canA, ca, k),
            lambda k: child_canons(canB, cb, k),
        )
        greedy(lambda k: label(sa[k]), lambda k: label(sb[k]))  # same label, changed kids
        return pairs, a_left, b_left

    def localize(
        a_ids: list[str], b_ids: list[str], here, root_level: bool
    ) -> list[tuple[str, str]]:
        """Report unpairable leftovers as add/remove; return the genuine pairs to descend."""
        pairs, a_left, b_left = pair_leftovers(a_ids, b_ids)
        prefix = path_text(here) if a_left or b_left else ""
        tail = "root subtree" if root_level else "subtree"
        for k in a_left:
            scope = "only in A" if root_level else f"only in A under {prefix}"
            changes.append(f"{scope}: {tail} {render(k, sa, ca)}")
        for k in b_left:
            scope = "only in B" if root_level else f"only in B under {prefix}"
            changes.append(f"{scope}: {tail} {render(k, sb, cb)}")
        return pairs

    # Iterative worklist: a recursive descent would overflow on a deep-linear divergence.
    worklist = [
        (an, bn, None) for an, bn in localize(*align(ra, rb), here=None, root_level=True)
    ]
    while worklist:
        an, bn, path = worklist.pop()
        la, lb = label(sa[an]), label(sb[bn])
        if la != lb:
            changes.append(f"diverges at {path_text(path) or '(root)'}: A={show(sa[an])!r} B={show(sb[bn])!r}")
            continue
        here = (path, show(sa[an]))
        a_only, b_only = align(ca.get(an, []), cb.get(bn, []))
        worklist.extend(
            (ca_id, cb_id, here) for ca_id, cb_id in localize(a_only, b_only, here, False)
        )

    return TreeDiff(identical=False, changes=changes or ["trees differ"])
