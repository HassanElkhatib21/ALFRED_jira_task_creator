from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src import config
from src.graph.state import GraphState
from src.graph import nodes


def _build_graph():
    g = StateGraph(GraphState)
    g.add_node("extract", nodes.extract)
    g.add_node("validate", nodes.validate)
    g.add_node("enrich", nodes.enrich)
    g.add_node("format_description", nodes.format_description)
    g.add_node("build_preview", nodes.build_preview)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", nodes.extract_branch, {"ready": "validate", "ask": END})
    g.add_conditional_edges("validate", nodes.has_errors, {"ok": "enrich", "fail": END})
    g.add_conditional_edges("enrich", nodes.has_errors, {"ok": "format_description", "fail": END})
    g.add_edge("format_description", "build_preview")
    g.add_edge("build_preview", END)
    return g


_compiled = None
_saver_cm = None


async def get_workflow():
    global _compiled, _saver_cm
    if _compiled is None:
        _saver_cm = AsyncSqliteSaver.from_conn_string(config.CHECKPOINT_DB)
        saver = await _saver_cm.__aenter__()
        _compiled = _build_graph().compile(checkpointer=saver)
    return _compiled
