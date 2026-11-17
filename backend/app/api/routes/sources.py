"""Protected source previews. Missing and unauthorized sources both return 404 (I12)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.api.dependencies import Container, CurrentUser
from app.core.errors import NotFoundError
from app.schemas.ingestion import SourcePreview

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("/{source_id}", response_model=SourcePreview)
async def get_source(
    source_id: str,
    user: CurrentUser,
    container: Container,
    location: str | None = Query(default=None, max_length=200),
) -> SourcePreview:
    preview = await container.data.sources.get_preview(user, source_id, location)
    if preview is None:
        raise NotFoundError()
    return preview


@router.get("/{source_id}/image")
async def get_source_image(source_id: str, user: CurrentUser, container: Container) -> Response:
    image = await container.data.sources.get_image(user, source_id)
    if image is None:
        raise NotFoundError()
    data, mime_type = image
    return Response(
        content=data,
        media_type=mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
