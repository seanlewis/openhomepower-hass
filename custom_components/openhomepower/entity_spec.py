"""Deriving the entity list from the register map.

Deliberately free of Home Assistant imports so the entity set can be tested
without a running HA instance — this is where mistakes are most likely (a wrong
device_class or state_class makes HA reject an entity outright).
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import DIAGNOSTIC_KEYS, EXCLUDED_KEYS
from .registry import RegisterMap


@dataclass(frozen=True)
class HomepowerSensorSpec:
    key: str
    name: str
    unit: str | None
    device_class: str | None
    state_class: str | None
    confidence: str
    diagnostic: bool


def build_specs(regmap: RegisterMap) -> list[HomepowerSensorSpec]:
    """One spec per exposed value: registers, composites and derived values."""
    specs: list[HomepowerSensorSpec] = []
    for mapping, forced in ((regmap.fields, None),
                            (regmap.composites, None),
                            (regmap.derived, "derived")):
        for key, entry in mapping.items():
            if key in EXCLUDED_KEYS:
                continue
            specs.append(HomepowerSensorSpec(
                key=key,
                name=entry.get("label", key.replace("_", " ").title()),
                unit=entry.get("unit"),
                device_class=entry.get("device_class"),
                state_class=entry.get("state_class"),
                confidence=forced or entry.get("confidence", "candidate"),
                diagnostic=key in DIAGNOSTIC_KEYS,
            ))
    return specs
