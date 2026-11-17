"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.dependencies import ServiceContainer, build_container
from app.api.routes import demo, health, ingest, query, sources, uploads
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_id_var
from app.schemas.api import ErrorBody, ErrorResponse

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id_var.get())
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def create_app(
    settings: Settings | None = None, container: ServiceContainer | None = None
) -> FastAPI:
    """Create the app. Pass `container` to inject services (tests); otherwise the
    container is built at startup from Codex's `build_data_layer` and the Groq provider."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("startup", extra={"app_env": settings.app_env, "demo_mode": settings.demo_mode})
        owned = container is None
        app.state.container = container or await build_container(settings)
        try:
            yield
        finally:
            if owned:
                await app.state.container.close()
            logger.info("shutdown")

    app = FastAPI(
        title="Enterprise Knowledge Assistant",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > settings.max_request_bytes:
                return _error(413, "payload_too_large", "The request body is too large.")
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.info("app_error", extra={"code": exc.code, "status": exc.status_code})
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        logger.info("request_validation_error", extra={"error_count": len(exc.errors())})
        return _error(422, "validation_failed", "The request was not valid.")

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", extra={"exc_type": type(exc).__name__})
        return _error(500, "internal_error", AppError.default_message)

    for router in (
        health.router,
        demo.router,
        query.router,
        uploads.router,
        ingest.router,
        sources.router,
    ):
        app.include_router(router)
    return app
