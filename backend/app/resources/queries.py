"""Exact one-minute subquery grids for archived hourly actual-use facts."""

from app.efficiency.queries import efficiency_query
from app.operations.catalog import query


def resource_query(cluster, template):
    name = template.removeprefix("res_")
    if name == "capacity_presence":
        source = efficiency_query(cluster, "eff_capacity")
        return f"max_over_time({source}[59m:1m])"
    metric, stat = name.rsplit("_", 1)
    functions = {
        "sum": "sum_over_time",
        "min": "min_over_time",
        "max": "max_over_time",
        "count": "count_over_time",
    }
    if metric not in ("gpu_util", "gpu_memory", "gpu_memory_free"):
        raise ValueError("metric not allowed")
    # Evaluation at XX:59 yields exactly XX:00..XX:59: 60 minute evaluations, not 61.
    return f"{functions[stat]}({query(cluster, 'ops_' + metric)}[59m:1m])"
