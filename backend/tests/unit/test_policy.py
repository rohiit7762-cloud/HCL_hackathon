"""PolicyService rules (§8.8). Test names reference invariants where relevant."""

from __future__ import annotations

import pytest

from app.schemas.common import Classification, DataCategory, SourceKind, SourceNeed
from app.schemas.evidence import EvidenceKind
from app.schemas.security import DenialReason, TaskAccessRequest, UserContext
from app.security.policy import DeterministicPolicyService, upload_source_id
from app.security.view_catalog import EMPLOYEE_HR, MANAGER_VISIBLE_COLUMNS
from tests.fakes.data_layer import (
    ADMIN,
    ASHA,
    JORDAN,
    MORGAN,
    ORBIT_USER,
    RILEY,
    SAM,
    InMemoryRegistry,
    MemoryAudit,
    evidence,
)

DOCS = frozenset({SourceNeed.DOCUMENTS})
STRUCTURED = frozenset({SourceNeed.STRUCTURED})


def request(
    categories: set[DataCategory],
    *,
    subjects: set[str] = frozenset(),  # type: ignore[assignment]
    unresolved: tuple[str, ...] = (),
    fields: set[str] = frozenset(),  # type: ignore[assignment]
    needs: frozenset[SourceNeed] = DOCS,
) -> TaskAccessRequest:
    return TaskAccessRequest(
        task_id="t1",
        data_categories=frozenset(categories),
        subject_employee_ids=frozenset(subjects),
        unresolved_subject_refs=unresolved,
        requested_fields=frozenset(fields),
        source_needs=needs,
    )


@pytest.fixture
def registry() -> InMemoryRegistry:
    return InMemoryRegistry()


@pytest.fixture
def policy(registry: InMemoryRegistry) -> DeterministicPolicyService:
    return DeterministicPolicyService(registry, MemoryAudit(), cache_ttl_seconds=0)


P = DataCategory.EMPLOYEE_PERSONAL


@pytest.mark.parametrize(
    ("who", "subjects", "fields", "unresolved", "allowed"),
    [
        (ASHA, {"E100"}, {"salary"}, (), True),  # own data
        (ASHA, {"E101"}, {"salary"}, (), False),  # another employee
        (ASHA, set(), {"salary"}, ("Sam",), False),  # unresolved person
        (SAM, {"E100"}, {"performance_rating"}, (), True),  # manager: rating of report
        (SAM, {"E100"}, {"salary"}, (), False),  # manager: never salary
        (SAM, {"E103"}, {"performance_rating"}, (), False),  # not a direct report
        (MORGAN, {"E100"}, {"salary"}, (), True),  # HR
        (MORGAN, set(), {"salary"}, ("<unspecified>",), True),  # HR aggregate
        (RILEY, {"E100"}, {"home_city"}, (), False),
    ],
)
async def test_i2_personal_data_rules(
    policy: DeterministicPolicyService,
    who: UserContext,
    subjects: set[str],
    fields: set[str],
    unresolved: tuple[str, ...],
    allowed: bool,
) -> None:
    decision = await policy.evaluate_task(
        who, request({P}, subjects=subjects, fields=fields, unresolved=unresolved, needs=STRUCTURED)
    )
    assert decision.allowed is allowed
    if not allowed:
        assert decision.reasons == (DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA,)
        assert decision.scope is None


async def test_manager_gets_restricted_hr_columns(policy: DeterministicPolicyService) -> None:
    decision = await policy.evaluate_task(
        SAM, request({P}, subjects={"E100"}, fields={"performance_rating"}, needs=STRUCTURED)
    )
    assert decision.scope is not None
    assert decision.scope.allowed_columns[EMPLOYEE_HR.name] == MANAGER_VISIBLE_COLUMNS
    assert "salary" not in decision.scope.allowed_columns[EMPLOYEE_HR.name]


@pytest.mark.parametrize(("who", "allowed"), [(ASHA, False), (JORDAN, False), (RILEY, True)])
async def test_confidential_business_requires_clearance_and_group(
    policy: DeterministicPolicyService, who: UserContext, allowed: bool
) -> None:
    decision = await policy.evaluate_task(who, request({DataCategory.CONFIDENTIAL_BUSINESS}))
    assert decision.allowed is allowed
    if not allowed:
        assert decision.reasons == (DenialReason.CONFIDENTIAL_BUSINESS,)


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (ASHA, {"onboarding", "runbook", "postmortem", "diagram"}),
        (JORDAN, {"onboarding", "postmortem", "diagram"}),  # not in eng-platform
        (RILEY, {"onboarding", "postmortem", "diagram", "falcon"}),
        (MORGAN, {"onboarding", "postmortem", "diagram", "comp-bands"}),
        (ORBIT_USER, {"orbit-doc"}),
        (ADMIN, {"onboarding", "postmortem", "diagram"}),
    ],
)
async def test_i3_scope_contains_only_authorized_sources(
    policy: DeterministicPolicyService, who: UserContext, expected: set[str]
) -> None:
    decision = await policy.evaluate_task(who, request({DataCategory.GENERAL_KNOWLEDGE}))
    assert decision.scope is not None
    assert set(decision.scope.authorized_source_ids) == expected
    assert decision.scope.tenant_id == who.tenant_id
    assert decision.scope.hash_is_consistent()


async def test_structured_views_follow_categories(policy: DeterministicPolicyService) -> None:
    ops = await policy.evaluate_task(ASHA, request({DataCategory.OPERATIONAL}, needs=STRUCTURED))
    assert ops.scope is not None
    assert {"api.incidents", "api.services"} <= ops.scope.allowed_views
    assert "api.employee_hr" not in ops.scope.allowed_views
    docs_only = await policy.evaluate_task(ASHA, request({DataCategory.OPERATIONAL}))
    assert docs_only.scope is not None and not docs_only.scope.allowed_views


async def test_i4_derived_image_inherits_parent_permissions(registry: InMemoryRegistry) -> None:
    from tests.fakes.data_layer import source

    registry.sources["runbook_img1"] = source(
        "runbook_img1", kind=SourceKind.IMAGE, parent="runbook", groups=("eng-platform",)
    )
    policy = DeterministicPolicyService(registry, cache_ttl_seconds=0)
    assert await policy.can_access_source(ASHA, "runbook_img1")
    assert not await policy.can_access_source(JORDAN, "runbook_img1")
    registry.revoke("runbook")
    assert not await policy.can_access_source(ASHA, "runbook_img1")


async def test_i3_filter_evidence_drops_out_of_scope_and_revoked(
    policy: DeterministicPolicyService, registry: InMemoryRegistry
) -> None:
    decision = await policy.evaluate_task(ASHA, request({DataCategory.GENERAL_KNOWLEDGE}))
    assert decision.scope is not None
    items = [
        evidence("t1", "runbook", "ok"),
        evidence("t1", "falcon", "not in scope"),
        evidence("t1", "orbit-doc", "other tenant"),
        evidence(
            "t1",
            upload_source_id(ASHA, "up_1"),
            "own upload",
            source_kind=SourceKind.USER_UPLOAD,
            kind=EvidenceKind.VISION_OBSERVATION,
        ),
        evidence(
            "t1",
            upload_source_id(JORDAN, "up_2"),
            "someone else's upload",
            source_kind=SourceKind.USER_UPLOAD,
            kind=EvidenceKind.VISION_OBSERVATION,
        ),
    ]
    kept = await policy.filter_evidence(ASHA, decision.scope, items)
    assert [e.excerpt for e in kept] == ["ok", "own upload"]

    registry.revoke("runbook")
    kept = await policy.filter_evidence(ASHA, decision.scope, items[:1])
    assert kept == []


async def test_decisions_are_audited(registry: InMemoryRegistry) -> None:
    audit = MemoryAudit()
    policy = DeterministicPolicyService(registry, audit, cache_ttl_seconds=0)
    await policy.evaluate_task(ASHA, request({P}, subjects={"E101"}, fields={"salary"}))
    assert len(audit.entries) == 1
    assert audit.entries[0].allowed is False
    assert audit.entries[0].employee_id == "E100"


def test_clearance_ordering_used_by_policy() -> None:
    assert Classification.RESTRICTED.allows(Classification.CONFIDENTIAL)
