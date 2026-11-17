"""Builds every data-layer service with shared connections (Neon, Chroma, embeddings)."""

from __future__ import annotations

from app.agents.retrieval import RetrievalAgent
from app.agents.sql import SQLAgent
from app.core.config import Settings
from app.ingestion.ocr import RapidOcrEngine
from app.ingestion.semantic_chunker import ChunkerConfig, SemanticChunker
from app.ingestion.structured_loader import StructuredLoader
from app.prompts import PromptLibrary
from app.providers.embedding_provider import FastEmbedProvider
from app.repositories.blob_store import BlobStore, FileUploadStore
from app.repositories.postgres_repository import Database
from app.repositories.principal_repository import PostgresPrincipalRepository
from app.repositories.source_repository import PostgresAuditSink, PostgresSourceRegistry
from app.repositories.vector_repository import ChromaVectorStore
from app.schemas.interfaces import DataLayer, LLMProvider
from app.security.policy import DeterministicPolicyService
from app.security.sensitivity import RegexSensitivityDetector
from app.services.ingestion import IngestionServiceImpl
from app.services.sources import SourceServiceImpl


async def build_data_layer(settings: Settings, llm: LLMProvider) -> DataLayer:
    db = Database(
        settings.neon_ingest_url.get_secret_value(),
        settings.neon_query_url.get_secret_value(),
    )
    await db.open()

    prompts = PromptLibrary()
    registry = PostgresSourceRegistry(db)
    policy = DeterministicPolicyService(registry, PostgresAuditSink(db))
    embeddings = FastEmbedProvider(settings.embedding_model)
    vectors = ChromaVectorStore.persistent(settings.chroma_path)
    vectors.ensure_compatible(embeddings.model_id, embeddings.dimension)
    blobs = BlobStore(settings.blob_path)
    uploads = FileUploadStore(settings.blob_path)
    ocr = RapidOcrEngine()

    retrieval = RetrievalAgent(embeddings, vectors, registry, blobs, uploads, llm, ocr, prompts)
    sql_agent = SQLAgent(
        llm,
        db,
        registry,
        prompts,
        timeout_ms=settings.sql_timeout_ms,
        max_rows=settings.sql_max_rows,
        min_aggregation_group=settings.min_aggregation_group,
    )
    chunker = SemanticChunker(
        embeddings,
        ChunkerConfig(
            similarity_threshold=settings.chunk_similarity_threshold,
            min_tokens=settings.chunk_min_tokens,
            max_tokens=settings.chunk_max_tokens,
        ),
    )
    ingestion = IngestionServiceImpl(
        registry,
        vectors,
        embeddings,
        chunker,
        blobs,
        StructuredLoader(db),
        llm,
        ocr,
        prompts,
        max_bytes=settings.max_ingest_bytes,
        on_change=policy.invalidate,
    )
    return DataLayer(
        principals=PostgresPrincipalRepository(db),
        policy=policy,
        sensitivity=RegexSensitivityDetector(),
        retrieval=retrieval,
        sql=sql_agent,
        sources=SourceServiceImpl(policy, registry, vectors, blobs),
        uploads=uploads,
        ingestion=ingestion,
        close=db.close,
    )
