"""Synthetic minute-grid sufficient statistics for offline coarse backfill tests."""

from app.core.config import CLUSTERS
from app.operations.mock import payload


def coarse_payload(fixture, cluster, template, start, end, step):
    if template == "res_capacity_presence":
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "cluster": CLUSTERS[cluster][1],
                            "node": g["node"],
                            "device_uuid": g["uuid"],
                        },
                        "values": [[ts, str(g["capacity"])] for ts in range(start, end + 1, step)],
                    }
                    for g in fixture[cluster]
                ],
            },
        }
    metric, stat = template.removeprefix("res_").rsplit("_", 1)
    result = payload(fixture, cluster, "ops_" + metric, start, end, step)
    for row in result["data"]["result"]:
        for point in row["values"]:
            point[1] = "60" if stat == "count" else str(float(point[1]) * 60) if stat == "sum" else point[1]
    return result
