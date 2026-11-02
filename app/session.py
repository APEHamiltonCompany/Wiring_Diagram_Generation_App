"""
session.py - the non-GUI controller behind the Review / Analysis / Diagram tabs.

The GUI (gui.py) only draws widgets and calls these methods, so every step can
be tested without a display:

    s = Session.open()                      # loads the newest DRD
    s.load_order("SO.xlsx")                 # -> Relevant / Not part of diagram
    s.add_pipettor("VANTAGEline", "L14-20")
    s.apply_pairing()
    res = s.run_analysis()
    s.export_csv("x.csv"); s.generate_diagram("x.drawio")

Only part numbers are matched automatically (review.sort_order). Nothing here
guesses devices from descriptions.
"""
from __future__ import annotations

import json
import os
import shutil

from .drd import (DRD, bundled_data_dir, user_data_dir, find_latest_html, build_and_cache,
                  styleguide_version, version_tuple)
from .xlsx_parser import parse_xlsx
from .pn_overrides import PNOverrides
from . import review as rv
from .power_engine import PowerEngine, EngineOptions, HHS_MODES, HHS_MODE_LABELS
from .csv_io import write_csv, read_csv
from .drawio_gen import generate_drawio

APP_VERSION = "2.1.0"


# --------------------------------------------------------------------------
# DRD loading: the newest of (user-rebuilt JSON, bundled JSON, bundled HTML)
# --------------------------------------------------------------------------
def _json_version(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("version", "")
    except (OSError, ValueError):
        return None


def locate_drd():
    """Return (DRD, source description)."""
    cands = []
    dirs = [("bundled", bundled_data_dir())]
    if os.path.normcase(os.path.abspath(user_data_dir())) != os.path.normcase(
            os.path.abspath(bundled_data_dir())):
        dirs.insert(0, ("user", user_data_dir()))
    for label, d in dirs:
        p = os.path.join(d, "drd.json")
        v = _json_version(p) if os.path.exists(p) else None
        if v is not None:
            cands.append((version_tuple(v), label == "user", p, f"{label} drd.json"))
    html, hv = find_latest_html(bundled_data_dir())
    if cands:
        cands.sort(reverse=True)
        best = cands[0]
        # a newer DRD HTML shipped than any cached JSON -> parse the HTML
        if html and version_tuple(hv) > best[0]:
            return DRD.load(html_path=html), os.path.basename(html)
        return DRD.load(json_path=best[2]), best[3]
    if html:
        return DRD.load(html_path=html), os.path.basename(html)
    raise FileNotFoundError("No DRD found. Put drd.json or DeviceReferenceDatabase_v*.html "
                            "in the data folder.")


def rebuild_drd(html_path):
    """Parse a DRD HTML into the writable user folder and return the new DRD."""
    out = os.path.join(user_data_dir(), "drd.json")
    db = build_and_cache(html_path, out)
    try:   # keep a copy of the source HTML beside it for reference
        shutil.copy2(html_path, os.path.join(user_data_dir(), os.path.basename(html_path)))
    except (OSError, shutil.SameFileError):
        pass
    return DRD(db)


# --------------------------------------------------------------------------
class Session:
    def __init__(self, drd: DRD, drd_source="", overrides=None):
        self.drd = drd
        self.drd_source = drd_source
        self.overrides = overrides if overrides is not None else PNOverrides.load()
        self.state = rv.ReviewState()
        self.options = EngineOptions(app_version=APP_VERSION)
        self.order_path = ""
        self.result = None
        self.messages = []

    @classmethod
    def open(cls):
        drd, src = locate_drd()
        return cls(drd, src)

    # ---- versions -------------------------------------------------------
    @property
    def styleguide_version(self):
        try:
            return styleguide_version()
        except Exception:
            return ""

    def version_line(self):
        sg = self.styleguide_version
        return f"DRD v{self.drd.version} ({self.drd_source})" + (f" | StyleGuide v{sg}" if sg else "")

    # ---- order ----------------------------------------------------------
    def load_order(self, path):
        items = parse_xlsx(path)
        lines = [rv.OrderLine(i.part_number or "", i.name or "", int(i.qty or 1)) for i in items]
        keep_pips = list(self.state.pipettors)
        self.state = rv.sort_order(lines, self.drd, self.overrides)
        self.state.pipettors = keep_pips
        self.order_path = path
        self.result = None
        self.options.project_name = os.path.splitext(os.path.basename(path))[0]
        self.options.manual_circuits.clear()
        return self.state

    def new_blank(self):
        self.state = rv.ReviewState()
        self.result = None
        self.order_path = ""

    # ---- review actions -------------------------------------------------
    def amps_text(self, d):
        if d.amp_override is not None:
            return f"{d.amp_override:g} (user)"
        rec = d.custom if d.is_custom else (self.drd.get(d.name) or {})
        if d.name == rv.HHS_CONTROL_BOX:
            return "0.3 + 1/unit"
        if (rec.get("role") or "") == "child":
            return "child (no load)"
        a = rec.get("amperage")
        if a is not None and rec.get("amperage_status", "ok") == "ok":
            return f"{a:g}"
        return rec.get("amperage_raw") or "?"

    def needs_amps(self, d):
        if d.amp_override is not None or d.name == rv.HHS_CONTROL_BOX:
            return False
        rec = d.custom if d.is_custom else (self.drd.get(d.name) or {})
        if (rec.get("role") or "") != "powered":
            return False
        return not (rec.get("amperage") is not None and rec.get("amperage_status", "ok") == "ok")

    def move_out(self, keys, remember=False):
        for k in keys:
            rv.move_to_excluded(self.state, k, self.overrides, remember, self.drd)
        self.result = None

    def restore(self, keys):
        for k in keys:
            rv.restore_excluded_device(self.state, k)
        rv._sort_relevant(self.state, self.drd)
        self.result = None

    def map_line(self, key, device_names=(), custom=None, remember=False):
        bad = [n for n in device_names if self.drd.get(n) is None]
        if bad:
            raise ValueError("Not DRD devices: " + ", ".join(bad))
        out = rv.map_excluded_line(self.state, key, list(device_names), custom,
                                   self.overrides, remember, self.drd)
        self.result = None
        return out

    def ignore_always(self, keys):
        for k in keys:
            e = self.state.find_excluded(k)
            if e is not None:
                rv.ignore_excluded_line(self.overrides, e)

    def add_customer(self, name, qty=1):
        d = rv.add_customer_device(self.state, name, qty, self.drd)
        self.result = None
        return d

    def add_custom(self, custom, qty=1):
        custom = dict(custom)
        custom.setdefault("role", "powered")
        custom.setdefault("category", "off_deck")
        if custom.get("amperage") is not None:
            custom.setdefault("amperage_status", "ok")
        d = rv.add_custom_device(self.state, custom, qty, self.drd)
        self.result = None
        return d

    def edit_device(self, key, qty=None, amps=""):
        """amps: float, None to clear the override, or "" to leave unchanged."""
        d = self.state.find(key)
        if d is None:
            return None
        if qty is not None:
            d.qty = max(1, int(qty))
        if amps != "":
            d.amp_override = None if amps is None else float(amps)
        self.result = None
        return d

    def apply_pairing(self):
        msgs = rv.apply_pairing_rules(self.state, self.drd)
        warns = rv.pairing_warnings(self.state, self.drd)
        self.result = None
        return msgs, warns

    # ---- pipettors ------------------------------------------------------
    def pipettor_models(self):
        return list(self.drd.pipettors.keys()) or ["STARline", "VANTAGEline"]

    def add_pipettor(self, model, power="", amperage=None):
        p = rv.default_pipettor(self.drd, model, power)
        if amperage is not None:
            p.amperage = float(amperage)
        self.state.pipettors.append(p)
        self.result = None
        return p

    def remove_pipettor(self, index):
        if 0 <= index < len(self.state.pipettors):
            self.state.pipettors.pop(index)
        self.result = None

    # ---- analysis -------------------------------------------------------
    def run_analysis(self):
        self.result = PowerEngine(self.drd, self.options).analyze(self.state)
        return self.result

    def pin_load(self, load_key, circuit_num):
        self.options.manual_circuits[load_key] = int(circuit_num)
        return self.run_analysis()

    def clear_pins(self):
        self.options.manual_circuits.clear()
        return self.run_analysis()

    def can_export(self):
        return self.result is not None and self.result.ok

    def export_csv(self, path):
        if self.result is None:
            raise RuntimeError("Run the analysis first.")
        if not self.result.ok:
            raise RuntimeError("Fix the blocking issues before exporting:\n- "
                               + "\n- ".join(self.result.issues))
        write_csv(self.result.model, path)
        return path

    def export_report(self, path):
        if self.result is None:
            raise RuntimeError("Run the analysis first.")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.result.report)
        return path

    # ---- diagram --------------------------------------------------------
    @staticmethod
    def load_csv(path):
        return read_csv(path)

    def generate_diagram(self, out_path, csv_path=None):
        """From a CSV file (hand-edited or not) or, if none, the current analysis."""
        if csv_path:
            model = read_csv(csv_path)
        else:
            if not self.can_export():
                raise RuntimeError("Run the analysis and fix blocking issues first, "
                                   "or open a CSV.")
            model = self.result.model
        generate_drawio(model, out_path)
        return out_path


__all__ = ["Session", "locate_drd", "rebuild_drd", "HHS_MODES", "HHS_MODE_LABELS", "APP_VERSION"]
