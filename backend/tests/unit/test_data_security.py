from datetime import UTC, datetime

import pytest

from app.ingestion.parsers.registry import ParserRegistry
from app.ingestion.semantic_chunker import SemanticChunker
from app.schemas.common import Classification, DataCategory, SourceKind
from app.schemas.ingestion import SourceMetadata
from app.schemas.interfaces import EmbeddingProvider
from app.schemas.security import TaskAccessRequest, UserContext
from app.security.policy import PolicyService


class FixedEmbeddingProvider(EmbeddingProvider):
    model_id = "fake-v1"
    dimension = 2

    async def embed_documents(self, texts):
        return [[0.0, 1.0] if "unrelated" in text.lower() else [1.0, 0.0] for text in texts]

    async def embed_query(self, text):
        return [1.0, 0.0]


def source(**updates):
    values = dict(
        source_id="s1",
        tenant_id="lumen",
        title="runbook",
        kind=SourceKind.DOCUMENT,
        mime_type="text/markdown",
        classification=Classification.INTERNAL,
        checksum_sha256="abc",
        version=1,
        blob_path="lumen/s1/1",
        ingested_by="a1",
        ingested_at=datetime.now(UTC),
    )
    values.update(updates)
    return SourceMetadata(**values)


def test_i3_parser_rejects_unsupported_and_oversized_content():
    parser = ParserRegistry(max_bytes=10)
    with pytest.raises(ValueError):
        parser.parse(b"x" * 11, "large.pdf")
    with pytest.raises(ValueError):
        parser.parse(b"x", "payload.exe")


@pytest.mark.asyncio
async def test_chunker_is_deterministic_and_carries_source_metadata():
    parser = ParserRegistry(max_bytes=10000)
    document = parser.parse(
        b"# Payments\n\nPayments fail over to the ledger.\n\nOther unrelated material.", "guide.md"
    )
    chunker = SemanticChunker(FixedEmbeddingProvider(), threshold=0.72, min_tokens=1, max_tokens=20)
    first = await chunker.chunk(document, source())
    second = await chunker.chunk(document, source())
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert all(c.tenant_id == "lumen" and c.source_id == "s1" for c in first)
    assert len(first) >= 2


@pytest.mark.asyncio
async def test_i1_i2_unresolved_personal_subject_is_denied():
    policy = PolicyService()
    user = UserContext(
        tenant_id="lumen",
        employee_id="a1",
        display_name="Asha",
        roles=frozenset({"employee"}),
        groups=frozenset({"all-staff"}),
        department="eng",
        clearance=Classification.INTERNAL,
    )
    request = TaskAccessRequest(
        task_id="t1",
        data_categories=frozenset({DataCategory.EMPLOYEE_PERSONAL}),
        unresolved_subject_refs=("Jordan",),
        source_needs=frozenset(),
    )
    decision = await policy.evaluate_task(user, request)
    assert not decision.allowed
