"""OpenTelemetry wiring.

Exports to Cloud Trace when a project is configured, console otherwise. Every
model turn, connector call and browser step becomes a span, so the reasoning
chain behind an action is recoverable after the fact — which is the whole point
of an audit trail that a regulator, or a judge, can follow.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("clickwright")
_tracer = None


def setup_telemetry() -> None:
    global _tracer
    if _tracer is not None:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "clickwright"})
        provider = TracerProvider(resource=resource)

        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        if project:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=project))
            )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("clickwright")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
        logger.debug("tracing unavailable: %s", exc)
        _tracer = False


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    if _tracer in (None, False):
        logger.info("%s %s", name, attributes)
        yield
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, str(value))
        yield
