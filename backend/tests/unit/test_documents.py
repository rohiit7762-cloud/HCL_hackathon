"""Parsers, semantic chunking, and ingestion → Chroma → retrieval with access filters."""

from __future__ import annotations

import io

import pymupdf
import pytest
from docx import Document

from app.agents.retrieval import RetrievalAgent
from app.core.errors import PermissionDeniedError
from app.ingestion.ocr import NullOcrEngine
from app.ingestion.parsers import BlockType, parser_for, sniff_mime
from app.ingestion.semantic_chunker import ChunkerConfig, SemanticChunker, split_sentences
from app.prompts import PromptLibrary
from app.repositories.blob_store import BlobStore, FileUploadStore
from app.repositories.vector_repository import ChromaVectorStore, EmbeddingMismatchError
from app.schemas.common import Classification, Modality, SourceNeed
from app.schemas.ingestion import IngestOptions
from app.schemas.llm import ModelRole
from app.schemas.security import AccessScope
from app.schemas.tools import RetrievalRequest
from app.security.policy import DeterministicPolicyService
from app.services.ingestion import IngestionServiceImpl
from tests.fakes.data_layer import (
    ADMIN,
    ASHA,
    JORDAN,
    HashEmbeddingProvider,
    InMemoryRegistry,
    source,
)
from tests.fakes.llm import FakeLLMProvider

MARKDOWN = b"""# Payments Runbook

## Escalation

Page the payments on-call engineer first. Escalate to the platform lead after 15 minutes.

- Step one: acknowledge the page
- Step two: open an incident channel

## Database Failover

| Setting | Value |
|---|---|
| RTO | 30 minutes |
| RPO | 5 minutes |

Promote the standby replica of the ledger database.
"""


def test_sniffing_checks_magic_bytes() -> None:
    assert sniff_mime("a.md", MARKDOWN) == "text/markdown"
    assert sniff_mime("a.pdf", b"not a pdf") is None
    assert sniff_mime("evil.md", b"%PDF-1.7") is None
    assert sniff_mime("a.png", b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_mime("a.exe", b"MZ\x00\x00") is None


def test_markdown_parser_keeps_structure() -> None:
    doc = parser_for("text/markdown").parse(MARKDOWN, "runbook.md")  # type: ignore[union-attr]
    kinds = [b.block_type for b in doc.blocks]
    assert BlockType.TABLE in kinds and kinds.count(BlockType.LIST_ITEM) == 2
    table = next(b for b in doc.blocks if b.block_type == BlockType.TABLE)
    assert table.heading_path == ["Payments Runbook", "Database Failover"]
    assert table.line_start is not None and "RTO" in table.text


def test_docx_parser_headings_and_tables() -> None:
    document = Document()
    document.add_heading("IT Setup", level=1)
    document.add_paragraph("Install the VPN client before day one.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Tool", "Owner"
    table.cell(1, 0).text, table.cell(1, 1).text = "VPN", "IT Helpdesk"
    buffer = io.BytesIO()
    document.save(buffer)
    data = buffer.getvalue()
    parsed = parser_for(sniff_mime("it.docx", data) or "").parse(data, "it.docx")  # type: ignore[union-attr]
    assert parsed.blocks[0].block_type == BlockType.HEADING
    assert any(b.block_type == BlockType.TABLE and "IT Helpdesk" in b.text for b in parsed.blocks)


def test_pdf_parser_pages_and_headings() -> None:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Postmortem INC-1042", fontsize=20)
    page.insert_text((72, 110), "The ledger database ran out of connections.", fontsize=11)
    page2 = pdf.new_page()
    page2.insert_text((72, 72), "Root cause was a connection pool leak.", fontsize=11)
    data = pdf.tobytes()
    parsed = parser_for("application/pdf").parse(data, "pm.pdf")  # type: ignore[union-attr]
    heading = parsed.blocks[0]
    assert heading.block_type == BlockType.HEADING and heading.text == "Postmortem INC-1042"
    assert any(b.page == 2 and "pool leak" in b.text for b in parsed.blocks)


def test_sentence_split_respects_abbreviations() -> None:
    assert split_sentences("Use e.g. the VPN. Then log in.") == [
        "Use e.g. the VPN.",
        "Then log in.",
    ]


async def test_chunker_splits_on_sections_and_respects_limits() -> None:
    doc = parser_for("text/markdown").parse(MARKDOWN, "runbook.md")  # type: ignore[union-attr]
    chunker = SemanticChunker(HashEmbeddingProvider(), ChunkerConfig(0.72, 5, 60))
    chunks = await chunker.chunk(doc, source("runbook"))
    assert len(chunks) >= 2
    assert all(c.token_count <= 60 for c in chunks)
    assert {tuple(c.heading_path[-1:]) for c in chunks} >= {("Escalation",), ("Database Failover",)}
    assert all(c.source_id == "runbook" and c.tenant_id == "lumen" for c in chunks)
    again = await chunker.chunk(doc, source("runbook"))
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]  # deterministic


async def test_chunker_topic_shift_creates_boundary() -> None:
    text = (
        b"Kafka brokers replicate partitions. Kafka consumers read partitions. "
        b"Kafka producers write partitions. Kafka topics hold partitions. "
        b"Payroll runs monthly salaries. Payroll deducts taxes monthly. "
        b"Payroll statements reach employees monthly."
    )
    doc = parser_for("text/plain").parse(text, "mixed.txt")  # type: ignore[union-attr]
    chunker = SemanticChunker(HashEmbeddingProvider(), ChunkerConfig(0.2, 5, 400))
    chunks = await chunker.chunk(doc, source("mixed"))
    assert len(chunks) == 2
    assert "Kafka" in chunks[0].text and "Payroll" in chunks[1].text


@pytest.fixture
def pipeline(tmp_path):  # type: ignore[no-untyped-def]
    registry = InMemoryRegistry(sources=[])
    vectors = ChromaVectorStore.ephemeral()
    embeddings = HashEmbeddingProvider()
    vectors.ensure_compatible(embeddings.model_id, embeddings.dimension)
    blobs = BlobStore(str(tmp_path))
    llm = FakeLLMProvider(vision=False)
    service = IngestionServiceImpl(
        registry,
        vectors,
        embeddings,
        SemanticChunker(embeddings, ChunkerConfig(0.5, 10, 120)),
        blobs,
        None,
        llm,
        NullOcrEngine(),
        PromptLibrary(),
        max_bytes=1_000_000,
    )
    retrieval = RetrievalAgent(
        embeddings,
        vectors,
        registry,
        blobs,
        FileUploadStore(str(tmp_path)),
        llm,
        NullOcrEngine(),
        PromptLibrary(),
    )
    policy = DeterministicPolicyService(registry, cache_ttl_seconds=0)
    return service, retrieval, policy, registry, vectors


async def _scope(policy: DeterministicPolicyService, who) -> AccessScope:  # type: ignore[no-untyped-def]
    from app.schemas.common import DataCategory
    from app.schemas.security import TaskAccessRequest

    decision = await policy.evaluate_task(
        who,
        TaskAccessRequest(
            task_id="t1",
            data_categories=frozenset({DataCategory.GENERAL_KNOWLEDGE}),
            source_needs=frozenset({SourceNeed.DOCUMENTS}),
        ),
    )
    assert decision.scope is not None
    return decision.scope


async def test_i3_ingest_then_retrieve_respects_groups(pipeline) -> None:  # type: ignore[no-untyped-def]
    service, retrieval, policy, _, _ = pipeline
    options = IngestOptions(classification=Classification.INTERNAL, allowed_groups=["eng-platform"])
    result = await service.ingest(
        ADMIN.model_copy(update={"tenant_id": "lumen"}), "payments_runbook.md", MARKDOWN, options
    )
    assert result.status == "indexed" and result.chunk_count >= 2

    request = RetrievalRequest(task_id="t1", query="ledger database failover RTO", attempt=1)
    asha = await retrieval.retrieve(ASHA, await _scope(policy, ASHA), request)
    assert asha.evidence and "30 minutes" in " ".join(e.excerpt for e in asha.evidence)
    assert asha.evidence[0].location.label

    jordan = await retrieval.retrieve(JORDAN, await _scope(policy, JORDAN), request)
    assert jordan.evidence == []  # not in eng-platform: never retrieved


async def test_reingest_same_content_is_skipped_and_new_version_replaces(pipeline) -> None:  # type: ignore[no-untyped-def]
    service, _, _, _, vectors = pipeline
    options = IngestOptions(classification=Classification.INTERNAL)
    first = await service.ingest(ADMIN, "runbook.md", MARKDOWN, options)
    again = await service.ingest(ADMIN, "runbook.md", MARKDOWN, options)
    assert again.status == "skipped_duplicate"
    updated = await service.ingest(
        ADMIN, "runbook.md", MARKDOWN + b"\nNew line about DNS.\n", options
    )
    assert updated.version == 2 and updated.source_id == first.source_id
    chunk = await vectors.get_chunk(first.source_id, None)
    assert chunk is not None and chunk.source_version == 2


async def test_ingest_requires_role(pipeline) -> None:  # type: ignore[no-untyped-def]
    service, *_ = pipeline
    with pytest.raises(PermissionDeniedError):
        await service.ingest(
            ASHA, "x.md", MARKDOWN, IngestOptions(classification=Classification.PUBLIC)
        )


async def test_i4_image_derived_chunks_inherit_acl(pipeline) -> None:  # type: ignore[no-untyped-def]
    service, retrieval, policy, _, _ = pipeline
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 120), "white").save(buffer, format="PNG")
    options = IngestOptions(
        title="Ledger diagram",
        classification=Classification.INTERNAL,
        allowed_groups=["eng-platform"],
    )
    result = await service.ingest(ADMIN, "ledger_diagram.png", buffer.getvalue(), options)
    assert result.status == "indexed"
    request = RetrievalRequest(
        task_id="t1",
        query="Ledger diagram image",
        attempt=1,
        modalities=frozenset({Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}),
        need_visual_interpretation=True,
    )
    asha = await retrieval.retrieve(ASHA, await _scope(policy, ASHA), request)
    assert asha.evidence and asha.evidence[0].image_ref
    assert any("Visual interpretation unavailable" in w for w in asha.warnings)
    jordan = await retrieval.retrieve(JORDAN, await _scope(policy, JORDAN), request)
    assert jordan.evidence == []


async def test_unauthorized_image_never_reaches_vision_model(pipeline) -> None:  # type: ignore[no-untyped-def]
    service, retrieval, policy, _, _ = pipeline
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 120), "white").save(buffer, format="PNG")
    await service.ingest(
        ADMIN,
        "secret.png",
        buffer.getvalue(),
        IngestOptions(classification=Classification.INTERNAL, allowed_groups=["finance"]),
    )
    vision_llm = FakeLLMProvider(vision=True).default(ModelRole.VISION, "a diagram")
    retrieval._llm = vision_llm
    request = RetrievalRequest(
        task_id="t1", query="secret image", attempt=1, need_visual_interpretation=True
    )
    await retrieval.retrieve(ASHA, await _scope(policy, ASHA), request)
    assert vision_llm.calls_for(ModelRole.VISION) == []


def test_embedding_mismatch_refuses_to_start() -> None:
    store = ChromaVectorStore.ephemeral()
    store.ensure_compatible("model-a", 256)
    with pytest.raises(EmbeddingMismatchError):
        store.ensure_compatible("model-b", 384)
