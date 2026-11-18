#!/usr/bin/env python3
"""Read-only R5 evidence; never migrate, sync, mutate resources or invoke models.

PYTHONPATH=backend .venv/bin/python scripts/validate-production.py --runs 8 --concurrency 2
"""

import argparse
import asyncio
import json
from urllib.parse import urlsplit

import httpx
from app.core.config import Settings
from app.validation.production import collect, measure


async def run(args):
    settings = Settings()
    report = await collect(settings, runs=args.runs, concurrency=args.concurrency)
    if args.http_base:
        parts = urlsplit(args.http_base)
        if (
            parts.scheme not in ("http", "https")
            or parts.hostname not in ("127.0.0.1", "localhost", "::1")
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("HTTP验收只允许显式本机 origin")
        headers = {
            "X-ROC-Gateway-Token": settings.gateway_shared_secret.get_secret_value()
        }
        async with httpx.AsyncClient(
            base_url=args.http_base,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            headers=headers,
        ) as client:

            async def query(path):
                response = await client.post(path, json={"cluster": "all"})
                response.raise_for_status()
                response.json()

            # Fixed DB-only routes; never use overview refresh, history filling or LLM endpoints.
            results = []
            for path in (
                "/api/v1/resource-statistics",
                "/api/v1/resource-statistics/workloads",
            ):
                results.append(
                    {
                        "case": path,
                        **await measure(
                            lambda p=path: query(p), min(args.runs, 8), args.concurrency
                        ),
                    }
                )
            report["http_benchmarks"] = results
            response = await client.get("/api/v1/operations/infrastructure")
            response.raise_for_status()
            runtime = response.json()
            telemetry = runtime["telemetry"]
            report["runtime_observation"] = {
                "uptime_seconds": telemetry["uptime_seconds"],
                "upstream_response_bytes": telemetry["upstream_response_bytes"],
                "upstream_active": telemetry["concurrency"].get("upstream_active", {}),
                "upstream_outstanding": telemetry["concurrency"].get(
                    "upstream_outstanding", {}
                ),
                "upstream_counts": sum(
                    r["count"]
                    for r in telemetry["metrics"]
                    if r["domain"] == "prometheus"
                ),
                "upstream_errors": sum(
                    r["errors"]
                    for r in telemetry["metrics"]
                    if r["domain"] == "prometheus"
                ),
                "upstream_recent_p95_max_ms": max(
                    (
                        r["p95_ms"]
                        for r in telemetry["metrics"]
                        if r["domain"] == "prometheus"
                    ),
                    default=None,
                ),
                "database_connections": (runtime["database"] or {}).get("connections"),
                "database_lock_waiters": (runtime["database"] or {}).get(
                    "lock_waiters"
                ),
            }
    measurements = report["benchmarks"] + report.get("http_benchmarks", [])
    failed = any(r["errors"] for r in measurements) or any(
        s["status"] != "healthy" for s in report["current_sources"]
    )
    report["validation_status"] = "issues_found" if failed else "observed"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict:
        # Enterprise identity/permissions and sustained production load have no automated sign-off.
        return 2
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=int, choices=range(1, 31), default=8, metavar="1..30"
    )
    parser.add_argument(
        "--concurrency", type=int, choices=range(1, 5), default=2, metavar="1..4"
    )
    parser.add_argument(
        "--http-base", help="可选本机HTTP origin；有界访问两个固定读库端点"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="当前企业验收未签署，输出证据后返回2，防止自动放行",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception:  # noqa: BLE001 - private DB/response details must never enter output
        print(
            json.dumps(
                {
                    "status": "error",
                    "detail": "验收未完成，请检查本机数据库、配置或接口状态",
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
