from __future__ import annotations

import threading

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from .jobs import JobMetrics, JobState


class WorkerMetrics:
    def __init__(self) -> None:
        self._render_lock = threading.Lock()
        self.registry = CollectorRegistry()
        self.enqueue = Counter(
            "paperless_docling_enqueue",
            "Webhook enqueue attempts by outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self.jobs = Gauge(
            "paperless_docling_jobs",
            "Current durable jobs by state.",
            ("state",),
            registry=self.registry,
        )
        self.ready_queue_depth = Gauge(
            "paperless_docling_ready_queue_depth",
            "Current number of jobs eligible for claim.",
            registry=self.registry,
        )
        self.oldest_eligible_age_seconds = Gauge(
            "paperless_docling_oldest_eligible_age_seconds",
            "Age of the oldest job eligible for claim.",
            registry=self.registry,
        )
        for outcome in ("accepted", "unavailable"):
            self.enqueue.labels(outcome=outcome)
        for state in JobState:
            self.jobs.labels(state=state.value).set(0)

    def record_enqueue(self, outcome: str) -> None:
        self.enqueue.labels(outcome=outcome).inc()

    def render(self, snapshot: JobMetrics) -> tuple[bytes, str]:
        with self._render_lock:
            for state in JobState:
                self.jobs.labels(state=state.value).set(snapshot.state_totals[state])
            self.ready_queue_depth.set(snapshot.ready_queue_depth)
            self.oldest_eligible_age_seconds.set(
                snapshot.oldest_eligible_age_seconds
            )
            return generate_latest(self.registry), CONTENT_TYPE_LATEST
