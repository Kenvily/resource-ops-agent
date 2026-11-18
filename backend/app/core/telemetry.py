"""Bounded, process-local operational counters; never store URLs or business labels."""

import math
import time
from collections import deque


class Telemetry:
    def __init__(self):
        self.started_at = time.time()
        self.concurrency: dict[str, dict[str, int]] = {}
        self.upstream_bytes = 0
        self.rows: dict[tuple[str, str, str], dict] = {}

    def begin(self, name):
        row = self.concurrency.setdefault(name, {"current": 0, "peak": 0})
        row["current"] += 1
        row["peak"] = max(row["peak"], row["current"])

    def end(self, name):
        self.concurrency[name]["current"] -= 1

    def record(self, domain: str, label: str, operation: str, seconds: float, outcome: str = "ok"):
        key = (domain, label, operation)
        row = self.rows.setdefault(
            key, {"count": 0, "errors": 0, "timeouts": 0, "seconds": deque(maxlen=512)}
        )
        row["count"] += 1
        row["errors"] += outcome == "error" or outcome == "timeout"
        row["timeouts"] += outcome == "timeout"
        row["seconds"].append(max(0, seconds))

    def snapshot(self):
        rows = []
        for (domain, label, operation), value in self.rows.items():
            durations = sorted(value["seconds"])

            def percentile(p, samples=durations):
                return round(samples[max(0, math.ceil(len(samples) * p) - 1)] * 1000, 2)

            rows.append(
                {
                    "domain": domain,
                    "label": label,
                    "operation": operation,
                    "count": value["count"],
                    "errors": value["errors"],
                    "timeouts": value["timeouts"],
                    "error_rate": value["errors"] / value["count"],
                    "p50_ms": percentile(0.5),
                    "p95_ms": percentile(0.95),
                    "recent_samples": len(durations),
                }
            )
        return {
            "started_at": self.started_at,
            "concurrency": {k: dict(v) for k, v in self.concurrency.items()},
            "upstream_response_bytes": self.upstream_bytes,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "latency_window": "最近 512 次 / 每项；计数为进程累计，重启归零",
            "metrics": rows,
        }
