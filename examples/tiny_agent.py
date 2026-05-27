"""A tiny but real LangGraph agent used to generate genuine checkpoint traces.

The graph deliberately produces two interesting shapes depending on input:

    plan → call_tool ──(error?)──▶ handle_error → respond
                       └──(ok)───────────────────▶ respond

* input containing ``"boom"`` makes ``call_tool`` write an ``error`` channel, so the
  run takes the ``handle_error`` branch — yielding an **error step** (for ``explain``)
  and a **longer path**.
* any other input skips ``handle_error`` — a **structurally different** run (for the
  Phase-3 ``diff``).

``build_app`` takes any ``BaseCheckpointSaver`` so tests can drive it with a
``SqliteSaver``; nothing here depends on tracegraph.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph


def _append(a: list[str] | None, b: list[str] | None) -> list[str]:
    return (a or []) + (b or [])


class AgentState(TypedDict):
    input: str
    error: str | None
    result: str | None
    log: Annotated[list[str], _append]


def _plan(state: AgentState) -> dict:
    return {"log": ["planned"]}


def _call_tool(state: AgentState) -> dict:
    if "boom" in state["input"]:
        return {"error": f"tool failed on input {state['input']!r}", "log": ["tool_error"]}
    return {"result": "tool ok", "log": ["tool_ok"]}


def _handle_error(state: AgentState) -> dict:
    return {"result": "recovered", "log": ["handled"]}


def _respond(state: AgentState) -> dict:
    return {"result": state.get("result") or "done", "log": ["responded"]}


def _route(state: AgentState) -> str:
    return "handle_error" if state.get("error") else "respond"


def build_app(checkpointer: BaseCheckpointSaver):
    g = StateGraph(AgentState)
    g.add_node("plan", _plan)
    g.add_node("call_tool", _call_tool)
    g.add_node("handle_error", _handle_error)
    g.add_node("respond", _respond)
    g.add_edge(START, "plan")
    g.add_edge("plan", "call_tool")
    g.add_conditional_edges(
        "call_tool", _route, {"handle_error": "handle_error", "respond": "respond"}
    )
    g.add_edge("handle_error", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)


def run(checkpointer: BaseCheckpointSaver, thread_id: str, user_input: str) -> dict:
    """Run one thread and return the final state. Checkpoints persist in ``checkpointer``."""
    app = build_app(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    return app.invoke(
        {"input": user_input, "error": None, "result": None, "log": []}, config
    )


if __name__ == "__main__":  # pragma: no cover - manual demo
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string("trace.db") as saver:
        print("thread A:", run(saver, "A", "boom"))
        print("thread B:", run(saver, "B", "hello"))
    print("wrote trace.db — try: tracegraph inspect A (Phase 2)")
