"""Let the HA-free modules be imported without Home Assistant installed.

`custom_components/openhomepower/__init__.py` imports Home Assistant, so a plain
`import openhomepower.const` would drag HA in. We register a synthetic package
whose __path__ points at the component directory but whose __init__ is never
executed; submodules and their relative imports then resolve normally.

This is what keeps the entity-generation tests runnable in CI (and on a laptop)
without installing Home Assistant.
"""
import os
import sys
import types

COMPONENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..",
                 "custom_components", "openhomepower")
)

if "openhomepower" not in sys.modules:
    package = types.ModuleType("openhomepower")
    package.__path__ = [COMPONENT]
    sys.modules["openhomepower"] = package
