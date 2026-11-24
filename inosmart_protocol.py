"""Re-export canonical Inosmart BLE protocol module without triggering HA imports."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_CANONICAL_PATH = pathlib.Path(__file__).resolve().parent / "custom_components" / "inosmart" / "protocol.py"
_MOD_NAME = "_inosmart_protocol_canonical"

_spec = importlib.util.spec_from_file_location(_MOD_NAME, _CANONICAL_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = _mod
_spec.loader.exec_module(_mod)

for _name in dir(_mod):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_mod, _name)
