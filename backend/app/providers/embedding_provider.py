"""Local embedding model behind the EmbeddingProvider interface (§8.3).

Uses fastembed (ONNX, no PyTorch) with BAAI/bge-small-en-v1.5 by default. The same
provider instance and model are used for indexing and querying.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_QUERY_CACHE_SIZE = 512
_BATCH_SIZE = 32


class FastEmbedProvider:
    def __init__(self, model_id: str, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding  # heavy import, deferred

        self.model_id = model_id
        self._model: Any = TextEmbedding(model_name=model_id, cache_dir=cache_dir)
        self.dimension = len(next(iter(self._model.embed(["dimension probe"]))))
        self._lock = asyncio.Lock()
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        logger.info(
            "embedding_model_loaded", extra={"model": model_id, "dimension": self.dimension}
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._embed_passages, list(texts))

    async def embed_query(self, text: str) -> list[float]:
        cached = self._query_cache.get(text)
        if cached is not None:
            self._query_cache.move_to_end(text)
            return cached
        async with self._lock:
            vector = await asyncio.to_thread(self._embed_query, text)
        self._query_cache[text] = vector
        if len(self._query_cache) > _QUERY_CACHE_SIZE:
            self._query_cache.popitem(last=False)
        return vector

    def _embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts, batch_size=_BATCH_SIZE)]

    def _embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()  # type: ignore[no-any-return]
