import os

import numpy as np
import pytest
from leadradar_ai import Embedder, FastEmbedder
from leadradar_ai.retrieval.embed import BATCH_SIZE, E5_SMALL_DIM


class RecordingModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def embed(self, texts, batch_size):
        self.calls.append((list(texts), batch_size))
        return [np.full(3, len(t), dtype=np.float32) for t in texts]


def test_e5_prefixes_and_batching():
    model = RecordingModel()
    e = FastEmbedder(model=model)
    assert isinstance(e, Embedder)
    vectors = e.embed_passages(["a", "bb"])
    query = e.embed_query("q")
    assert model.calls == [(["passage: a", "passage: bb"], BATCH_SIZE), (["query: q"], BATCH_SIZE)]
    assert vectors == [[10.0] * 3, [11.0] * 3]
    assert isinstance(query[0], float)
    assert e.embed_passages([]) == []
    assert len(model.calls) == 2


@pytest.mark.live
def test_real_model_is_multilingual_and_normalized():
    e = FastEmbedder(cache_dir=os.environ.get("AI_EMBED_CACHE_DIR"))
    q = np.array(e.embed_query("Is the company running AI or RPA automation initiatives?"))
    ai_de, dividend = np.array(
        e.embed_passages(
            [
                "DHL setzt agentische KI zur Bearbeitung von Kundenanfragen ein.",
                "Die Dividende steigt auf 1,85 Euro je Aktie.",
            ]
        )
    )
    assert q.shape == (E5_SMALL_DIM,)
    assert np.isclose(np.linalg.norm(q), 1.0, atol=1e-3)
    assert q @ ai_de > q @ dividend
