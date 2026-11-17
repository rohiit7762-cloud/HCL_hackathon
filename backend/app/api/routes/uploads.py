"""User image uploads (visible only to the uploader)."""

from __future__ import annotations

from fastapi import APIRouter, UploadFile

from app.api.dependencies import Container, CurrentUser
from app.core.errors import ValidationFailedError
from app.schemas.api import UploadResponse

router = APIRouter(prefix="/api/v1", tags=["uploads"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def sniff_image_type(data: bytes) -> str | None:
    """Identify PNG/JPEG/WebP by magic bytes; never trust the client's content type."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post("/uploads", response_model=UploadResponse)
async def upload_image(file: UploadFile, user: CurrentUser, container: Container) -> UploadResponse:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationFailedError("Images must be 5 MB or smaller.")
    mime_type = sniff_image_type(data)
    if mime_type is None:
        raise ValidationFailedError("Only PNG, JPEG and WebP images are supported.")
    upload_id = await container.data.uploads.save(user, data, mime_type)
    return UploadResponse(upload_id=upload_id)
