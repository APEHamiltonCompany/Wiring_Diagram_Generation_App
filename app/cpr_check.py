"""
cpr_check.py - optional "SO vs CPR" check for the Review tab.

Given a CPR .docx (devices + system options) and the loaded Sales Order lines,
it reports every CPR device that is not on the SO, flags quantity disagreements
so the user can pick the final number, and lists the powered system options
(instrument, HEPA/UV/CAP, AC box, logistics cabinet, fans) that need power data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .so_cpr_check.models import CprData, CprDevice, ResultRow, SoLine
from .so_cpr_check.compare import compare
from .so_cpr_check.system_rules import PLATFORM_PATTERNS

OTP_RE = re.compile(r"OTP-[A-Z0-9]+(?:-[A-Z0-9]+)+", re.I)
PLAIN_PART_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]*\d[A-Z0-9.\-]*$", re.I)

POWERED_OPTION_RULES = [
    ("HEPA/UV/CAP", r"\bHEPA\b|\bUV\b|\bCAP\b"),
    ("AC box", r"\bAC\s*Box\b"),
    ("Logistics cabinet", r"\bLogistics\s+Cabinet\b"),
    ("Fans", r"\bFans?\b"),
]


def normalize_otp(text):
    out = []
    for match in OTP_RE.findall(text or ""):
        match = match.upper()
        if match not in out:
            out.append(match)
    return out


def _pn_key(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def plain_part_number(text):
    text = (text or "").strip()
    if PLAIN_PART_RE.match(text):
        return text.upper()
    return ""


@dataclass
class CprDeviceCount:
    otp: str = ""
    name: str = ""
    availability: str = ""
    cpr_qty: int = 0
    so_qty: float = 0


@dataclass
class QtyConflict:
    key: str
    part_number: str
    description: str
    so_qty: int
    cpr_qty: int
    final: Optional[int] = None

    @property
    def resolved(self):
        return self.final is not None


@dataclass
class PoweredOption:
    kind: str
    label: str
    upgrade: bool = False


@dataclass
class CprCheck:
    cpr_path: str = ""
    rows: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    powered: list = field(default_factory=list)
    family: str = ""
    upgrade: bool = False

    def unresolved(self):
        return [c for c in self.conflicts if not c.resolved]

    def find_conflict(self, key):
        for c in self.conflicts:
            if c.key == key:
                return c
        return None


def powered_options(system):
    if system is None:
        return []
    out = []
    used = set()
    for option in system.options:
        if option.checked is True and option.group == "Platform" and option.label in PLATFORM_PATTERNS:
            out.append(PoweredOption("Instrument", f"{system.family} {option.label}", system.upgrade))
            used.add(id(option))
    for option in system.options:
        if option.checked is not True:
            continue
        if id(option) in used:
            continue
        if re.search(r"\bnone\b", option.label, re.IGNORECASE):
            continue
        for kind, pattern in POWERED_OPTION_RULES:
            if re.search(pattern, option.label, re.IGNORECASE):
                out.append(PoweredOption(kind, f"{option.group}: {option.label}", system.upgrade))
                break
    return out


def so_lines_from_items(items):
    out = []
    for i, item in enumerate(items):
        out.append(SoLine(row=i + 1, product_number=item.part_number or "",
                          product_name=item.name or "", quantity=float(item.qty or 1),
                          unit="", unit_price=0.0))
    return out


def check_data(cpr, so_lines, cpr_path=""):
    counts = {}
    for device in cpr.devices:
        name = f"{device.vendor} {device.model_name}".strip()
        otps = normalize_otp(device.otp_number)
        if not otps:
            plain = plain_part_number(device.otp_number)
            if plain:
                otps = [plain]
        if otps:
            for otp in otps:
                count = counts.setdefault(otp, CprDeviceCount(otp=otp, name=name,
                                                              availability=device.availability))
                count.cpr_qty += getattr(device, 'quantity', 1) or 1
        else:
            key = "NOOTP:" + name.upper()
            count = counts.setdefault(key, CprDeviceCount(otp="", name=name,
                                                          availability=device.availability))
            count.cpr_qty += getattr(device, 'quantity', 1) or 1
    for count in counts.values():
        if not count.otp:
            continue
        count.so_qty = sum(line.quantity for line in so_lines
                           if _pn_key(line.product_number) == _pn_key(count.otp))
    devices = []
    for count in counts.values():
        if not count.otp:
            continue
        parts = count.name.split(None, 1) or [""]
        devices.append(CprDevice(count.otp, parts[0], parts[1] if len(parts) > 1 else "",
                                 count.availability))
    deduped = CprData(parts=cpr.parts, devices=devices, spec_names=cpr.spec_names,
                      system=cpr.system)
    rows, warnings = compare(so_lines, deduped)
    missing = []
    conflicts = []
    new_rows = []
    for i, count in enumerate(counts.values(), 1):
        if count.so_qty == 0:
            row = ResultRow("Device",
                            cpr_sk_pn=count.otp or "(no OTP #)",
                            cpr_part_name=count.name,
                            cpr_qty=count.cpr_qty,
                            status="Only in CPR",
                            note=f"Not on SO - add to SO (CPR availability: {count.availability})")
            missing.append(count)
        elif count.so_qty == count.cpr_qty:
            row = ResultRow("Device",
                            cpr_sk_pn=count.otp,
                            cpr_part_name=count.name,
                            cpr_qty=count.cpr_qty,
                            so_product_number=count.otp,
                            so_qty=count.so_qty,
                            status="Match",
                            note="Matched on OTP #")
        else:
            row = ResultRow("Device",
                            cpr_sk_pn=count.otp,
                            cpr_part_name=count.name,
                            cpr_qty=count.cpr_qty,
                            so_product_number=count.otp,
                            so_qty=count.so_qty,
                            status="Qty mismatch",
                            note=f"CPR lists {count.cpr_qty}, SO has {count.so_qty:g} - "
                                 "pick the final quantity")
            conflicts.append(QtyConflict(key=f"Q{i}", part_number=count.otp,
                                         description=count.name,
                                         so_qty=int(round(count.so_qty)),
                                         cpr_qty=count.cpr_qty))
        new_rows.append(row)
    dropped = [i for i, r in enumerate(rows) if r.type == "Device" and r.cpr_sk_pn]
    if dropped:
        pos = dropped[0]
        for i in reversed(dropped):
            del rows[i]
    else:
        pos = next((i for i, r in enumerate(rows) if r.type == "System"), len(rows))
    rows[pos:pos] = new_rows
    powered = powered_options(cpr.system)
    return CprCheck(cpr_path=cpr_path, rows=rows, warnings=warnings,
                    devices=list(counts.values()), missing=missing, conflicts=conflicts,
                    powered=powered,
                    family=cpr.system.family if cpr.system else "",
                    upgrade=cpr.system.upgrade if cpr.system else False)


def run_check(cpr_path, so_lines):
    try:
        from .so_cpr_check.cpr_reader import read_cpr
    except ImportError as ex:
        raise RuntimeError("The CPR check needs python-docx: pip install python-docx") from ex
    return check_data(read_cpr(cpr_path), so_lines, cpr_path)
