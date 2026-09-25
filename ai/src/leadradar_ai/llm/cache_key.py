import hashlib
import json

from pydantic import BaseModel


def cache_key(prompt_version: str, system: str, contents: str, output_model: type[BaseModel]) -> str:
    """sha256(prompt_version | system | contents | schema): any prompt or schema change misses the cache."""
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True, ensure_ascii=False)
    sep = "\x1f"  # unit separator: fields cannot run into each other
    payload = f"{prompt_version}{sep}{system}{sep}{contents}{sep}{schema}"
    return hashlib.sha256(payload.encode()).hexdigest()


def estimate_tokens(*texts: str) -> int:
    """Rough budget check: ~4 characters per token."""
    return sum(len(t) for t in texts) // 4
