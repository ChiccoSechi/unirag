"""Embeddings with Workers AI (`bge-m3`, vectors of 1024 numbers).

Always use the same model: changing it needs a new Vectorize index.
"""

from dataclasses import dataclass

import httpx

from core.cloudflare import run_model
from core.config import cloudflare

DIMENSIONS = 1024  # bge-m3; another model needs a new index


@dataclass(frozen=True)
class Embedding:
    vectors: list[list[float]]
    neurons: float


def embedding(texts: list[str], client: httpx.Client | None = None) -> Embedding:
    """One vector per text, in the same order."""
    if not texts:
        return Embedding(vectors=[], neurons=0.0)
    result = run_model(cloudflare().embedding_model, {"text": texts}, client)
    vectors = result.get("data") or []
    if len(vectors) != len(texts) or any(len(v) != DIMENSIONS for v in vectors):
        raise ValueError("embedding: unexpected number or size of vectors")
    neurons = float((result.get("meta") or {}).get("neurons") or 0.0)
    return Embedding(vectors=vectors, neurons=neurons)
