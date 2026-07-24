"""Embeddings, cached by content hash — the main cost lever in README section 2.

Anthropic does not expose an embeddings endpoint, so the provider here is separate
from the chat model:

  local  (default) deterministic hashed-lexical vectors. No key, no spend, offline.
                   Good enough for the recall pass, which only has to keep the true
                   match inside the top-k the LLM precision pass then re-ranks.
  voyage           Voyage AI, Anthropic's recommended embedding partner. Better
                   semantic recall; needs VOYAGE_API_KEY.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from ..config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class LocalHashEmbedder:
    """Hashed bag-of-words with sublinear term weighting, L2-normalized.

    Deterministic and dependency-free. Cosine similarity over these vectors is a
    lexical-overlap signal, not a semantic one — which is the honest description
    of what it buys you. Swap to `voyage` when recall matters more than zero spend.
    """

    def __init__(self, dim: int | None = None):
        self.dim = dim or get_settings().embedding_dim

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] & 1 else -1.0
        return index, sign

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        counts: dict[str, int] = {}
        for token in tokenize(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            index, sign = self._bucket(token)
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


class VoyageEmbedder:
    def __init__(self, model: str = "voyage-3-large", dim: int | None = None):
        self.model = model
        self.dim = dim or get_settings().embedding_dim

    def embed(self, text: str) -> list[float]:
        import os

        import httpx

        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise RuntimeError("VOYAGE_API_KEY is required for embedding_provider=voyage")
        response = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": self.model,
                "input": [text],
                "input_type": "document",
                "output_dimension": self.dim,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.embedding_provider == "voyage":
        return VoyageEmbedder(dim=settings.embedding_dim)
    return LocalHashEmbedder(dim=settings.embedding_dim)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
