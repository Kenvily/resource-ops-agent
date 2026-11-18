from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Metric = Literal[
    "inventory", "allocation", "gpu_util", "gpu_memory", "cpu_utilization_pct", "memory_used_bytes", "load1"
]
Window = Literal["1h", "1d", "1w", "1mo", "3mo", "all"]


class AgentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=500)
    cluster: Literal["yz", "zw", "all"] | None = None
    window: Window | None = None
    metrics: list[Metric] = Field(default_factory=list, max_length=7)
    parent_id: UUID | None = None
    entity_ref: str | None = Field(default=None, max_length=40)


def understand(question):
    """Local vocabulary extraction; ambiguity is clarified, never sent as private free text."""
    import re

    concepts = {
        "cpu_utilization_pct": r"cpu|处理器|算力负载",
        "memory_used_bytes": r"系统内存|主机内存|机器内存",
        "load1": r"负载|load",
        "gpu_util": r"gpu.*利用|利用率|计算活|低效|空闲|闲置|浪费|忙不忙|卡忙",
        "gpu_memory": r"物理显存|实际显存|显存占用",
        "allocation": r"额度|分配|容量|显存|资源|业务|Pod|Namespace|GPU|显卡|服务器|机器|卡数|几张",
    }
    metrics = [m for m, pattern in concepts.items() if re.search(pattern, question, re.IGNORECASE)]
    if "cpu_utilization_pct" in metrics and not re.search("GPU|卡|显存", question, re.IGNORECASE):
        metrics = [m for m in metrics if m != "gpu_util"]
        if not re.search(r"额度|分配|显存|GPU", question, re.IGNORECASE):
            metrics = [m for m in metrics if m != "allocation"]
    inventory = bool(
        re.search(
            r"配置|概况|概览|总体|整体|硬件|规模|现状|型号|核数|核心数|温度|健康|节点状态|机房.*(?:如何|情况|怎么样)|服务器.*(?:型号|核数|核心数)",
            question,
        )
    )
    if inventory:
        metrics = (
            ["inventory"] if not re.search(r"历史|趋势|变化|过去|最近|近.*[天月周]", question) else metrics
        )
    operations = [
        name
        for name, pattern in {
            "planning": r"部署|每卡|每张卡|配置评估",
            "report": r"运营报告|周报|日报|运营汇总",
            "history_quality": r"缺|回填|采集|覆盖|不同步|断|空白|只有|不完整|不全",
            "change": r"为什么|为何|相反|反常|变化|下降|上涨|增长|变少|变多|原因|异常|对比|比较|分析|飙|突然|不一样",
            "efficiency": r"低效|浪费|闲置|空闲|使用.*低|利用.*低|忙不忙|卡忙",
            "trend": r"历史|趋势|曲线|过去|最近|近.*月|近.*天",
        }.items()
        if re.search(pattern, question, re.IGNORECASE)
    ]
    if "gpu_util" not in metrics and "cpu_utilization_pct" in metrics:
        operations = [op for op in operations if op != "efficiency"]
    if re.search(r"空闲|未使用", question) and not re.search(r"低效|利用率|计算活动|浪费", question):
        metrics = list(dict.fromkeys([m for m in metrics if m != "gpu_util"] + ["gpu_memory"]))
        operations = [op for op in operations if op != "efficiency"]
    if (
        inventory
        and metrics == ["inventory"]
        and not re.search(r"为什么|为何|原因|缺|异常|变化|下降|增长", question)
    ):
        operations = [op for op in operations if op != "change"]
    parsed_window = next(
        (
            window
            for window, pattern in (
                ("3mo", r"(?:近|最近|过去)(?:3|三)个?月"),
                ("1mo", r"(?:近|最近|过去)(?:1|一)个?月"),
                ("1w", r"(?:近|最近|过去)(?:(?:1|一)周|(?:7|七)天)"),
                ("1d", r"(?:近|最近|过去)(?:(?:1|一)天|24小时)"),
                ("1h", r"(?:近|最近|过去)(?:1|一)小时"),
                ("all", r"全部历史|所有历史"),
            )
            if re.search(pattern, question)
        ),
        None,
    )
    thresholds = [
        float(v) for v in re.findall(r"(?:低于|小于|不超过|上限|阈值)\s*(-?\d+(?:\.\d+)?)\s*%", question)
    ]
    refused = bool(
        re.search(
            r"https?://|kubectl|\bssh\b|\b(?:delete|drop|update)\b|删除|重启|迁移任务|调整配额|释放|自动部署",
            question,
            re.IGNORECASE,
        )
    )
    return {
        "window": parsed_window,
        "thresholds": thresholds,
        "metrics": metrics,
        "operations": operations,
        "refused": refused,
        "inventory": inventory,
        "direct": bool(metrics)
        and (
            inventory
            and metrics == ["inventory"]
            and not operations
            or not operations
            and bool(re.search(r"多少|几张|几台|总数|列出|快照|查看当前", question))
        ),
    }
