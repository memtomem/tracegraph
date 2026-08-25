"""Structural regression diff over the **derived** ``TREE_PARENT`` forest.

Two runs are compared as rooted trees. Isomorphism uses the Aho–Hopcroft–Ullman (AHU)
canonical form — each node's canonical value is its label plus the *sorted* canonical values
of its children — which is exact and linear-time on a forest (the whole reason we project the
multi-parent causal graph down to a single-parent tree for diffing).

Canonical values are **nested tuples**, not strings: ``(label, (child_canon, ...))``. Tuples
are hashable and compared structurally, so — unlike string concatenation — a label containing
``(``, ``)`` or ``,`` can never forge a false match.

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
                kids = tuple(sorted(canon[c] for c in children.get(nid, [])))
                canon[nid] = (label(steps[nid]), kids)
            else:
                stack.append((nid, True))
                for c in children.get(nid, []):
                    stack.append((c, False))
    return canon


def _render(canon: Canon) -> str:
    """Human-readable rendering of a canonical subtree (display only).

    Iterative post-order — a deep subtree (e.g. a whole present-only-in-A chain) would
    overflow a recursive render. Memoizes by the canon value itself: equal subtrees render
    identically, so sharing one cache across the forest is safe.
    """
    rendered: dict[Canon, str] = {}
    stack: list[tuple[Canon, bool]] = [(canon, False)]
    while stack:
        node, ready = stack.pop()
        node_label, kids = node
        if ready or not kids:
            shown = node_label or "·"
            rendered[node] = (
                shown if not kids else f"{shown}({', '.join(rendered[k] for k in kids)})"
            )
        else:
            stack.append((node, True))
            for k in kids:
                stack.append((k, False))
    return rendered[canon]


def canonical(nt: NormalizedTrace, label: LabelFn = default_label) -> Canon:
    """The forest's canonical value: sorted tuple of its roots' canonical values."""
    children = _children(nt)
    steps = nt.steps_by_id()
    roots = _roots(nt, children)
    canon = _subtree_canons(steps, children, roots, label)
    return tuple(sorted(canon[r] for r in roots))


def is_isomorphic(a: NormalizedTrace, b: NormalizedTrace, label: LabelFn = default_label) -> bool:
    return canonical(a, label) == canonical(b, label)


@dataclass
class TreeDiff:
    identical: bool
    changes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # truthy when there IS a difference
        return not self.identical


def diff(a: NormalizedTrace, b: NormalizedTrace, label: LabelFn = default_label) -> TreeDiff:
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
    canA = _subtree_canons(sa, ca, ra, label)
    canB = _subtree_canons(sb, cb, rb, label)
    # Same comparison as is_isomorphic(), reusing the canons computed above.
    if tuple(sorted(canA[r] for r in ra)) == tuple(sorted(canB[r] for r in rb)):
        return TreeDiff(identical=True)

    changes: list[str] = []

    def align(a_ids: list[str], b_ids: list[str]) -> tuple[list[str], list[str]]:
        """Pop A/B ids whose canonical subtrees match; return the leftovers on each side."""
        bucket: dict[Canon, list[str]] = {}
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
                    bk = pool.pop(0)
                    used_b.add(bk)
                    pairs.append((k, bk))
                else:
                    still_a.append(k)
            a_left = still_a
            b_left = [k for k in b_left if k not in used_b]

        def child_canons(
            canon_map: dict[str, Canon], child_map: dict[str, list[str]], k: str
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
        a_ids: list[str], b_ids: list[str], here: tuple[str, ...], root_level: bool
    ) -> list[tuple[str, str]]:
        """Report unpairable leftovers as add/remove; return the genuine pairs to descend."""
        pairs, a_left, b_left = pair_leftovers(a_ids, b_ids)
        prefix = " > ".join(here)
        tail = "root subtree" if root_level else "subtree"
        for k in a_left:
            scope = "only in A" if root_level else f"only in A under {prefix}"
            changes.append(f"{scope}: {tail} {_render(canA[k])}")
        for k in b_left:
            scope = "only in B" if root_level else f"only in B under {prefix}"
            changes.append(f"{scope}: {tail} {_render(canB[k])}")
        return pairs

    # Iterative worklist: a recursive descent would overflow on a deep-linear divergence.
    worklist: list[tuple[str, str, tuple[str, ...]]] = [
        (an, bn, ()) for an, bn in localize(*align(ra, rb), here=(), root_level=True)
    ]
    while worklist:
        an, bn, path = worklist.pop()
        la, lb = label(sa[an]), label(sb[bn])
        if la != lb:
            changes.append(f"diverges at {' > '.join(path) or '(root)'}: A={la!r} B={lb!r}")
            continue
        here = path + (la,)
        a_only, b_only = align(ca.get(an, []), cb.get(bn, []))
        worklist.extend(
            (ca_id, cb_id, here) for ca_id, cb_id in localize(a_only, b_only, here, False)
        )

    return TreeDiff(identical=False, changes=changes or ["trees differ"])
