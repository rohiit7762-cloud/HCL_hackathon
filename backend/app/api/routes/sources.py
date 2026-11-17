from fastapi import APIRouter, HTTPException, Request

from app.api.routes.demo import _user

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("/{source_id}")
async def source_preview(source_id: str, request: Request):
    from app.demo import DOCS

    registry = getattr(request.app.state, "sources", None)
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if registry and orchestrator:
        user = _user(request)
        source = registry.get(source_id)
        if source is None or not await orchestrator.policy.can_access_source(user, source_id):
            raise HTTPException(404, "Source not found")
        content = request.app.state.blobs.get(source.blob_path) or b""
        excerpt = content.decode("utf-8", errors="replace")[:1200] if source.mime_type.startswith("text/") else None
        return {"source_id": source.source_id, "title": source.title, "excerpt": excerpt, "location_label": "Source preview"}

    for sid, title, text, location in DOCS.values():
        if sid == source_id:
            return {"source_id": sid, "title": title, "excerpt": text, "location_label": location}
    raise HTTPException(404, "Source not found")
