from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from app.repositories.vector_repository import ChromaVectorRepository
from app.schemas.common import Modality
from app.schemas.evidence import Evidence, EvidenceKind, EvidenceLocation, RetrievalResult
from app.schemas.interfaces import EmbeddingProvider, PolicyService
from app.schemas.security import AccessScope, UserContext
from app.schemas.tools import RetrievalRequest


class RetrievalAgent:
    def __init__(
        self,
        vectors: ChromaVectorRepository,
        embeddings: EmbeddingProvider,
        policy: PolicyService,
        sources: dict[str, object],
    ):
        self.vectors, self.embeddings, self.policy, self.sources = (
            vectors,
            embeddings,
            policy,
            sources,
        )

    async def retrieve(
        self, user: UserContext, scope: AccessScope, request: RetrievalRequest
    ) -> RetrievalResult:
        if not scope.hash_is_consistent() or user.tenant_id != scope.tenant_id:
            return RetrievalResult(task_id=request.task_id)
        if not scope.authorized_source_ids:
            return RetrievalResult(task_id=request.task_id)
        query = " ".join([request.query, *request.keywords])
        embedding = await self.embeddings.embed_query(query)
        candidates = self.vectors.query(
            embedding,
            user.tenant_id,
            scope.authorized_source_ids,
            request.modalities,
            request.top_k * 2,
        )
        terms = {x.lower() for x in request.keywords}
        ranked = []
        for hit in candidates:
            m = hit["metadata"]
            text = hit["text"]
            lexical = sum(term in text.lower() for term in terms) * 0.025
            ranked.append((1 - float(hit["distance"]) + lexical, hit))
        ranked.sort(key=lambda x: x[0], reverse=True)
        count = Counter()
        evidence = []
        for score, hit in ranked:
            m = hit["metadata"]
            sid = m["source_id"]
            if count[sid] >= 3:
                continue
            src = self.sources.get(sid)
            if src is None:
                continue
            modality = Modality(m["modality"])
            kind = EvidenceKind.TEXT_CHUNK
            if modality in {Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}:
                kind = EvidenceKind.IMAGE
            label = m.get("location_label") or "source"
            evidence.append(
                Evidence(
                    evidence_id=f"{request.task_id}-{len(evidence) + 1}",
                    task_id=request.task_id,
                    kind=kind,
                    source_id=sid,
                    source_title=src.title,
                    source_kind=src.kind,
                    classification=src.classification,
                    excerpt=hit["text"][:1500],
                    location=EvidenceLocation(
                        page=m.get("page") or None,
                        section_path=m.get("heading_path", "").split(" > ")
                        if m.get("heading_path")
                        else [],
                        label=label,
                    ),
                    score=score,
                    image_ref=f"/api/v1/sources/{sid}/image"
                    if kind == EvidenceKind.IMAGE
                    else None,
                    derived_from=m.get("image_source_id") or None,
                    retrieved_at=datetime.now(UTC),
                )
            )
            count[sid] += 1
        filtered = await self.policy.filter_evidence(user, scope, evidence)
        return RetrievalResult(task_id=request.task_id, evidence=filtered)
