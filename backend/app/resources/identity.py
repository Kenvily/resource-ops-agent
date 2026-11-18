"""Metadata association is strict and separate from business model identity."""

from collections import defaultdict


def device_metadata(payload, cluster, at, inventory):
    from app.operations.metrics import samples

    candidates = defaultdict(set)
    by_uuid = defaultdict(set)
    for node, uuid in inventory:
        by_uuid[uuid].add(node)
    try:
        for labels, _ in samples(payload, cluster, at):
            identifiers = {labels[k] for k in ("UUID", "device_uuid", "deviceuuid") if labels.get(k)}
            if len(identifiers) != 1:
                continue
            uuid = next(iter(identifiers))
            if len(by_uuid[uuid]) != 1 or labels.get("GPU_I_ID") or labels.get("GPU_I_PROFILE"):
                continue
            model, driver = labels.get("modelName"), labels.get("DCGM_FI_DRIVER_VERSION")
            if any(v is not None and (not isinstance(v, str) or len(v) > 200) for v in (model, driver)):
                continue
            candidates[(next(iter(by_uuid[uuid])), uuid)].add((model, driver))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {}
    return {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}
