"""Local, file-based adapters for the CLI and development without core: LLM cache, usage log, parser fixtures.

Core has SQL implementations of the same ports; these keep `lr-ai` runs reproducible and quota-free on
repeat (the cache survives between runs, the usage log keeps the daily RPD count).
"""

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from leadradar_ai.contracts import AnalysisDocument, LLMCallRecord
from leadradar_ai.llm.limiter import PACIFIC, pacific_today
from leadradar_ai.llm.types import Thinking, TransportError, TransportResponse


class FileLLMCache:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    async def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["value"]

    async def set(self, key: str, value: dict, meta: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"value": value, "meta": meta}, ensure_ascii=False), encoding="utf-8")


class FileUsageSink:
    """Appends every LLM call to a JSONL log; counts today's quota-consuming calls per model (Pacific day)."""

    def __init__(self, path: str | Path, today: Callable[[], date] = pacific_today) -> None:
        self.path = Path(path)
        self._today = today

    async def record(self, call: LLMCallRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {"at": datetime.now(PACIFIC).isoformat(), **call.model_dump(mode="json")}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    async def used_today(self, model: str) -> int:
        today = self._today().isoformat()
        return sum(
            1
            for r in self.records()
            if r["model"] == model
            and r["at"][:10] == today
            and r["status"] not in ("cache_hit", "rate_limited")
        )


class CacheOnlyTransport:
    """Transport for offline runs: every cache miss fails instead of calling the API."""

    async def generate(
        self, *, model: str, system: str, contents: str, output_model: type[BaseModel], thinking: Thinking
    ) -> TransportResponse:
        raise TransportError(400, "cache-only mode: this prompt is not cached; rerun with --live")


def load_parser_jsonl(path: str | Path) -> list[AnalysisDocument]:
    """Documents written by `lr-parser collect --out` (parser.Document) → AnalysisDocument with stable ids."""
    docs = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        digest = raw.get("content_hash") or hashlib.sha256(raw["text"].encode()).hexdigest()
        try:
            docs.append(
                AnalysisDocument(
                    id=uuid5(NAMESPACE_URL, f"leadradar:document:{digest}"),
                    source_type=raw["source_type"],
                    source_name=raw["source_name"],
                    url=raw.get("canonical_url") or raw["url"],
                    title=raw.get("title"),
                    text=raw["text"],
                    published_at=raw.get("published_at"),
                    fetched_at=raw["fetched_at"],
                    language=raw.get("language"),
                    meta=raw.get("meta") or {},
                )
            )
        except (KeyError, ValueError) as e:
            raise ValueError(f"{path}:{n}: not a parser Document: {e}") from e
    return docs
