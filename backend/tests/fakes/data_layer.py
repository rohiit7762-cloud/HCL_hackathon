"""In-memory test doubles for the data layer.

The PolicyService and SensitivityDetector used with these fakes are the REAL
implementations, so access rules are exercised exactly as in production.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.repositories.base import AuditEntry
from app.schemas.common import Classification, SourceKind
from app.schemas.evidence import Evidence, EvidenceKind, EvidenceLocation, RetrievalResult
from app.schemas.ingestion import IngestOptions, IngestResult, SourceMetadata, SourcePreview
from app.schemas.interfaces import DataLayer
from app.schemas.llm import ImageInput
from app.schemas.security import AccessScope, DemoUser, UserContext
from app.schemas.tools import (
    RetrievalRequest,
    SQLRequest,
    SQLResult,
    StructuredProvenance,
    StructuredQuerySpec,
    ToolFailure,
)
from app.security.policy import DeterministicPolicyService
from app.security.sensitivity import RegexSensitivityDetector

TENANT = "lumen"


def user(
    employee_id: str,
    *,
    roles: Sequence[str] = ("employee",),
    groups: Sequence[str] = ("all-staff",),
    clearance: Classification = Classification.INTERNAL,
    reports: Sequence[str] = (),
    tenant: str = TENANT,
    name: str | None = None,
) -> UserContext:
    return UserContext(
        tenant_id=tenant,
        employee_id=employee_id,
        display_name=name or employee_id,
        roles=frozenset(roles),
        groups=frozenset(groups),
        department="Engineering",
        clearance=clearance,
        direct_report_ids=frozenset(reports),
    )


ASHA = user("E100", name="Asha Rao", groups=("eng-platform", "all-staff"))
JORDAN = user("E101", name="Jordan Lee")
SAM = user(
    "E102",
    name="Sam Patel",
    roles=("employee", "manager"),
    groups=("eng-platform",),
    clearance=Classification.CONFIDENTIAL,
    reports=("E100", "E101"),
)
MORGAN = user(
    "E103",
    name="Morgan Diaz",
    roles=("employee", "hr"),
    groups=("hr-team", "all-staff"),
    clearance=Classification.RESTRICTED,
)
RILEY = user("E104", name="Riley Chen", groups=("finance",), clearance=Classification.CONFIDENTIAL)
ADMIN = user(
    "E900", name="Admin Bot", roles=("ingestor", "admin"), clearance=Classification.RESTRICTED
)
ORBIT_USER = user("O100", name="Olu Orbit", tenant="orbit")

PEOPLE = [ASHA, JORDAN, SAM, MORGAN, RILEY, ADMIN, ORBIT_USER]


class InMemoryPrincipals:
    def __init__(self, people: Sequence[UserContext] = PEOPLE) -> None:
        self._people = {(p.tenant_id, p.employee_id): p for p in people}

    async def get_user_context(self, tenant_id: str, employee_id: str) -> UserContext | None:
        return self._people.get((tenant_id, employee_id))

    async def list_demo_users(self, tenant_id: str) -> list[DemoUser]:
        return [
            DemoUser(
                tenant_id=p.tenant_id,
                employee_id=p.employee_id,
                display_name=p.display_name,
                title=p.title,
                access_summary="test persona",
            )
            for p in self._people.values()
            if p.tenant_id == tenant_id
        ]

    async def resolve_people(self, user: UserContext, refs: Sequence[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for ref in refs:
            name = re.sub(r"'s$", "", ref.strip()).lower()
            out[ref] = sorted(
                p.employee_id
                for p in self._people.values()
                if p.tenant_id == user.tenant_id
                and name in {p.display_name.lower(), *p.display_name.lower().split()}
            )
        return out


def source(
    source_id: str,
    *,
    title: str | None = None,
    classification: Classification = Classification.INTERNAL,
    groups: Sequence[str] = (),
    roles: Sequence[str] = (),
    kind: SourceKind = SourceKind.DOCUMENT,
    tenant: str = TENANT,
    personal: bool = False,
    confidential_business: bool = False,
    subjects: Sequence[str] = (),
    parent: str | None = None,
) -> SourceMetadata:
    return SourceMetadata(
        source_id=source_id,
        tenant_id=tenant,
        title=title or source_id,
        kind=kind,
        mime_type="image/png" if kind == SourceKind.IMAGE else "text/markdown",
        classification=classification,
        allowed_groups=frozenset(groups),
        allowed_roles=frozenset(roles),
        contains_personal_data=personal,
        contains_confidential_business=confidential_business,
        subject_employee_ids=frozenset(subjects),
        parent_source_id=parent,
        checksum_sha256=hashlib.sha256(source_id.encode()).hexdigest(),
        version=1,
        blob_path=f"{tenant}/{source_id}/1/file",
        ingested_by="E900",
        ingested_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


SOURCES = [
    source("onboarding", title="Onboarding Guide", classification=Classification.PUBLIC),
    source("runbook", title="Payments Failover Runbook", groups=("eng-platform",)),
    source("postmortem", title="Postmortem INC-1042"),
    source("diagram", title="Payments Architecture Diagram", kind=SourceKind.IMAGE),
    source(
        "falcon",
        title="Project Falcon Memo",
        classification=Classification.CONFIDENTIAL,
        groups=("finance",),
        confidential_business=True,
    ),
    source(
        "comp-bands",
        title="Compensation Bands",
        classification=Classification.RESTRICTED,
        groups=("hr-team",),
        personal=True,
    ),
    source("orbit-doc", title="Orbit Handbook", tenant="orbit"),
]


class InMemoryRegistry:
    def __init__(self, sources: Sequence[SourceMetadata] = SOURCES) -> None:
        self.sources = {s.source_id: s for s in sources}
        self.datasets: dict[tuple[str, str], str] = {}

    async def list_sources(self, tenant_id: str) -> list[SourceMetadata]:
        return [s for s in self.sources.values() if s.tenant_id == tenant_id and not s.revoked]

    async def get(self, source_id: str) -> SourceMetadata | None:
        return self.sources.get(source_id)

    async def find_latest_by_title(self, tenant_id: str, title: str) -> SourceMetadata | None:
        found = [
            s
            for s in self.sources.values()
            if s.tenant_id == tenant_id and s.title == title and not s.parent_source_id
        ]
        return max(found, key=lambda s: s.version) if found else None

    async def find_by_checksum(self, tenant_id: str, checksum: str) -> SourceMetadata | None:
        return next(
            (
                s
                for s in self.sources.values()
                if s.tenant_id == tenant_id and s.checksum_sha256 == checksum
            ),
            None,
        )

    async def upsert(self, source: SourceMetadata) -> None:
        self.sources[source.source_id] = source

    async def delete(self, source_id: str) -> None:
        self.sources = {
            k: v
            for k, v in self.sources.items()
            if k != source_id and v.parent_source_id != source_id
        }

    def revoke(self, source_id: str) -> None:
        self.sources[source_id] = self.sources[source_id].model_copy(update={"revoked": True})

    async def dataset_source_ids(self, tenant_id: str, datasets: Sequence[str]) -> list[str]:
        return [self.datasets[(tenant_id, d)] for d in datasets if (tenant_id, d) in self.datasets]

    async def register_dataset(
        self,
        tenant_id: str,
        dataset_name: str,
        source_id: str,
        classification: Classification,
        allowed_groups: Sequence[str],
        columns: Sequence[str],
    ) -> None:
        self.datasets[(tenant_id, dataset_name)] = source_id


@dataclass
class MemoryAudit:
    entries: list[AuditEntry] = field(default_factory=list)

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class HashEmbeddingProvider:
    """Deterministic bag-of-words hashing embedder (no model download)."""

    model_id = "test-hash-embedding"
    dimension = 256

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if len(word) < 3:
                continue
            digest = hashlib.md5(word.encode()).digest()
            vector[digest[0] % self.dimension] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


def evidence(
    task_id: str,
    source_id: str,
    excerpt: str,
    *,
    score: float = 0.8,
    label: str = "p. 1",
    kind: EvidenceKind = EvidenceKind.TEXT_CHUNK,
    source_kind: SourceKind = SourceKind.DOCUMENT,
    title: str | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=f"{task_id}.raw",
        task_id=task_id,
        kind=kind,
        source_id=source_id,
        source_title=title or source_id,
        source_kind=source_kind,
        classification=Classification.INTERNAL,
        excerpt=excerpt,
        location=EvidenceLocation(label=label),
        score=score,
        retrieved_at=datetime.now(UTC),
    )


RetrievalScript = Callable[[UserContext, AccessScope, RetrievalRequest], RetrievalResult]


@dataclass
class FakeRetrievalTool:
    """Returns scripted evidence, but only from sources inside the given scope."""

    corpus: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )  # source -> [(label, text)]
    calls: list[tuple[str, RetrievalRequest, AccessScope]] = field(default_factory=list)
    script: RetrievalScript | None = None

    async def retrieve(
        self, user: UserContext, scope: AccessScope, request: RetrievalRequest
    ) -> RetrievalResult:
        self.calls.append((user.employee_id, request, scope))
        if self.script:
            return self.script(user, scope, request)
        terms = set(
            re.findall(r"[a-z0-9]{4,}", (request.query + " " + " ".join(request.keywords)).lower())
        )
        found: list[Evidence] = []
        for source_id, passages in self.corpus.items():
            if source_id not in scope.authorized_source_ids:
                continue
            for label, text in passages:
                overlap = len(terms & set(re.findall(r"[a-z0-9]{4,}", text.lower())))
                if overlap:
                    found.append(
                        evidence(
                            request.task_id,
                            source_id,
                            text,
                            label=label,
                            score=min(0.95, 0.4 + 0.1 * overlap),
                        )
                    )
        return RetrievalResult(task_id=request.task_id, evidence=found)

    def calls_for(self, task_id: str) -> list[RetrievalRequest]:
        return [r for _, r, _ in self.calls if r.task_id == task_id]


@dataclass
class FakeSQLTool:
    results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # view -> rows
    calls: list[tuple[str, SQLRequest, AccessScope]] = field(default_factory=list)

    async def query(
        self, user: UserContext, scope: AccessScope, request: SQLRequest
    ) -> SQLResult | ToolFailure:
        self.calls.append((user.employee_id, request, scope))
        view = next((v for v in sorted(scope.allowed_views) if v in self.results), None)
        if view is None:
            return ToolFailure(
                task_id=request.task_id,
                code="no_permitted_view",
                safe_message="There is no structured data you can access for this part.",
            )
        rows = self.results[view]
        return SQLResult(
            task_id=request.task_id,
            spec=StructuredQuerySpec(view=view),
            columns=list(rows[0]) if rows else [],
            rows=rows,
            row_count=len(rows),
            truncated=False,
            provenance=StructuredProvenance(
                view=view,
                dataset_source_ids=[f"ds-{view}"],
                query_fingerprint="f" * 64,
                executed_at=datetime.now(UTC),
            ),
        )


class FakeSourceService:
    def __init__(self, policy: DeterministicPolicyService, registry: InMemoryRegistry) -> None:
        self._policy, self._registry = policy, registry

    async def get_preview(
        self, user: UserContext, source_id: str, evidence_location: str | None = None
    ) -> SourcePreview | None:
        if not await self._policy.can_access_source(user, source_id):
            return None
        s = self._registry.sources[source_id]
        return SourcePreview(
            source_id=s.source_id,
            title=s.title,
            kind=s.kind,
            classification=s.classification,
            excerpt="preview text",
        )

    async def get_image(self, user: UserContext, source_id: str) -> tuple[bytes, str] | None:
        if not await self._policy.can_access_source(user, source_id):
            return None
        return (b"\x89PNG\r\n\x1a\nfake", "image/png")


class FakeUploadStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], ImageInput] = {}

    async def save(self, user: UserContext, data: bytes, mime_type: str) -> str:
        upload_id = f"up_{len(self.items):032x}"
        self.items[(user.employee_id, upload_id)] = ImageInput(data=data, mime_type=mime_type)  # type: ignore[arg-type]
        return upload_id

    async def load(self, user: UserContext, upload_id: str) -> ImageInput | None:
        return self.items.get((user.employee_id, upload_id))


class FakeIngestion:
    async def ingest(
        self, user: UserContext, filename: str, data: bytes, options: IngestOptions
    ) -> IngestResult:
        return IngestResult(source_id="src_test", version=1, status="indexed", chunk_count=1)


@dataclass
class DataLayerHarness:
    data: DataLayer
    registry: InMemoryRegistry
    retrieval: FakeRetrievalTool
    sql: FakeSQLTool
    audit: MemoryAudit


def build_test_data_layer(
    corpus: dict[str, list[tuple[str, str]]] | None = None,
    sql_results: dict[str, list[dict[str, Any]]] | None = None,
) -> DataLayerHarness:
    registry = InMemoryRegistry()
    audit = MemoryAudit()
    policy = DeterministicPolicyService(registry, audit, cache_ttl_seconds=0)
    retrieval = FakeRetrievalTool(corpus=corpus or {})
    sql = FakeSQLTool(results=sql_results or {})
    data = DataLayer(
        principals=InMemoryPrincipals(),
        policy=policy,
        sensitivity=RegexSensitivityDetector(),
        retrieval=retrieval,
        sql=sql,
        sources=FakeSourceService(policy, registry),
        uploads=FakeUploadStore(),
        ingestion=FakeIngestion(),
    )
    return DataLayerHarness(data, registry, retrieval, sql, audit)
