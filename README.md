# Lumen Enterprise Knowledge Copilot

Hackathon prototype for access-controlled enterprise search. The current UI starts in a synthetic offline demo mode and works without provider credentials. It demonstrates persona switching, partial refusals, evidence citations, and source previews.

## Run on Windows

From the repository root, in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Open `.env` and set `DEMO_JWT_SECRET` to a random value of at least 32 characters. Then start the API and UI in separate terminals:

```powershell
..venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --app-dir backend --reload
```

```powershell
..venv\Scripts\python.exe -m streamlit run frontend\app.py
```

Open `http://localhost:8501`. Choose a visibly labeled synthetic identity in the sidebar. Suggested prompts are provided in the UI.

## Add credentials for live providers

Provider values go in the root `.env` file (never commit it):

- `GROQ_API_KEY` and the `GROQ_MODEL_*` values are read by settings, but the current UI demo route still uses deterministic synthetic responses. Live Groq orchestration is not wired yet.
- `NEON_MIGRATION_URL`, `NEON_INGEST_URL`, and `NEON_QUERY_URL` are three separate Neon credentials. Apply `backend/migrations/001_core.sql` using the migration role first. The structured loader/query classes are not yet wired to the demo UI route.
- `DEMO_MODE=true` keeps synthetic identity selection available. Set it to `false` only after configuring a trusted identity resolver.

The offline demo route is intentionally synthetic; it does not query external data or claim to have checked real company records.

## Checks

```powershell
python -m pytest backend/tests -q
python -m ruff check backend/app
```

The data layer includes PDF/DOCX/Markdown/TXT parsers, semantic chunking, sentence-transformer embeddings, Chroma retrieval, OCR/caption ingestion, Neon loading/query support, and deterministic policy checks. See [the implementation contract](docs/IMPLEMENTATION_PROMPT.md) for remaining integration invariants.
