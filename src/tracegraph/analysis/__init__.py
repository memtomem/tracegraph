"""Analysis layer. Each analysis declares which graph layer it reads.

| analysis           | layer                      |
|--------------------|----------------------------|
| explain / RCA      | raw CAUSED_BY              |
| diff (AHU)         | derived TREE_PARENT        |
| inspect render     | derived TREE_PARENT        |
"""

from tracegraph.analysis.ahu import (
    TreeDiff,
    canonical,
    default_label,
    diff,
    is_isomorphic,
    structure_only,
)
from tracegraph.analysis.explain import Explanation, explain
from tracegraph.analysis.patterns import (
    PRESETS,
    Match,
    PathPattern,
    StepPredicate,
    find_matches,
    search,
)

__all__ = [
    "Explanation",
    "explain",
    "TreeDiff",
    "canonical",
    "default_label",
    "diff",
    "is_isomorphic",
    "structure_only",
    "PRESETS",
    "Match",
    "PathPattern",
    "StepPredicate",
    "find_matches",
    "search",
]
