"""Ingest LangGraph checkpoint history into a :class:`RawTrace`.

Reads any ``BaseCheckpointSaver`` via ``.list()`` and reconstructs the causal chain from
each checkpoint's ``parent_config``. One checkpoint (super-step) becomes one ``Step``;
the parent-checkpoint link becomes a ``CAUSED_BY`` edge (effect → cause).

**Phase 1 scope (intentional):** only the root checkpoint namespace (``checkpoint_ns ==
""``) is ingested, and causality comes from ``parent_config`` within a single thread.
Cross-namespace / subgraph parentage (``metadata.parents``) is deferred until a real
fixture proves its step ordering — see ``docs`` / the plan.

A few facts about the checkpoint stream this relies on (verified against
langgraph-checkpoint 4.x):

* ``checkpoint["id"]`` is a monotonic UUIDv6; ``metadata["step"]`` increases with the
  parent chain, so a cause's ``seq`` is always < its effect's (satisfies ``validate_raw``).
* ``metadata["writes"]`` is **not** reliably populated by ``list()``; instead we read the
  serialized ``channel_values``. A step "introduced" an error when a designated error
  channel (default ``"error"``) is truthy at that checkpoint but falsy at its parent.
* the producing node's name is recovered best-effort from the parent checkpoint's
  internal ``branch:to:<node>`` channel; absent that, ``name`` is left unset.
"""

from __future__ import annotations

from typing import Any

from tracegraph.model import (
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)

_ROOT_NS = ""
_BRANCH_PREFIX = "branch:to:"
_SOURCE_MAP = {
    "input": StepSource.INPUT,
    "loop": StepSource.LOOP,
    "update": StepSource.UPDATE,
    "fork": StepSource.FORK,
}


class LangGraphCheckpointAdapter:
    def __init__(
        self,
        saver: Any,
        *,
        error_channel: str = "error",
        source_kind: str = "langgraph",
    ) -> None:
        self._saver = saver
        self._error_channel = error_channel
        self._source_kind = source_kind

    # --- TraceAdapter ---

    def discover(self) -> list[str]:
        """Distinct thread ids known to the saver (best effort; not all savers support it)."""
        try:
            tuples = list(self._saver.list(None))
        except Exception as exc:  # noqa: BLE001 - surface as a clear capability error
            raise NotImplementedError(
                "this checkpointer does not support listing all threads"
            ) from exc
        seen: list[str] = []
        for t in tuples:
            tid = (t.config.get("configurable") or {}).get("thread_id")
            if tid is not None and tid not in seen:
                seen.append(tid)
        return seen

    def ingest(self, trace_id: str) -> RawTrace:
        config = {"configurable": {"thread_id": trace_id}}
        tuples = [
            t
            for t in self._saver.list(config)
            if (t.config.get("configurable") or {}).get("checkpoint_ns", "") == _ROOT_NS
        ]
        if not tuples:
            raise KeyError(f"no root-namespace checkpoints for thread_id {trace_id!r}")

        checkpoints: dict[str, dict] = {}
        parent_of: dict[str, str | None] = {}
        steps: dict[str, Step] = {}
        for t in tuples:
            cp = t.checkpoint
            md = t.metadata or {}
            cid = cp["id"]
            parent_cfg = (t.parent_config or {}).get("configurable") or {}
            checkpoints[cid] = cp
            parent_of[cid] = parent_cfg.get("checkpoint_id")
            steps[cid] = Step(
                step_id=cid,
                trace_id=trace_id,
                seq=int(md.get("step", 0)),
                ts=cp.get("ts"),
                source=_SOURCE_MAP.get(md.get("source", "loop"), StepSource.LOOP),
                kind=StepKind.CHAIN,
                status=StepStatus.OK,
            )

        # A non-None parent we didn't ingest means the parent lives outside the root
        # namespace (a subgraph). Silently dropping it would fabricate a false extra root,
        # so we refuse — cross-namespace ingestion is the deferred Phase-1+ scope.
        dropped = {
            cid: pid
            for cid, pid in parent_of.items()
            if pid is not None and pid not in steps
        }
        if dropped:
            cid, pid = next(iter(dropped.items()))
            raise NotImplementedError(
                f"checkpoint {cid!r} has parent {pid!r} outside the root namespace; "
                "cross-namespace (subgraph) ingestion is deferred — this adapter reads "
                "checkpoint_ns='' only"
            )

        edges = [
            Edge(type=EdgeType.CAUSED_BY, src=cid, dst=pid)
            for cid, pid in parent_of.items()
            if pid is not None
        ]

        for cid, step in steps.items():
            parent_cp = checkpoints.get(parent_of.get(cid) or "")
            name = self._producing_node(parent_cp)
            if name:
                step.name = name
                if "tool" in name.lower():
                    step.kind = StepKind.TOOL
            err = self._error_introduced(checkpoints[cid], parent_cp)
            if err is not None:
                step.status = StepStatus.ERROR
                step.error_msg = err

        any_error = any(s.status is StepStatus.ERROR for s in steps.values())
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
        )
        return RawTrace(
            trace=trace,
            steps=sorted(steps.values(), key=lambda s: s.seq),
            causal_edges=edges,
        )

    # --- helpers ---

    @staticmethod
    def _producing_node(parent_cp: dict | None) -> str | None:
        if not parent_cp:
            return None
        for channel in parent_cp.get("channel_values", {}):
            if channel.startswith(_BRANCH_PREFIX):
                return channel[len(_BRANCH_PREFIX):]
        return None

    def _error_introduced(self, cp: dict, parent_cp: dict | None) -> str | None:
        """Return the error message iff this checkpoint is where the error first appears."""
        current = cp.get("channel_values", {}).get(self._error_channel)
        if not current:
            return None
        previous = (
            (parent_cp or {}).get("channel_values", {}).get(self._error_channel)
            if parent_cp
            else None
        )
        if previous:  # error was already present upstream -> not introduced here
            return None
        return str(current)
