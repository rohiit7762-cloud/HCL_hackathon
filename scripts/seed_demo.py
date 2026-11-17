"""Ingest the synthetic demo corpus (data/demo) into Neon Postgres and Chroma.

    python scripts/seed_demo.py           # ingest (unchanged files are skipped)
    python scripts/seed_demo.py --reset   # wipe demo data, vectors and blobs first

Each file's access metadata comes from its `<file>.meta.json` sidecar. Image captions
use the Groq vision model when GROQ_API_KEY is set, otherwise OCR-derived captions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, enter_root

DEMO = ROOT / "data" / "demo"
# Load people first (identity), then other structured data, then documents.
PRIORITY = {"employees.json": 0, "orbit_employees.json": 0, ".csv": 1, ".json": 1}
CORE_TABLES = [
    "audit_log",
    "dataset_rows",
    "datasets",
    "sources",
    "incidents",
    "services",
    "employee_groups",
    "employee_roles",
    "employees",
    "tenants",
]


def order(path: Path) -> tuple[int, str]:
    return (PRIORITY.get(path.name, PRIORITY.get(path.suffix, 2)), path.name)


async def reset(settings) -> None:  # type: ignore[no-untyped-def]
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        settings.neon_ingest_url.get_secret_value(), autocommit=True
    ) as conn:
        for table in CORE_TABLES:
            await conn.execute(f"DELETE FROM core.{table}")  # fixed names, not input
    for directory in (settings.chroma_path, settings.blob_path):
        shutil.rmtree(ROOT / directory, ignore_errors=True)
    print("Reset: cleared core tables, vector index and blobs.")


async def seed(do_reset: bool) -> int:
    from app.core.config import Settings
    from app.providers.groq_provider import GroqLLMProvider
    from app.providers.offline import OfflineLLMProvider
    from app.schemas.common import Classification
    from app.schemas.ingestion import IngestOptions
    from app.schemas.security import UserContext
    from app.services.data_layer import build_data_layer

    settings = Settings()
    if do_reset:
        await reset(settings)
    has_key = bool(settings.groq_api_key.get_secret_value())
    llm = GroqLLMProvider(settings) if has_key else OfflineLLMProvider()
    if not has_key:
        print("GROQ_API_KEY not set: image captions will be derived from OCR text.")
    data = await build_data_layer(settings, llm)

    failures = 0
    try:
        for path in sorted(
            (p for p in DEMO.iterdir() if not p.name.endswith(".meta.json")), key=order
        ):
            meta = json.loads((DEMO / f"{path.name}.meta.json").read_text(encoding="utf-8"))
            tenant = meta.pop("tenant_id")
            operator = UserContext(  # trusted seeding identity for this tenant
                tenant_id=tenant,
                employee_id="seed-script",
                display_name="Seed script",
                roles=frozenset({"ingestor"}),
                groups=frozenset(),
                department="IT",
                clearance=Classification.RESTRICTED,
            )
            result = await data.ingestion.ingest(
                operator, path.name, path.read_bytes(), IngestOptions.model_validate(meta)
            )
            detail = (
                f"{result.chunk_count} chunks" if result.chunk_count else f"{result.row_count} rows"
            )
            print(f"  {result.status:<17} {tenant:<6} {path.name:<40} {detail}")
            for warning in result.warnings:
                print(f"      ! {warning}")
            failures += result.status == "failed"
    finally:
        if data.close:
            await data.close()
    print("Done." if not failures else f"Done with {failures} failure(s).")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--reset", action="store_true", help="delete existing demo data first")
    args = parser.parse_args()
    enter_root()
    from app.repositories.postgres_repository import use_selector_event_loop_on_windows

    use_selector_event_loop_on_windows()
    return asyncio.run(seed(args.reset))


if __name__ == "__main__":
    raise SystemExit(main())
