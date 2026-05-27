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
a readable *heuristic* localization (it pairs unmatched children positionally), not a proven
minimal tree-edit script; on trees with several changed siblings sharing labels it may point at
a plausible-but-not-unique node.
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
    """Canonical AHU tuple per node id (memoized over the whole forest)."""
    canon: dict[str, Canon] = {}

    def go(nid: str) -> Canon:
        kids = tuple(sorted(go(c) for c in children.get(nid, [])))
        canon[nid] = (label(steps[nid]), kids)
        return canon[nid]

    for r in roots:
        go(r)
    return canon


def _render(canon: Canon) -> str:
    """Human-readable rendering of a canonical subtree (display only)."""
    label, kids = canon
    shown = label or "·"
    return shown if not kids else f"{shown}({', '.join(_render(k) for k in kids)})"


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
    descends through single mismatched pairs to pinpoint the deepest divergence, and reports
    leftover subtrees as present-only-in-A / present-only-in-B.
    """
    if is_isomorphic(a, b, label):
        return TreeDiff(identical=True)

    sa, sb = a.steps_by_id(), b.steps_by_id()
    ca, cb = _children(a), _children(b)
    ra, rb = _roots(a, ca), _roots(b, cb)
    canA = _subtree_canons(sa, ca, ra, label)
    canB = _subtree_canons(sb, cb, rb, label)
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

    def walk(an: str, bn: str, path: list[str]) -> None:
        la, lb = label(sa[an]), label(sb[bn])
        if la != lb:
            changes.append(f"diverges at {' > '.join(path) or '(root)'}: A={la!r} B={lb!r}")
            return
        here = path + [la]
        a_only, b_only = align(ca.get(an, []), cb.get(bn, []))
        while a_only and b_only:  # descend paired mismatches to localize deeper
            walk(a_only.pop(0), b_only.pop(0), here)
        for k in a_only:
            changes.append(f"only in A under {' > '.join(here)}: subtree {_render(canA[k])}")
        for k in b_only:
            changes.append(f"only in B under {' > '.join(here)}: subtree {_render(canB[k])}")

    a_only, b_only = align(ra, rb)
    while a_only and b_only:
        walk(a_only.pop(0), b_only.pop(0), [])
    for r in a_only:
        changes.append(f"only in A: root subtree {_render(canA[r])}")
    for r in b_only:
        changes.append(f"only in B: root subtree {_render(canB[r])}")

    return TreeDiff(identical=False, changes=changes or ["trees differ"])
