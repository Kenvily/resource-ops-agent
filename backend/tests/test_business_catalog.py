"""Approved Pod mappings resolve predictably without guessing or splitting allocations."""

import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.resources.catalog import BusinessCatalog, load_catalog


def catalog(models=None):
    return BusinessCatalog.model_validate(
        {
            "models": models
            or [
                {
                    "model_key": "demo-model",
                    "name": "演示模型",
                    "category": "演示类别",
                    "model_names": ["model-v1", "model-v2"],
                    "selectors": [
                        {
                            "cluster": "yz",
                            "field": "pod_name",
                            "match": "kubernetes_pod",
                            "value": "demo-service",
                        }
                    ],
                }
            ]
        }
    )


def test_approved_pod_identity_survives_restarts_but_does_not_use_namespace_or_substrings():
    c = catalog()
    for pod in ("demo-service", "demo-service-7c97f7d86b-bcd23", "demo-service-0"):
        assert c.resolve("yz", "team", pod, "worker").model_key == "demo-model"
    for args in (
        ("zw", "team", "demo-service", "worker"),
        ("yz", "demo-service", "different-pod", "demo-service"),
        ("yz", "team", "demo-service-other", "worker"),
        ("yz", "team", "prefix-demo-service", "worker"),
    ):
        assert c.resolve(*args) is None


def test_conflicting_rules_remain_unknown_instead_of_double_counting():
    c = catalog()
    second = c.models[0].model_dump()
    second.update(model_key="other", selectors=[{"field": "namespace_name", "value": "team"}])
    c = catalog([c.models[0].model_dump(), second])
    assert c.resolve("yz", "team", "demo-service", "worker") is None
    with pytest.raises(ValidationError):
        catalog([second, second])


def test_catalog_is_validated_redacts_errors_and_supports_external_configuration(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(catalog().model_dump_json())
    settings = Settings(_env_file=None, resource_model_catalog_file=str(path))
    assert settings.resource_model_catalog.models[0].category == "演示类别"
    path.write_text(json.dumps({"models": [{"private-pod": "secret-namespace"}]}))
    with pytest.raises(ValueError) as exc:
        load_catalog(str(path))
    assert "secret-namespace" not in str(exc.value)
    assert load_catalog("").models == []


def test_delivered_catalog_has_unique_approved_pod_rules():
    from pathlib import Path

    c = load_catalog(str(Path(__file__).resolve().parents[2] / "config/business-models.json"))
    assert len(c.models) == 21  # duplicate supplied entry is represented once
    assert all(s.field == "pod_name" for m in c.models for s in m.selectors)
    assert all(m.category for m in c.models)


def test_pod_family_accepts_deployment_segments_and_numeric_versions_with_boundaries():
    c = catalog()
    c.models[0].selectors[0].match = "pod_prefix"
    for pod in (
        "demo-service-r1-test-abc123",
        "demo-service01-02-0-worker",
        "demo-service.2026",
        "demo-service",
    ):
        assert c.resolve("yz", "team", pod, "worker").model_key == "demo-model"
    for pod in ("demo-serviceother-1", "prefix-demo-service", "demo-service01other"):
        assert c.resolve("yz", "team", pod, "worker") is None
    c.models[0].selectors[0].value = "demo-v1"
    assert c.resolve("yz", "team", "demo-v1-a-more-than-standard-suffix", "worker")
    assert c.resolve("yz", "team", "demo-v10-worker", "worker") is None
    assert c.resolve("zw", "team", "demo-v1-worker", "worker") is None


def test_longest_family_wins_but_exact_rules_keep_priority_and_ties_stay_unknown():
    short = catalog().models[0].model_dump()
    short["selectors"][0].update(match="pod_prefix", value="demo")
    specific = {
        **short,
        "model_key": "specific",
        "selectors": [
            {
                "cluster": "yz",
                "field": "pod_name",
                "match": "pod_prefix",
                "value": "demo-service",
            }
        ],
    }
    c = catalog([short, specific])
    assert c.resolve("yz", "team", "demo-service-run-01", "worker").model_key == "specific"
    exact = {
        **short,
        "model_key": "exact",
        "selectors": [
            {
                "cluster": "yz",
                "field": "pod_name",
                "value": "demo-service-run-01",
            }
        ],
    }
    c = catalog([short, specific, exact])
    assert c.resolve("yz", "team", "demo-service-run-01", "worker").model_key == "exact"
    tied = {
        **specific,
        "model_key": "tied",
        "selectors": [
            {
                "cluster": "all",
                "field": "pod_name",
                "match": "pod_prefix",
                "value": "demo-service",
            }
        ],
    }
    assert catalog([short, specific, tied]).resolve("yz", "team", "demo-service-run-01", "worker") is None
