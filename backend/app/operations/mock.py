"""Explicit synthetic operational metrics; never used in Live mode."""

from app.core.config import CLUSTERS


def payload(fixture, cluster, template, start, end, step):
    result = []
    nodes = set()
    for index, gpu in enumerate(fixture[cluster]):
        node = gpu["node"]
        labels = {
            "cluster": CLUSTERS[cluster][1],
            "UUID": gpu["uuid"],
            "modelName": "MOCK-GPU",
            "DCGM_FI_DRIVER_VERSION": "MOCK-DRIVER",
        }
        low = index % 4 == 0
        memory_mib = (128e6 if low else 8e9) / 2**20
        values = {
            "ops_gpu_util": 2 if low else 65,
            "ops_gpu_memory": memory_mib,
            "ops_gpu_memory_free": 24000 - memory_mib,
            "ops_gpu_temp": 90 if index == 0 else 45,
            "ops_gpu_xid": 0,
            "ops_gpu_ecc": 0,
        }
        if template in ("ops_node_info", "ops_node_ready"):
            if node in nodes:
                continue
            nodes.add(node)
            labels = {"cluster": CLUSTERS[cluster][1], "node": node, "internal_ip": node}
            value: float = 1
        else:
            value = values[template]
        result.append({"metric": labels, "values": [[ts, str(value)] for ts in range(start, end + 1, step)]})
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}
