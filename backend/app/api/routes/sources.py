from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("/{source_id}")
async def source_preview(source_id: str):
    from app.demo import DOCS

    for sid, title, text, location in DOCS.values():
        if sid == source_id:
            return {"source_id": sid, "title": title, "excerpt": text, "location_label": location}
    raise HTTPException(404, "Source not found")
