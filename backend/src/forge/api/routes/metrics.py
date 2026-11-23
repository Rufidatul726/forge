from fastapi import APIRouter, Depends, Response

from forge.api.observability import get_metrics_registry
from forge.config import ForgeSettings, get_settings

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    summary="Scrape operational metrics",
    description="Prometheus-compatible metrics scrape endpoint for multi-instance distributed monitoring.",
    response_class=Response,
)
def get_metrics(settings: ForgeSettings = Depends(get_settings)) -> Response:
    if not settings.metrics_enabled:
        return Response(content="# Metrics collection is disabled\n", media_type="text/plain")
    registry = get_metrics_registry()
    content = registry.render_prometheus(
        instance_id=settings.instance_id or "unknown",
        environment=settings.environment,
        version="0.1.0",
    )
    return Response(
        content=content,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
