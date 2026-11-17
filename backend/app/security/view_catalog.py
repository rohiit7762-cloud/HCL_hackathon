"""Catalog of the approved Postgres views the SQL Agent may query.

Single source of truth for view names, column types and column sensitivity. The
PolicyService derives allowed views/columns from it, the SQL Agent validates and
compiles query specs against it, and the migrations create matching views.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.common import DataCategory


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    pg_type: str  # used for parameter casts; trusted, never from user input
    description: str
    sensitive: bool = False  # EMPLOYEE_PERSONAL data


@dataclass(frozen=True)
class ViewSpec:
    name: str  # schema-qualified, e.g. "api.incidents"
    description: str
    category: DataCategory
    datasets: tuple[str, ...]  # dataset names behind the view (for provenance)
    columns: tuple[ColumnSpec, ...]
    example_questions: tuple[str, ...] = field(default=())

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)


EMPLOYEE_DIRECTORY = ViewSpec(
    name="api.employee_directory",
    description="Company directory: who works where, their role, work contact and expertise.",
    category=DataCategory.EMPLOYEE_DIRECTORY,
    datasets=("employees",),
    columns=(
        ColumnSpec("employee_id", "text", "Employee ID"),
        ColumnSpec("name", "text", "Full name"),
        ColumnSpec("title", "text", "Job title"),
        ColumnSpec("department", "text", "Department"),
        ColumnSpec("team", "text", "Team, e.g. 'platform', 'payments'"),
        ColumnSpec("work_email", "text", "Work email"),
        ColumnSpec("location", "text", "Office location"),
        ColumnSpec("expertise", "text", "Comma-separated expertise keywords"),
        ColumnSpec("manager_name", "text", "Manager's name"),
        ColumnSpec("hire_date", "date", "Hire date"),
    ),
    example_questions=("Who knows Kafka?", "Who is on the platform team?"),
)

EMPLOYEE_HR = ViewSpec(
    name="api.employee_hr",
    description=(
        "HR records. The database already returns only the rows this user may see: "
        "their own row, their direct reports (for managers), or everyone (for HR). "
        "Do NOT filter by manager or by the user; to list 'my direct reports' or 'my team', "
        "select the needed columns with no filters."
    ),
    category=DataCategory.EMPLOYEE_PERSONAL,
    datasets=("employees",),
    columns=(
        ColumnSpec("employee_id", "text", "Employee ID"),
        ColumnSpec("name", "text", "Full name"),
        ColumnSpec("department", "text", "Department"),
        ColumnSpec("team", "text", "Team"),
        ColumnSpec("performance_rating", "text", "Latest performance rating", sensitive=True),
        ColumnSpec("salary", "numeric", "Annual base salary (USD)", sensitive=True),
        ColumnSpec("personal_phone", "text", "Personal phone number", sensitive=True),
        ColumnSpec("home_city", "text", "Home city", sensitive=True),
    ),
)

INCIDENTS = ViewSpec(
    name="api.incidents",
    description="Operational incidents (tickets) with priority, service, status and root cause.",
    category=DataCategory.OPERATIONAL,
    datasets=("incidents",),
    columns=(
        ColumnSpec("incident_id", "text", "Incident ID, e.g. INC-1042"),
        ColumnSpec("priority", "text", "P1 (highest) to P4"),
        ColumnSpec("status", "text", "open, mitigated or resolved"),
        ColumnSpec("service", "text", "Affected service name, e.g. 'payments-api'"),
        ColumnSpec("title", "text", "Short title"),
        ColumnSpec("root_cause_category", "text", "e.g. database, deployment, network"),
        ColumnSpec("created_at", "timestamptz", "When the incident started (UTC)"),
        ColumnSpec("resolved_at", "timestamptz", "When it was resolved (UTC), null if open"),
        ColumnSpec("resolution_hours", "numeric", "Hours from start to resolution"),
        ColumnSpec("assignee_name", "text", "Engineer assigned"),
    ),
    example_questions=("How many P1 incidents did payments-api have last month?",),
)

SERVICES = ViewSpec(
    name="api.services",
    description="Service catalog: owners, tiers and on-call groups.",
    category=DataCategory.OPERATIONAL,
    datasets=("services",),
    columns=(
        ColumnSpec("service_id", "text", "Service ID"),
        ColumnSpec("name", "text", "Service name, e.g. 'payments-api'"),
        ColumnSpec("owner_team", "text", "Owning team"),
        ColumnSpec("tier", "text", "tier-1 (critical) to tier-3"),
        ColumnSpec("oncall_group", "text", "On-call rotation name"),
        ColumnSpec("owner_name", "text", "Service owner's name"),
    ),
)

DATASET_ROWS = ViewSpec(
    name="api.dataset_rows",
    description="Rows of other uploaded CSV/JSON datasets, as JSON. Filter by dataset_name.",
    category=DataCategory.GENERAL_KNOWLEDGE,
    datasets=(),
    columns=(
        ColumnSpec("dataset_name", "text", "Dataset name"),
        ColumnSpec("row_number", "integer", "Row number"),
        ColumnSpec("data", "jsonb", "Row content"),
    ),
)

VIEWS: dict[str, ViewSpec] = {
    v.name: v for v in (EMPLOYEE_DIRECTORY, EMPLOYEE_HR, INCIDENTS, SERVICES, DATASET_ROWS)
}

# Fields a manager may see for direct reports (never compensation or contact details).
MANAGER_VISIBLE_FIELDS: frozenset[str] = frozenset({"performance_rating", "rating", "performance"})
MANAGER_VISIBLE_COLUMNS: frozenset[str] = frozenset(
    {"employee_id", "name", "department", "team", "performance_rating"}
)
