from pathlib import Path

import httpx

base = "http://127.0.0.1:8000"
session = (
    httpx.post(base + "/api/v1/demo/session", json={"employee_id": "ingestor"}, timeout=10)
    .raise_for_status()
    .json()
)
token = session["access_token"]
source_path = Path("docs/IMPLEMENTATION_PROMPT.md")
with source_path.open("rb") as stream:
    upload = (
        httpx.post(
            base + "/api/v1/ingest",
            headers={"Authorization": f"Bearer {token}"},
            data={"classification": "internal"},
            files={"file": (source_path.name, stream, "text/markdown")},
            timeout=180,
        )
        .raise_for_status()
        .json()
    )
print(f"Source indexed; status={upload['status']}; chunks={upload['chunk_count']}.")

query = (
    httpx.post(
        base + "/api/v1/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What are the rules for the single evidence retrieval retry?"},
        timeout=120,
    )
    .raise_for_status()
    .json()
)
print(
    f"Live evidence query completed; tasks={len(query['tasks'])}; citations={len(query['citations'])}."
)
