"""D1 and Vectorize through Cloudflare's HTTP API (pipeline and local RAG).

The Worker does not use this module: there D1 and Vectorize are reached through bindings.

- D1: one query with parameters (never values pasted into the SQL) or a batch of queries, which is
  atomic: if one fails none is applied (checked on the database on 12 October 2026).
- Vectorize: insert or replace in batches (NDJSON file, as `wrangler` does), delete by ID, search
  with metadata filters. Inserts and deletes are asynchronous: they return a mutation ID and
  `wait` waits until the index has processed it.

Errors hold only the HTTP status and Cloudflare's codes: never SQL, parameters, texts or tokens.
"""

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from core.cloudflare import call
from core.config import MissingConfiguration, cloudflare

VECTORIZE_BATCH = 1000  # vectors per request (API limit: 5,000)
DELETE_BATCH = 100  # API limit: with 101 IDs it answers HTTP 400, code 40007 (12/10/2026)

Param = str | int | float | None


@dataclass(frozen=True)
class Vector:
    id: str
    values: list[float]
    metadata: dict = field(default_factory=dict)


# --- D1 ---


def _database() -> str:
    database = cloudflare().d1_database
    if not database:
        raise MissingConfiguration("D1_DATABASE_ID")
    return database


def _params(params: Sequence[Param]) -> list[str | None]:
    """D1's API wants parameters as text: SQLite converts them to the column type."""
    return [None if p is None else str(p) for p in params]


def d1(sql: str, params: Sequence[Param] = (), client: httpx.Client | None = None) -> list:
    """Rows returned by a query (list of column → value dictionaries)."""
    body = {"sql": sql, "params": _params(params)}
    result = call("D1", "POST", f"/d1/database/{_database()}/query", client, json=body) or []
    return (result[0].get("results") or []) if result else []


def d1_batch(
    statements: Sequence[tuple[str, Sequence[Param]]], client: httpx.Client | None = None
) -> list[list]:
    """Several queries at once, atomic: all applied or none. The rows of each."""
    if not statements:
        return []
    body = {"batch": [{"sql": sql, "params": _params(p)} for sql, p in statements]}
    result = call("D1", "POST", f"/d1/database/{_database()}/query", client, json=body) or []
    return [r.get("results") or [] for r in result]


# --- Vectorize ---


def _index() -> str:
    return f"/vectorize/v2/indexes/{cloudflare().vectorize_index}"


def insert(vectors: Sequence[Vector], client: httpx.Client | None = None) -> list[str]:
    """Inserts or replaces (same ID = replaced); returns the mutation IDs."""
    mutations = []
    for i in range(0, len(vectors), VECTORIZE_BATCH):
        lines = "\n".join(
            json.dumps({"id": v.id, "values": v.values, "metadata": v.metadata}, ensure_ascii=False)
            for v in vectors[i : i + VECTORIZE_BATCH]
        )
        file = {"vectors": ("vectors.ndjson", lines.encode(), "application/x-ndjson")}
        result = call("Vectorize", "POST", f"{_index()}/upsert", client, files=file) or {}
        mutations.append(result.get("mutationId", ""))
    return mutations


def delete(ids: Sequence[str], client: httpx.Client | None = None) -> list[str]:
    """Deletes the vectors with these IDs (missing ones are ignored)."""
    mutations = []
    for i in range(0, len(ids), DELETE_BATCH):
        body = {"ids": list(ids[i : i + DELETE_BATCH])}
        result = call("Vectorize", "POST", f"{_index()}/delete_by_ids", client, json=body) or {}
        mutations.append(result.get("mutationId", ""))
    return mutations


def search(
    vector: Sequence[float],
    top_k: int = 8,
    filter: dict | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """The most similar chunks (id, score, metadata), at most 50 with metadata."""
    body = {"vector": list(vector), "topK": top_k, "returnMetadata": "all", "returnValues": False}
    if filter:
        body["filter"] = filter
    result = call("Vectorize", "POST", f"{_index()}/query", client, json=body) or {}
    return result.get("matches") or []


def info(client: httpx.Client | None = None) -> dict:
    """Number of vectors and last mutation processed by the index."""
    return call("Vectorize", "GET", f"{_index()}/info", client) or {}


def wait(
    mutation: str,
    max_seconds: float = 600,
    delay: float = 5,
    client: httpx.Client | None = None,
) -> None:
    """Waits until the index has processed the mutation (mutations are processed in order: waiting
    for the last one sent is enough). Past the maximum time: TimeoutError."""
    if not mutation:
        return
    end = time.monotonic() + max_seconds
    while info(client).get("processedUpToMutation") != mutation:
        if time.monotonic() > end:
            raise TimeoutError("Vectorize: mutation not processed yet")
        time.sleep(delay)
