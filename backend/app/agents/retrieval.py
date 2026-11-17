"""Retrieval Agent: semantic + image retrieval over authorized content (§8.9, §8.5).

Access filters are applied BEFORE retrieval (Chroma is queried only within the
task's authorized source IDs) and before any image reaches a vision model.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.errors import AppError
from app.core.logging import get_logger
from app.ingestion.ocr import OcrEngine
from app.prompts import PromptLibrary
from app.repositories.base import ChunkHit, SourceRegistry, VectorStore
from app.repositories.blob_store import BlobStore
from app.schemas.common import Classification, Modality, SourceKind
from app.schemas.evidence import Evidence, EvidenceKind, EvidenceLocation, RetrievalResult
from app.schemas.ingestion import SourceMetadata
from app.schemas.interfaces import EmbeddingProvider, LLMProvider, UploadStore
from app.schemas.llm import ImageInput
from app.schemas.security import AccessScope, UserContext
from app.schemas.tools import RetrievalRequest
from app.security.policy import upload_source_id

logger = get_logger(__name__)

MAX_PER_SOURCE = 3
MAX_VISION_IMAGES = 2
MAX_EXCERPT = 1400
KEYWORD_BOOST = 0.08
VISION_UNAVAILABLE = (
    "Visual interpretation unavailable; this answer uses OCR text and captions only."
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}")


class RetrievalAgent:
    def __init__(
        self,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
        registry: SourceRegistry,
        blobs: BlobStore,
        uploads: UploadStore,
        llm: LLMProvider,
        ocr: OcrEngine,
        prompts: PromptLibrary,
    ) -> None:
        self._embeddings = embeddings
        self._vectors = vectors
        self._registry = registry
        self._blobs = blobs
        self._uploads = uploads
        self._llm = llm
        self._ocr = ocr
        self._prompts = prompts

    async def retrieve(
        self, user: UserContext, scope: AccessScope, request: RetrievalRequest
    ) -> RetrievalResult:
        warnings: list[str] = []
        evidence: list[Evidence] = []
        query_text = " ".join([request.query, *request.keywords])

        for upload_id in request.upload_ids:
            item, extra_query = await self._upload_evidence(user, request, upload_id, warnings)
            if item:
                evidence.append(item)
                query_text = f"{query_text} {extra_query}"[:2000]

        if scope.authorized_source_ids and scope.tenant_id == user.tenant_id:
            hits = await self._search(user, scope, request, query_text)
            sources = await self._sources_for(hits)
            evidence += [
                self._hit_evidence(request, h, sources)
                for h in hits
                if h.chunk.source_id in sources
            ]
            if request.need_visual_interpretation:
                evidence += await self._vision_evidence(request, hits, sources, warnings)

        return RetrievalResult(task_id=request.task_id, evidence=evidence, warnings=warnings)

    # --- semantic search -------------------------------------------------
    async def _search(
        self, user: UserContext, scope: AccessScope, request: RetrievalRequest, query_text: str
    ) -> list[ChunkHit]:
        vector = await self._embeddings.embed_query(query_text)
        hits = await self._vectors.query(
            vector,
            tenant_id=user.tenant_id,
            source_ids=sorted(scope.authorized_source_ids),
            modalities=sorted(m.value for m in request.modalities),
            top_k=request.top_k * 2,
        )
        terms = {w.lower() for w in _WORD.findall(" ".join([request.query, *request.keywords]))}

        def boosted(hit: ChunkHit) -> float:
            if not terms:
                return hit.score
            words = {w.lower() for w in _WORD.findall(hit.chunk.text)}
            return min(1.0, hit.score + KEYWORD_BOOST * len(terms & words) / len(terms))

        ranked = sorted(hits, key=boosted, reverse=True)
        per_source: dict[str, int] = {}
        chosen: list[ChunkHit] = []
        for hit in ranked:
            count = per_source.get(hit.chunk.source_id, 0)
            if count >= MAX_PER_SOURCE:
                continue
            per_source[hit.chunk.source_id] = count + 1
            chosen.append(ChunkHit(hit.chunk, boosted(hit), hit.image_source_id))
            if len(chosen) >= request.top_k:
                break
        return chosen

    async def _sources_for(self, hits: list[ChunkHit]) -> dict[str, SourceMetadata]:
        sources: dict[str, SourceMetadata] = {}
        for hit in hits:
            source_id = hit.chunk.source_id
            if source_id not in sources:
                source = await self._registry.get(source_id)
                if source is not None and not source.revoked:
                    sources[source_id] = source
        return sources

    def _hit_evidence(
        self, request: RetrievalRequest, hit: ChunkHit, sources: dict[str, SourceMetadata]
    ) -> Evidence:
        source = sources[hit.chunk.source_id]
        is_image = hit.chunk.modality in {Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}
        return Evidence(
            evidence_id=f"{request.task_id}.r{hit.chunk.ordinal}",
            task_id=request.task_id,
            kind=EvidenceKind.IMAGE if is_image else EvidenceKind.TEXT_CHUNK,
            source_id=source.source_id,
            source_title=source.title,
            source_kind=source.kind,
            classification=source.classification,
            excerpt=_trim(hit.chunk.text),
            location=hit.chunk.location,
            score=round(hit.score, 4),
            image_ref=f"/api/v1/sources/{source.source_id}/image" if is_image else None,
            derived_from=source.parent_source_id,
            retrieved_at=datetime.now(UTC),
        )

    # --- images ----------------------------------------------------------
    async def _vision_evidence(
        self,
        request: RetrievalRequest,
        hits: list[ChunkHit],
        sources: dict[str, SourceMetadata],
        warnings: list[str],
    ) -> list[Evidence]:
        image_sources: list[SourceMetadata] = []
        for hit in hits:
            source = sources.get(hit.chunk.source_id)
            if source and source.kind == SourceKind.IMAGE and source not in image_sources:
                image_sources.append(source)
        if not image_sources:
            return []
        if not self._llm.supports_vision():
            warnings.append(VISION_UNAVAILABLE)
            return []
        observations: list[Evidence] = []
        instruction = self._prompts.render("image_question", question=request.query)
        for source in image_sources[:MAX_VISION_IMAGES]:
            data = await self._blobs.read(source.blob_path)
            if data is None:
                continue
            try:
                answer = await self._llm.describe_image(
                    ImageInput(data=data, mime_type=source.mime_type, source_id=source.source_id),
                    instruction,
                )
            except AppError:
                if VISION_UNAVAILABLE not in warnings:
                    warnings.append(VISION_UNAVAILABLE)
                continue
            observations.append(
                Evidence(
                    evidence_id=f"{request.task_id}.v{len(observations)}",
                    task_id=request.task_id,
                    kind=EvidenceKind.VISION_OBSERVATION,
                    source_id=source.source_id,
                    source_title=source.title,
                    source_kind=source.kind,
                    classification=source.classification,
                    excerpt=_trim(answer),
                    location=EvidenceLocation(label="visual interpretation of the image"),
                    image_ref=f"/api/v1/sources/{source.source_id}/image",
                    derived_from=source.parent_source_id,
                    retrieved_at=datetime.now(UTC),
                )
            )
        return observations

    async def _upload_evidence(
        self, user: UserContext, request: RetrievalRequest, upload_id: str, warnings: list[str]
    ) -> tuple[Evidence | None, str]:
        image = await self._uploads.load(user, upload_id)  # owner-only
        if image is None:
            return None, ""
        ocr_text = await self._ocr.extract(image.data)
        description = ""
        if self._llm.supports_vision():
            try:
                description = await self._llm.describe_image(
                    image, self._prompts.render("image_question", question=request.query)
                )
            except AppError:
                warnings.append(VISION_UNAVAILABLE)
        else:
            warnings.append(VISION_UNAVAILABLE)
        parts = [
            p for p in (description, f"Text in the image:\n{ocr_text}" if ocr_text else "") if p
        ]
        if not parts:
            return None, ""
        return (
            Evidence(
                evidence_id=f"{request.task_id}.u{upload_id[-6:]}",
                task_id=request.task_id,
                kind=EvidenceKind.VISION_OBSERVATION,
                source_id=upload_source_id(user, upload_id),
                source_title="Your uploaded image",
                source_kind=SourceKind.USER_UPLOAD,
                classification=Classification.INTERNAL,
                excerpt=_trim("\n\n".join(parts)),
                location=EvidenceLocation(label="uploaded image"),
                retrieved_at=datetime.now(UTC),
            ),
            ocr_text[:300],
        )


def _trim(text: str, limit: int = MAX_EXCERPT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[: end + 1] if end > limit // 2 else cut).rstrip() + " …"
