# Enterprise Intelligent Knowledge Assistant

An AI copilot that answers employees' questions over company documents and structured data, with citations, while enforcing who is allowed to see what.

- **Splits multi-part questions** into tasks and routes each one to document search (Chroma), structured queries (Neon Postgres), or both.
- **Declines only the restricted parts** of a question (for example another employee's salary) and answers the rest.
- **Checks the evidence** for each task and retries a weak search once, then answers what is supported and states any gaps.
- **Validates every answer** before release: citations must exist and still be accessible, personal data is redacted unless the task allows it, and unsupported claims are removed.

Built with FastAPI, LangGraph, Groq, a local embedding model (fastembed `BAAI/bge-small-en-v1.5`), Chroma, Neon Postgres, Pydantic and Streamlit. The full design is in [docs/IMPLEMENTATION_PROMPT.md](docs/IMPLEMENTATION_PROMPT.md).

## Architecture

```
Streamlit ──► FastAPI ──► identity (server-side) ──► LangGraph workflow
                                                      │
  plan_and_check_compliance  (LLM decomposition + deterministic PolicyService)
        │  declined parts get a templated refusal and never run
        ▼
  per task, in dependency order and in parallel:
      Retrieval Agent (Chroma, only authorized sources)  and/or
      SQL Agent (validated query spec → parameterized SQL on RLS-protected views)
        ▼
      Evidence Check ──insufficient & first attempt──► refine query ──► retry once
        ▼
  Answer Agent (structured, cited segments) ──► Final compliance validation ──► response
```

## Security model

- **Trusted identity:** roles, groups and clearance always come from the database. Tokens carry only tenant and employee IDs; anything the client sends about roles is ignored.
- **Deterministic access decisions:** a rule-based `PolicyService` decides access. The LLM's classification can only make a decision stricter.
- **Database enforcement:** the SQL Agent's `kb_query` role can only read approved `api.*` views. Those views are owned by `kb_views` and protected by forced row-level security. The LLM never writes SQL.
- **Retrieval filtering:** documents are filtered by tenant and authorized source IDs before search, and every result is re-checked afterwards.
- **Same 404 for "forbidden" and "missing":** a source you can't access is indistinguishable from one that doesn't exist.

`python scripts/check_db_security.py` verifies the database guarantees live. All 13 checks pass.

## Run it (Windows, macOS or Linux)

1. **Install.** You need Python 3.11+.
   ```
   python -m venv .venv
   .venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
   pip install -e ".[dev]"
   ```
2. **Configure.** Copy `.env.example` to `.env`, then set `GROQ_API_KEY`, `NEON_MIGRATION_URL` (your Neon owner connection string) and a long random `DEMO_JWT_SECRET`.
3. **Set up the database.** This creates the roles, applies migrations, and writes `NEON_INGEST_URL` and `NEON_QUERY_URL` to `.env`.
   ```
   python scripts/setup_db.py
   ```
4. **Load the synthetic demo data.**
   ```
   python scripts/seed_demo.py
   ```
5. **Start the backend and the UI** in two terminals:
   ```
   python scripts/run_backend.py --port 8765
   streamlit run frontend/app.py
   ```
   The UI expects the backend at `http://localhost:8765`. To use another port, set the `BACKEND_URL` environment variable before starting Streamlit.

## Demo personas (synthetic)

| Persona | What they can see |
|---|---|
| Asha Rao, Platform Engineer | Engineering docs and runbooks |
| Jordan Lee, new hire | Company-wide docs only |
| Sam Patel, Engineering Manager | Direct reports' ratings, not their salaries |
| Morgan Diaz, HR | Employee HR records and pay bands |
| Riley Chen, Finance | The confidential Project Falcon memo |

Try these:
- As Asha: "What's the escalation path for payments outages, and what is Sam Patel's salary?" The salary part is declined.
- As Asha: "How many P1 incidents hit payments-api last month, and what was the root cause of INC-1042?"
- "Summarize the Project Falcon memo." Asha gets nothing; Riley gets a cited summary.
- As Sam: "What are my direct reports' performance ratings and salaries?"

## Tests

```
pytest            # 104 tests; no Groq key or database needed
ruff check backend && mypy backend/app
```

## Notes

- **Groq models:** model IDs are set in `.env`. The defaults use `openai/gpt-oss-120b` for planning and answers and `openai/gpt-oss-20b` for SQL, evidence and grounding checks. Spreading calls across models helps stay within free-tier rate limits.
- **Images:** image interpretation is turned off for this demo (`GROQ_VISION_ENABLED=false`). Diagrams are still searchable through OCR text.
