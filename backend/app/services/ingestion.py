"""Ingestion service (§8.12): documents, images and structured data.

Every derived artifact (chunks, OCR text, captions, embedded images) inherits the
access metadata of its original source (invariant I4). Failures leave no partial
chunks behind.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePath

from app.core.errors import AppError, PermissionDeniedError, ValidationFailedError
from app.core.logging import get_logger
from app.ingestion.ocr import OcrEngine
from app.ingestion.parsers import STRUCTURED_TYPES, ParsedDocument, parser_for, sniff_mime
from app.ingestion.semantic_chunker import SemanticChunker, chunk_id, count_tokens, embedding_text
from app.ingestion.structured_loader import StructuredLoader
from app.prompts import PromptLibrary
from app.repositories.base import SourceRegistry, VectorStore
from app.repositories.blob_store import BlobStore
from app.schemas.common import Modality, SourceKind
from app.schemas.evidence import EvidenceLocation
from app.schemas.ingestion import ChunkRecord, IngestOptions, IngestResult, SourceMetadata
from app.schemas.interfaces import EmbeddingProvider, LLMProvider
from app.schemas.llm import ImageInput
from app.schemas.security import UserContext

logger = get_logger(__name__)

INGEST_ROLES = ("ingestor", "admin")
MAX_IMAGES_PER_DOCUMENT = 8
_MIME_FOR_IMAGE = {"image/png", "image/jpeg", "image/webp"}


class IngestionServiceImpl:
    def __init__(
        self,
        registry: SourceRegistry,
        vectors: VectorStore,
        embeddings: EmbeddingProvider,
        chunker: SemanticChunker,
        blobs: BlobStore,
        loader: StructuredLoader | None,
        llm: LLMProvider,
        ocr: OcrEngine,
        prompts: PromptLibrary,
        *,
        max_bytes: int,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._vectors = vectors
        self._embeddings = embeddings
        self._chunker = chunker
        self._blobs = blobs
        self._loader = loader
        self._llm = llm
        self._ocr = ocr
        self._prompts = prompts
        self._max_bytes = max_bytes
        self._on_change = on_change

    async def ingest(
        self, user: UserContext, filename: str, data: bytes, options: IngestOptions
    ) -> IngestResult:
        if not user.has_role(*INGEST_ROLES):
            raise PermissionDeniedError()
        if len(data) > self._max_bytes:
            raise ValidationFailedError("The file is too large to ingest.")
        mime = sniff_mime(filename, data)
        if mime is None:
            raise ValidationFailedError("Unsupported or unrecognized file type.")

        tenant = user.tenant_id
        checksum = hashlib.sha256(data).hexdigest()
        title = options.title or PurePath(filename).stem.replace("_", " ")
        previous = await self._registry.find_latest_by_title(tenant, title)
        duplicate = await self._registry.find_by_checksum(tenant, checksum)
        if (
            duplicate is not None
            and previous is not None
            and duplicate.source_id == previous.source_id
        ):
            return IngestResult(
                source_id=duplicate.source_id, version=duplicate.version, status="skipped_duplicate"
            )

        source_id = previous.source_id if previous else _source_id(tenant, title)
        version = previous.version + 1 if previous else 1
        kind = (
            SourceKind.IMAGE
            if mime in _MIME_FOR_IMAGE
            else SourceKind.STRUCTURED
            if mime in STRUCTURED_TYPES.values()
            else SourceKind.DOCUMENT
        )
        blob_path = self._blobs.path_for(tenant, source_id, version, PurePath(filename).name)
        source = SourceMetadata(
            source_id=source_id,
            tenant_id=tenant,
            title=title,
            kind=kind,
            mime_type=mime,
            classification=options.classification,
            allowed_groups=frozenset(options.allowed_groups),
            allowed_roles=frozenset(options.allowed_roles),
            contains_personal_data=options.contains_personal_data,
            contains_confidential_business=options.contains_confidential_business,
            subject_employee_ids=frozenset(options.subject_employee_ids),
            checksum_sha256=checksum,
            version=version,
            blob_path=blob_path,
            ingested_by=user.employee_id,
            ingested_at=datetime.now(UTC),
        )

        try:
            await self._blobs.save(blob_path, data)
            if kind == SourceKind.STRUCTURED:
                result = await self._ingest_structured(source, filename, data, options)
            else:
                await self._remove_previous(source_id)
                if kind == SourceKind.IMAGE:
                    count = await self._index_image(source, data, context=title)
                    result = IngestResult(
                        source_id=source_id, version=version, status="indexed", chunk_count=count
                    )
                else:
                    result = await self._ingest_document(source, filename, data, mime)
        except AppError:
            await self._cleanup(source_id)
            raise
        except Exception as exc:
            logger.error(
                "ingest_failed", extra={"source_id": source_id, "exc_type": type(exc).__name__}
            )
            await self._cleanup(source_id)
            return IngestResult(
                source_id=source_id,
                version=version,
                status="failed",
                warnings=["Ingestion failed; no content was indexed."],
            )

        if self._on_change:
            self._on_change(tenant)
        logger.info(
            "ingested",
            extra={
                "source_id": source_id,
                "kind": kind.value,
                "status": result.status,
                "chunks": result.chunk_count,
                "rows": result.row_count,
            },
        )
        return result

    # --- documents -------------------------------------------------------
    async def _ingest_document(
        self, source: SourceMetadata, filename: str, data: bytes, mime: str
    ) -> IngestResult:
        parser = parser_for(mime)
        if parser is None:
            raise ValidationFailedError("Unsupported document type.")
        document: ParsedDocument = parser.parse(data, filename)
        chunks = await self._chunker.chunk(document, source)
        warnings: list[str] = []
        if not chunks:
            warnings.append("No text could be extracted from this document.")
        vectors = await self._embeddings.embed_documents([embedding_text(c) for c in chunks])
        await self._vectors.upsert_chunks(chunks, vectors)
        await self._registry.upsert(source)

        images = document.image_blocks[:MAX_IMAGES_PER_DOCUMENT]
        context = " ".join(b.text for b in document.text_blocks[:3])[:400]
        image_chunks = 0
        for number, block in enumerate(images, start=1):
            assert block.image_bytes is not None and block.image_mime is not None
            child = source.model_copy(
                update={
                    "source_id": f"{source.source_id}_img{number}",
                    "title": f"{source.title} — figure {number}",
                    "kind": SourceKind.IMAGE,
                    "mime_type": block.image_mime,
                    "parent_source_id": source.source_id,
                    "checksum_sha256": hashlib.sha256(block.image_bytes).hexdigest(),
                    "blob_path": self._blobs.path_for(
                        source.tenant_id,
                        source.source_id,
                        source.version,
                        f"figure{number}.{block.image_mime.split('/')[-1]}",
                    ),
                }
            )
            await self._blobs.save(child.blob_path, block.image_bytes)
            image_chunks += await self._index_image(
                child, block.image_bytes, context=f"{source.title}. {context}", page=block.page
            )
        return IngestResult(
            source_id=source.source_id,
            version=source.version,
            status="indexed",
            chunk_count=len(chunks) + image_chunks,
            warnings=warnings,
        )

    # --- images ----------------------------------------------------------
    async def _index_image(
        self, source: SourceMetadata, data: bytes, *, context: str, page: int | None = None
    ) -> int:
        ocr_text = await self._ocr.extract(data)
        caption = await self._caption(source, data, context, ocr_text)
        where = f"p. {page} · " if page else ""
        records: list[ChunkRecord] = []
        for ordinal, (modality, text) in enumerate(
            [(Modality.IMAGE_CAPTION, caption), (Modality.IMAGE_OCR, ocr_text)]
        ):
            if not text.strip():
                continue
            label = "image description" if modality == Modality.IMAGE_CAPTION else "text in image"
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id(source.source_id, source.version, ordinal, modality),
                    source_id=source.source_id,
                    source_version=source.version,
                    tenant_id=source.tenant_id,
                    ordinal=ordinal,
                    modality=modality,
                    text=text.strip(),
                    heading_path=[source.title],
                    location=EvidenceLocation(page=page, label=f"{where}{label}"),
                    token_count=count_tokens(text),
                    classification=source.classification,
                    embedding_model=self._embeddings.model_id,
                )
            )
        vectors = await self._embeddings.embed_documents([embedding_text(r) for r in records])
        await self._vectors.upsert_chunks(
            records, vectors, extra={"image_source_id": source.source_id}
        )
        await self._registry.upsert(source)
        return len(records)

    async def _caption(self, source: SourceMetadata, data: bytes, context: str, ocr: str) -> str:
        if self._llm.supports_vision():
            try:
                return await self._llm.describe_image(
                    ImageInput(data=data, mime_type=source.mime_type, source_id=source.source_id),
                    self._prompts.render("image_caption", context=context[:400]),
                )
            except AppError as exc:
                logger.warning(
                    "caption_failed", extra={"source_id": source.source_id, "reason": exc.code}
                )
        # Derived caption (no vision): title, document context and OCR labels.
        labels = ", ".join(line for line in ocr.splitlines()[:15] if line.strip())
        return f"Image: {source.title}. {context[:300]} Labels: {labels}".strip()

    # --- structured ------------------------------------------------------
    async def _ingest_structured(
        self, source: SourceMetadata, filename: str, data: bytes, options: IngestOptions
    ) -> IngestResult:
        if self._loader is None:
            raise ValidationFailedError("Structured data loading is not configured.")
        dataset = options.dataset_name or _dataset_name(filename)
        result = await self._loader.load(source.tenant_id, dataset, filename, data)
        await self._registry.upsert(source)
        await self._registry.register_dataset(
            source.tenant_id,
            dataset,
            source.source_id,
            source.classification,
            sorted(source.allowed_groups),
            result.columns,
        )
        return IngestResult(
            source_id=source.source_id,
            version=source.version,
            status="loaded",
            row_count=result.row_count,
            warnings=result.warnings,
        )

    # --- cleanup ---------------------------------------------------------
    async def _remove_previous(self, source_id: str) -> None:
        await self._vectors.delete_source(source_id)
        for child_number in range(1, MAX_IMAGES_PER_DOCUMENT + 1):
            await self._vectors.delete_source(f"{source_id}_img{child_number}")

    async def _cleanup(self, source_id: str) -> None:
        try:
            await self._remove_previous(source_id)
            await self._registry.delete(source_id)
        except Exception as exc:  # best effort
            logger.warning("ingest_cleanup_failed", extra={"exc_type": type(exc).__name__})


def _source_id(tenant_id: str, title: str) -> str:
    return "src_" + hashlib.sha256(f"{tenant_id}|{title}".encode()).hexdigest()[:16]


def _dataset_name(filename: str) -> str:
    stem = PurePath(filename).stem.lower()
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in stem).strip("_")
    return (cleaned or "dataset")[:63] if cleaned[:1].isalpha() else f"d_{cleaned}"[:63]
