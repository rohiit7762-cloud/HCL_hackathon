"""Document and data ingestion. Restricted to the ingestor/admin roles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, UploadFile
from pydantic import ValidationError

from app.api.dependencies import Container, CurrentUser
from app.core.errors import PermissionDeniedError, ValidationFailedError
from app.schemas.ingestion import IngestOptions, IngestResult

router = APIRouter(prefix="/api/v1", tags=["ingestion"])

INGEST_ROLES = ("ingestor", "admin")


@router.post("/ingest", response_model=IngestResult)
async def ingest(
    file: UploadFile,
    options: Annotated[str, Form(description="IngestOptions as JSON")],
    user: CurrentUser,
    container: Container,
) -> IngestResult:
    if not user.has_role(*INGEST_ROLES):
        raise PermissionDeniedError()
    try:
        parsed = IngestOptions.model_validate_json(options)
    except ValidationError as exc:
        raise ValidationFailedError("Ingestion options are not valid.") from exc
    limit = container.settings.max_ingest_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailedError("The file is too large to ingest.")
    return await container.data.ingestion.ingest(user, file.filename or "upload", data, parsed)
