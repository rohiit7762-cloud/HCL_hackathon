import json
import sys
from pathlib import Path

sys.path.insert(0, "backend")

import chromadb

from app.core.config import get_settings

settings = get_settings()
manifest = Path(settings.blob_path) / "sources.json"
if not manifest.exists():
    print("No smoke-test source manifest found.")
    raise SystemExit(0)
records = json.loads(manifest.read_text(encoding="utf-8"))
removed = [row for row in records if row.get("title") == "IMPLEMENTATION_PROMPT.md"]
if removed:
    collection = chromadb.PersistentClient(path=settings.chroma_path).get_collection("knowledge")
    for row in removed:
        collection.delete(where={"source_id": row["source_id"]})
        blob = (Path(settings.blob_path) / row["blob_path"]).resolve()
        if Path(settings.blob_path).resolve() in blob.parents and blob.is_file():
            blob.unlink()
    manifest.write_text(
        json.dumps([row for row in records if row not in removed]), encoding="utf-8"
    )
print(f"Removed {len(removed)} temporary prompt source(s) from local indexing.")
