"""Deterministic PolicyService (§8.8). The only place access rules live.

Pure rules over trusted data: no LLM. The planner's model output can add
categories (making decisions stricter) but nothing here trusts it to grant access.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from app.core.logging import get_logger
from app.repositories.base import AuditEntry, AuditSink, SourceRegistry
from app.schemas.common import Classification, DataCategory, SourceKind, SourceNeed
from app.schemas.evidence import Evidence
from app.schemas.ingestion import SourceMetadata
from app.schemas.security import (
    AccessScope,
    DenialReason,
    PolicyDecision,
    TaskAccessRequest,
    UserContext,
)
from app.security.view_catalog import (
    DATASET_ROWS,
    EMPLOYEE_DIRECTORY,
    EMPLOYEE_HR,
    INCIDENTS,
    MANAGER_VISIBLE_COLUMNS,
    MANAGER_VISIBLE_FIELDS,
    SERVICES,
    ViewSpec,
)

logger = get_logger(__name__)

POLICY_VERSION = "2026-11-17.1"
HR_ROLE = "hr"
MANAGER_ROLE = "manager"
UPLOAD_PREFIX = "upload"

PERSONAL_FIELDS: frozenset[str] = frozenset(
    {
        "salary",
        "bonus",
        "compensation",
        "performance_rating",
        "rating",
        "performance",
        "personal_phone",
        "home_city",
        "home_address",
        "health",
        "national_id",
        "disciplinary",
    }
)


def upload_source_id(user: UserContext, upload_id: str) -> str:
    """Source ID for evidence derived from a user upload; encodes the owner."""
    return f"{UPLOAD_PREFIX}:{user.tenant_id}:{user.employee_id}:{upload_id}"


class DeterministicPolicyService:
    def __init__(
        self,
        registry: SourceRegistry,
        audit: AuditSink | None = None,
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, list[SourceMetadata]]] = {}

    # --- PolicyService ---------------------------------------------------
    async def evaluate_task(self, user: UserContext, request: TaskAccessRequest) -> PolicyDecision:
        reasons = self._personal_data_reasons(user, request)
        scope: AccessScope | None = None
        if not reasons:
            sources = await self._authorized_sources(user, request)
            if DataCategory.CONFIDENTIAL_BUSINESS in request.data_categories and not (
                user.clearance.allows(Classification.CONFIDENTIAL)
                and any(s.contains_confidential_business for s in sources)
            ):
                reasons = [DenialReason.CONFIDENTIAL_BUSINESS]
            else:
                views = self._allowed_views(user, request)
                scope = AccessScope.create(
                    tenant_id=user.tenant_id,
                    authorized_source_ids=[s.source_id for s in sources],
                    max_classification=user.clearance,
                    allowed_views=views.keys(),
                    allowed_columns=views,
                )
        decision = PolicyDecision(
            task_id=request.task_id,
            allowed=not reasons,
            reasons=tuple(reasons),
            scope=scope,
            policy_version=POLICY_VERSION,
        )
        await self._record(user, request, decision)
        return decision

    async def can_access_source(self, user: UserContext, source_id: str) -> bool:
        source = await self._registry.get(source_id)  # fresh read: catches revocation
        if source is None or not self.source_allowed(user, source):
            return False
        if source.parent_source_id:
            return await self.can_access_source(user, source.parent_source_id)
        return True

    async def filter_evidence(
        self, user: UserContext, scope: AccessScope, evidence: Sequence[Evidence]
    ) -> list[Evidence]:
        kept: list[Evidence] = []
        checked: dict[str, bool] = {}
        for item in evidence:
            if item.source_kind == SourceKind.USER_UPLOAD:
                if item.source_id.startswith(upload_source_id(user, "")):
                    kept.append(item)
                continue
            if item.source_kind == SourceKind.STRUCTURED:
                kept.append(item)  # governed by view scope + database RLS
                continue
            ids = [item.source_id] + ([item.derived_from] if item.derived_from else [])
            ok = True
            for source_id in ids:
                if source_id not in scope.authorized_source_ids:
                    ok = False
                    break
                if source_id not in checked:
                    checked[source_id] = await self.can_access_source(user, source_id)
                ok = ok and checked[source_id]
            if ok and scope.max_classification.allows(item.classification):
                kept.append(item)
        if len(kept) != len(evidence):
            logger.info("evidence_filtered", extra={"dropped": len(evidence) - len(kept)})
        return kept

    # --- rules -------------------------------------------------------------
    @staticmethod
    def source_allowed(user: UserContext, source: SourceMetadata) -> bool:
        """Rule 5: tenant, revocation, clearance, groups/roles, personal data."""
        if source.tenant_id != user.tenant_id or source.revoked:
            return False
        if not user.clearance.allows(source.classification):
            return False
        restricted = bool(source.allowed_groups or source.allowed_roles)
        if restricted and not (
            (source.allowed_groups & user.groups) or (source.allowed_roles & user.roles)
        ):
            return False
        if source.contains_personal_data and not user.has_role(HR_ROLE):
            subjects = set(source.subject_employee_ids)
            if not subjects or subjects - {user.employee_id}:
                return False
        return True

    @staticmethod
    def _personal_data_reasons(user: UserContext, request: TaskAccessRequest) -> list[DenialReason]:
        """Rule 2 (other employees' personal data) and rule 4 (aggregations)."""
        if DataCategory.EMPLOYEE_PERSONAL not in request.data_categories:
            return []
        if user.has_role(HR_ROLE):
            return []
        if request.unresolved_subject_refs:
            return [DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA]
        others = set(request.subject_employee_ids) - {user.employee_id}
        if not others:
            return []  # asking about oneself
        personal_fields = set(request.requested_fields) & PERSONAL_FIELDS
        if (
            user.has_role(MANAGER_ROLE)
            and others <= user.direct_report_ids
            and personal_fields
            and personal_fields <= MANAGER_VISIBLE_FIELDS
        ):
            return []
        return [DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA]

    async def _authorized_sources(
        self, user: UserContext, request: TaskAccessRequest
    ) -> list[SourceMetadata]:
        if not request.source_needs & {SourceNeed.DOCUMENTS, SourceNeed.IMAGES}:
            return []
        return [
            s
            for s in await self._tenant_sources(user.tenant_id)
            if s.kind in {SourceKind.DOCUMENT, SourceKind.IMAGE} and self.source_allowed(user, s)
        ]

    @staticmethod
    def _allowed_views(user: UserContext, request: TaskAccessRequest) -> dict[str, frozenset[str]]:
        if SourceNeed.STRUCTURED not in request.source_needs:
            return {}
        categories = request.data_categories
        chosen: dict[str, frozenset[str]] = {}

        def allow(view: ViewSpec, columns: frozenset[str] | None = None) -> None:
            chosen[view.name] = columns if columns is not None else view.column_names

        allow(EMPLOYEE_DIRECTORY)
        if DataCategory.OPERATIONAL in categories or DataCategory.GENERAL_KNOWLEDGE in categories:
            allow(INCIDENTS)
            allow(SERVICES)
            allow(DATASET_ROWS)
        if DataCategory.EMPLOYEE_PERSONAL in categories:
            others = set(request.subject_employee_ids) - {user.employee_id}
            if user.has_role(HR_ROLE) or not others:
                allow(EMPLOYEE_HR)
            else:  # manager viewing direct reports (rule 2 already passed)
                allow(EMPLOYEE_HR, MANAGER_VISIBLE_COLUMNS)
        return chosen

    # --- helpers -----------------------------------------------------------
    async def _tenant_sources(self, tenant_id: str) -> list[SourceMetadata]:
        cached = self._cache.get(tenant_id)
        now = time.monotonic()
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        sources = await self._registry.list_sources(tenant_id)
        self._cache[tenant_id] = (now, sources)
        return sources

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)

    async def _record(
        self, user: UserContext, request: TaskAccessRequest, decision: PolicyDecision
    ) -> None:
        logger.info(
            "policy_decision",
            extra={
                "task_id": request.task_id,
                "allowed": decision.allowed,
                "reasons": [r.value for r in decision.reasons],
                "categories": sorted(c.value for c in request.data_categories),
            },
        )
        if self._audit is None:
            return
        try:
            await self._audit.record(
                AuditEntry(
                    tenant_id=user.tenant_id,
                    employee_id=user.employee_id,
                    task_id=request.task_id,
                    categories=tuple(sorted(request.data_categories)),
                    allowed=decision.allowed,
                    reasons=decision.reasons,
                    policy_version=decision.policy_version,
                )
            )
        except Exception as exc:  # auditing must never break answering
            logger.warning("audit_failed", extra={"exc_type": type(exc).__name__})
