-- 001: core tables, row-level security, approved views and grants.
-- Requires roles kb_views (NOLOGIN), kb_ingest and kb_query to exist (scripts/setup_db.py).

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS api;

-- ---------------------------------------------------------------- tables
CREATE TABLE IF NOT EXISTS core.tenants (
    tenant_id text PRIMARY KEY,
    name      text NOT NULL
);

CREATE TABLE IF NOT EXISTS core.employees (
    tenant_id           text NOT NULL REFERENCES core.tenants,
    employee_id         text NOT NULL,
    name                text NOT NULL,
    work_email          text,
    title               text,
    department          text,
    team                text,
    manager_id          text,
    location            text,
    expertise           text[] NOT NULL DEFAULT '{}',
    hire_date           date,
    salary              numeric(12, 2),
    performance_rating  text,
    personal_phone      text,
    home_city           text,
    clearance           text NOT NULL DEFAULT 'internal'
        CHECK (clearance IN ('public', 'internal', 'confidential', 'restricted')),
    demo_access_summary text,
    PRIMARY KEY (tenant_id, employee_id)
);

CREATE TABLE IF NOT EXISTS core.employee_roles (
    tenant_id   text NOT NULL,
    employee_id text NOT NULL,
    role        text NOT NULL,
    PRIMARY KEY (tenant_id, employee_id, role)
);

CREATE TABLE IF NOT EXISTS core.employee_groups (
    tenant_id   text NOT NULL,
    employee_id text NOT NULL,
    group_name  text NOT NULL,
    PRIMARY KEY (tenant_id, employee_id, group_name)
);

CREATE TABLE IF NOT EXISTS core.services (
    tenant_id         text NOT NULL REFERENCES core.tenants,
    service_id        text NOT NULL,
    name              text NOT NULL,
    owner_team        text,
    tier              text,
    oncall_group      text,
    owner_employee_id text,
    PRIMARY KEY (tenant_id, service_id)
);

CREATE TABLE IF NOT EXISTS core.incidents (
    tenant_id           text NOT NULL REFERENCES core.tenants,
    incident_id         text NOT NULL,
    priority            text NOT NULL CHECK (priority IN ('P1', 'P2', 'P3', 'P4')),
    status              text NOT NULL,
    service             text NOT NULL,
    title               text NOT NULL,
    summary             text,
    root_cause_category text,
    created_at          timestamptz NOT NULL,
    resolved_at         timestamptz,
    assignee_id         text,
    PRIMARY KEY (tenant_id, incident_id)
);

CREATE TABLE IF NOT EXISTS core.sources (
    source_id                      text PRIMARY KEY,
    tenant_id                      text NOT NULL REFERENCES core.tenants,
    title                          text NOT NULL,
    kind                           text NOT NULL,
    mime_type                      text NOT NULL,
    classification                 text NOT NULL,
    allowed_groups                 text[] NOT NULL DEFAULT '{}',
    allowed_roles                  text[] NOT NULL DEFAULT '{}',
    owner_department               text,
    contains_personal_data         boolean NOT NULL DEFAULT false,
    contains_confidential_business boolean NOT NULL DEFAULT false,
    subject_employee_ids           text[] NOT NULL DEFAULT '{}',
    parent_source_id               text,
    checksum_sha256                text NOT NULL,
    version                        integer NOT NULL,
    blob_path                      text NOT NULL,
    ingested_by                    text NOT NULL,
    ingested_at                    timestamptz NOT NULL,
    revoked                        boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS sources_tenant_title ON core.sources (tenant_id, title);
CREATE INDEX IF NOT EXISTS sources_tenant_checksum ON core.sources (tenant_id, checksum_sha256);

CREATE TABLE IF NOT EXISTS core.datasets (
    tenant_id      text NOT NULL REFERENCES core.tenants,
    dataset_name   text NOT NULL,
    source_id      text NOT NULL,
    classification text NOT NULL,
    allowed_groups text[] NOT NULL DEFAULT '{}',
    columns        text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, dataset_name)
);

CREATE TABLE IF NOT EXISTS core.dataset_rows (
    tenant_id    text NOT NULL,
    dataset_name text NOT NULL,
    row_number   integer NOT NULL,
    data         jsonb NOT NULL,
    PRIMARY KEY (tenant_id, dataset_name, row_number)
);

CREATE TABLE IF NOT EXISTS core.audit_log (
    id             bigserial PRIMARY KEY,
    tenant_id      text NOT NULL,
    employee_id    text NOT NULL,
    task_id        text NOT NULL,
    categories     text[] NOT NULL,
    allowed        boolean NOT NULL,
    reasons        text[] NOT NULL,
    policy_version text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------- row-level security
-- kb_views (owner of the api.* views) sees only the current session's tenant.
-- kb_ingest is the trusted backend/ingestion role and may read and write all rows.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenants', 'employees', 'employee_roles', 'employee_groups',
                             'services', 'incidents', 'sources', 'datasets', 'dataset_rows',
                             'audit_log']
    LOOP
        EXECUTE format('ALTER TABLE core.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE core.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON core.%I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON core.%I FOR SELECT TO kb_views '
            'USING (tenant_id = current_setting(''app.tenant_id'', true))', t);
        EXECUTE format('DROP POLICY IF EXISTS backend_all ON core.%I', t);
        EXECUTE format(
            'CREATE POLICY backend_all ON core.%I FOR ALL TO kb_ingest USING (true) WITH CHECK (true)', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- grants
REVOKE ALL ON SCHEMA core FROM PUBLIC;
REVOKE ALL ON SCHEMA api FROM PUBLIC;
GRANT USAGE ON SCHEMA core TO kb_views, kb_ingest;
GRANT SELECT ON core.tenants, core.employees, core.employee_roles, core.employee_groups,
                core.services, core.incidents, core.datasets, core.dataset_rows TO kb_views;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core TO kb_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO kb_ingest;
GRANT USAGE, CREATE ON SCHEMA api TO kb_views;

-- ---------------------------------------------------------------- views
-- Views are owned by kb_views (no BYPASSRLS), so the policies above always apply.
-- Session context is set per transaction by the backend with set_config(..., true).

CREATE OR REPLACE VIEW api.employee_directory WITH (security_barrier = true) AS
SELECT e.employee_id,
       e.name,
       e.title,
       e.department,
       e.team,
       e.work_email,
       e.location,
       array_to_string(e.expertise, ', ') AS expertise,
       m.name AS manager_name,
       e.hire_date
FROM core.employees e
LEFT JOIN core.employees m ON m.tenant_id = e.tenant_id AND m.employee_id = e.manager_id
WHERE e.tenant_id = current_setting('app.tenant_id', true);

CREATE OR REPLACE VIEW api.employee_hr WITH (security_barrier = true) AS
WITH me AS (
    SELECT current_setting('app.tenant_id', true)   AS tenant_id,
           current_setting('app.employee_id', true) AS employee_id
), my_roles AS (
    SELECT bool_or(r.role = 'hr') AS is_hr, bool_or(r.role = 'manager') AS is_manager
    FROM core.employee_roles r, me
    WHERE r.tenant_id = me.tenant_id AND r.employee_id = me.employee_id
)
SELECT e.employee_id,
       e.name,
       e.department,
       e.team,
       e.performance_rating,
       CASE WHEN e.employee_id = me.employee_id OR coalesce(my_roles.is_hr, false)
            THEN e.salary END AS salary,
       CASE WHEN e.employee_id = me.employee_id OR coalesce(my_roles.is_hr, false)
            THEN e.personal_phone END AS personal_phone,
       CASE WHEN e.employee_id = me.employee_id OR coalesce(my_roles.is_hr, false)
            THEN e.home_city END AS home_city
FROM core.employees e, me, my_roles
WHERE e.tenant_id = me.tenant_id
  AND (e.employee_id = me.employee_id
       OR coalesce(my_roles.is_hr, false)
       OR (coalesce(my_roles.is_manager, false) AND e.manager_id = me.employee_id));

CREATE OR REPLACE VIEW api.incidents WITH (security_barrier = true) AS
SELECT i.incident_id,
       i.priority,
       i.status,
       i.service,
       i.title,
       i.root_cause_category,
       i.created_at,
       i.resolved_at,
       round((extract(epoch FROM (i.resolved_at - i.created_at)) / 3600)::numeric, 1)
           AS resolution_hours,
       a.name AS assignee_name
FROM core.incidents i
LEFT JOIN core.employees a ON a.tenant_id = i.tenant_id AND a.employee_id = i.assignee_id
WHERE i.tenant_id = current_setting('app.tenant_id', true);

CREATE OR REPLACE VIEW api.services WITH (security_barrier = true) AS
SELECT s.service_id,
       s.name,
       s.owner_team,
       s.tier,
       s.oncall_group,
       o.name AS owner_name
FROM core.services s
LEFT JOIN core.employees o ON o.tenant_id = s.tenant_id AND o.employee_id = s.owner_employee_id
WHERE s.tenant_id = current_setting('app.tenant_id', true);

CREATE OR REPLACE VIEW api.dataset_rows WITH (security_barrier = true) AS
WITH me AS (
    SELECT e.tenant_id, e.employee_id,
           CASE e.clearance WHEN 'public' THEN 0 WHEN 'internal' THEN 1
                            WHEN 'confidential' THEN 2 ELSE 3 END AS clearance_rank,
           coalesce((SELECT array_agg(g.group_name) FROM core.employee_groups g
                     WHERE g.tenant_id = e.tenant_id AND g.employee_id = e.employee_id),
                    '{}') AS groups
    FROM core.employees e
    WHERE e.tenant_id = current_setting('app.tenant_id', true)
      AND e.employee_id = current_setting('app.employee_id', true)
)
SELECT r.dataset_name, r.row_number, r.data
FROM core.dataset_rows r
JOIN core.datasets d ON d.tenant_id = r.tenant_id AND d.dataset_name = r.dataset_name
JOIN me ON me.tenant_id = r.tenant_id
WHERE (CASE d.classification WHEN 'public' THEN 0 WHEN 'internal' THEN 1
                             WHEN 'confidential' THEN 2 ELSE 3 END) <= me.clearance_rank
  AND (cardinality(d.allowed_groups) = 0 OR d.allowed_groups && me.groups);

ALTER VIEW api.employee_directory OWNER TO kb_views;
ALTER VIEW api.employee_hr OWNER TO kb_views;
ALTER VIEW api.incidents OWNER TO kb_views;
ALTER VIEW api.services OWNER TO kb_views;
ALTER VIEW api.dataset_rows OWNER TO kb_views;

GRANT USAGE ON SCHEMA api TO kb_query;
GRANT SELECT ON api.employee_directory, api.employee_hr, api.incidents, api.services,
                api.dataset_rows TO kb_query;
