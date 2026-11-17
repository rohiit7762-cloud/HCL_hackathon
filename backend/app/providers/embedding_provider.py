from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor


class SentenceTransformerEmbeddingProvider:
    def __init__(self, model_id: str, cache_size: int = 512) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self._model = SentenceTransformer(model_id)
        self.dimension = self._model.get_sentence_embedding_dimension()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size

    def _encode(self, text: str, query: bool) -> list[float]:
        if query and text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        value = (
            ("Represent this sentence for searching relevant passages: " + text)
            if query and "bge" in self.model_id.lower()
            else text
        )
        vector = self._model.encode([value], normalize_embeddings=True, show_progress_bar=False)[
            0
        ].tolist()
        if query:
            self._cache[text] = vector
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return vector

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._encode_batch, list(texts))

    def _encode_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        ).tolist()

    async def embed_query(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._encode, text, True)
