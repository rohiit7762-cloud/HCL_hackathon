from __future__ import annotations

from typing import Any

from app.schemas.common import Modality
from app.schemas.ingestion import ChunkRecord


class ChromaVectorRepository:
    def __init__(self, path: str, embedding_model: str, dimension: int) -> None:
        import chromadb

        self.client = chromadb.PersistentClient(path=path)
        self.collection = self.client.get_or_create_collection(
            "knowledge",
            metadata={
                "hnsw:space": "cosine",
                "embedding_model": embedding_model,
                "embedding_dimension": dimension,
            },
        )
        meta = self.collection.metadata or {}
        if (
            meta.get("embedding_model") != embedding_model
            or meta.get("embedding_dimension") != dimension
        ):
            raise RuntimeError("Chroma embedding model/dimension mismatch; re-index the collection")

    def upsert_chunks(self, chunks: list[ChunkRecord], embeddings: list[list[float]]) -> None:
        if not chunks:
            return
        self.collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[
                {
                    "source_id": c.source_id,
                    "source_version": c.source_version,
                    "tenant_id": c.tenant_id,
                    "modality": c.modality.value,
                    "classification": c.classification.value,
                    "ordinal": c.ordinal,
                    "page": c.location.page or 0,
                    "location_label": c.location.label,
                    "heading_path": " > ".join(c.heading_path),
                    "image_source_id": c.source_id
                    if c.modality in {Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}
                    else "",
                }
                for c in chunks
            ],
        )

    def delete_source(self, source_id: str) -> None:
        self.collection.delete(where={"source_id": source_id})

    def query(
        self,
        embedding: list[float],
        tenant_id: str,
        source_ids: frozenset[str],
        modalities: frozenset[Modality],
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not source_ids or not modalities:
            return []
        result = self.collection.query(
            query_embeddings=[embedding],
            n_results=min(top_k, 100),
            where={
                "$and": [
                    {"tenant_id": tenant_id},
                    {"source_id": {"$in": sorted(source_ids)}},
                    {"modality": {"$in": [m.value for m in modalities]}},
                ]
            },
            include=["documents", "metadatas", "distances"],
        )
        return [
            {"text": t, "metadata": m, "distance": d}
            for t, m, d in zip(
                result["documents"][0], result["metadatas"][0], result["distances"][0], strict=False
            )
        ]
