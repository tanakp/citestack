"""Per-application metrics. Labels are bounded by route/operation, never raw user input."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "citestack_http_requests_total",
            "Completed HTTP requests",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "citestack_http_duration_seconds",
            "End-to-end HTTP duration",
            ["route"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 120, 300),
            registry=self.registry,
        )
        self.inflight = Gauge(
            "citestack_http_inflight", "Active HTTP requests", registry=self.registry
        )
        self.inference = Gauge(
            "citestack_inference_active",
            "Worker slots occupied, including abandoned requests",
            registry=self.registry,
        )
        self.outcomes = Counter(
            "citestack_output_total",
            "Application outcomes",
            ["operation", "outcome"],
            registry=self.registry,
        )

    def render(self):
        return generate_latest(self.registry)
