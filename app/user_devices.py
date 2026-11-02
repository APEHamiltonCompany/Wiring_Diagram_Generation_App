"""
user_devices.py - per-user store of devices the user supplied that are not in the
DRD. This is kept SEPARATE from the source DRD file. Project-specific overrides
must never be written back into the DRD / StyleGuide (per the analysis rules), so
unknown-device details the user enters are saved here for convenient reuse only.
"""

from __future__ import annotations

import json
import os


def _store_path():
    """Location of the per-user unknown-device store.

    Kept separate from the DRD. When running as a frozen PyInstaller bundle the
    install dir is read-only, so fall back to a writable per-user config dir.
    """
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        cfg = os.path.join(base, "WireDiagramBuilder")
        return os.path.join(cfg, "user_devices.json")
    return os.path.join(root, "data", "user_devices.json")


def load_user_devices() -> dict:
    p = _store_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_user_device(name, amperage, power_cable, comm_cable, category, notes=""):
    data = load_user_devices()
    data[name] = {
        "name": name, "amperage": amperage, "power_cable": power_cable,
        "comm_cable": comm_cable, "category": category, "notes": notes,
    }
    p = _store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data
