"""``explain`` — the causal-chain primitive, reads the RAW layer only.

Per the layer contract, root-cause work reads ``CAUSED_BY`` (every real predecessor),
never the derived single-parent tree. ``explain`` walks backward from a step (typically
a failure) and surfaces the full set of real causes, explicitly flagging any step whose
causality is *lossy* under the tree projection — so a reader is never misled into
trusting the single ``TREE_PARENT`` as "the" cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracegraph.model import Step
from tracegraph.store.base import GraphStore


@dataclass
class Explanation:
    target: Step
    #: Raw causal ancestors of ``target``, nearest cause first.
    chain: list[Step] = field(default_factory=list)
    #: step_ids within {target} ∪ chain whose true causality was collapsed by the
    #: single-parent projection (had more than one CAUSED_BY).
    lossy_steps: list[str] = field(default_factory=list)

    @property
    def is_lossy(self) -> bool:
        return bool(self.lossy_steps)


def explain(
    store: GraphStore, step_id: str, *, steps: dict[str, Step] | None = None
) -> Explanation:
    """Build the raw causal chain leading to ``step_id``.

    ``steps`` lets a caller that already holds the trace's step map (e.g. the CLI
    explaining many matches against one store) skip re-materializing the whole trace
    per call — ``store.trace()`` can be a full graph pull on the LadybugDB backend.
    """
    if steps is None:
        steps = store.trace().steps_by_id()
    target = steps[step_id]
    chain = store.ancestors(step_id)
    lossy = [s.step_id for s in [target, *chain] if s.projection_lossy]
    return Explanation(target=target, chain=chain, lossy_steps=lossy)
