CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS api;
CREATE TABLE IF NOT EXISTS core.tenants(tenant_id text PRIMARY KEY,name text NOT NULL);
CREATE TABLE IF NOT EXISTS core.employees(tenant_id text NOT NULL,employee_id text NOT NULL,name text NOT NULL,work_email text,title text,department text,manager_id text,location text,expertise text[] NOT NULL DEFAULT '{}',hire_date date,salary numeric,performance_rating numeric,personal_phone text,home_city text,clearance text NOT NULL DEFAULT 'internal',PRIMARY KEY(tenant_id,employee_id));
CREATE TABLE IF NOT EXISTS core.employee_groups(tenant_id text NOT NULL,employee_id text NOT NULL,group_name text NOT NULL,PRIMARY KEY(tenant_id,employee_id,group_name));
CREATE TABLE IF NOT EXISTS core.employee_roles(tenant_id text NOT NULL,employee_id text NOT NULL,role_name text NOT NULL,PRIMARY KEY(tenant_id,employee_id,role_name));
CREATE TABLE IF NOT EXISTS core.incidents(tenant_id text NOT NULL,id text NOT NULL,priority text,status text,service text,title text,summary text,created_at timestamptz,resolved_at timestamptz,assignee_id text,root_cause_category text,PRIMARY KEY(tenant_id,id));
CREATE TABLE IF NOT EXISTS core.services(tenant_id text NOT NULL,id text NOT NULL,name text,owner_team text,tier text,oncall_group text,PRIMARY KEY(tenant_id,id));
CREATE TABLE IF NOT EXISTS core.sources(tenant_id text NOT NULL,source_id text NOT NULL,title text,kind text,mime_type text,classification text,allowed_groups text[] DEFAULT '{}',allowed_roles text[] DEFAULT '{}',owner_department text,contains_personal_data boolean DEFAULT false,contains_confidential_business boolean DEFAULT false,subject_employee_ids text[] DEFAULT '{}',parent_source_id text,checksum_sha256 text,version integer,blob_path text,ingested_by text,ingested_at timestamptz DEFAULT now(),revoked boolean DEFAULT false,PRIMARY KEY(tenant_id,source_id));
CREATE TABLE IF NOT EXISTS core.datasets(tenant_id text NOT NULL,dataset_name text NOT NULL,classification text NOT NULL,allowed_groups text[] DEFAULT '{}',columns jsonb NOT NULL DEFAULT '{}',source_id text NOT NULL,PRIMARY KEY(tenant_id,dataset_name));
CREATE TABLE IF NOT EXISTS core.dataset_rows(tenant_id text NOT NULL,dataset_name text NOT NULL,row_id text NOT NULL,data jsonb NOT NULL,source_id text NOT NULL,PRIMARY KEY(tenant_id,dataset_name,row_id));
CREATE TABLE IF NOT EXISTS core.audit_log(id bigserial PRIMARY KEY,tenant_id text NOT NULL,employee_id text NOT NULL,task_category text,effect text NOT NULL,reason_code text,policy_version text NOT NULL,created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS core.user_uploads(tenant_id text NOT NULL,employee_id text NOT NULL,upload_id text NOT NULL,blob_path text NOT NULL,mime_type text NOT NULL,expires_at timestamptz NOT NULL,PRIMARY KEY(tenant_id,employee_id,upload_id));

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kb_owner') THEN CREATE ROLE kb_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kb_ingest') THEN CREATE ROLE kb_ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kb_query') THEN CREATE ROLE kb_query LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; END IF;
END $$;
GRANT USAGE ON SCHEMA core,api TO kb_ingest,kb_query;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA core TO kb_ingest;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA core TO kb_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO kb_ingest;

DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['employees','employee_groups','employee_roles','incidents','services','sources','datasets','dataset_rows','audit_log','user_uploads'] LOOP
  EXECUTE format('ALTER TABLE core.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE core.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_isolation ON core.%I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
 END LOOP;
END $$;

CREATE OR REPLACE VIEW api.employee_directory WITH (security_invoker=true,security_barrier=true) AS
 SELECT e.tenant_id,e.employee_id,e.name,e.work_email,e.title,e.department,e.expertise,e.manager_id,m.name AS manager_name
 FROM core.employees e LEFT JOIN core.employees m ON m.tenant_id=e.tenant_id AND m.employee_id=e.manager_id;
CREATE OR REPLACE VIEW api.incidents WITH (security_invoker=true,security_barrier=true) AS SELECT tenant_id,id,priority,status,service,title,summary,created_at,resolved_at,assignee_id,root_cause_category FROM core.incidents;
CREATE OR REPLACE VIEW api.services WITH (security_invoker=true,security_barrier=true) AS SELECT tenant_id,id,name,owner_team,tier,oncall_group FROM core.services;
CREATE OR REPLACE VIEW api.dataset_rows WITH (security_invoker=true,security_barrier=true) AS SELECT r.tenant_id,r.dataset_name,r.row_id,r.data,r.source_id FROM core.dataset_rows r JOIN core.datasets d USING(tenant_id,dataset_name);
CREATE OR REPLACE VIEW api.employee_hr WITH (security_invoker=true,security_barrier=true) AS
 SELECT e.tenant_id,e.employee_id,
 CASE WHEN e.employee_id=current_setting('app.employee_id',true) OR EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='hr') THEN e.salary ELSE NULL END AS salary,
 CASE WHEN e.employee_id=current_setting('app.employee_id',true) OR EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='hr') OR (e.manager_id=current_setting('app.employee_id',true) AND EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='manager')) THEN e.performance_rating ELSE NULL END AS performance_rating,
 CASE WHEN e.employee_id=current_setting('app.employee_id',true) OR EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='hr') THEN e.personal_phone ELSE NULL END AS personal_phone,
 CASE WHEN e.employee_id=current_setting('app.employee_id',true) OR EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='hr') THEN e.home_city ELSE NULL END AS home_city
 FROM core.employees e
 WHERE EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='hr')
 OR e.employee_id=current_setting('app.employee_id',true)
 OR (e.manager_id=current_setting('app.employee_id',true) AND EXISTS(SELECT 1 FROM core.employee_roles r WHERE r.tenant_id=e.tenant_id AND r.employee_id=current_setting('app.employee_id',true) AND r.role_name='manager'));
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM PUBLIC,kb_query;
GRANT SELECT ON api.employee_directory,api.incidents,api.services,api.dataset_rows,api.employee_hr TO kb_query;
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT SELECT ON TABLES TO kb_query;
