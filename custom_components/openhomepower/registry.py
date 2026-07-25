"""Load registers.yaml and turn a raw register map into named readings.

The YAML file is the specification; this module is just an interpreter for it.
Keeping the map as data means contributors can add hardware variants without
touching code, and other languages can consume the same file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

from .protocol import as_signed, decode_ascii, decode_clock

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(HERE, "registers.yaml")

CONFIRMED = "confirmed"
CANDIDATE = "candidate"
DERIVED = "derived"


@dataclass(frozen=True)
class Reading:
    """One decoded value, carrying its provenance.

    `confidence` travels with the value on purpose: the UI must be able to
    distinguish a verified reading from an educated guess.
    """

    key: str
    value: object
    unit: str | None
    label: str
    confidence: str
    register: int | None = None
    device_class: str | None = None
    state_class: str | None = None
    enum_label: str | None = None

    @property
    def is_certain(self) -> bool:
        return self.confidence == CONFIRMED


class RegisterMap:
    def __init__(self, spec: dict):
        self.spec = spec
        self.fields = spec.get("fields", {})
        self.composites = spec.get("composites", {})
        self.derived = spec.get("derived", {})

    @classmethod
    def load(cls, path: str = DEFAULT_MAP) -> "RegisterMap":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    @property
    def version(self) -> int:
        return self.spec.get("spec_version", 0)

    # -- decoding ---------------------------------------------------------
    def decode(self, regs: dict[int, int]) -> dict[str, Reading]:
        """Decode a raw register map into named, provenance-carrying readings."""
        out: dict[str, Reading] = {}

        for key, spec in self.fields.items():
            idx = spec["register"]
            if idx not in regs:
                continue
            raw = regs[idx]
            if spec.get("signed"):
                raw = as_signed(raw)
            scale = spec.get("scale", 1)
            value = raw * scale
            if isinstance(scale, float):
                # avoid 0.30000000000000004 in the UI
                value = round(value, 4)
            enum = spec.get("enum") or {}
            out[key] = Reading(
                key=key,
                value=value,
                unit=spec.get("unit"),
                label=spec.get("label", key),
                confidence=spec.get("confidence", CANDIDATE),
                register=idx,
                device_class=spec.get("device_class"),
                state_class=spec.get("state_class"),
                enum_label=enum.get(raw),
            )

        for key, spec in self.composites.items():
            kind = spec.get("kind")
            idxs = spec.get("registers", [])
            value = None
            if kind == "clock":
                value = decode_clock(regs, idxs[0])
            elif kind == "ascii":
                value = decode_ascii(regs, idxs)
            if value is None:
                continue
            out[key] = Reading(
                key=key, value=value, unit=None,
                label=spec.get("label", key),
                confidence=spec.get("confidence", CANDIDATE),
                register=idxs[0] if idxs else None,
            )

        out.update(self._derive(out))
        return out

    def _derive(self, readings: dict[str, Reading]) -> dict[str, Reading]:
        """Evaluate the derived formulas.

        Formulas are restricted to names, + and - by design: this is a data
        file, and it must never become an arbitrary code-execution vector.
        """
        out: dict[str, Reading] = {}
        for key, spec in self.derived.items():
            formula = spec.get("formula", "")
            total, ok = 0.0, True
            sign = 1
            for token in formula.replace("-", " - ").replace("+", " + ").split():
                if token == "+":
                    sign = 1
                elif token == "-":
                    sign = -1
                else:
                    r = readings.get(token)
                    if r is None or not isinstance(r.value, (int, float)):
                        ok = False
                        break
                    total += sign * r.value
            if not ok:
                continue
            out[key] = Reading(
                key=key, value=round(total, 4), unit=spec.get("unit"),
                label=spec.get("label", key), confidence=DERIVED,
                device_class=spec.get("device_class"),
                state_class=spec.get("state_class"),
            )
        return out
