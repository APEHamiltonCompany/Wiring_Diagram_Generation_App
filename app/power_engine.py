"""
power_engine.py - power distribution analysis (Mode 1) from the reviewed list.

Input is the ReviewState the user approved on the sorting screen (review.py):
only the devices in the "Relevant" list and the pipettors picked by hand are
used. Nothing is matched by name here; every device is already a DRD record
(or a custom device the user entered).

What it does
  1. Expands every selected device into physical units with stable ids.
  2. Works out loads:
       * Powered devices use their DRD amperage (or the user's override).
       * Child devices never get a circuit and add no load, unless the DRD
         note says "Adds xA to the <parent>" (e.g. Pre-Heat Module -> HHS
         Control Box). Amperage listed on a child is ignored.
       * HHS Control Box = 0.3A + 1A per HHS / HHC / Pre-Heat unit on it
         (max 8 units per box). Boxes are counted from the order.
  3. HHS connection method (StyleGuide HHS table): TCC ports (STAR, no USB),
     VANTAGE Periphery Box (each unit gets a Periphery port + a USB A port),
     or HHS Control Box (DB9 to the box, one unit per box gets USB to the PC,
     HHC first). "auto" picks Control Box when one is on the order, else TCC
     for STAR, else Periphery for VANTAGE.
  4. Hub check against the main PC's ports (never for a Customer-supplied PC).
  5. Packs loads into circuits (80% rule unless the user approves going over),
     PC on Circuit 1, dedicated VANTAGE circuit last. Multi-plug devices stay
     on one circuit when they fit; otherwise each plug becomes its own load
     (amperage split evenly, reported as an assumption).
  6. Builds the explicit connection list Mode 2 draws.

Project decisions (overrides, 80% approval, manual circuit moves) live in the
options for this run only and are never written back to the DRD.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from .csv_io import PowerModel, DeviceRow, CircuitRow, ConnectionRow
from .drd import (DRD, HHS_FAMILY, HHS_CONTROL_BOX, HHS_BOX_BASE_A, HHS_BOX_PER_UNIT_A,
                  HHS_BOX_MAX_A, CUSTOMER_PC, split_cables, count_power_plugs,
                  cable_kind, parse_ports)

HHS_UNITS_PER_BOX = int(round((HHS_BOX_MAX_A - HHS_BOX_BASE_A) / HHS_BOX_PER_UNIT_A))
HHC_NAME = "Hamilton Heater Cooler"
PREHEAT_NAME = "Hamilton Pre-Heat Module"

# Main PC port inventory (StyleGuide / Power_Distribution_Analysis_Summary)
PC_PORTS = {
    "AIO PC (All-in-One)": {"usb": 8, "serial": 0, "ethernet": 1},
    "Standard PC": {"usb": 10, "serial": 1, "ethernet": 1},
    "Industrial PC": {"usb": 8, "serial": 6, "ethernet": 2},
    "L3 PC": {"usb": 10, "serial": 0, "ethernet": 3},
}
HUB_FOR_KIND = {"serial": "Serial Hub", "usb": "USB Hub", "ethernet": "NetGear Router"}
ROUTER_NAMES = ("NetGear Router", "Mikrotik Router Switch")

HHS_MODES = ("auto", "control_box", "tcc", "periphery")
HHS_MODE_LABELS = {
    "auto": "Automatic",
    "control_box": "HHS Control Box",
    "tcc": "Microlab STAR TCC ports",
    "periphery": "Microlab VANTAGE Periphery Box",
}


@dataclass
class EngineOptions:
    facility_circuit: str = "15A"          # "15A" / "20A"
    over_80_approved: bool = False         # allow loads up to 100% of the rating
    main_pc: str = ""                      # DRD §3 main PC; "" = from the order, else Customer-supplied
    hhs_mode: str = "auto"
    isolate_vantage: bool = True           # keep a C19 VANTAGE on its own circuit
    project_name: str = ""
    manual_circuits: dict = field(default_factory=dict)   # load key -> circuit number
    styleguide_version: str = ""
    app_version: str = ""


@dataclass
class Unit:
    id: str
    name: str                 # display / DRD name
    rec: dict
    role: str                 # powered / child / pipettor / none
    category: str
    amperage: Optional[float]
    power_cable: str
    comm_cable: str
    source: str
    part_number: str = ""
    parent: str = ""          # parent unit id
    note: str = ""
    pip: object = None        # PipettorChoice for pipettor units
    plugs: int = 0
    extra_load: float = 0.0   # added by children ("Adds xA to ...")
    custom: bool = False

    @property
    def load(self):
        return (self.amperage or 0.0) + self.extra_load


@dataclass
class Load:
    key: str                  # "<unit id>" or "<unit id>#plug2"
    unit: Unit
    amps: float
    plugs: int


@dataclass
class Circuit:
    num: int
    rating: str
    outlet: str
    cap: float
    loads: list = field(default_factory=list)
    dedicated: bool = False

    @property
    def load(self):
        return sum(l.amps for l in self.loads)

    @property
    def connections(self):
        return sum(max(1, l.plugs) for l in self.loads)

    @property
    def power_strip(self):
        return self.connections > 2


@dataclass
class AnalysisResult:
    model: PowerModel
    units: list
    circuits: list
    hub: dict
    hhs: dict
    issues: list              # blocking problems (must fix before export)
    warnings: list
    assumptions: list
    report: str = ""

    @property
    def ok(self):
        return not self.issues


def _fmt(v):
    if v is None:
        return "-"
    f = float(v)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else f"{f:.1f}"


class PowerEngine:
    def __init__(self, drd: DRD, options: EngineOptions):
        self.drd = drd
        self.opt = options
        if not self.opt.styleguide_version:
            try:
                from .drd import styleguide_version
                self.opt.styleguide_version = styleguide_version()
            except Exception:       # version stamp is informational only
                pass

    # ------------------------------------------------------------------
    def analyze(self, state) -> AnalysisResult:
        self.issues, self.warnings, self.assumptions = [], [], []
        self.conns = []
        self.units = []
        self._ids = {}
        self._build_units(state)
        self._resolve_main_pc()
        self.hhs = self._hhs_plan()
        self._child_parents()
        self.hub = self._hub_check()
        self._comm_connections()
        circuits = self._assign_circuits()
        self._power_connections(circuits)
        model = self._build_model(circuits)
        res = AnalysisResult(model, self.units, circuits, self.hub, self.hhs,
                             list(dict.fromkeys(self.issues)),
                             list(dict.fromkeys(self.warnings)),
                             list(dict.fromkeys(self.assumptions)))
        res.report = format_report(res, self)
        return res

    # ------------------------------------------------------------------
    # 1. units
    # ------------------------------------------------------------------
    def _new_id(self, base):
        n = self._ids.get(base, 0) + 1
        self._ids[base] = n
        return base if n == 1 else f"{base} #{n}"

    def _fix_first_ids(self):
        """Give the first unit '#1' when a name has several units."""
        for u in self.units:
            if self._ids.get(u.id, 0) > 1:
                u.id = f"{u.id} #1"

    def _build_units(self, state):
        for p in state.pipettors:
            pid = self._new_id(p.type if len(state.pipettors) == 1 else f"{p.type}")
            amps = p.amperage
            if p.type == "VANTAGE" and p.power in ("L14-20", "L21-20"):
                amps = None
            u = Unit(pid, p.name, self.drd.pipettors.get(p.model, {}), "pipettor",
                     "pipettor", amps, p.power, "", "Pipettor picker", pip=p, plugs=1)
            if p.type == "STAR" and not amps:
                self.issues.append(f"{p.model}: enter the STAR amperage in the pipettor picker.")
            self.units.append(u)
        for d in state.relevant:
            rec = d.custom if d.is_custom else (self.drd.get(d.name) or {})
            if not rec:
                self.issues.append(f"{d.name}: not found in DRD v{self.drd.version}.")
                continue
            role = (rec.get("role") or "").lower()
            if role not in ("powered", "child", "none"):
                if role:
                    self.issues.append(f"{d.name}: DRD role '{rec.get('role_raw', role)}' "
                                       f"is not Powered or Child.")
                else:
                    self.issues.append(f"{d.name}: the DRD has no Role for this device. "
                                       f"Set it in the DRD (or use a custom device).")
                role = "powered"
            for _ in range(max(1, int(d.qty or 1))):
                u = Unit(self._new_id(d.name), d.name, rec, role,
                         rec.get("category") or "off_deck", None,
                         rec.get("power_cable", "") or "", rec.get("comm_cable", "") or "",
                         d.source, ", ".join(d.lines), note=d.note, custom=d.is_custom)
                if role == "powered":
                    u.plugs = count_power_plugs(u.power_cable)
                    u.amperage = self._amperage(d, rec)
                self.units.append(u)
        self._fix_first_ids()

    def _amperage(self, d, rec):
        if d.amp_override is not None:
            if rec.get("amperage") is not None and abs(rec["amperage"] - d.amp_override) > 1e-9:
                self.assumptions.append(f"{d.name}: using {_fmt(d.amp_override)}A entered by "
                                        f"the user (DRD lists {_fmt(rec['amperage'])}A).")
            return float(d.amp_override)
        if d.name == HHS_CONTROL_BOX:
            return None                 # computed from the units on it
        status = rec.get("amperage_status", "ok")
        a = rec.get("amperage")
        if a is not None and status == "ok":
            return float(a)
        if status == "none" and count_power_plugs(rec.get("power_cable", "")) == 0:
            return None                 # e.g. Serial Hub: no facility power
        raw = rec.get("amperage_raw", "")
        self.issues.append(f"{d.name}: DRD amperage is '{raw or 'blank'}'. "
                           f"Enter an amperage on the review screen.")
        return None

    def by_name(self, name):
        return [u for u in self.units if u.name == name]

    def by_id(self, uid):
        for u in self.units:
            if u.id == uid:
                return u
        return None

    def _conn(self, src, tgt, typ, label="", sport="", tport=""):
        self.conns.append(ConnectionRow(src, sport, tgt, tport, typ, label))

    # ------------------------------------------------------------------
    # 2. main PC
    # ------------------------------------------------------------------
    def _resolve_main_pc(self):
        mains = [u for u in self.units if u.category == "pc" and u.rec.get("is_main_pc")]
        if self.opt.main_pc:
            if not any(u.name == self.opt.main_pc for u in mains):
                rec = self.drd.get(self.opt.main_pc) or {}
                u = Unit(self._new_id(self.opt.main_pc), self.opt.main_pc, rec, "powered", "pc",
                         rec.get("amperage"), rec.get("power_cable", ""), "",
                         "Selected on review screen", plugs=count_power_plugs(rec.get("power_cable", "")))
                self.units.insert(len([x for x in self.units if x.role == "pipettor"]), u)
                mains = [u] + mains
            mains.sort(key=lambda u: u.name != self.opt.main_pc)
        if not mains:
            rec = self.drd.get(CUSTOMER_PC) or {}
            u = Unit(self._new_id(CUSTOMER_PC), CUSTOMER_PC, rec, "powered", "pc",
                     rec.get("amperage"), rec.get("power_cable", ""), "",
                     "No PC on order", plugs=count_power_plugs(rec.get("power_cable", "")))
            self.units.insert(0, u)
            mains = [u]
            self.assumptions.append("No main PC on the order: using the Customer-supplied PC "
                                    "values from DRD §3.")
        if len(mains) > 1:
            self.warnings.append("More than one main PC: " + ", ".join(m.id for m in mains)
                                 + f". {mains[0].id} is used as the main PC.")
        self.main_pc = mains[0]

    # ------------------------------------------------------------------
    # 3. HHS family
    # ------------------------------------------------------------------
    def _hhs_plan(self):
        units = [u for u in self.units if u.name in HHS_FAMILY]
        boxes = self.by_name(HHS_CONTROL_BOX)
        pips = [u for u in self.units if u.role == "pipettor"]
        star = next((p for p in pips if p.pip.type == "STAR"), None)
        van = next((p for p in pips if p.pip.type == "VANTAGE"), None)
        mode = self.opt.hhs_mode if self.opt.hhs_mode in HHS_MODES else "auto"
        info = {"mode": mode, "units": [u.id for u in units], "boxes": {}}
        if not units:
            for b in boxes:
                b.amperage = HHS_BOX_BASE_A
                info["boxes"][b.id] = []
            if boxes:
                self.warnings.append("HHS Control Box on the order but no HHS / HHC / Pre-Heat "
                                     "units in the Relevant list.")
            info["mode"] = "none"
            return info
        if mode == "auto":
            mode = "control_box" if boxes else ("tcc" if star else ("periphery" if van else "control_box"))
            self.assumptions.append(f"HHS connection method: {HHS_MODE_LABELS[mode]} (automatic).")
        info["mode"] = mode
        if mode == "control_box":
            if not boxes:
                self.issues.append("HHS units need an HHS Control Box but none is in the Relevant "
                                   "list. Add one, or pick TCC / Periphery Box.")
                return info
            cap_units = HHS_UNITS_PER_BOX * len(boxes)
            if len(units) > cap_units:
                self.issues.append(f"{len(units)} HHS-family units but {len(boxes)} HHS Control "
                                   f"Box(es) hold at most {cap_units}. Add a box.")
            # HHC first so a box's USB unit is an HHC when possible
            ordered = sorted(units, key=lambda u: (u.name != HHC_NAME, u.name == PREHEAT_NAME))
            per = [[] for _ in boxes]
            for i, u in enumerate(ordered):
                per[min(i // HHS_UNITS_PER_BOX, len(boxes) - 1)].append(u)
            for b, us in zip(boxes, per):
                b.extra_load = 0.0
                b.amperage = HHS_BOX_BASE_A + HHS_BOX_PER_UNIT_A * len(us)
                if b.amperage > HHS_BOX_MAX_A + 1e-9:
                    self.issues.append(f"{b.id} would draw {_fmt(b.amperage)}A (max "
                                       f"{_fmt(HHS_BOX_MAX_A)}A).")
                info["boxes"][b.id] = [u.id for u in us]
                usb_done = False
                for u in us:
                    u.parent = b.id
                    if u.name == PREHEAT_NAME:
                        self._conn(u.id, b.id, "power", "Power via HHS Control Box")
                        continue
                    pair = dict(self.drd.parents_of(u.name)).get(HHS_CONTROL_BOX) or ["RS-232 DB9"]
                    self._conn(u.id, b.id, "serial", pair[0])
                    if not usb_done:
                        usb = next((c for c in split_cables(u.comm_cable) if cable_kind(c) == "usb"),
                                   "USB")
                        self._conn(u.id, self.main_pc.id, "usb", usb)
                        usb_done = True
            return info
        # direct to a pipettor
        pip = star if mode == "tcc" else van
        if pip is None:
            self.issues.append(f"HHS method '{HHS_MODE_LABELS[mode]}' needs a "
                               f"{'STAR' if mode == 'tcc' else 'VANTAGE'} pipettor.")
            return info
        for b in boxes:
            b.amperage = HHS_BOX_BASE_A
            self.warnings.append(f"{b.id} is on the order but HHS units connect through "
                                 f"{HHS_MODE_LABELS[mode]}. Move it to 'Not part of diagram' "
                                 f"if it is not used.")
        ports = ["TCC1", "TCC2"] if mode == "tcc" else ["High", "Medium", "Low", "General"]
        if len(units) > len(ports):
            self.issues.append(f"{len(units)} HHS-family units but only {len(ports)} "
                               f"{'TCC' if mode == 'tcc' else 'Periphery'} ports.")
        if mode == "periphery" and len(units) > 2:
            self.warnings.append("DRD §2 lists High or Medium for HHS on a VANTAGE; extra "
                                 "units were put on Low / General. Check this.")
        for i, u in enumerate(units):
            u.parent = pip.id
            port = ports[i] if i < len(ports) else ports[-1]
            self._conn(u.id, pip.id, "periphery", "TCC adapter" if mode == "periphery" else "TCC",
                       tport=port)
            if mode == "periphery" and u.name != PREHEAT_NAME:
                usb = next((c for c in split_cables(u.comm_cable) if cable_kind(c) == "usb"), "USB")
                self._conn(u.id, pip.id, "usb", usb, tport=f"USB A{'' if i == 0 else i + 1}")
        info["pipettor"] = pip.id
        return info

    # ------------------------------------------------------------------
    # 4. children, links, daisy chains, pipettor ports
    # ------------------------------------------------------------------
    def _child_parents(self):
        used_ports = {}
        pips = {u.pip.type: u for u in self.units if u.role == "pipettor"}
        for u in self.units:
            if u.name in HHS_FAMILY or u.role == "pipettor":
                continue
            if u.role == "child":
                self._attach_child(u)
            # §2 pipettor connections (required ones are drawn)
            for ptype, pu in pips.items():
                pc = self.drd.pipettor_connection(u.name, ptype)
                if pc and pc.get("required"):
                    self._pipettor_ports(u, pu, pc.get("cables", ""), used_ports, "periphery",
                                         self._first_cable(u))
            for pp in self.drd.pipettor_pairing(u.name):
                pu = pips.get(pp.get("pipettor"))
                if pu and not any(c.source == u.id and c.target == pu.id for c in self.conns):
                    self.warnings.append(f"{u.id}: DRD §7 connects it to the {pu.pip.type}; "
                                         f"no port is named, check the diagram.")
        # adds_load ("Adds 1A to the HHS Control Box") for children not handled by HHS logic
        for u in self.units:
            al = u.rec.get("adds_load") or None
            if not al or u.name in HHS_FAMILY:
                continue
            tgt = self.by_id(u.parent) if u.parent else None
            if tgt is None or tgt.name != al.get("to"):
                cands = self.by_name(al.get("to", ""))
                tgt = cands[0] if cands else None
            if tgt is None:
                self.warnings.append(f"{u.id} adds {_fmt(al['amps'])}A to the {al.get('to')}, "
                                     f"which is not in the system.")
            else:
                tgt.extra_load += float(al["amps"])
        self._links()
        self._daisy_chains()

    def _first_cable(self, u):
        cs = split_cables(u.comm_cable)
        return cs[0] if cs else ""

    def _pipettor_ports(self, u, pu, text, used, typ, label):
        ports, mode = parse_ports(text, pu.pip.type)
        if not ports:
            self.warnings.append(f"{u.id}: could not read a {pu.pip.type} port from "
                                 f"'{text}' (DRD §2).")
            return
        taken = used.setdefault(pu.id, set())
        if mode == "any":
            free = [p for p in ports if p not in taken]
            ports = [free[0] if free else ports[0]]
        for p in ports:
            taken.add(p)
            self._conn(u.id, pu.id, typ, label or text, tport=p)

    def _attach_child(self, u):
        parents = [p for p, _c in self.drd.parents_of(u.name)]
        present = [p for p in parents if self.by_name(p)]
        if present:
            pname = present[0]
            cons = dict(self.drd.parents_of(u.name))[pname] or [""]
            # spread children over parent units round-robin
            pus = self.by_name(pname)
            siblings = [x for x in self.units if x.parent and self.by_id(x.parent)
                        and self.by_id(x.parent).name == pname and x.name == u.name]
            parent = pus[len(siblings) % len(pus)]
            u.parent = parent.id
            for c in cons:
                if not c or c == "-":
                    continue
                self._conn(u.id, parent.id, cable_kind(c) or "periphery", c)
            if all((not c or c == "-") for c in cons):
                self._conn(u.id, parent.id, "periphery", "")
            return
        # no §7 parent present: a powered kit mate (shares a part number) is the parent
        mates = [m for m in self.drd.kit_mates(u.name)
                 if (self.drd.get(m) or {}).get("role") == "powered" and self.by_name(m)]
        if mates:
            parent = self.by_name(mates[0])[0]
            u.parent = parent.id
            cables = split_cables(u.comm_cable) or [""]
            for c in cables:
                self._conn(u.id, parent.id, cable_kind(c) or "periphery", c)
            self.assumptions.append(f"{u.id}: connected to {parent.id} (same kit part number).")
            return
        pip_ok = any(c.source == u.id for c in self.conns)
        if not pip_ok and not any(self.drd.pipettor_connection(u.name, t) for t in ("STAR", "VANTAGE")):
            if parents:
                self.issues.append(f"{u.id} is a Child device but its parent "
                                   f"({' / '.join(parents)}) is not in the Relevant list.")
            else:
                self.warnings.append(f"{u.id} is a Child device with no parent in DRD §7.")

    def _links(self):
        done = set()
        for u in self.units:
            for partner, cons in self.drd.links_of(u.name):
                ps = self.by_name(partner)
                if not ps:
                    continue
                key = tuple(sorted((u.name, partner)))
                if key in done:
                    continue
                done.add(key)
                a_units = self.by_name(u.name)
                for i, a in enumerate(a_units):
                    b = ps[i % len(ps)]
                    for c in cons or [""]:
                        self._conn(a.id, b.id, cable_kind(c) or "periphery", c)
                        a.__dict__.setdefault("_link_kinds", set()).add(cable_kind(c))
                        b.__dict__.setdefault("_link_kinds", set()).add(cable_kind(c))

    def _daisy_chains(self):
        seen = set()
        for u in self.units:
            if u.name in seen:
                continue
            cons = self.drd.daisy_chain(u.name)
            if not cons:
                continue
            seen.add(u.name)
            chain = self.by_name(u.name)
            for a, b in zip(chain, chain[1:]):
                for c in cons:
                    self._conn(a.id, b.id, cable_kind(c) or "periphery", c)
                b.__dict__["_chained"] = True

    # ------------------------------------------------------------------
    # 5. hubs + comm to the PC
    # ------------------------------------------------------------------
    def _pc_bound_cables(self, u):
        """Comm cables of a unit that go to the PC (or a hub)."""
        if u.role != "powered" or u.category in ("pc", "hub"):
            return []
        if u.__dict__.get("_chained"):
            return []
        link_kinds = u.__dict__.get("_link_kinds", set())
        out = []
        for c in split_cables(u.comm_cable):
            k = cable_kind(c)
            if not k or k == "power" or k in link_kinds:
                continue
            if c.lower().startswith("see "):
                continue
            out.append((c, k))
        return out

    def _hub_check(self):
        pc = self.main_pc
        need = {"usb": 0, "serial": 0, "ethernet": 0}
        for u in self.units:
            for _c, k in self._pc_bound_cables(u):
                if k in need:
                    need[k] += 1
        for u in self.units:
            if u.role == "pipettor":
                if u.pip.type == "STAR" or self._uses_periphery_box(u):
                    need["usb"] += 1
                if u.pip.type == "VANTAGE":
                    need["ethernet"] += 1
        need["usb"] += sum(1 for c in self.conns if c.type == "usb" and c.target == pc.id)
        hubs = {u.name: u for u in self.units if u.category == "hub"}
        res = {"pc": pc.name, "need": need, "added": [], "detail": ""}
        if pc.name == CUSTOMER_PC or pc.name not in PC_PORTS:
            res["detail"] = (f"{pc.name}: port counts unknown, no hub is added automatically.")
            return res
        ports = PC_PORTS[pc.name]
        res["ports"] = ports
        notes = []
        for kind in ("serial", "usb", "ethernet"):
            have_hub = any(self._hub_kind(h) == kind for h in hubs)
            if need[kind] > ports[kind] and not have_hub:
                name = HUB_FOR_KIND[kind]
                rec = self.drd.get(name) or {}
                u = Unit(self._new_id(name), name, rec, rec.get("role", "powered") or "powered",
                         "hub", rec.get("amperage") if rec.get("amperage_status") == "ok" else None,
                         rec.get("power_cable", ""), rec.get("comm_cable", ""), "Added by hub check",
                         plugs=count_power_plugs(rec.get("power_cable", "")))
                self.units.append(u)
                hubs[name] = u
                res["added"].append(name)
                notes.append(f"{need[kind]} {kind} connections, the {pc.name} has "
                             f"{ports[kind]}: added a {name}.")
        if not notes:
            notes.append(f"The {pc.name} has enough ports (USB {ports['usb']}, serial "
                         f"{ports['serial']}, RJ45 {ports['ethernet']}).")
        res["detail"] = " ".join(notes)
        return res

    # VANTAGE ports that are NOT part of the Periphery Box
    _NON_PERIPHERY_PORTS = {"Ext RJ45", "Ext USB B", "power", "P SKT"}

    def _uses_periphery_box(self, u):
        """True when any connection lands on a Periphery Box port of VANTAGE u."""
        for c in self.conns:
            for end, port in ((c.target, c.target_port), (c.source, c.source_port)):
                if end == u.id and port and port not in self._NON_PERIPHERY_PORTS \
                        and not port.startswith("PWR"):
                    return True
        return False

    @staticmethod
    def _hub_kind(name):
        low = name.lower()
        if "serial" in low:
            return "serial"
        if "usb" in low:
            return "usb"
        if "router" in low or "switch" in low:
            return "ethernet"
        return ""

    def _comm_connections(self):
        pc = self.main_pc
        hubs = {self._hub_kind(u.name): u for u in self.units if u.category == "hub"}
        for h in hubs.values():
            c = self._first_cable(h) or "USB"
            if self._hub_kind(h.name) == "ethernet":
                c = "Ethernet"
            self._conn(h.id, pc.id, cable_kind(c) or "usb", c)
        for u in self.units:
            if u.role == "pipettor":
                if u.pip.type == "STAR":
                    self._conn(pc.id, u.id, "usb", "USB A to B", tport="USB B")
                else:
                    tgt = hubs.get("ethernet", pc)
                    self._conn(tgt.id, u.id, "ethernet", "Ethernet", tport="Ext RJ45")
                    # StyleGuide 6.2: the external USB to the PC is only needed when
                    # something is connected to the Periphery Box.
                    if self._uses_periphery_box(u):
                        self._conn(pc.id, u.id, "usb", "USB A to B", tport="Ext USB B")
                continue
            for c, k in self._pc_bound_cables(u):
                tgt = hubs.get(k, pc)
                self._conn(u.id, tgt.id, k, c)
        # other PCs (device PCs) - only if they have no link: comm to nothing, power only

    # ------------------------------------------------------------------
    # 6. circuits
    # ------------------------------------------------------------------
    def _rating(self):
        return 20 if self.opt.facility_circuit.startswith("20") else 15

    def _cap(self):
        r = self._rating()
        return float(r) if self.opt.over_80_approved else r * 0.8

    def _loads(self):
        loads = []
        cap = self._cap()
        for u in self.units:
            if u.role not in ("powered",) or u.category == "pipettor":
                continue
            if u.plugs == 0:
                continue
            if u.parent:            # powered through another device
                continue
            amps = u.load
            if amps > cap + 1e-9 and u.plugs > 1:
                share = amps / u.plugs
                for i in range(u.plugs):
                    loads.append(Load(f"{u.id}#plug{i + 1}", u, share, 1))
                self.assumptions.append(f"{u.id}: {_fmt(amps)}A is over one circuit, so its "
                                        f"{u.plugs} plugs are spread over circuits at "
                                        f"{_fmt(share)}A each (even split assumed).")
            else:
                if amps > cap + 1e-9:
                    self.issues.append(f"{u.id} draws {_fmt(amps)}A, more than one "
                                       f"{self._rating()}A circuit allows ({_fmt(cap)}A).")
                loads.append(Load(u.id, u, amps, u.plugs))
        return loads

    def _merge_circuits(self, circuits):
        rating = float(self._rating())
        while True:
            best = None
            nums = [n for n in sorted(circuits) if not circuits[n].dedicated]
            for i, a in enumerate(nums):
                for b in nums[i + 1:]:
                    tot = circuits[a].load + circuits[b].load
                    if tot > rating + 1e-9:
                        continue
                    has_pc = any(l.unit is self.main_pc for l in circuits[a].loads + circuits[b].loads)
                    score = (has_pc, tot)
                    if best is None or score < best[0]:
                        best = (score, a, b)
            if best is None:
                break
            _s, a, b = best
            circuits[a].loads.extend(circuits[b].loads)
            circuits[a].cap = rating
            del circuits[b]
        self._renumber(circuits)

    def _new_circuit(self, num):
        rating = self._rating()
        outlet = "120V NEMA 5-20R" if rating == 20 else "120V NEMA 5-15R"
        return Circuit(num, f"{rating}A", outlet, rating * 0.8)

    def _make_dedicated(self, c, u):
        rating = self._rating()
        c.dedicated = True
        if u.pip.power == "C19":
            c.cap = float(rating)
            c.rating, c.outlet = f"{rating}A", f"120V {'NEMA 5-20R' if rating == 20 else 'NEMA 5-15R'}"
        else:
            c.rating, c.outlet, c.cap = "20A", f"208V {u.pip.power}R", 20.0

    def _is_dedicated_pip(self, u):
        return u.pip.type == "VANTAGE" and (u.pip.power != "C19" or self.opt.isolate_vantage)

    @staticmethod
    def _renumber(circuits):
        for new_num, old in enumerate(sorted(circuits), start=1):
            if new_num != old:
                c = circuits.pop(old)
                c.num = new_num
                circuits[new_num] = c

    def _fixed_circuits(self, loads, manual):
        """User-edited layout: every load stays on the circuit it was moved to."""
        circuits = {}

        def get(num):
            if num not in circuits:
                circuits[num] = self._new_circuit(num)
            return circuits[num]

        ded = set()
        for u in self.units:
            if u.role == "pipettor":
                loads.append(Load(u.id, u, u.amperage or 0.0, 1))
                if self._is_dedicated_pip(u):
                    ded.add(u.id)
        known = {l.key for l in loads}
        gone = [k for k in manual if k not in known]
        if gone:
            self.warnings.append("Circuit moves dropped, these loads are no longer in the analysis: "
                                 + ", ".join(gone))
        rest = []
        for l in loads:
            if l.key in manual:
                get(manual[l.key]).loads.append(l)
                if l.key in ded:
                    self._make_dedicated(circuits[manual[l.key]], l.unit)
            else:
                rest.append(l)
        for l in sorted(rest, key=lambda l: -l.amps):
            if l.key in ded:
                c = get(max(circuits, default=0) + 1)
                self._make_dedicated(c, l.unit)
            else:
                fit = [n for n in sorted(circuits) if not circuits[n].dedicated
                       and circuits[n].load + l.amps <= circuits[n].cap + 1e-9]
                c = get(fit[0] if fit else max(circuits, default=0) + 1)
            c.loads.append(l)
            self.warnings.append(f"{l.key}: not in the edited circuit layout, placed automatically.")
        self._renumber(circuits)
        for c in circuits.values():
            if c.dedicated and len(c.loads) > 1:
                pip = next(l.key for l in c.loads if l.key in ded)
                self.issues.append(f"Circuit {c.num} is dedicated to {pip}; move the other "
                                   f"load(s) off it or move {pip} to its own circuit.")
        return circuits

    def _assign_circuits(self):
        manual = {k: int(v) for k, v in (self.opt.manual_circuits or {}).items()}
        if manual:
            circuits = self._fixed_circuits(self._loads(), manual)
        else:
            circuits = self._auto_circuits()
        return self._check_circuits([circuits[k] for k in sorted(circuits)])

    def _auto_circuits(self):
        loads = self._loads()
        circuits = {}

        def get(num):
            if num not in circuits:
                circuits[num] = self._new_circuit(num)
            return circuits[num]

        rest = []
        for l in loads:
            if l.unit is self.main_pc:
                get(1).loads.append(l)
            else:
                rest.append(l)
        # pipettor (STAR / C19 VANTAGE that is not isolated) packs like a load
        for u in self.units:
            if u.role == "pipettor" and not self._is_dedicated_pip(u):
                rest.append(Load(u.id, u, u.amperage or 0.0, 1))
        # first-fit decreasing, grouped by kind, preferring circuits with <= 2 plugs
        parents = {u.parent for u in self.units if u.parent}

        def grp(l):
            # layout groups: PC/pipettor, stand-alone devices, control boxes
            # (anything other units hang off), on-deck devices
            if l.unit.role == "pipettor":
                return 0
            if l.unit.id in parents:
                return 3
            return {"pc": 1, "hub": 1, "off_deck": 2, "on_deck": 4}.get(l.unit.category, 2)
        for l in sorted(rest, key=lambda l: (grp(l), -l.amps)):
            best = None
            for num in sorted(circuits):
                c = circuits[num]
                if c.dedicated or c.load + l.amps > c.cap + 1e-9:
                    continue
                score = (c.connections + l.plugs > 2, not any(grp(x) == grp(l) for x in c.loads),
                         -(c.load + l.amps))
                if best is None or score < best[0]:
                    best = (score, num)
            num = best[1] if best else (max(circuits) + 1 if circuits else 1)
            get(num).loads.append(l)
        # Going over 80% (approved): pack at 80% first, then merge the two
        # lightest circuits while the total stays within the rating. Circuits
        # holding the main PC are merged last.
        if self.opt.over_80_approved:
            self._merge_circuits(circuits)
        # dedicated / isolated VANTAGE circuits, numbered last
        for u in self.units:
            if u.role != "pipettor" or not self._is_dedicated_pip(u):
                continue
            c = get(max(circuits) + 1 if circuits else 1)
            self._make_dedicated(c, u)
            c.loads.append(Load(u.id, u, u.amperage or 0.0, 1))
        return circuits

    def _check_circuits(self, out):
        for c in out:
            if c.load > c.cap + 1e-9 and not c.dedicated:
                if c.load <= self._rating() + 1e-9 and not self.opt.over_80_approved:
                    self.issues.append(f"Circuit {c.num}: {_fmt(c.load)}A is over 80% of "
                                       f"{c.rating}. Approve going over 80% or move a load.")
                elif c.load > self._rating() + 1e-9 and not c.dedicated:
                    self.issues.append(f"Circuit {c.num}: {_fmt(c.load)}A is over the "
                                       f"{c.rating} rating.")
        if self.opt.over_80_approved:
            for c in out:
                if c.load > self._rating() * 0.8 + 1e-9 and not c.dedicated:
                    self.assumptions.append(f"Circuit {c.num} runs at {_fmt(c.load)}A, over 80% "
                                            f"(approved by the user).")
        return out

    def _power_connections(self, circuits):
        for c in circuits:
            src = f"Power Strip {c.num}" if c.power_strip else f"Circuit {c.num}"
            if c.power_strip:
                self._conn(src, f"Circuit {c.num}", "power", "")
            for l in c.loads:
                u = l.unit
                if u.role == "pipettor":
                    port = "C13" if u.pip.type == "STAR" else "power"
                    self._conn(src, u.id, "power", u.power_cable, tport=port)
                    continue
                cables = split_cables(u.power_cable) or [u.power_cable]
                for _ in range(max(1, l.plugs)):
                    self._conn(src, u.id, "power", re.sub(r"^\s*\d+\s*x\s*", "", cables[0]))
            for l in c.loads:
                l.unit.__dict__.setdefault("_circuits", []).append(str(c.num))

    # ------------------------------------------------------------------
    # 7. model
    # ------------------------------------------------------------------
    def _build_model(self, circuits):
        pips = [u for u in self.units if u.role == "pipettor"]
        meta = {
            "schema_version": "2.0",
            "project_name": self.opt.project_name,
            "facility_circuit": f"{self._rating()}A",
            "pipettor": "+".join(p.pip.type for p in pips),
            "pipettors": "; ".join(f"{p.id}={p.pip.name}|{p.pip.type}|{p.pip.power}|"
                                   f"{_fmt(p.amperage) if p.amperage else ''}" for p in pips),
            "vantage_phase": next((p.pip.power for p in pips if p.pip.type == "VANTAGE"), ""),
            "pc_type": self.main_pc.name,
            "main_pc_id": self.main_pc.id,
            "hhs_mode": self.hhs.get("mode", ""),
            "over_80_approved": "yes" if self.opt.over_80_approved else "no",
            "drd_version": self.drd.version,
            "styleguide_version": self.opt.styleguide_version,
            "generated_by": f"WireDiagram App {self.opt.app_version}".strip(),
            "generated_on": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        rows = []
        for u in self.units:
            rec = u.rec or {}
            circ = ",".join(u.__dict__.get("_circuits", []))
            powered = bool(circ)
            reason = ""
            if not powered:
                if u.role == "child":
                    reason = f"Child of {u.parent}" if u.parent else "Child device"
                elif u.parent:
                    reason = f"Powered through {u.parent}"
                elif u.plugs == 0:
                    reason = "No facility power"
            rows.append(DeviceRow(
                id=u.id, name=u.name, drd_name=u.pip.model if u.pip else u.name, part_number=u.part_number, qty=1,
                amperage=(u.load if u.role != "child" else None) if u.load or u.amperage else None,
                plugs=u.plugs if powered else 0,
                power_cable=u.power_cable, comm_cable=u.comm_cable,
                category=u.category, role=u.role, parent=u.parent,
                section=rec.get("section", "custom" if u.custom else ""),
                width=rec.get("width"), height=rec.get("height"),
                circuit=circ, source=u.source, powered=powered, unpowered_reason=reason,
                notes=u.note,
                connect_power_to=(f"Circuit {circ}" if powered else u.parent),
                connect_comm_to="",
            ))
        crow = []
        for c in circuits:
            head = (c.cap if c.dedicated else self._rating()) - c.load
            crow.append(CircuitRow(str(c.num), c.rating, c.outlet, round(c.load, 2),
                                   round(head, 2), "Yes" if c.power_strip else "No",
                                   "; ".join(dict.fromkeys(l.unit.id for l in c.loads))))
        return PowerModel(meta, rows, crow, list(self.conns))


# --------------------------------------------------------------------------
def format_report(res: AnalysisResult, eng: PowerEngine = None) -> str:
    m = res.model
    L = []
    L.append(f"POWER DISTRIBUTION ANALYSIS - {m.meta.get('project_name') or 'Project'}")
    L.append(f"DRD v{m.meta.get('drd_version')}  |  StyleGuide v{m.meta.get('styleguide_version') or '?'}"
             f"  |  {m.meta.get('facility_circuit')} circuits  |  {m.meta.get('generated_on')}")
    L.append("")
    L.append("1. POWERED DEVICES (on facility circuits)")
    for d in m.devices:
        if d.powered:
            amp = "dedicated" if (d.role == "pipettor" and d.amperage is None) else f"{_fmt(d.amperage)}A"
            L.append(f"   {d.id:<40} {amp:>9}  {d.plugs} plug(s)  "
                     f"{d.power_cable}  -> Circuit {d.circuit}")
    L.append("")
    L.append("2. ON THE DIAGRAM, NO CIRCUIT")
    for d in m.devices:
        if not d.powered:
            L.append(f"   {d.id:<40} {d.unpowered_reason}")
    L.append("")
    L.append(f"3. HHS CONNECTION METHOD: {HHS_MODE_LABELS.get(res.hhs.get('mode'), res.hhs.get('mode'))}")
    for b, us in res.hhs.get("boxes", {}).items():
        L.append(f"   {b}: {len(us)} unit(s) -> {_fmt(HHS_BOX_BASE_A + HHS_BOX_PER_UNIT_A * len(us))}A")
    L.append("")
    L.append("4. HUB CHECK")
    L.append("   " + (res.hub.get("detail") or ""))
    L.append("")
    L.append("5. DISTRIBUTION")
    L.append(f"   {'Circuit':<8} {'Rating':<7} {'Outlet':<30} {'Load':>6} {'Headroom':>9}  Strip  Devices")
    for c in m.circuits:
        L.append(f"   {c.circuit:<8} {c.rating:<7} {c.voltage_outlet:<30} {_fmt(c.load_a):>5}A "
                 f"{_fmt(c.headroom_a):>8}A  {c.power_strip:<5}  {c.devices}")
    for title, items in (("6. MUST FIX BEFORE EXPORT", res.issues),
                         ("7. WARNINGS", res.warnings), ("8. ASSUMPTIONS", res.assumptions)):
        if items:
            L.append("")
            L.append(title)
            L.extend(f"   - {x}" for x in items)
    return "\n".join(L)
