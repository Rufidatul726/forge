import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from forge.api.errors import (
    handle_http_error,
    handle_provider_error,
    handle_unexpected_error,
    handle_validation_error,
)
from forge.api.observability import (
    JsonLogFormatter,
    TraceContext,
    get_metrics_registry,
    sanitize_header_id,
)
from forge.api.routes import agent, generation, health, metrics, providers, sessions
from forge.config import ForgeSettings, get_settings
from forge.api.rate_limit import SlidingWindowRateLimiter
from forge.providers.errors import ProviderError


def create_app(settings: ForgeSettings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    api_logger = logging.getLogger("forge.api")
    api_logger.setLevel(active_settings.log_level)

    if active_settings.log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        api_logger.handlers = [handler]
        api_logger.propagate = False
    else:
        logging.basicConfig(
            level=active_settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    rate_limiter = SlidingWindowRateLimiter(
        active_settings.rate_limit_requests,
        active_settings.rate_limit_window_seconds,
    )
    metrics_registry = get_metrics_registry()
    instance_id = active_settings.instance_id or "unknown"

    application = FastAPI(
        title="Forge API",
        description="HTTP API for the Forge coding agent.",
        version="0.1.0",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["DELETE", "GET", "POST"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Correlation-ID",
            "traceparent",
        ],
        expose_headers=["X-Request-ID", "X-Instance-ID", "X-Correlation-ID", "traceparent"],
    )
    application.dependency_overrides[get_settings] = lambda: active_settings

    @application.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw_request_id = request.headers.get("X-Request-ID")
        request_id = sanitize_header_id(raw_request_id) or uuid4().hex
        correlation_id = sanitize_header_id(request.headers.get("X-Correlation-ID"))
        trace_context = TraceContext.parse_or_create(request.headers.get("traceparent"))

        request.state.request_id = request_id
        request.state.instance_id = instance_id
        request.state.correlation_id = correlation_id
        request.state.trace_context = trace_context

        if active_settings.metrics_enabled:
            metrics_registry.inc_in_flight()

        started_at = perf_counter()
        try:
            protected_path = request.url.path in {
                "/api/v1/generate",
                "/api/v1/agent/tasks",
                "/api/v1/agent/tasks/stream",
                "/api/v1/sessions",
            } or request.url.path.startswith("/api/v1/sessions/")
            if request.method != "OPTIONS" and protected_path:
                client_id = request.client.host if request.client else "unknown"
                allowed, retry_after = rate_limiter.check(client_id)
                if not allowed:
                    headers = {
                        "Retry-After": str(retry_after),
                        "X-Request-ID": request_id,
                        "X-Instance-ID": instance_id,
                        "traceparent": trace_context.traceparent,
                    }
                    if correlation_id:
                        headers["X-Correlation-ID"] = correlation_id
                    response = JSONResponse(
                        status_code=429,
                        content={
                            "error": {
                                "code": "rate_limited",
                                "message": "Too many requests. Please try again later.",
                            },
                            "request_id": request_id,
                        },
                        headers=headers,
                    )
                    duration_ms = (perf_counter() - started_at) * 1000
                    if active_settings.metrics_enabled:
                        metrics_registry.record_rate_limited(request.url.path)
                        metrics_registry.record_request(
                            request.method,
                            request.url.path,
                            429,
                            duration_ms / 1000.0,
                        )
                    api_logger.info(
                        "request_rate_limited instance_id=%s request_id=%s method=%s path=%s client=%s retry_after=%d",
                        instance_id,
                        request_id,
                        request.method,
                        request.url.path,
                        client_id,
                        retry_after,
                        extra={
                            "instance_id": instance_id,
                            "request_id": request_id,
                            "correlation_id": correlation_id,
                            "trace_id": trace_context.trace_id,
                            "client": client_id,
                            "method": request.method,
                            "path": request.url.path,
                            "status_code": 429,
                        },
                    )
                    return response

            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Instance-ID"] = instance_id
            response.headers["traceparent"] = trace_context.traceparent
            if correlation_id:
                response.headers["X-Correlation-ID"] = correlation_id

            duration_ms = (perf_counter() - started_at) * 1000
            if active_settings.metrics_enabled:
                metrics_registry.record_request(
                    request.method,
                    request.url.path,
                    response.status_code,
                    duration_ms / 1000.0,
                )
            api_logger.info(
                "request_completed instance_id=%s request_id=%s method=%s path=%s status_code=%d duration_ms=%.2f",
                instance_id,
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "instance_id": instance_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "trace_id": trace_context.trace_id,
                    "span_id": trace_context.parent_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return response
        finally:
            if active_settings.metrics_enabled:
                metrics_registry.dec_in_flight()

    application.add_exception_handler(StarletteHTTPException, handle_http_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(ProviderError, handle_provider_error)
    application.add_exception_handler(Exception, handle_unexpected_error)

    application.include_router(generation.router, prefix="/api/v1")
    application.include_router(agent.router, prefix="/api/v1")
    application.include_router(sessions.router, prefix="/api/v1")
    application.include_router(health.router, prefix="/api/v1")
    application.include_router(metrics.router, prefix="/api/v1")
    application.include_router(providers.router, prefix="/api/v1")
    return application


app = create_app()
