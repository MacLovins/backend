"""The analysis graph (SPEC §1.7.1) — a workflow, not an agent (ARCHITECTURE P1):

START → resolve → collect → index ──Send(per service)──▶ [prefilter → extract → verify → score]
      ──▶ finalize → END
                                                          prefilter ──fingerprint unchanged──▶ score

Each service runs in its own subgraph, in parallel. Checkpoints after every step (thread_id =
"{run_id}:{company_id}"): after a worker crash `run_analysis` resumes from the failed step.
LLM quota exhaustion pauses only the affected services; the others finish, and `run_analysis` raises
AnalysisPaused. Running it again later is incremental: finished services skip the LLM by fingerprint.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send

from leadradar_ai.contracts import AnalysisInput
from leadradar_ai.errors import AnalysisPaused, LeadRadarAIError
from leadradar_ai.pipeline.deps import AnalysisDeps
from leadradar_ai.pipeline.nodes import Nodes
from leadradar_ai.pipeline.state import AnalysisOutput, AnalysisState, ServiceOutput, ServiceState


def _retry_transient(exc: Exception) -> bool:
    """Never retry domain errors (QuotaExhausted must pause the run at once)."""
    return not isinstance(exc, LeadRadarAIError) and isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _after(next_node: str):
    def route(state: ServiceState) -> str:
        return END if state.get("failed") else next_node

    return route


def _after_prefilter(state: ServiceState) -> str:
    if state.get("failed"):
        return END
    return "score" if state.get("skip_extraction") else "extract"


def _fan_out(state: AnalysisState) -> list[Send]:
    inp = state["input"]
    return [Send("service", {"input": inp, "company": state["company"], "service": s}) for s in inp.services]


def build_service_graph(nodes: Nodes):
    g = StateGraph(ServiceState, output_schema=ServiceOutput)
    g.add_node("prefilter", nodes.prefilter)
    g.add_node("extract", nodes.extract)
    g.add_node("verify", nodes.verify)
    g.add_node("score", nodes.score)
    g.add_edge(START, "prefilter")
    g.add_conditional_edges("prefilter", _after_prefilter, ["extract", "score", END])
    g.add_conditional_edges("extract", _after("verify"), ["verify", END])
    g.add_conditional_edges("verify", _after("score"), ["score", END])
    g.add_edge("score", END)
    return g.compile()


def build_analysis_graph(deps: AnalysisDeps):
    nodes = Nodes(deps)
    g = StateGraph(AnalysisState, output_schema=AnalysisOutput)
    g.add_node("resolve", nodes.resolve)
    g.add_node("collect", nodes.collect)
    g.add_node("index", nodes.index, retry_policy=RetryPolicy(max_attempts=3, retry_on=_retry_transient))
    g.add_node("service", build_service_graph(nodes))
    g.add_node("finalize", nodes.finalize)
    g.add_edge(START, "resolve")
    g.add_edge("resolve", "collect")
    g.add_edge("collect", "index")
    g.add_conditional_edges("index", _fan_out, ["service"])
    g.add_edge("service", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=deps.checkpointer)


def thread_config(inp: AnalysisInput) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"{inp.run_id}:{inp.company.id}"}}


async def run_analysis(graph: Any, inp: AnalysisInput) -> AnalysisOutput:
    """Run the analysis, or resume it from the checkpoint if this run/company was interrupted by a crash.

    Raises AnalysisPaused (a QuotaExhausted: core marks the company "paused"; calling again later continues).
    """
    config = thread_config(inp)
    output = None
    if graph.checkpointer is not None:
        snapshot = await graph.aget_state(config)
        if snapshot.next:
            output = await graph.ainvoke(None, config)
    if output is None:
        output = await graph.ainvoke({"input": inp}, config)
    paused = [str(o.service_id) for o in output.get("outcomes", []) if o.status == "paused"]
    if paused:
        raise AnalysisPaused("main", paused, output)
    return output
