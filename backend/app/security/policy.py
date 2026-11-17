"""Deterministic, fail-closed task and source authorization (I1-I4, I12)."""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.common import Classification, DataCategory, SourceNeed
from app.schemas.evidence import Evidence
from app.schemas.ingestion import SourceMetadata
from app.schemas.security import (
    AccessScope,
    DenialReason,
    PolicyDecision,
    TaskAccessRequest,
    UserContext,
)

POLICY_VERSION = "2026-11-17.1"
MANAGER_VISIBLE_FIELDS = frozenset({"performance_rating"})


class PolicyService:
    """Pure authorization rules; current source records are passed to this service."""

    def __init__(
        self, sources: Sequence[SourceMetadata] = (), min_aggregation_group: int = 5
    ) -> None:
        self._sources = {s.source_id: s for s in sources}
        self.min_aggregation_group = min_aggregation_group
        self._access_cache: dict[tuple[UserContext, str], bool] = {}

    def replace_sources(self, sources: Sequence[SourceMetadata]) -> None:
        self._sources = {s.source_id: s for s in sources}
        self._access_cache.clear()

    def register_source(self, source: SourceMetadata) -> None:
        self._sources[source.source_id] = source
        self._access_cache.clear()

    def _cached_source_access(self, user: UserContext, source_id: str) -> bool:
        key = (user, source_id)
        if key in self._access_cache:
            return self._access_cache[key]
        source = self._sources.get(source_id)
        if source is None or source.revoked or source.tenant_id != user.tenant_id:
            allowed = False
        elif not user.clearance.allows(source.classification):
            allowed = False
        elif (source.allowed_groups or source.allowed_roles) and not (
            source.allowed_groups.intersection(user.groups)
            or source.allowed_roles.intersection(user.roles)
        ):
            allowed = False
        elif source.contains_personal_data and (
            not source.subject_employee_ids
            or not self._can_view_personal(user, source.subject_employee_ids, frozenset())
        ):
            allowed = False
        else:
            allowed = True
        if len(self._access_cache) >= 4096:
            self._access_cache.clear()
        self._access_cache[key] = allowed
        return allowed

    def _can_view_personal(
        self, user: UserContext, subjects: frozenset[str], fields: frozenset[str]
    ) -> bool:
        if not subjects:
            return False
        normalized = {subject.rsplit(":", 1)[-1] for subject in subjects}
        if normalized == {user.employee_id}:
            return True
        if "hr" in user.roles:
            return True
        return bool(
            normalized.issubset(user.direct_report_ids)
            and "manager" in user.roles
            and fields
            and fields.issubset(MANAGER_VISIBLE_FIELDS)
        )

    async def evaluate_task(self, user: UserContext, request: TaskAccessRequest) -> PolicyDecision:
        reasons: set[DenialReason] = set()
        categories = request.data_categories
        if request.unresolved_subject_refs and DataCategory.EMPLOYEE_PERSONAL in categories:
            reasons.add(DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA)
        subjects = request.subject_employee_ids
        if any(not s.startswith(user.tenant_id + ":") for s in subjects):
            reasons.add(DenialReason.CROSS_TENANT)
        if DataCategory.EMPLOYEE_PERSONAL in categories and not self._can_view_personal(
            user, subjects, request.requested_fields
        ):
            reasons.add(DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA)
        if DataCategory.CONFIDENTIAL_BUSINESS in categories:
            matching = [
                s
                for s in self._sources.values()
                if s.tenant_id == user.tenant_id
                and s.contains_confidential_business
                and not s.revoked
            ]
            clearance = user.clearance.allows(Classification.CONFIDENTIAL)
            governed = any(
                not s.allowed_groups or bool(s.allowed_groups.intersection(user.groups))
                for s in matching
            )
            if not (clearance and governed):
                reasons.add(DenialReason.CONFIDENTIAL_BUSINESS)
        if reasons:
            return PolicyDecision(
                task_id=request.task_id,
                allowed=False,
                reasons=tuple(sorted(reasons)),
                policy_version=POLICY_VERSION,
            )
        candidates = [
            s for s in self._sources.values() if s.tenant_id == user.tenant_id and not s.revoked
        ]
        if DataCategory.CONFIDENTIAL_BUSINESS in categories:
            candidates = [s for s in candidates if s.contains_confidential_business]
        needs = request.source_needs
        kind_map = {
            SourceNeed.DOCUMENTS: {"document"},
            SourceNeed.IMAGES: {"image"},
            SourceNeed.STRUCTURED: {"structured"},
        }
        candidates = [
            s for s in candidates if not needs or any(s.kind.value in kind_map[n] for n in needs)
        ]
        accessible = [s for s in candidates if self._cached_source_access(user, s.source_id)]
        max_class = max(
            (s.classification for s in accessible),
            key=lambda c: c.rank,
            default=Classification.PUBLIC,
        )
        allowed_views = frozenset(s.title for s in accessible if s.kind.value == "structured")
        scope = AccessScope.create(
            tenant_id=user.tenant_id,
            authorized_source_ids=[s.source_id for s in accessible],
            max_classification=max_class,
            allowed_views=allowed_views,
        )
        return PolicyDecision(
            task_id=request.task_id, allowed=True, scope=scope, policy_version=POLICY_VERSION
        )

    async def can_access_source(self, user: UserContext, source_id: str) -> bool:
        return self._cached_source_access(user, source_id)

    async def filter_evidence(
        self, user: UserContext, scope: AccessScope, evidence: Sequence[Evidence]
    ) -> list[Evidence]:
        if not scope.hash_is_consistent() or scope.tenant_id != user.tenant_id:
            return []
        result = []
        for item in evidence:
            if item.source_id not in scope.authorized_source_ids:
                continue
            if item.derived_from and item.derived_from not in scope.authorized_source_ids:
                continue
            if await self.can_access_source(user, item.source_id):
                result.append(item)
        return result
