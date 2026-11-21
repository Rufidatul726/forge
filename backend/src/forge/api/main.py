import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from forge.api.errors import (
    handle_http_error,
    handle_provider_error,
    handle_unexpected_error,
    handle_validation_error,
)
from forge.api.routes import generation, health, providers
from forge.config import ForgeSettings, get_settings
from forge.providers.errors import ProviderError


def create_app(settings: ForgeSettings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    logging.basicConfig(
        level=active_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    api_logger = logging.getLogger("forge.api")
    api_logger.setLevel(active_settings.log_level)

    application = FastAPI(
        title="Forge API",
        description="HTTP API for the Forge coding agent.",
        version="0.1.0",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    application.dependency_overrides[get_settings] = lambda: active_settings

    @application.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = uuid4().hex
        request.state.request_id = request_id
        started_at = perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        duration_ms = (perf_counter() - started_at) * 1000
        api_logger.info(
            "request_completed request_id=%s method=%s path=%s status_code=%d duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    application.add_exception_handler(StarletteHTTPException, handle_http_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(ProviderError, handle_provider_error)
    application.add_exception_handler(Exception, handle_unexpected_error)

    application.include_router(generation.router, prefix="/api/v1")
    application.include_router(health.router, prefix="/api/v1")
    application.include_router(providers.router, prefix="/api/v1")
    return application


app = create_app()
