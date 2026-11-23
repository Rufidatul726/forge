import json
import logging
from uuid import uuid4

import pytest
from pydantic import ValidationError

from forge.api.observability import (
    DEFAULT_LATENCY_BUCKETS,
    JsonLogFormatter,
    MetricsRegistry,
    TraceContext,
    sanitize_header_id,
)
from forge.config import ForgeSettings


def test_trace_context_parse_and_generation():
    # Parse valid W3C traceparent
    original = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = TraceContext.parse_or_create(original)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.flags == "01"
    assert len(ctx.parent_id) == 16
    assert ctx.traceparent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert ctx.traceparent.endswith("-01")

    # Generate when traceparent is missing or invalid
    generated = TraceContext.parse_or_create(None)
    assert len(generated.trace_id) == 32
    assert len(generated.parent_id) == 16
    assert generated.traceparent.startswith(f"00-{generated.trace_id}-")

    invalid = TraceContext.parse_or_create("invalid-format")
    assert len(invalid.trace_id) == 32


def test_sanitize_header_id():
    assert sanitize_header_id("valid-id_123.ABC") == "valid-id_123.ABC"
    assert sanitize_header_id(None) is None
    assert sanitize_header_id("") is None
    assert sanitize_header_id("has space") is None
    assert sanitize_header_id("injection\r\nX-Bad: 1") is None
    assert sanitize_header_id("a" * 129) is None


def test_json_log_formatter():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="forge.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.instance_id = "test-node-1"
    record.request_id = "req-abc-123"
    record.correlation_id = "corr-xyz"
    record.trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    record.method = "POST"
    record.path = "/api/v1/generate"
    record.status_code = 200
    record.duration_ms = 45.67

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "forge.api"
    assert parsed["message"] == "request_completed"
    assert parsed["instance_id"] == "test-node-1"
    assert parsed["request_id"] == "req-abc-123"
    assert parsed["correlation_id"] == "corr-xyz"
    assert parsed["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parsed["method"] == "POST"
    assert parsed["path"] == "/api/v1/generate"
    assert parsed["status_code"] == 200
    assert parsed["duration_ms"] == 45.67
    assert "timestamp" in parsed


def test_metrics_registry_prometheus_rendering():
    registry = MetricsRegistry()
    registry.inc_in_flight()
    registry.record_request("GET", "/api/v1/health", 200, 0.012)
    registry.record_request("GET", "/api/v1/health", 200, 0.004)
    registry.record_request("POST", "/api/v1/generate", 429, 0.002)
    registry.record_rate_limited("/api/v1/generate")
    registry.dec_in_flight()

    rendered = registry.render_prometheus(
        instance_id="node-test-1",
        environment="test",
        version="0.1.0",
    )

    assert 'forge_instance_info{instance_id="node-test-1",environment="test",version="0.1.0"} 1' in rendered
    assert 'forge_http_requests_in_flight{instance_id="node-test-1"} 0' in rendered
    assert 'forge_http_requests_total{method="GET",path="/api/v1/health",status="200",instance_id="node-test-1"} 2' in rendered
    assert 'forge_http_requests_total{method="POST",path="/api/v1/generate",status="429",instance_id="node-test-1"} 1' in rendered
    assert 'forge_rate_limited_requests_total{path="/api/v1/generate",instance_id="node-test-1"} 1' in rendered
    assert 'forge_http_request_duration_seconds_bucket{method="GET",path="/api/v1/health",instance_id="node-test-1",le="0.005"} 1' in rendered
    assert 'forge_http_request_duration_seconds_bucket{method="GET",path="/api/v1/health",instance_id="node-test-1",le="+Inf"} 2' in rendered
    assert 'forge_http_request_duration_seconds_count{method="GET",path="/api/v1/health",instance_id="node-test-1"} 2' in rendered


def test_settings_instance_id_validation():
    # Valid custom instance_id
    settings = ForgeSettings(_env_file=None, instance_id="instance-prod-01")
    assert settings.instance_id == "instance-prod-01"

    # Default generated instance_id when unset
    default_settings = ForgeSettings(_env_file=None)
    assert default_settings.instance_id is not None
    assert len(default_settings.instance_id) == 12

    # Invalid instance_id with disallowed characters
    with pytest.raises(ValidationError):
        ForgeSettings(_env_file=None, instance_id="bad instance with spaces!")
