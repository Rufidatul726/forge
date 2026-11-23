import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# W3C traceparent regex: 00-{32 hex trace_id}-{16 hex parent_id}-{2 hex flags}
_TRACEPARENT_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_HEADER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")

DEFAULT_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    parent_id: str
    flags: str = "01"

    @classmethod
    def parse_or_create(cls, traceparent: str | None = None) -> "TraceContext":
        if traceparent:
            match = _TRACEPARENT_PATTERN.match(traceparent.strip().lower())
            if match:
                trace_id, parent_id, flags = match.groups()
                # Create a new child span ID for this request hop
                child_span_id = uuid4().hex[:16]
                return cls(trace_id=trace_id, parent_id=child_span_id, flags=flags)
        trace_id = uuid4().hex
        span_id = uuid4().hex[:16]
        return cls(trace_id=trace_id, parent_id=span_id, flags="01")

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.parent_id}-{self.flags}"


def sanitize_header_id(value: str | None) -> str | None:
    if not value:
        return None
    val = value.strip()
    if _HEADER_ID_PATTERN.match(val):
        return val
    return None


class JsonLogFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attr in (
            "instance_id",
            "request_id",
            "correlation_id",
            "trace_id",
            "span_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client",
        ):
            if hasattr(record, attr):
                payload[attr] = getattr(record, attr)

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exception"] = record.exc_text

        return json.dumps(payload, separators=(",", ":"))


class MetricsRegistry:
    """Thread-safe in-memory metrics registry for Prometheus-compatible scraping."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = sorted(buckets)
        # (method, path, status) -> count
        self._requests_total: dict[tuple[str, str, int], int] = {}
        # (method, path) -> (bucket_counts, sum, total_count)
        self._request_durations: dict[tuple[str, str], list[float]] = {}
        self._requests_in_flight: int = 0
        # path -> count
        self._rate_limited_total: dict[str, int] = {}

    def inc_in_flight(self) -> None:
        with self._lock:
            self._requests_in_flight += 1

    def dec_in_flight(self) -> None:
        with self._lock:
            if self._requests_in_flight > 0:
                self._requests_in_flight -= 1

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        with self._lock:
            key = (method, path, status_code)
            self._requests_total[key] = self._requests_total.get(key, 0) + 1

            dur_key = (method, path)
            if dur_key not in self._request_durations:
                # [count_b1, count_b2, ..., count_+Inf, sum, total_count]
                self._request_durations[dur_key] = [0.0] * (len(self._buckets) + 2)

            data = self._request_durations[dur_key]
            for i, bound in enumerate(self._buckets):
                if duration_seconds <= bound:
                    data[i] += 1
            data[len(self._buckets)] += duration_seconds  # sum
            data[len(self._buckets) + 1] += 1  # total count

    def record_rate_limited(self, path: str) -> None:
        with self._lock:
            self._rate_limited_total[path] = self._rate_limited_total.get(path, 0) + 1

    def render_prometheus(
        self,
        instance_id: str,
        environment: str = "production",
        version: str = "0.1.0",
    ) -> str:
        lines: list[str] = [
            "# HELP forge_instance_info Instance metadata and status.",
            "# TYPE forge_instance_info gauge",
            f'forge_instance_info{{instance_id="{instance_id}",environment="{environment}",version="{version}"}} 1',
            "# HELP forge_http_requests_in_flight Current number of HTTP requests being processed.",
            "# TYPE forge_http_requests_in_flight gauge",
        ]
        with self._lock:
            lines.append(
                f'forge_http_requests_in_flight{{instance_id="{instance_id}"}} {self._requests_in_flight}'
            )

            lines.append("# HELP forge_http_requests_total Total number of HTTP requests processed.")
            lines.append("# TYPE forge_http_requests_total counter")
            for (method, path, status), count in sorted(self._requests_total.items()):
                lines.append(
                    f'forge_http_requests_total{{method="{method}",path="{path}",status="{status}",instance_id="{instance_id}"}} {count}'
                )

            lines.append("# HELP forge_http_request_duration_seconds HTTP request duration in seconds.")
            lines.append("# TYPE forge_http_request_duration_seconds histogram")
            for (method, path), data in sorted(self._request_durations.items()):
                cumulative = 0
                for i, bound in enumerate(self._buckets):
                    cumulative += int(data[i])
                    bound_str = f"{bound:.3f}".rstrip("0").rstrip(".") if bound < 1 else str(bound)
                    lines.append(
                        f'forge_http_request_duration_seconds_bucket{{method="{method}",path="{path}",instance_id="{instance_id}",le="{bound_str}"}} {cumulative}'
                    )
                total_count = int(data[len(self._buckets) + 1])
                lines.append(
                    f'forge_http_request_duration_seconds_bucket{{method="{method}",path="{path}",instance_id="{instance_id}",le="+Inf"}} {total_count}'
                )
                dur_sum = data[len(self._buckets)]
                lines.append(
                    f'forge_http_request_duration_seconds_sum{{method="{method}",path="{path}",instance_id="{instance_id}"}} {dur_sum:.6f}'
                )
                lines.append(
                    f'forge_http_request_duration_seconds_count{{method="{method}",path="{path}",instance_id="{instance_id}"}} {total_count}'
                )

            lines.append("# HELP forge_rate_limited_requests_total Total number of rate-limited requests.")
            lines.append("# TYPE forge_rate_limited_requests_total counter")
            for path, count in sorted(self._rate_limited_total.items()):
                lines.append(
                    f'forge_rate_limited_requests_total{{path="{path}",instance_id="{instance_id}"}} {count}'
                )

        return "\n".join(lines) + "\n"


_GLOBAL_METRICS = MetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    return _GLOBAL_METRICS
