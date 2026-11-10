"""
review.py - order sorting and the review state (Mode 1, before the analysis).

Replaces the old "prompt for every unknown line" flow:

  1. ``sort_order()`` splits the Sales Order / Quote into two lists, with no
     prompts:
        * relevant  - devices whose part number matches the DRD (kits expand
                      into every DRD record that shares the part number)
        * excluded  - every other line (carriers, tips, services, APE MISC,
                      cables, pipettor/installation lines, ...)
  2. The user moves things between the lists, maps lines to DRD or custom
     devices, adds customer-supplied devices and picks pipettors manually.
  3. ``apply_pairing_rules()`` adds the parent/child devices DRD §7 requires
     (marked "Added by pairing" so the user can see and undo them).

Only part numbers are matched automatically. Descriptions are never used to
guess a device.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from .drd import DRD, HHS_FAMILY, HHS_CONTROL_BOX, PIPETTOR_VARIANTS, norm_part

SRC_ORDER = "Sales Order"
SRC_CUSTOMER = "Customer-supplied"
SRC_PAIRING = "Added by pairing"
SRC_MANUAL = "Mapped by user"
SRC_SAVED = "Saved part # rule"
SRC_CUSTOM = "Custom device"
SRC_CPR = "Not on SO – add to SO"

REASON_CPR_NO_DRD = "From CPR – not on SO, not in DRD"
REASON_CPR_OPTION = "From CPR system options – powered, needs amps"


@dataclass
class OrderLine:
    part_number: str = ""
    description: str = ""
    qty: int = 1


@dataclass
class SelectedDevice:
    key: str
    name: str
    qty: int = 1
    source: str = SRC_ORDER
    lines: list = field(default_factory=list)        # order part numbers it came from
    amp_override: Optional[float] = None
    cpr_amperage: Optional[float] = None
    custom: Optional[dict] = None                    # set for non-DRD devices
    note: str = ""

    @property
    def is_custom(self):
        return self.custom is not None


@dataclass
class ExcludedEntry:
    """Something in the "Not part of diagram" list: either an order line that
    did not match, or a device the user moved out (``device`` is set)."""
    key: str
    line: Optional[OrderLine] = None
    device: Optional[SelectedDevice] = None
    reason: str = ""


@dataclass
class PipettorChoice:
    model: str = "STARline"          # DRD §1 name
    type: str = "STAR"               # STAR / VANTAGE
    power: str = "C13"               # C13 / C19 / L14-20 / L21-20
    amperage: Optional[float] = None
    variant: str = ""                # StyleGuide §6 name (STARlet, STAR-V 2.0, ...)

    @property
    def name(self):
        return self.variant or self.model

    def label(self):
        a = f", {self.amperage:g}A" if self.amperage else ""
        return f"{self.variant or self.type} ({self.power}{a})"


@dataclass
class ReviewState:
    relevant: list = field(default_factory=list)     # SelectedDevice
    excluded: list = field(default_factory=list)     # ExcludedEntry
    pipettors: list = field(default_factory=list)    # PipettorChoice
    suppressed: set = field(default_factory=set)     # names the user removed after auto-add
    log: list = field(default_factory=list)
    _n: int = 0

    def next_key(self, prefix="K"):
        self._n += 1
        return f"{prefix}{self._n}"

    def find(self, key):
        for d in self.relevant:
            if d.key == key:
                return d
        return None

    def find_excluded(self, key):
        for e in self.excluded:
            if e.key == key:
                return e
        return None

    def names(self):
        return {d.name for d in self.relevant}

    def pipettor_types(self):
        return {p.type for p in self.pipettors}


# --------------------------------------------------------------------------
# Pipettor defaults (DRD §1 + project decisions)
# --------------------------------------------------------------------------
def default_pipettor(drd: DRD, model: str, power: str = "", variant: str = "") -> PipettorChoice:
    rec = drd.pipettors.get(model, {})
    ptype = rec.get("type") or ("VANTAGE" if "vantage" in model.lower() else "STAR")
    if variant not in PIPETTOR_VARIANTS.get(ptype, []):
        variant = ""
    if ptype == "STAR":
        return PipettorChoice(model, "STAR", power or "C13", rec.get("amperage") or 6.3, variant)
    power = power or "L14-20"
    amp = 10.0 if power == "C19" else None      # single-phase C19 = 10A; L14/L21 dedicated
    return PipettorChoice(model, "VANTAGE", power, amp, variant)


# --------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------
def _add_relevant(state, name, qty, source, pn, kit_member, agg):
    key = name
    a = agg.setdefault(key, {"single": 0, "kit": 0, "lines": [], "source": source})
    if kit_member:
        a["kit"] = max(a["kit"], qty)
    else:
        a["single"] += qty
    if pn and pn not in a["lines"]:
        a["lines"].append(pn)
    if source != SRC_ORDER:
        a["source"] = source


def sort_order(lines, drd: DRD, overrides=None) -> ReviewState:
    """Split order lines into relevant DRD devices and excluded lines.

    Quantities: a line that matches ONE DRD record adds its quantity (two HHS
    lines of 2 -> 4 HHS). A line whose part number is a kit (matches several
    records) contributes max() rather than a sum, so a control box shared by
    two kit lines is not counted twice. A DRD note like "2x per kit" multiplies
    the kit quantity."""
    state = ReviewState()
    agg = {}
    customs = []
    for li in lines:
        ol = li if isinstance(li, OrderLine) else OrderLine(
            getattr(li, "part_number", ""), getattr(li, "name", ""), getattr(li, "qty", 1))
        rule = overrides.get(ol.part_number) if (overrides and ol.part_number) else None
        if rule and rule.get("action") == "ignore":
            state.excluded.append(ExcludedEntry(state.next_key("X"), line=ol,
                                                reason="Ignored by saved part # rule"))
            continue
        if rule and rule.get("action") == "map":
            names = [n for n in rule.get("devices", []) if drd.get(n)]
            kit = (len(names) + len(rule.get("custom", []))) > 1
            for n in names:
                _add_relevant(state, n, ol.qty, SRC_SAVED, ol.part_number, kit, agg)
            for c in rule.get("custom", []):
                customs.append((copy.deepcopy(c), ol))
            if names or rule.get("custom"):
                continue
        matches = drd.find_part(ol.part_number) if ol.part_number else []
        if not matches:
            state.excluded.append(ExcludedEntry(state.next_key("X"), line=ol,
                                                reason="No DRD part number match"))
            continue
        kit = len(matches) > 1
        for name, _how in matches:
            rec = drd.get(name) or {}
            q = ol.qty * (rec.get("kit_qty", 1) if kit else 1)
            _add_relevant(state, name, q, SRC_ORDER, ol.part_number, kit, agg)

    for name, a in agg.items():
        qty = max(a["single"], a["kit"]) if a["single"] and a["kit"] else (a["single"] or a["kit"])
        state.relevant.append(SelectedDevice(state.next_key("D"), name, max(1, qty),
                                             a["source"], list(a["lines"])))
    for c, ol in customs:
        state.relevant.append(SelectedDevice(state.next_key("D"), c.get("name", ol.description),
                                             ol.qty, SRC_SAVED, [ol.part_number], custom=c))
    _sort_relevant(state, drd)
    return state


def _sort_relevant(state, drd):
    order = {"pc": 0, "hub": 1, "off_deck": 2, "on_deck": 3}

    def k(d):
        rec = drd.get(d.name) or d.custom or {}
        return (order.get(rec.get("category", "off_deck"), 4), d.name.lower())
    state.relevant.sort(key=k)


# --------------------------------------------------------------------------
# User actions
# --------------------------------------------------------------------------
def move_to_excluded(state, key, overrides=None, remember=False, drd=None):
    d = state.find(key)
    if d is None:
        return None
    state.relevant.remove(d)
    if d.source == SRC_PAIRING:
        state.suppressed.add(d.name)
    entry = ExcludedEntry(state.next_key("X"), device=d, reason="Moved out by user")
    state.excluded.append(entry)
    if remember and overrides is not None:
        for pn in d.lines:
            remaining = [x for x in state.relevant if pn in x.lines]
            if remaining:
                overrides.set_map(pn, devices=[x.name for x in remaining if not x.is_custom],
                                  custom=[x.custom for x in remaining if x.is_custom],
                                  note=f"{d.name} removed by user")
            else:
                overrides.set_ignore(pn, note=f"{d.name} removed by user")
    return entry


def restore_excluded_device(state, key):
    e = state.find_excluded(key)
    if e is None or e.device is None:
        return None
    state.excluded.remove(e)
    state.suppressed.discard(e.device.name)
    state.relevant.append(e.device)
    return e.device


def map_excluded_line(state, key, device_names=(), custom=None, overrides=None,
                      remember=False, drd=None):
    """Move an excluded order line to the relevant list as one or more DRD
    devices and/or a custom device."""
    e = state.find_excluded(key)
    if e is None:
        return []
    if e.device is not None and not device_names and custom is None:
        return [restore_excluded_device(state, key)]
    ol = e.line or OrderLine(description=e.device.name if e.device else "")
    state.excluded.remove(e)
    added = []
    for n in device_names:
        added.append(_merge_or_add(state, n, ol.qty, SRC_MANUAL, ol.part_number))
    if custom is not None:
        d = SelectedDevice(state.next_key("D"), custom.get("name") or ol.description,
                           ol.qty, SRC_CUSTOM, [ol.part_number] if ol.part_number else [],
                           custom=dict(custom))
        state.relevant.append(d)
        added.append(d)
    if remember and overrides is not None and ol.part_number:
        overrides.set_map(ol.part_number, devices=list(device_names),
                          custom=[custom] if custom else [],
                          note=f"Mapped from '{ol.description}'")
    if drd is not None:
        _sort_relevant(state, drd)
    return added


def ignore_excluded_line(overrides, entry):
    """Save an "always ignore" rule for an excluded order line."""
    if overrides is not None and entry.line and entry.line.part_number:
        overrides.set_ignore(entry.line.part_number, note=entry.line.description)


def _merge_or_add(state, name, qty, source, pn=""):
    for d in state.relevant:
        if d.name == name and not d.is_custom and d.source == source:
            d.qty += qty
            if pn and pn not in d.lines:
                d.lines.append(pn)
            return d
    d = SelectedDevice(state.next_key("D"), name, max(1, qty), source, [pn] if pn else [])
    state.relevant.append(d)
    state.suppressed.discard(name)
    return d


def add_customer_device(state, name, qty=1, drd=None):
    if drd is not None and drd.get(name) is None:
        raise ValueError(f"'{name}' is not a DRD v{drd.version} device.")
    d = _merge_or_add(state, name, qty, SRC_CUSTOMER)
    if drd is not None:
        _sort_relevant(state, drd)
    return d


def add_custom_device(state, custom: dict, qty=1, drd=None):
    d = SelectedDevice(state.next_key("D"), custom.get("name", "Custom device"), max(1, qty),
                       SRC_CUSTOM, custom=dict(custom))
    state.relevant.append(d)
    if drd is not None:
        _sort_relevant(state, drd)
    return d


# --------------------------------------------------------------------------
# DRD §7 pairing rules
# --------------------------------------------------------------------------
def apply_pairing_rules(state: ReviewState, drd: DRD):
    """Add devices that DRD §7 says must be present. Returns log messages.

    * A Child whose parent(s) are all missing gets its parent added when there
      is exactly one candidate parent.
    * A parent (non-hub, non-PC) whose ONLY §7 child type is missing gets that
      child added (e.g. BioTek 405 -> BioTek Pump), one per parent unit.
    * HHS-family units are handled by the analysis (TCC / Periphery Box /
      HHS Control Box rules) and are skipped here.
    * Anything the user removed after it was auto-added is not re-added.
    Ambiguous cases are reported, never guessed."""
    msgs = []
    changed = True
    guard = 0
    while changed and guard < 5:
        changed = False
        guard += 1
        present = state.names()
        for d in list(state.relevant):
            if d.is_custom or d.name in HHS_FAMILY:
                continue
            rec = drd.get(d.name) or {}
            # child -> parent
            if rec.get("role") == "child":
                parents = [p for p, _c in drd.parents_of(d.name)]
                pip_ok = any(drd.pipettor_connection(d.name, t) for t in state.pipettor_types()) \
                    or any(pp["pipettor"] in state.pipettor_types()
                           for pp in drd.pipettor_pairing(d.name))
                if parents and not (set(parents) & present) and not pip_ok:
                    mates = [m for m in drd.kit_mates(d.name)
                             if (drd.get(m) or {}).get("role") == "powered" and m in present]
                    if mates:
                        continue
                    if len(parents) == 1 and parents[0] not in state.suppressed:
                        n = add_customer_device(state, parents[0], 1)
                        n.source = SRC_PAIRING
                        n.note = f"Parent of {d.name} (DRD §7)"
                        msgs.append(f"Added {parents[0]}: required parent of {d.name} (DRD §7).")
                        changed = True
                        present = state.names()
                    elif len(parents) > 1:
                        msgs.append(f"{d.name} needs one of: {', '.join(parents)} (DRD §7). "
                                    f"Add the right one with 'Add customer-supplied device'.")
            # parent -> its only child type
            if rec.get("role") == "powered" and rec.get("category") not in ("hub", "pc"):
                kids = [c for c, _x in drd.children_of(d.name) if c not in HHS_FAMILY]
                kid_types = list(dict.fromkeys(kids))
                if len(kid_types) == 1:
                    kid = kid_types[0]
                    if kid not in present and kid not in state.suppressed:
                        n = add_customer_device(state, kid, d.qty)
                        n.source = SRC_PAIRING
                        n.note = f"Child of {d.name} (DRD §7)"
                        msgs.append(f"Added {kid} x{d.qty}: paired child of {d.name} (DRD §7).")
                        changed = True
                        present = state.names()
    _sort_relevant(state, drd)
    state.log.extend(msgs)
    return list(dict.fromkeys(msgs))


def pairing_warnings(state: ReviewState, drd: DRD):
    """Devices with §7 partners where none of the partners are present."""
    out = []
    present = state.names()
    for d in state.relevant:
        if d.is_custom:
            continue
        partners = ([p for p, _ in drd.parents_of(d.name)] + [c for c, _ in drd.children_of(d.name)]
                    + [l for l, _ in drd.links_of(d.name)])
        partners = [p for p in partners if p and p != d.name]
        if d.name == HHS_CONTROL_BOX:
            partners = [p for p in partners if p not in HHS_FAMILY] or partners
            if any(x in present for x in HHS_FAMILY):
                continue
        if partners and not (set(partners) & present):
            if d.name in HHS_FAMILY:
                continue
            out.append(f"{d.name}: none of its DRD §7 partners are in the system "
                       f"({', '.join(dict.fromkeys(partners))}).")
    return out
