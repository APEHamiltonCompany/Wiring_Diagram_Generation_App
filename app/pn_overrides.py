"""
pn_overrides.py - saved part-number rules ("Remember for this part #").

Stored OUTSIDE the DRD in a per-user JSON file. Each rule is keyed by the
normalised order part number and is either:

    {"action": "ignore", "pn": "96005-01", "note": "..."}
        -> the line always goes to "Not part of diagram"
    {"action": "map", "pn": "6610032-01", "devices": ["BioTek 405"],
     "custom": [ {custom device dict}, ... ], "note": "..."}
        -> the line always becomes these DRD / custom devices

Rules are explicit choices made by the user. Nothing here is guessed.
"""

from __future__ import annotations

import json
import os
import sys

from .drd import norm_part


def store_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "WireDiagramBuilder", "pn_overrides.json")
    return os.path.join(root, "data", "pn_overrides.json")


class PNOverrides:
    def __init__(self, path=None, rules=None):
        self.path = path
        self.rules = dict(rules or {})

    @classmethod
    def load(cls, path=None):
        path = path or store_path()
        rules = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rules = json.load(f)
            except (OSError, json.JSONDecodeError):
                rules = {}
        return cls(path, rules)

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.rules, f, indent=2)

    def get(self, part_number):
        return self.rules.get(norm_part(part_number))

    def set_ignore(self, part_number, note=""):
        key = norm_part(part_number)
        if key:
            self.rules[key] = {"action": "ignore", "pn": part_number, "note": note}
            self.save()

    def set_map(self, part_number, devices=None, custom=None, note=""):
        key = norm_part(part_number)
        if key:
            self.rules[key] = {"action": "map", "pn": part_number,
                               "devices": list(devices or []),
                               "custom": list(custom or []), "note": note}
            self.save()

    def remove(self, part_number):
        key = norm_part(part_number)
        if key in self.rules:
            del self.rules[key]
            self.save()
