"""Publication redaction preserves measurements while removing private identities."""

import runpy
from pathlib import Path
from uuid import UUID

module = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/showcase-data.py"), run_name="showcase_test"
)
anonymize = module["anonymize"]


def test_anonymization_keeps_values_and_consistent_identity_references():
    payload = {
        "devices": [
            {
                "node": "private-node.example",
                "device_uuid": "GPU-private-123",
                "gpu_model": "PUBLIC-GPU",
                "utilization_pct": 0.125,
                "physical_used_mib": None,
            }
        ],
        "workloads": [
            {"namespace": "private-team", "pod": "private-worker-01", "container": "private-container"}
        ],
        "summary": "private-node.example GPU-private-123 private-worker-01 private-team private-container",
        "business_models": [
            {
                "name": "业务私有名称",
                "model_key": "private-business",
                "category": "内部类别",
                "model_names": ["private-version-v1"],
                "attributes": {"owner": "private-person"},
                "description": "private description",
            }
        ],
        "question": "private question",
        "origin": "https://private.example:9090/path",
        "address": "192.0.2.1:9100",
    }
    result = anonymize(payload)
    assert result["devices"][0]["utilization_pct"] == 0.125
    assert result["devices"][0]["physical_used_mib"] is None
    assert result["devices"][0]["gpu_model"] == "PUBLIC-GPU"
    assert result["devices"][0]["node"] in result["summary"]
    text = str(result)
    for raw in ["private", "业务私有名称", "内部类别", "192.0.2.1"]:
        assert raw not in text


def test_task_ids_stay_valid_and_repeat_consistently_after_redaction():
    task = "a1111111-1111-4111-8111-111111111111"
    result = anonymize({"id": task, "parent_id": task, "clusters": ["yz", "zw"], "label": "亦庄 / 兆维"})
    assert result["id"] == result["parent_id"] and result["id"] != task
    assert str(UUID(result["id"])) == result["id"]
    assert result["clusters"] == ["yz", "zw"]
    assert result["label"] == "集群 A / 集群 B"
