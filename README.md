# HCL_hackathon

## Data and security layer

Codex-owned services live under `backend/app/ingestion`, `backend/app/repositories`, `backend/app/security`, and `backend/app/agents`. The document path accepts PDF, DOCX, Markdown, and TXT. It preserves section/location metadata, chunks by embedding similarity and configured token limits, then writes scalar metadata to the cosine Chroma collection. Images use OCR/caption evidence and are stored as tenant-scoped blobs. The SQL agent accepts a Pydantic `StructuredQuerySpec`; it validates the view and every field against the task's immutable `AccessScope`, compiles identifiers with `psycopg.sql.Identifier`, and binds values.

Apply `backend/migrations/001_core.sql` using the migration connection. Configure the three independent `NEON_*_URL` values in `.env`; the query role receives access only to `api` views, and base tables have forced tenant RLS. Keep the ingestion service behind the `ingestor` or `admin` role. `PolicyService` is the deterministic authorization source. It returns source IDs and SQL views only inside the task scope, and `SourceService` maps both absent and inaccessible sources to `None` so routes can return the same 404 response.

The local source registry and blob adapter are intended for the demo composition; replace the registry with its Neon-backed adapter before a multi-process deployment. Run checks with `python -m pytest backend/tests -q` and `python -m ruff check backend/app`.

