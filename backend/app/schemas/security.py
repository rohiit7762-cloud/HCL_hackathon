"""Identity, policy and sensitivity contracts (§6.2)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Classification, DataCategory, SourceNeed


class UserContext(BaseModel):
    """Trusted identity. Built only by the IdentityResolver from server-side data (I1)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    employee_id: str
    display_name: str
    title: str = ""
    roles: frozenset[str]
    groups: frozenset[str]
    department: str
    clearance: Classification
    direct_report_ids: frozenset[str] = frozenset()
    is_synthetic: bool = False

    def has_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)


class DemoUser(BaseModel):
    """A synthetic identity offered by the demo identity selector."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    employee_id: str
    display_name: str
    title: str
    access_summary: str  # one line: what this persona can see


class DenialReason(StrEnum):
    CROSS_TENANT = "cross_tenant"
    OTHER_EMPLOYEE_PERSONAL_DATA = "other_employee_personal_data"
    CONFIDENTIAL_BUSINESS = "confidential_business"
    INSUFFICIENT_CLEARANCE = "insufficient_clearance"
    AGGREGATION_TOO_SMALL = "aggregation_too_small"
    UNSUPPORTED_OPERATION = "unsupported_operation"


class TaskAccessRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    data_categories: frozenset[DataCategory]
    subject_employee_ids: frozenset[str] = frozenset()  # people the task is ABOUT
    unresolved_subject_refs: tuple[str, ...] = ()  # names that could not be resolved
    requested_fields: frozenset[str] = frozenset()  # normalized, e.g. "salary"
    source_needs: frozenset[SourceNeed]


class AccessScope(BaseModel):
    """What a single task may touch. Immutable; retries must reuse it unchanged (I6)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    authorized_source_ids: frozenset[str]
    allowed_views: frozenset[str] = frozenset()
    allowed_columns: Mapping[str, frozenset[str]] = Field(default_factory=dict)
    max_classification: Classification
    scope_hash: str

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        authorized_source_ids: Iterable[str],
        max_classification: Classification,
        allowed_views: Iterable[str] = (),
        allowed_columns: Mapping[str, Iterable[str]] | None = None,
    ) -> AccessScope:
        """Build a scope with a deterministic `scope_hash`. Always use this constructor."""
        sources = frozenset(authorized_source_ids)
        views = frozenset(allowed_views)
        columns = {view: frozenset(cols) for view, cols in (allowed_columns or {}).items()}
        return cls(
            tenant_id=tenant_id,
            authorized_source_ids=sources,
            allowed_views=views,
            allowed_columns=columns,
            max_classification=max_classification,
            scope_hash=compute_scope_hash(tenant_id, sources, views, columns, max_classification),
        )

    def hash_is_consistent(self) -> bool:
        return self.scope_hash == compute_scope_hash(
            self.tenant_id,
            self.authorized_source_ids,
            self.allowed_views,
            self.allowed_columns,
            self.max_classification,
        )


def compute_scope_hash(
    tenant_id: str,
    sources: frozenset[str],
    views: frozenset[str],
    columns: Mapping[str, frozenset[str]],
    max_classification: Classification,
) -> str:
    canonical = json.dumps(
        {
            "tenant": tenant_id,
            "sources": sorted(sources),
            "views": sorted(views),
            "columns": {view: sorted(cols) for view, cols in sorted(columns.items())},
            "max_classification": max_classification.value,
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    allowed: bool
    reasons: tuple[DenialReason, ...] = ()
    scope: AccessScope | None = None  # None when not allowed
    policy_version: str


class SensitivityFindings(BaseModel):
    """Deterministic detection result for a piece of user text."""

    model_config = ConfigDict(frozen=True)

    categories: frozenset[DataCategory] = frozenset()
    fields: frozenset[str] = frozenset()
    # (start, end) character spans in the scanned text that triggered each category.
    spans: tuple[tuple[int, int, DataCategory], ...] = ()


class SensitiveSpan(BaseModel):
    """A PII-like or confidential span found in generated output."""

    model_config = ConfigDict(frozen=True)

    start: int
    end: int
    kind: str  # e.g. "email", "phone", "national_id", "compensation", "employee_id"
    text: str
    category: DataCategory
