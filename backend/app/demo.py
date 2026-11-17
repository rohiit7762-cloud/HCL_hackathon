"""Synthetic, credential-free demo runtime used until live providers are configured."""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.api import Citation, ProgressEvent, QueryResponse, TaskResult, TaskStatus
from app.schemas.common import Classification, DataCategory, SourceKind
from app.schemas.evidence import EvidenceKind
from app.schemas.security import DemoUser, UserContext

DEMO_USERS = [
    DemoUser(
        tenant_id="lumen",
        employee_id="asha",
        display_name="Asha Rao",
        title="Platform Engineer",
        access_summary="Engineering runbooks and architecture",
    ),
    DemoUser(
        tenant_id="lumen",
        employee_id="jordan",
        display_name="Jordan Lee",
        title="New Hire",
        access_summary="Onboarding and employee directory",
    ),
    DemoUser(
        tenant_id="lumen",
        employee_id="sam",
        display_name="Sam Patel",
        title="Engineering Manager",
        access_summary="Engineering sources and direct report ratings",
    ),
    DemoUser(
        tenant_id="lumen",
        employee_id="morgan",
        display_name="Morgan Diaz",
        title="HR Partner",
        access_summary="HR sources and approved employee data",
    ),
    DemoUser(
        tenant_id="lumen",
        employee_id="riley",
        display_name="Riley Chen",
        title="Finance Analyst",
        access_summary="Finance group sources",
    ),
    DemoUser(
        tenant_id="lumen",
        employee_id="ingestor",
        display_name="Admin Bot",
        title="Ingestion Administrator",
        access_summary="Upload and index approved organization sources",
    ),
]

DOCS = {
    "onboarding": (
        "onboarding_guide",
        "New Employee Onboarding",
        "Week one: set up your laptop, enroll in MFA, join the all-staff group, and review the engineering handbook. Contact IT Service Desk for VPN access.",
        "Onboarding > First week",
    ),
    "vpn": (
        "it_setup",
        "IT Setup Guide",
        "For VPN access, install the approved client, enroll in MFA, then open an IT Service Desk ticket. Include your device name and department.",
        "IT setup > VPN",
    ),
    "failover": (
        "payments_runbook",
        "Payments Failover Runbook",
        "For database failover, page the payments on-call, verify replica lag, promote the healthy standby, then run reconciliation checks before restoring traffic.",
        "Payments > Database failover",
    ),
    "incident": (
        "inc1042_postmortem",
        "Incident INC-1042 Postmortem",
        "INC-1042 was caused by connection pool exhaustion after a traffic increase. The team raised pool limits and added saturation alerts.",
        "Postmortem > Root cause",
    ),
    "architecture": (
        "payments_architecture",
        "Payments Architecture",
        "The Payments API sends ledger transactions to the Ledger Service, which writes to the Ledger DB. Kafka carries asynchronous settlement events.",
        "Architecture > Ledger flow",
    ),
    "escalation": (
        "payments_runbook",
        "Payments Failover Runbook",
        "For a payments outage, page the service on-call, notify the incident commander, and use the incident channel for updates.",
        "Payments > Escalation",
    ),
    "falcon": (
        "project_falcon",
        "Project Falcon Memo",
        "The Project Falcon acquisition memo is restricted to the finance group.",
        "Project Falcon > Overview",
    ),
}


def context_for(employee_id: str) -> UserContext | None:
    roles = {
        "asha": ({"employee"}, {"all-staff", "eng-platform"}, Classification.INTERNAL),
        "jordan": ({"employee"}, {"all-staff"}, Classification.INTERNAL),
        "sam": ({"manager"}, {"all-staff", "eng-platform"}, Classification.CONFIDENTIAL),
        "morgan": ({"hr"}, {"all-staff", "hr-team"}, Classification.CONFIDENTIAL),
        "riley": ({"employee"}, {"all-staff", "finance"}, Classification.CONFIDENTIAL),
        "ingestor": ({"ingestor", "admin"}, {"all-staff"}, Classification.RESTRICTED),
    }.get(employee_id)
    if not roles:
        return None
    return UserContext(
        tenant_id="lumen",
        employee_id=employee_id,
        display_name=next(u.display_name for u in DEMO_USERS if u.employee_id == employee_id),
        title=next(u.title for u in DEMO_USERS if u.employee_id == employee_id),
        roles=frozenset(roles[0]),
        groups=frozenset(roles[1]),
        department="finance"
        if employee_id == "riley"
        else "hr"
        if employee_id == "morgan"
        else "eng",
        clearance=roles[2],
        direct_report_ids=frozenset({"asha", "jordan"}) if employee_id == "sam" else frozenset(),
        is_synthetic=True,
    )


def answer_demo(
    query: str, user: UserContext, request_id: str, conversation_id: str | None = None
) -> QueryResponse:
    q = query.lower()
    selected = []
    denied = []
    if any(w in q for w in ("onboard", "first week", "new hire")):
        selected.append("onboarding")
    if "vpn" in q:
        selected.append("vpn")
    if any(w in q for w in ("failover", "rto", "payments outage", "escalation")):
        selected.append("failover" if "failover" in q or "rto" in q else "escalation")
    if any(w in q for w in ("inc-1042", "root cause", "postmortem")):
        selected.append("incident")
    if any(w in q for w in ("diagram", "architecture", "ledger db")):
        selected.append("architecture")
    if any(w in q for w in ("falcon", "acquisition memo")):
        if "finance" in user.groups:
            selected.append("falcon")
        else:
            denied.append("I can’t provide that part of the request.")
    if any(w in q for w in ("salary", "compensation", "performance rating", "rating")):
        if "hr" in user.roles or "salary" not in q and user.employee_id == "sam":
            selected.append("hr")
        elif "my salary" in q or "my rating" in q:
            selected.append("self_hr")
        else:
            denied.append("I can’t provide personal information about another employee.")
    if "p1" in q and "incident" in q:
        selected.append("incident_count")
    if not selected and not denied:
        selected = ["onboarding"] if any(w in q for w in ("setup", "help", "where")) else []
    citations = []
    task_results = []
    sections = []
    for i, key in enumerate(dict.fromkeys(selected), 1):
        if key in DOCS:
            sid, title, excerpt, location = DOCS[key]
            citation_id = len(citations) + 1
            kind = EvidenceKind.IMAGE if key == "architecture" else EvidenceKind.TEXT_CHUNK
            citations.append(
                Citation(
                    citation_id=citation_id,
                    evidence_id=f"e{i}",
                    source_id=sid,
                    title=title,
                    kind=kind,
                    location_label=location,
                    excerpt=excerpt,
                    preview_url=f"/api/v1/sources/{sid}",
                )
            )
            sections.append(f"**{title}:** {excerpt} [{citation_id}]")
        elif key == "hr":
            sections.append(
                "I can help with permitted HR questions, but this demo does not include live employee records."
            )
        elif key == "self_hr":
            sections.append(
                "Your personal record is not loaded in this synthetic demo, so I can’t verify that value."
            )
        elif key == "incident_count":
            sections.append(
                "The synthetic incident dataset is available after Neon is configured; I can’t provide a verified count in offline demo mode."
            )
        task_results.append(
            TaskResult(
                task_id=f"t{i}",
                question=query,
                status=TaskStatus.ANSWERED if key in DOCS else TaskStatus.INSUFFICIENT_EVIDENCE,
                answer_markdown=sections[-1] if sections else "",
                citation_ids=[citations[-1].citation_id] if key in DOCS else [],
                attempts=1,
            )
        )
    if denied:
        sections.extend(denied)
    if not sections:
        sections = [
            "I couldn’t find a supported answer in the demo knowledge base. Try asking about onboarding, VPN setup, payments failover, INC-1042, or the payments architecture."
        ]
    if denied and task_results:
        status = TaskStatus.PARTIAL
    elif denied:
        status = TaskStatus.DECLINED
    elif citations:
        status = TaskStatus.ANSWERED
    else:
        status = TaskStatus.INSUFFICIENT_EVIDENCE
    return QueryResponse(
        request_id=request_id,
        conversation_id=conversation_id or request_id,
        answer_markdown="\n\n".join(sections),
        tasks=task_results,
        citations=citations,
        progress=[
            ProgressEvent(step="identity", status="done", message="Demo identity verified"),
            ProgressEvent(
                step="planning", status="done", message="Question routed to demo sources"
            ),
            ProgressEvent(step="compliance", status="done", message="Access policy applied"),
            ProgressEvent(
                step="retrieval",
                status="done" if citations else "skipped",
                message="Authorized demo evidence checked",
            ),
            ProgressEvent(step="answer", status="done", message="Grounded response prepared"),
            ProgressEvent(
                step="validation", status="done", message="Citations and response checked"
            ),
        ],
        warnings=[
            "Offline synthetic demo mode: structured records and live model responses require credentials."
        ],
    )
