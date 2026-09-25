"""FastEmbedder — local CPU embeddings (SPEC AI-04): intfloat/multilingual-e5-small, 384-d, normalized.

fastembed has no built-in e5-small, so it is registered as a custom ONNX model from the HF repo.
e5 needs the "query: " / "passage: " prefixes. The model is loaded lazily (~0.5 GB download on first use;
the Docker image warms it at build time).
"""

import threading
from collections.abc import Iterable
from typing import Any

E5_SMALL = "intfloat/multilingual-e5-small"
E5_SMALL_DIM = 384
BATCH_SIZE = 64

_register_lock = threading.Lock()
_registered: set[str] = set()


def _ensure_registered(model_name: str) -> None:
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    with _register_lock:
        if model_name in _registered:
            return
        supported = {m["model"] for m in TextEmbedding.list_supported_models()}
        if model_name == E5_SMALL and model_name not in supported:
            TextEmbedding.add_custom_model(
                model=E5_SMALL,
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=E5_SMALL),
                dim=E5_SMALL_DIM,
                model_file="onnx/model.onnx",
            )
        _registered.add(model_name)


class FastEmbedder:
    def __init__(
        self,
        model_name: str = E5_SMALL,
        *,
        cache_dir: str | None = None,
        threads: int | None = None,
        model: Any = None,  # injected in tests: anything with .embed(texts, batch_size) → iterable of vectors
    ) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir
        self._threads = threads
        self._model = model
        self._lock = threading.Lock()

    def _get_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    _ensure_registered(self.model_name)
                    self._model = TextEmbedding(
                        self.model_name, cache_dir=self._cache_dir, threads=self._threads
                    )
        return self._model

    def warm_up(self) -> None:
        """Download and load the model now (worker start, Docker build)."""
        self.embed_query("warm up")

    def _embed(self, texts: Iterable[str]) -> list[list[float]]:
        return [
            [float(x) for x in vec] for vec in self._get_model().embed(list(texts), batch_size=BATCH_SIZE)
        ]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return self._embed(f"passage: {t}" for t in texts) if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"query: {text}"])[0]
