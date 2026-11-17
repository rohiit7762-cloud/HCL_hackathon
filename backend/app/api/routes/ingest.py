from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.routes.demo import _user
from app.schemas.common import Classification
from app.schemas.ingestion import IngestOptions, IngestResult

router = APIRouter(prefix="/api/v1", tags=["ingestion"])


@router.post("/ingest", response_model=IngestResult)
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    classification: Classification = Form(Classification.INTERNAL),
    allowed_groups: str = Form(""),
    allowed_roles: str = Form(""),
    contains_personal_data: bool = Form(False),
    contains_confidential_business: bool = Form(False),
    dataset_name: str | None = Form(None),
):
    user = _user(request)
    service = getattr(request.app.state, "ingestion", None)
    if service is None:
        raise HTTPException(
            503, "Live ingestion is unavailable; configure Groq and the embedding model."
        )
    if file.size and file.size > request.app.state.settings.max_ingest_bytes:
        raise HTTPException(413, "File exceeds the ingestion limit.")
    data = await file.read(request.app.state.settings.max_ingest_bytes + 1)
    options = IngestOptions(
        title=file.filename,
        classification=classification,
        allowed_groups=[x.strip() for x in allowed_groups.split(",") if x.strip()],
        allowed_roles=[x.strip() for x in allowed_roles.split(",") if x.strip()],
        contains_personal_data=contains_personal_data,
        contains_confidential_business=contains_confidential_business,
        dataset_name=dataset_name,
    )
    try:
        result = await service.ingest(user, file.filename or "upload", data, options)
        request.app.state.orchestrator.policy.register_source(
            service.registry.get(result.source_id)
        )
        return result
    except PermissionError as exc:
        raise HTTPException(403, "Ingestion requires an authorized ingestor identity.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
