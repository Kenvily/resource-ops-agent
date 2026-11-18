"""Closed metric definitions, with explicit units and identity requirements."""

import json

from app.core.config import CLUSTERS

# These are exporter units, not inferred from observed values.
CATALOG = {
    "gpu_util": ("DCGM_FI_DEV_GPU_UTIL", "DCGM GPU 利用率", "%", 0, 100),
    "gpu_memory_free": ("DCGM_FI_DEV_FB_FREE", "DCGM 物理显存剩余", "MiB", 0, 2**40),
    "gpu_memory": ("DCGM_FI_DEV_FB_USED", "DCGM 物理显存", "MiB", 0, 2**40),
    "gpu_temp": ("DCGM_FI_DEV_GPU_TEMP", "GPU 温度", "°C", -20, 150),
    "gpu_xid": ("DCGM_FI_DEV_XID_ERRORS", "最近 XID 错误编号", "编号", 0, 1_000_000),
    "gpu_ecc": ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "累计双位 ECC 错误", "次", 0, 2**53 - 1),
    "node_ready": ("kube_node_status_condition", "GPU 节点 Ready", "状态", 0, 1),
}


def query(cluster, template):
    key = template.removeprefix("ops_")
    name = "kube_node_info" if key == "node_info" else CATALOG[key][0]
    match = "cluster=" + json.dumps(CLUSTERS[cluster][1])
    if key == "node_ready":
        match += ',condition="Ready",status="true"'
    metric = name + "{" + match + "}"
    return f"({metric} and (time()-timestamp({metric})<=120) and (time()-timestamp({metric})>=0))"
