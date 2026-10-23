"""OTLP/HTTP log ingestion.

Any OpenTelemetry collector or SDK can point at `/v1/otlp` and its logs land in
`log_events` alongside everything else. That is the point of supporting the
protocol: nobody has to write a Powerbase-specific exporter to get their data in.

**JSON encoding only.** Protobuf is the other half of OTLP/HTTP and is not
implemented — a request with `Content-Type: application/protobuf` is refused with
a clear message rather than mis-parsed. Most collectors can be told to send JSON
(`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`).

Two things this module is careful about:

* **`db.statement` is never stored.** OTLP carries it as an attribute, and a
  statement with literals in it is exactly what must not be indexed. It is
  reduced to its shape first.
* **Retries are idempotent.** OTLP exporters retry, and a retried export must
  not double-write. The batch id is derived from the payload, and each event id
  from the record's own identity, so a retry is a no-op at both levels.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import repository as repo
from .models import LogEventIn, LogEventsBatch

# OTLP severity numbers are ranges, not values. These are the lower bounds of
# each band, from the specification.
_SEVERITY_BANDS: tuple[tuple[int, str], ...] = (
    (21, "FATAL"),
    (17, "ERROR"),
    (13, "WARN"),
    (9, "INFO"),
    (5, "DEBUG"),
    (1, "TRACE"),
)

_VALID_SEVERITY = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}

#: Resource attributes that map onto real columns rather than into `attributes`.
_RESOURCE_MAP = {
    "service.name": "service_name",
    "service.version": "app_version",
    "deployment.environment": "environment",
    "deployment.environment.name": "environment",
}

#: Log-record attributes that map onto real columns.
_ATTRIBUTE_MAP = {
    "http.route": "route_template",
    "http.method": "http_method",
    "http.status_code": "status_code",
    "http.request_id": "request_id",
    "error.type": "error_code",
    "exception.type": "error_code",
    "powerbase.release": "release_id",
}

#: Attributes whose *values* must never be stored as written.
_SQL_ATTRIBUTES = ("db.statement", "db.query.text", "db.query")

MAX_ATTRIBUTES = 64
MAX_MESSAGE_CHARS = 8000


def _value(raw: Any) -> Any:
    """Unwrap an OTLP AnyValue."""
    if not isinstance(raw, dict):
        return raw
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in raw:
            value = raw[key]
            if key == "intValue":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return value
            if key == "doubleValue":
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return value
            return value
    if "bytesValue" in raw:
        # Not decoded: a byte blob is not something to index.
        return "[bytes]"
    if "arrayValue" in raw:
        values = (raw["arrayValue"] or {}).get("values") or []
        return [_value(item) for item in values][:MAX_ATTRIBUTES]
    if "kvlistValue" in raw:
        pairs = (raw["kvlistValue"] or {}).get("values") or []
        return attributes(pairs)
    return None


def attributes(pairs: Any) -> dict[str, Any]:
    """OTLP's `[{key, value}]` list into a plain object."""
    out: dict[str, Any] = {}
    if not isinstance(pairs, list):
        return out
    for pair in pairs[: MAX_ATTRIBUTES * 2]:
        if not isinstance(pair, dict):
            continue
        key = pair.get("key")
        if not isinstance(key, str) or not key:
            continue
        if len(out) >= MAX_ATTRIBUTES:
            break
        out[key] = _value(pair.get("value"))
    return out


def severity_from(record: dict) -> str:
    """`severityText` if it is one of ours, else the band `severityNumber` falls in."""
    text = record.get("severityText")
    if isinstance(text, str) and text.upper() in _VALID_SEVERITY:
        return text.upper()

    number = record.get("severityNumber")
    if isinstance(number, str):
        try:
            number = int(number)
        except ValueError:
            number = None
    if isinstance(number, int):
        for floor, name in _SEVERITY_BANDS:
            if number >= floor:
                return name
    return "INFO"


def ns_to_iso(value: Any) -> str:
    """OTLP timestamps are nanoseconds since the epoch, as a string."""
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if nanos <= 0:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    seconds, remainder = divmod(nanos, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    micros = remainder // 1000
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{micros:06d}Z"


def sql_shape(statement: str) -> str:
    """A statement reduced to its shape. The literals are the reason this exists."""
    import re

    template = re.sub(r"'(?:[^']|'')*'", "?", statement)
    template = re.sub(r'"(?:[^"]|"")*"', "?", template)
    template = re.sub(r"\$\d+", "?", template)
    template = re.sub(r"\b\d+(?:\.\d+)?\b", "?", template)
    return re.sub(r"\s+", " ", template).strip()


def _text(body: Any) -> str | None:
    if body is None:
        return None
    if isinstance(body, dict):
        value = _value(body)
        if value is None:
            return None
        return str(value)[:MAX_MESSAGE_CHARS]
    return str(body)[:MAX_MESSAGE_CHARS]


def _event_id(record: dict, index: int, scope_name: str) -> str:
    """A stable id, so an exporter's retry does not create a second row."""
    trace = record.get("traceId") or ""
    span = record.get("spanId") or ""
    stamp = record.get("timeUnixNano") or record.get("observedTimeUnixNano") or ""
    if trace or span:
        return f"otlp-{trace}-{span}"[:128]
    seed = f"{scope_name}|{stamp}|{index}|{_text(record.get('body')) or ''}"
    return "otlp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def batch_id_for(payload: dict, app_id: str = "") -> str:
    """Derived from the payload *and the project*, so the same export twice is
    one write and two projects sending identical data do not collide."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    seed = f"{app_id}|{canonical}"
    return "otlp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def convert(payload: Any, app_id: str) -> tuple[dict, int]:
    """OTLP/JSON -> a `log_events` batch payload.

    Returns ``(batch, rejected)``. A malformed record is counted and skipped
    rather than failing the whole export — an exporter should not lose a
    thousand good records to one bad one, and OTLP has a channel for saying so.
    """
    if not isinstance(payload, dict):
        return {"events": []}, 0

    resource_logs = payload.get("resourceLogs")
    if not isinstance(resource_logs, list):
        return {"events": []}, 0

    events: list[dict] = []
    rejected = 0

    for resource_log in resource_logs:
        if not isinstance(resource_log, dict):
            rejected += 1
            continue

        resource_attrs = attributes((resource_log.get("resource") or {}).get("attributes"))
        resource_fields = {
            column: resource_attrs.pop(source, None)
            for source, column in _RESOURCE_MAP.items()
            if source in resource_attrs
        }

        scope_logs = resource_log.get("scopeLogs")
        if not isinstance(scope_logs, list):
            rejected += 1
            continue

        for scope_log in scope_logs:
            if not isinstance(scope_log, dict):
                rejected += 1
                continue
            scope = scope_log.get("scope") or {}
            scope_name = scope.get("name") if isinstance(scope, dict) else None
            sdk_version = scope.get("version") if isinstance(scope, dict) else None

            records = scope_log.get("logRecords")
            if not isinstance(records, list):
                rejected += 1
                continue

            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    rejected += 1
                    continue

                record_attrs = attributes(record.get("attributes"))

                # A SQL statement becomes its shape. Never the statement.
                for key in _SQL_ATTRIBUTES:
                    if isinstance(record_attrs.get(key), str):
                        record_attrs[f"{key}_shape"] = sql_shape(record_attrs.pop(key))

                fields: dict[str, Any] = {}
                for source, column in _ATTRIBUTE_MAP.items():
                    if source in record_attrs:
                        fields[column] = record_attrs.pop(source)

                status = fields.get("status_code")
                if isinstance(status, str) and status.isdigit():
                    fields["status_code"] = int(status)
                if not isinstance(fields.get("status_code"), int):
                    fields.pop("status_code", None)

                method = fields.get("http_method")
                if isinstance(method, str):
                    fields["http_method"] = method.upper()[:16]
                    if fields["http_method"] not in {
                        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE",
                    }:
                        fields.pop("http_method")

                # Anything left over, resource attributes included, goes in
                # `attributes` and is sanitised by the normal ingest path.
                merged = {**resource_attrs, **record_attrs}
                for key, value in resource_fields.items():
                    if value is not None and key not in fields:
                        fields[key] = value

                event = {
                    "event_id": _event_id(record, index, scope_name or ""),
                    "ts": ns_to_iso(
                        record.get("timeUnixNano") or record.get("observedTimeUnixNano")
                    ),
                    "source": "otel",
                    "severity": severity_from(record),
                    "event_type": (scope_name or "otel.log")[:128],
                    "attributes": merged,
                }
                message = _text(record.get("body"))
                if message:
                    event["message"] = message
                if record.get("traceId"):
                    event["trace_id"] = str(record["traceId"])[:64]
                if record.get("spanId"):
                    event["span_id"] = str(record["spanId"])[:32]
                if sdk_version:
                    event["sdk_version"] = str(sdk_version)[:32]
                for key, value in fields.items():
                    if value is not None:
                        event[key] = value

                events.append(event)

    return {"events": events}, rejected


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #
def register_otlp_routes(app, db, settings, ingest_auth) -> None:
    """Mount OTLP/HTTP log ingestion.

    The project comes from the key. A key is already bound to exactly one
    project, so an exporter does not have to be told which one — and cannot
    write to another.
    """
    import json as _json

    def partial(rejected: int, message: str | None = None) -> JSONResponse:
        if rejected == 0:
            return JSONResponse(status_code=200, content={})
        body: dict = {"rejectedLogRecords": rejected}
        if message:
            body["errorMessage"] = message[:256]
        return JSONResponse(status_code=200, content={"partialSuccess": body})

    async def _ingest(request: Request, auth) -> JSONResponse:
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            if "protobuf" in content_type or "octet" in content_type:
                raise HTTPException(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "OTLP/HTTP protobuf is not implemented. Send JSON: set "
                    "OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the collector.",
                )
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"unsupported content type {content_type!r}; OTLP/JSON is application/json",
            )

        raw = await request.body()
        if len(raw) > settings.max_otlp_bytes:
            raise HTTPException(
                413, f"body exceeds {settings.max_otlp_bytes} bytes"
            )

        try:
            payload = _json.loads(raw or b"{}")
        except _json.JSONDecodeError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"body is not valid JSON: {exc.msg}"
            ) from None

        target_app = auth.app_id
        batch_payload, rejected = convert(payload, target_app)
        events = batch_payload["events"]

        if len(events) > settings.max_batch_size:
            raise HTTPException(
                413,
                f"export exceeds max size of {settings.max_batch_size} records",
            )
        if not events:
            return partial(rejected, "no log records in the export")

        # Validate record by record. One malformed record must not cost the
        # exporter the other nine hundred, and OTLP has a channel for saying so.
        valid: list[LogEventIn] = []
        for raw_event in events:
            try:
                valid.append(LogEventIn(**raw_event))
            except ValidationError:
                rejected += 1

        if not valid:
            return partial(rejected, "no log record passed validation")

        batch = LogEventsBatch(
            batch_id=batch_id_for(payload, target_app),
            app_id=target_app,
            events=valid,
        )
        result = repo.write_log_events(
            db,
            target_app,
            batch,
            settings.redaction_enabled,
            settings.log_events_max_attributes,
        )
        if result.get("skipped_duplicates"):
            rejected += int(result["skipped_duplicates"])
        return partial(rejected)

    @app.post("/v1/otlp/v1/logs", tags=["otlp"])
    async def otlp_logs(request: Request, auth=Depends(ingest_auth)) -> JSONResponse:
        """OTLP/HTTP JSON logs. Configure the collector's base endpoint at /v1/otlp."""
        return await _ingest(request, auth)

    @app.post("/v1/otlp/logs", tags=["otlp"], include_in_schema=False)
    async def otlp_logs_alias(request: Request, auth=Depends(ingest_auth)) -> JSONResponse:
        """The same thing without the version segment, because exporters differ."""
        return await _ingest(request, auth)


    async def _ingest_metrics_or_traces(request: Request, auth, which: str) -> JSONResponse:
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "OTLP/HTTP protobuf is not implemented. Set "
                "OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the collector.",
            )
        raw = await request.body()
        if len(raw) > settings.max_otlp_bytes:
            raise HTTPException(413, f"body exceeds {settings.max_otlp_bytes} bytes")
        try:
            payload = _json.loads(raw or b"{}")
        except _json.JSONDecodeError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"body is not valid JSON: {exc.msg}"
            ) from None

        from . import traces as traces_module

        if which == "metrics":
            converted, rejected = convert_metrics(payload, auth.app_id)
            rows = converted["points"]
            if len(rows) > settings.max_batch_size:
                raise HTTPException(413, f"export exceeds {settings.max_batch_size} points")
            result = traces_module.write_metric_points(
                db, auth.app_id, rows, settings.redaction_enabled
            )
        else:
            converted, rejected = convert_traces(payload, auth.app_id)
            rows = converted["spans"]
            if len(rows) > settings.max_batch_size:
                raise HTTPException(413, f"export exceeds {settings.max_batch_size} spans")
            result = traces_module.write_spans(
                db, auth.app_id, rows, settings.redaction_enabled
            )

        rejected += int(result.get("skipped_duplicates") or 0)
        return partial(rejected)

    @app.post("/v1/otlp/v1/metrics", tags=["otlp"])
    async def otlp_metrics(request: Request, auth=Depends(ingest_auth)) -> JSONResponse:
        """OTLP/HTTP JSON metrics. Gauges, sums and histograms."""
        return await _ingest_metrics_or_traces(request, auth, "metrics")

    @app.post("/v1/otlp/v1/traces", tags=["otlp"])
    async def otlp_traces(request: Request, auth=Depends(ingest_auth)) -> JSONResponse:
        """OTLP/HTTP JSON traces. Spans, with their trace and parent ids."""
        return await _ingest_metrics_or_traces(request, auth, "traces")


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
_SPAN_KINDS = {
    1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
}
_SPAN_STATUS = {0: "unset", 1: "ok", 2: "error"}

#: Attributes promoted onto span columns rather than kept in `attributes`.
_SPAN_ATTRIBUTE_MAP = {
    "http.route": "route_template",
    "http.target": "route_template",
}


def _dimension_hash(attributes: dict) -> str:
    """A stable handle for an attribute set, so a retried metric deduplicates."""
    canonical = json.dumps(attributes, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def convert_metrics(payload: Any, app_id: str) -> tuple[dict, int]:
    """OTLP/JSON resourceMetrics -> metric point rows."""
    if not isinstance(payload, dict):
        return {"points": []}, 0
    resource_metrics = payload.get("resourceMetrics")
    if not isinstance(resource_metrics, list):
        return {"points": []}, 0

    points: list[dict] = []
    rejected = 0

    for resource_metric in resource_metrics:
        if not isinstance(resource_metric, dict):
            rejected += 1
            continue
        resource_attrs = attributes((resource_metric.get("resource") or {}).get("attributes"))
        service_name = resource_attrs.get("service.name")
        leftover = {k: v for k, v in resource_attrs.items() if k != "service.name"}

        scope_metrics = resource_metric.get("scopeMetrics")
        if not isinstance(scope_metrics, list):
            rejected += 1
            continue

        for scope_metric in scope_metrics:
            if not isinstance(scope_metric, dict):
                rejected += 1
                continue
            metrics = scope_metric.get("metrics")
            if not isinstance(metrics, list):
                rejected += 1
                continue

            for metric in metrics:
                if not isinstance(metric, dict):
                    rejected += 1
                    continue
                name = metric.get("name")
                if not isinstance(name, str) or not name:
                    rejected += 1
                    continue
                unit = metric.get("unit") if isinstance(metric.get("unit"), str) else None

                for kind in ("gauge", "sum", "histogram"):
                    body = metric.get(kind)
                    if not isinstance(body, dict):
                        continue
                    data_points = body.get("dataPoints")
                    if not isinstance(data_points, list):
                        continue
                    for data_point in data_points:
                        if not isinstance(data_point, dict):
                            rejected += 1
                            continue
                        row = _metric_point(
                            data_point, kind, name, unit, service_name, leftover, app_id
                        )
                        if row is None:
                            rejected += 1
                        else:
                            points.append(row)

    return {"points": points}, rejected


def _metric_point(
    data_point: dict,
    kind: str,
    name: str,
    unit: str | None,
    service_name: Any,
    resource_attrs: dict,
    app_id: str,
) -> dict | None:
    if kind == "histogram":
        raw = data_point.get("sum")
        count = data_point.get("count")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        try:
            count_value = int(count)
        except (TypeError, ValueError):
            count_value = None
    else:
        raw = data_point.get("asDouble", data_point.get("asInt"))
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        count_value = None

    point_attrs = attributes(data_point.get("attributes"))
    if kind == "histogram":
        for key in ("bucketCounts", "explicitBounds"):
            if key in data_point:
                point_attrs[key] = data_point[key]

    merged = {**resource_attrs, **point_attrs}
    return {
        "metric_name": name[:200],
        "metric_kind": kind,
        "unit": unit,
        "service_name": service_name if isinstance(service_name, str) else None,
        "point_time": ns_to_iso(
            data_point.get("timeUnixNano") or data_point.get("startTimeUnixNano")
        ),
        "value": value,
        "count": count_value,
        "attributes": merged,
        "dimension_hash": _dimension_hash(merged),
    }


# --------------------------------------------------------------------------- #
# Traces
# --------------------------------------------------------------------------- #
def convert_traces(payload: Any, app_id: str) -> tuple[dict, int]:
    """OTLP/JSON resourceSpans -> span rows."""
    if not isinstance(payload, dict):
        return {"spans": []}, 0
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        return {"spans": []}, 0

    spans: list[dict] = []
    rejected = 0

    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            rejected += 1
            continue
        resource_attrs = attributes((resource_span.get("resource") or {}).get("attributes"))
        service_name = resource_attrs.get("service.name")
        leftover = {k: v for k, v in resource_attrs.items() if k != "service.name"}

        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            rejected += 1
            continue

        for scope_span in scope_spans:
            if not isinstance(scope_span, dict):
                rejected += 1
                continue
            records = scope_span.get("spans")
            if not isinstance(records, list):
                rejected += 1
                continue

            for record in records:
                if not isinstance(record, dict):
                    rejected += 1
                    continue
                span = _span_row(record, service_name, leftover)
                if span is None:
                    rejected += 1
                else:
                    spans.append(span)

    return {"spans": spans}, rejected


def _span_row(record: dict, service_name: Any, resource_attrs: dict) -> dict | None:
    trace_id = record.get("traceId")
    span_id = record.get("spanId")
    if not isinstance(trace_id, str) or not isinstance(span_id, str) or not span_id:
        return None

    start = record.get("startTimeUnixNano")
    end = record.get("endTimeUnixNano")
    start_iso = ns_to_iso(start)
    end_iso = ns_to_iso(end)

    try:
        duration_ms = max(0, (int(end) - int(start)) // 1_000_000)
    except (TypeError, ValueError):
        duration_ms = 0

    span_attrs = attributes(record.get("attributes"))
    fields: dict[str, Any] = {}
    for source, column in _SPAN_ATTRIBUTE_MAP.items():
        if source in span_attrs and column not in fields:
            fields[column] = span_attrs.pop(source)

    status = record.get("status") if isinstance(record.get("status"), dict) else {}
    status_code = _SPAN_STATUS.get(
        status.get("code") if isinstance(status.get("code"), int) else 0, "unset"
    )
    message = status.get("message")

    name = record.get("name")
    kind = record.get("kind") if isinstance(record.get("kind"), int) else 1

    return {
        "span_id": span_id[:32],
        "trace_id": trace_id[:64],
        "parent_span_id": (record.get("parentSpanId") or None),
        "service_name": service_name if isinstance(service_name, str) else None,
        "operation": (name if isinstance(name, str) and name else "unnamed")[:256],
        "span_kind": _SPAN_KINDS.get(kind, "internal"),
        "status": status_code,
        "status_message": (message[:500] if isinstance(message, str) else None),
        "start_time": start_iso,
        "end_time": end_iso,
        "duration_ms": duration_ms,
        "route_template": fields.get("route_template"),
        "attributes": {**resource_attrs, **span_attrs},
    }
