"""Interface contracts between components (§6.8).

Owners:
  Claude: LLMProvider
  Codex:  EmbeddingProvider, PrincipalRepository, PolicyService, SensitivityDetector,
          RetrievalTool, SQLTool, SourceService, UploadStore, IngestionService

Any access-checked method that returns None means "not found OR not permitted";
callers must treat both identically (invariant I12).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from app.schemas.evidence import Evidence, RetrievalResult
from app.schemas.ingestion import IngestOptions, IngestResult, SourcePreview
from app.schemas.llm import ChatMessage, ImageInput, ModelRole
from app.schemas.security import (
    AccessScope,
    DemoUser,
    PolicyDecision,
    SensitiveSpan,
    SensitivityFindings,
    TaskAccessRequest,
    UserContext,
)
from app.schemas.tools import RetrievalRequest, SQLRequest, SQLResult, ToolFailure

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str: ...

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        max_tokens: int | None = None,
    ) -> T:
        """Return a validated instance of `schema`. Raises ValidationFailedError."""
        ...

    async def describe_image(self, image: ImageInput, instruction: str) -> str:
        """Raises CapabilityLimitError when the configured model has no vision support."""
        ...

    def supports_vision(self) -> bool: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class PrincipalRepository(Protocol):
    async def get_user_context(self, tenant_id: str, employee_id: str) -> UserContext | None: ...

    async def list_demo_users(self, tenant_id: str) -> list[DemoUser]: ...

    async def resolve_people(self, user: UserContext, refs: Sequence[str]) -> dict[str, list[str]]:
        """Map each reference (as written) to matching employee IDs in the user's tenant.

        An empty list means unresolved. Never raises for unknown names.
        """
        ...


@runtime_checkable
class PolicyService(Protocol):
    async def evaluate_task(
        self, user: UserContext, request: TaskAccessRequest
    ) -> PolicyDecision: ...

    async def can_access_source(self, user: UserContext, source_id: str) -> bool: ...

    async def filter_evidence(
        self, user: UserContext, scope: AccessScope, evidence: Sequence[Evidence]
    ) -> list[Evidence]: ...


@runtime_checkable
class SensitivityDetector(Protocol):
    def detect(self, text: str) -> SensitivityFindings:
        """Classify user text into data categories and normalized field names."""
        ...

    def scan_output(self, text: str) -> list[SensitiveSpan]:
        """Find PII-like or confidential spans in generated output."""
        ...


@runtime_checkable
class RetrievalTool(Protocol):
    async def retrieve(
        self, user: UserContext, scope: AccessScope, request: RetrievalRequest
    ) -> RetrievalResult: ...


@runtime_checkable
class SQLTool(Protocol):
    async def query(
        self, user: UserContext, scope: AccessScope, request: SQLRequest
    ) -> SQLResult | ToolFailure: ...


@runtime_checkable
class SourceService(Protocol):
    async def get_preview(
        self, user: UserContext, source_id: str, evidence_location: str | None = None
    ) -> SourcePreview | None: ...

    async def get_image(self, user: UserContext, source_id: str) -> tuple[bytes, str] | None:
        """Return (bytes, mime_type) or None."""
        ...


@runtime_checkable
class UploadStore(Protocol):
    async def save(self, user: UserContext, data: bytes, mime_type: str) -> str:
        """Store a user-uploaded image; return its upload_id."""
        ...

    async def load(self, user: UserContext, upload_id: str) -> ImageInput | None: ...


@runtime_checkable
class IngestionService(Protocol):
    async def ingest(
        self, user: UserContext, filename: str, data: bytes, options: IngestOptions
    ) -> IngestResult: ...
