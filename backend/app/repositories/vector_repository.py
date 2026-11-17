"""Chroma-backed vector store (§8.4).

Every query is filtered by tenant AND an explicit list of authorized source IDs;
there is no code path that searches without that filter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence
from typing import Any

from app.core.logging import get_logger
from app.repositories.base import ChunkHit
from app.schemas.common import Classification, Modality
from app.schemas.evidence import EvidenceLocation
from app.schemas.ingestion import ChunkRecord

logger = get_logger(__name__)

COLLECTION = "knowledge"


class EmbeddingMismatchError(RuntimeError):
    pass


class ChromaVectorStore:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._collection = client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    @classmethod
    def persistent(cls, path: str) -> ChromaVectorStore:
        import chromadb

        return cls(chromadb.PersistentClient(path=path))

    @classmethod
    def ephemeral(cls) -> ChromaVectorStore:
        import chromadb

        client = chromadb.EphemeralClient()
        with contextlib.suppress(Exception):  # collection may not exist yet
            client.delete_collection(COLLECTION)
        return cls(client)

    def ensure_compatible(self, model_id: str, dimension: int) -> None:
        """Refuse to mix embedding models in one index (§8.3)."""
        meta = dict(self._collection.metadata or {})
        recorded_model, recorded_dim = meta.get("embedding_model"), meta.get("dimension")
        if recorded_model is None:
            if self._collection.count() > 0:
                raise EmbeddingMismatchError("Index has no embedding metadata; re-index required.")
            meta.update({"embedding_model": model_id, "dimension": dimension})
            meta.pop("hnsw:space", None)  # immutable after creation
            self._collection.modify(metadata={**meta})
            return
        if recorded_model != model_id or int(str(recorded_dim)) != dimension:
            raise EmbeddingMismatchError(
                f"Index was built with {recorded_model} ({recorded_dim}d) but the configured "
                f"model is {model_id} ({dimension}d). Delete CHROMA_PATH and re-run seeding."
            )

    async def upsert_chunks(
        self,
        chunks: Sequence[ChunkRecord],
        embeddings: Sequence[Sequence[float]],
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not chunks:
            return
        metadatas = [_to_metadata(c, extra or {}) for c in chunks]
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[c.chunk_id for c in chunks],
            embeddings=[list(e) for e in embeddings],
            documents=[c.text for c in chunks],
            metadatas=metadatas,
        )

    async def delete_source(self, source_id: str) -> None:
        await asyncio.to_thread(self._collection.delete, where={"source_id": source_id})

    async def query(
        self,
        embedding: Sequence[float],
        *,
        tenant_id: str,
        source_ids: Sequence[str],
        modalities: Sequence[str],
        top_k: int,
    ) -> list[ChunkHit]:
        if not source_ids or not modalities or top_k <= 0:
            return []
        where = {
            "$and": [
                {"tenant_id": tenant_id},
                {"source_id": {"$in": list(source_ids)}},
                {"modality": {"$in": list(modalities)}},
            ]
        }
        result = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[list(embedding)],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[ChunkHit] = []
        for doc, meta, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        ):
            # Defense in depth: never trust the filter alone.
            if meta.get("tenant_id") != tenant_id or meta.get("source_id") not in source_ids:
                continue
            hits.append(
                ChunkHit(
                    chunk=_from_metadata(doc, meta),
                    score=max(0.0, min(1.0, 1.0 - float(distance))),
                    image_source_id=str(meta.get("image_source_id") or "") or None,
                )
            )
        return hits

    async def get_chunk(self, source_id: str, location_label: str | None) -> ChunkRecord | None:
        clauses: list[dict[str, Any]] = [{"source_id": source_id}]
        if location_label:
            clauses.append({"location_label": location_label})
        where = {"$and": clauses} if len(clauses) > 1 else clauses[0]
        result = await asyncio.to_thread(
            self._collection.get, where=where, include=["documents", "metadatas"]
        )
        if not result["ids"]:
            return None if location_label is None else await self.get_chunk(source_id, None)
        pairs = sorted(
            zip(result["documents"], result["metadatas"], strict=True),
            key=lambda p: int(p[1].get("ordinal", 0)),
        )
        return _from_metadata(*pairs[0])


def _to_metadata(chunk: ChunkRecord, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": chunk.source_id,
        "source_version": chunk.source_version,
        "tenant_id": chunk.tenant_id,
        "ordinal": chunk.ordinal,
        "modality": chunk.modality.value,
        "classification": chunk.classification.value,
        "page": chunk.location.page if chunk.location.page is not None else -1,
        "location_label": chunk.location.label,
        "location_json": chunk.location.model_dump_json(),
        "heading_path": " > ".join(chunk.heading_path),
        "token_count": chunk.token_count,
        "embedding_model": chunk.embedding_model,
        "image_source_id": str(extra.get("image_source_id") or ""),
    }


def _from_metadata(document: str, meta: dict[str, Any]) -> ChunkRecord:
    heading = str(meta.get("heading_path") or "")
    return ChunkRecord(
        chunk_id=str(meta.get("chunk_id", "")) or f"{meta['source_id']}:{meta['ordinal']}",
        source_id=str(meta["source_id"]),
        source_version=int(meta["source_version"]),
        tenant_id=str(meta["tenant_id"]),
        ordinal=int(meta["ordinal"]),
        modality=Modality(meta["modality"]),
        text=document,
        heading_path=heading.split(" > ") if heading else [],
        location=EvidenceLocation.model_validate(json.loads(meta["location_json"])),
        token_count=int(meta.get("token_count", 0)),
        classification=Classification(meta["classification"]),
        embedding_model=str(meta.get("embedding_model", "")),
    )
