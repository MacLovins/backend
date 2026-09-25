from dataclasses import dataclass, field
from typing import Any

from leadradar_ai.llm.types import LLMClient
from leadradar_ai.ports import AnalysisStore, Collector, Embedder, ProgressSink
from leadradar_ai.retrieval.prefilter import PrefilterConfig


@dataclass
class AnalysisDeps:
    """Everything external the graph needs.

    core builds it once at worker start (leadradar_core/worker/deps.py).

    checkpointer: AsyncPostgresSaver in core, InMemorySaver in tests, None = no resume.
    """

    collector: Collector
    store: AnalysisStore
    progress: ProgressSink
    llm: LLMClient
    embedder: Embedder
    checkpointer: Any = None
    prefilter: PrefilterConfig = field(default_factory=PrefilterConfig)
    max_input_tokens: int = 30_000
    max_items_per_source: int = 50
    collect_attempts: int = 2
    resolve_attempts: int = 3
    retry_backoff_s: float = 2.0
