"""Configured ownership rules with bounded Pod family matching and ambiguity handling."""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    cluster: Literal["yz", "zw", "all"] = "all"
    field: Literal["namespace_name", "pod_name", "container_name"]
    match: Literal["exact", "kubernetes_pod", "pod_prefix"] = "exact"
    value: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def valid_match(self):
        if self.match != "exact" and self.field != "pod_name":
            raise ValueError("Pod family matching requires pod_name")
        return self

    def matches(self, cluster, namespace, pod, container):
        if self.cluster not in (cluster, "all"):
            return False
        fields = {"namespace_name": namespace, "pod_name": pod, "container_name": container}
        value = fields[self.field]
        if self.match == "exact":
            return value == self.value
        if self.match == "pod_prefix":
            if value == self.value:
                return True
            if not value.startswith(self.value):
                return False
            suffix = value[len(self.value) :]
            # Deployment segments are unrestricted; adjacent numeric versions are allowed
            # only after a nonnumeric base, so a configured v1 never captures v10.
            return suffix.startswith(("-", ".")) or (
                not self.value[-1].isdigit() and bool(re.match(r"[0-9]+(?:[-.]|$)", suffix))
            )
        return self.field == "pod_name" and (
            value == self.value
            or bool(re.fullmatch(re.escape(self.value) + r"-(?:[a-z0-9]{8,10}-[a-z0-9]{5}|[0-9]+)", value))
        )


class BusinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="未分类", min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    model_names: list[str] = Field(default_factory=list, max_length=50)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=30)
    selectors: list[ModelSelector] = Field(min_length=1, max_length=100)


class BusinessCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    models: list[BusinessModel] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_rules(self):
        if len({m.model_key for m in self.models}) != len(self.models):
            raise ValueError("Duplicate catalog model key")
        selectors = [(s.cluster, s.field, s.match, s.value) for m in self.models for s in m.selectors]
        if len(selectors) != len(set(selectors)):
            raise ValueError("Duplicate catalog selector")
        return self

    def resolve(self, cluster, namespace, pod, container):
        strict = []
        families = []
        for model in self.models:
            for selector in model.selectors:
                if not selector.matches(cluster, namespace, pod, container):
                    continue
                if selector.match == "pod_prefix":
                    families.append((len(selector.value), model))
                elif model not in strict:
                    strict.append(model)
        if strict:
            return strict[0] if len(strict) == 1 else None
        if not families:
            return None
        longest = max(length for length, _ in families)
        candidates = {model.model_key: model for length, model in families if length == longest}
        return next(iter(candidates.values())) if len(candidates) == 1 else None


def load_catalog(path: str) -> BusinessCatalog:
    if not path:
        return BusinessCatalog()
    try:
        file = Path(path)
        if file.stat().st_size > 1_000_000:
            raise ValueError("Catalog too large")
        return BusinessCatalog.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        # Never emit raw selectors or catalog contents in startup errors.
        raise ValueError("Invalid business model catalog; check local JSON configuration") from None
