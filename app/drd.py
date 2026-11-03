"""
drd.py - Device Reference Database loader.

Parses DeviceReferenceDatabase_v*.html into a structured, in-memory device
database and caches it as data/drd.json. The DRD is the SOURCE OF TRUTH for
device role, amperage, cables, dimensions, category (section) and pairings.

Review markup handling
----------------------
The DRD is maintained with review markup:
  * ``<del>``/``<s>`` text is superseded and is IGNORED. ``<del>6.0</del> 7.0``
    imports as ``7.0``.
  * A row whose Device Name cell is empty once struck text is removed is a
    struck (removed / moved) row and is skipped entirely, so moved PCs and
    removed devices never come back as duplicates or ghost devices.
  * Yellow highlighting (``class="highlight-new"``) is presentation only and is
    ignored.

Columns are located by HEADER TEXT, not by position, so adding or re-ordering
DRD columns does not break the import.

Part numbers are the ONLY automatic order-matching key (see ``find_part``).
Device names are used only to cross-reference DRD sections with each other
(e.g. a §7 pairing that says "HHS Ctrl Box" -> the §6 "HHS Control Box").
"""

from __future__ import annotations

import html
import json
import os
import re
from html.parser import HTMLParser
from typing import Optional

# --------------------------------------------------------------------------
# Colour typo mapping (output-only normalisation, unchanged from v1)
# --------------------------------------------------------------------------
COLOR_TYPO_MAP = {
    "#ffff": "#FFFFFF", "#0000": "#000000", "#cccc": "#CCCCCC",
    "#3333": "#333333", "#6666": "#666666", "#dddd": "#DDDDDD",
}
COLOR_TYPO_NOTE = (
    "Colour typo normalisation applied: 4-digit hex codes in the source "
    "documents were expanded to valid 6-digit hex "
    "(#FFFF->#FFFFFF, #0000->#000000, #CCCC->#CCCCCC, "
    "#3333->#333333, #6666->#666666, #dddd->#DDDDDD). "
    "These corrections are applied only in the app's generated output and are "
    "never written back into the source DRD / StyleGuide files."
)


def map_color_typo(color: Optional[str]) -> Optional[str]:
    if not color:
        return color
    c = color.strip()
    if c.lower() in COLOR_TYPO_MAP:
        return COLOR_TYPO_MAP[c.lower()]
    m = re.fullmatch(r"#([0-9a-fA-F]{4})", c)
    if m:
        body = m.group(1)
        return "#" + (body + body)[:6].upper()
    return c


# --------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------
# PC types that can be the system's main PC (StyleGuide §4.1 port table).
# Device-specific PCs in DRD §3 (Celigo PC, Waters PC, ...) are never the main PC.
MAIN_PC_TYPES = ["AIO PC (All-in-One)", "Standard PC", "Industrial PC",
                 "L3 PC", "Customer-supplied PC"]
CUSTOMER_PC = "Customer-supplied PC"

# Devices that follow the StyleGuide §5.3/§5.4 HHS rules (TCC port, VANTAGE
# Periphery Box or HHS Control Box). The Pre-Heat Module is powered identically
# (no comms; adds 1A to the control box like the others).
HHS_FAMILY = ["Hamilton Heater Shaker", "Hamilton Heater Cooler",
              "Hamilton Pre-Heat Module"]
HHS_CONTROL_BOX = "HHS Control Box"
HHS_BOX_BASE_A = 0.3          # StyleGuide §5.3: x + 0.3A
HHS_BOX_PER_UNIT_A = 1.0
HHS_BOX_MAX_A = 8.3           # => 8 units per control box

# Temporary stand-in names used in §7. "BioTek Washer" currently stands for the
# BioTek 405 (user instruction, DRD v1.1.1). Remove once the DRD adds its own
# washer rows.
PAIRING_STANDINS = {"biotek washer": "BioTek 405"}

# Pipettor port inventories (StyleGuide §6). Used to read §2 connection text.
STAR_PORTS = ["TCC1", "TCC2", "Ext. Can", "EX IO", "Div1", "Div2", "Div3",
              "P1", "P2", "Int. Board"]
VANTAGE_PORTS = ["High", "Medium", "Low", "General"]

# Acceptable pipettor names per family (StyleGuide §6.1 / §6.2).
PIPETTOR_VARIANTS = {
    "STAR": ["STAR", "STARlet", "STARplus", "STAR-2", "STARlet-2"],
    "VANTAGE": ["VANTAGE 2.0", "VANTAGE 1.3", "STAR-V 2.0", "STAR-V 1.3"],
}


# --------------------------------------------------------------------------
# HTML table extraction (struck-text aware)
# --------------------------------------------------------------------------
class _TableParser(HTMLParser):
    """Collect every <table> as rows of cells. Each cell is a dict with
    ``text`` (live text, <del>/<s> removed) and ``struck`` (removed text).
    <br> becomes a newline."""

    STRIKE_TAGS = ("del", "s", "strike")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table_sections = []
        self._cur_table = None
        self._cur_row = None
        self._live = None
        self._struck = None
        self._strike_depth = 0
        self._last_h2_id = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h2" and "id" in a:
            self._last_h2_id = a["id"]
        elif tag == "table":
            self._cur_table = []
            self.table_sections.append(self._last_h2_id)
        elif tag == "tr" and self._cur_table is not None:
            self._cur_row = []
        elif tag in ("td", "th") and self._cur_row is not None:
            self._live, self._struck, self._strike_depth = [], [], 0
        elif tag in self.STRIKE_TAGS and self._live is not None:
            self._strike_depth += 1
        elif tag == "br" and self._live is not None:
            (self._struck if self._strike_depth else self._live).append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._live is not None:
            (self._struck if self._strike_depth else self._live).append("\n")

    def handle_endtag(self, tag):
        if tag in self.STRIKE_TAGS and self._live is not None and self._strike_depth:
            self._strike_depth -= 1
        elif tag in ("td", "th") and self._live is not None:
            self._cur_row.append({"text": _clean("".join(self._live)),
                                  "struck": _clean("".join(self._struck))})
            self._live = self._struck = None
        elif tag == "tr" and self._cur_row is not None:
            self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag == "table" and self._cur_table is not None:
            self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data):
        if self._live is not None:
            (self._struck if self._strike_depth else self._live).append(data)


def _clean(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------
# Field parsers
# --------------------------------------------------------------------------
def parse_amperage(raw: str):
    """Return (value|None, status). status is one of:
    ok      - a plain number (e.g. "4", "7.0", "12A")
    none    - "-" / blank: no amperage of its own
    varies  - "Varies": must come from a picker/formula/override
    invalid - anything else ("10-16", "<1", ...): never treated as 0
    """
    r = (raw or "").strip()
    low = r.lower()
    if low in ("", "-", "\u2013", "\u2014", "n/a", "none"):
        return None, "none"
    if "varies" in low or "vary" in low:
        return None, "varies"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*a?", low)
    if m:
        return float(m.group(1)), "ok"
    return None, "invalid"


def parse_dims(raw: str):
    m = re.search(r"(\d+)\s*[x\u00d7]\s*(\d+)", raw or "")
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return f"{w}x{h}", w, h
    return (raw or "").strip(), None, None


def split_part_numbers(raw: str):
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[\n,]", raw)
            if p.strip() and p.strip() not in ("-", "\u2013", "\u2014")]


def norm_part(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("\u2122", "").replace("\u00ae", "")
    s = re.sub(r"\bctrl\b", "control", s)
    s = re.sub(r"[\[\]()]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_cables(spec: str):
    """Split a cable cell into individual cables (newline or comma separated).
    "or" alternatives keep only the first option ("USB ... or Ethernet")."""
    spec = (spec or "").strip()
    if not spec or spec in ("-", "\u2013", "\u2014"):
        return []
    out = []
    for part in re.split(r"[\n,]", spec):
        part = part.strip().lstrip("-").strip()
        if not part:
            continue
        part = re.split(r"\s+or\s+", part)[0].strip()
        if part:
            out.append(part)
    return out


def count_power_plugs(power_cable: str) -> int:
    """Number of facility plugs a power-cable spec implies ("2x C19 ..." -> 2)."""
    spec = (power_cable or "").strip()
    if not spec or spec in ("-",):
        return 0
    m = re.match(r"\s*(\d+)\s*x\b", spec.lower())
    return int(m.group(1)) if m else 1


def cable_kind(cable: str) -> str:
    """Classify one cable string into a connector colour family
    (power / usb / serial / ethernet / periphery). Anything that is not clearly
    one of the first four uses the purple periphery "catch-all"."""
    low = (cable or "").lower()
    if not low or low == "-":
        return ""
    if any(k in low for k in ("rs-232", "rs232", "db9", "db15", "db25", "d9 ",
                              "d15", "serial")):
        return "serial"
    if "usb" in low:
        return "usb"
    if any(k in low for k in ("ethernet", "rj45", "tcp", "poe")):
        return "ethernet"
    if re.search(r"\bc1[34][rp]?\b|nema|din power|power cable|ac/dc", low):
        return "power"
    return "periphery"


def parse_ports(text: str, pip_type: str):
    """Read pipettor port names out of a §2 / device connection string.
    Returns (ports, mode) where mode is "any" (alternatives, "or") or "all"."""
    t = (text or "")
    inv = STAR_PORTS if pip_type == "STAR" else VANTAGE_PORTS
    found = []
    for p in inv:
        if re.search(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])", t.lower()):
            found.append(p)
    if pip_type == "STAR" and not any(p.startswith("Div") for p in found) \
            and re.search(r"\bdiv\b", t.lower()):
        found += ["Div1", "Div2", "Div3"]
        return found, "any"
    mode = "any" if re.search(r"\bor\b", t.lower()) else "all"
    return found, mode


# --------------------------------------------------------------------------
# Header mapping
# --------------------------------------------------------------------------
_HEADER_KEYS = [
    ("name", ("device name", "pc type", "primary device")),
    ("paired", ("paired device",)),
    ("pn", ("part number",)),
    ("role", ("role",)),
    ("amp", ("amperage",)),
    ("power", ("power cable",)),
    ("comm", ("communication",)),
    ("dims", ("dimension",)),
    ("notes", ("notes",)),
    ("pipettor", ("pipettor",)),
    ("required", ("required",)),
    ("cables", ("connecting cable", "connection type")),
]


def _map_header(row):
    idx = {}
    for ci, cell in enumerate(row):
        h = cell["text"].lower()
        for key, cands in _HEADER_KEYS:
            if key in idx:
                continue
            if any(h.startswith(c) for c in cands):
                idx[key] = ci
                break
    return idx


def _cell(row, idx, key):
    ci = idx.get(key)
    if ci is None or ci >= len(row):
        return ""
    return row[ci]["text"]


def _row_struck(row, idx):
    """A row is struck when its name cell has no live text left."""
    ci = idx.get("name", 0)
    return ci >= len(row) or not row[ci]["text"].strip()


# --------------------------------------------------------------------------
# Main parser
# --------------------------------------------------------------------------
def _version_from_html(content: str) -> str:
    live = re.sub(r"<(del|s|strike)\b[^>]*>.*?</\1>", "", content, flags=re.S | re.I)
    m = re.search(r"Version:\s*</strong>\s*([0-9]+(?:\.[0-9]+)+)", live)
    if not m:
        m = re.search(r"Version:?\s*v?([0-9]+(?:\.[0-9]+)+)", re.sub(r"<[^>]+>", " ", live))
    return m.group(1) if m else ""


def parse_drd_html(html_path: str) -> dict:
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    parser = _TableParser()
    parser.feed(content)

    pipettors, pcs, hubs, devices = {}, {}, {}, {}
    pip_connected, raw_pairings = [], []
    skipped_struck = 0

    for tbl, section in zip(parser.tables, parser.table_sections):
        if not tbl or len(tbl) < 2:
            continue
        idx = _map_header(tbl[0])
        for row in tbl[1:]:
            if _row_struck(row, idx):
                skipped_struck += 1
                continue
            name = _cell(row, idx, "name").replace("\n", " ").strip()
            if section == "section1":
                amp, status = parse_amperage(_cell(row, idx, "amp"))
                pipettors[name] = {
                    "name": name, "amperage": amp, "amperage_raw": _cell(row, idx, "amp"),
                    "amperage_status": status,
                    "power_cable": _cell(row, idx, "power"),
                    "comm_cable": _cell(row, idx, "comm"),
                    "type": "VANTAGE" if "vantage" in name.lower() else "STAR",
                }
            elif section == "section2":
                pip_connected.append({
                    "device_raw": name,
                    "pipettor": _cell(row, idx, "pipettor").strip().upper(),
                    "required": _cell(row, idx, "required").strip().lower().startswith("y"),
                    "cables": _cell(row, idx, "cables"),
                })
            elif section in ("section3", "section4", "section5", "section6"):
                amp_raw = _cell(row, idx, "amp")
                amp, status = parse_amperage(amp_raw)
                dims, w, h = parse_dims(_cell(row, idx, "dims"))
                notes = _cell(row, idx, "notes")
                rec = {
                    "name": name,
                    "part_numbers": split_part_numbers(_cell(row, idx, "pn")),
                    "role": "",
                    "amperage": amp, "amperage_raw": amp_raw.strip(),
                    "amperage_status": status,
                    "power_cable": _cell(row, idx, "power"),
                    "comm_cable": _cell(row, idx, "comm"),
                    "dimensions": dims, "width": w, "height": h,
                    "notes": notes,
                    "section": section,
                    "former_names": [s for s in row[idx.get("name", 0)]["struck"].split("\n") if s],
                }
                m = re.search(r"(\d+)\s*x\s*per\s*kit", notes or "", re.I)
                rec["kit_qty"] = int(m.group(1)) if m else 1
                m = re.search(r"adds\s+(\d+(?:\.\d+)?)\s*a\s+to\s+the\s+(.+?)\s+amperage",
                              notes or "", re.I)
                rec["adds_load"] = ({"amps": float(m.group(1)), "to": m.group(2).strip()}
                                    if m else None)
                if section == "section3":
                    rec["category"] = "pc"
                    rec["role"] = "powered"
                    rec["is_main_pc"] = name in MAIN_PC_TYPES
                    pcs[name] = rec
                elif section == "section4":
                    rec["category"] = "hub"
                    rec["role"] = "powered" if rec["power_cable"].strip() not in ("", "-") else "none"
                    hubs[name] = rec
                else:
                    rec["category"] = "on_deck" if section == "section5" else "off_deck"
                    role = _cell(row, idx, "role").strip().lower()
                    rec["role"] = role if role in ("powered", "child") else ""
                    rec["role_raw"] = _cell(row, idx, "role").strip()
                    devices[name] = rec
            elif section == "section7":
                raw_pairings.append({
                    "primary_raw": name,
                    "partners_raw": [p for p in _cell(row, idx, "paired").split("\n") if p.strip()],
                    "connections": [c for c in _cell(row, idx, "cables").split("\n") if c.strip()],
                    "notes": _cell(row, idx, "notes"),
                })

    db = {
        "version": _version_from_html(content),
        "source": os.path.basename(html_path),
        "color_typo_note": COLOR_TYPO_NOTE,
        "pipettors": pipettors,
        "pcs": pcs,
        "hubs": hubs,
        "devices": devices,
        "pipettor_connected": pip_connected,
        "raw_pairings": raw_pairings,
        "skipped_struck_rows": skipped_struck,
    }
    _resolve_cross_references(db)
    return db


# --------------------------------------------------------------------------
# Cross-reference resolution (§2 and §7 names -> §3-§6 records)
# --------------------------------------------------------------------------
def _all_records(db):
    for coll in ("devices", "pcs", "hubs"):
        for name, rec in db.get(coll, {}).items():
            yield coll, name, rec


def resolve_name(db, raw: str):
    """Resolve a DRD-internal reference (§2/§7 text) to a record name.
    Exact (case/punctuation/"Ctrl" insensitive) -> space-insensitive ->
    unique token-subset match with the fewest extra tokens. Returns name or None.
    This is only used between DRD sections, never for order lines."""
    if not raw:
        return None
    low = raw.strip().lower()
    if low in PAIRING_STANDINS:
        return PAIRING_STANDINS[low]
    q = norm_name(raw)
    names = [n for _c, n, _r in _all_records(db)]
    for n in names:
        if norm_name(n) == q:
            return n
    qk = q.replace(" ", "")
    for n in names:
        if norm_name(n).replace(" ", "") == qk:
            return n
    qt = set(q.split())
    if not qt:
        return None
    cands = []
    for n in names:
        nt = set(norm_name(n).split())
        if qt <= nt:
            cands.append((len(nt - qt), n))
    if not cands:
        return None
    cands.sort()
    if len(cands) == 1 or cands[0][0] < cands[1][0]:
        return cands[0][1]
    return None


def _is_pipettor_ref(raw: str) -> str:
    low = (raw or "").lower()
    if re.search(r"\bstar\b.*\b(div|port|tcc|periphery)\b", low):
        return "STAR"
    if re.search(r"\bvantage\b.*\b(port|periphery)\b", low):
        return "VANTAGE"
    return ""


def _role_of(db, name):
    for coll in ("devices", "pcs", "hubs"):
        if name in db.get(coll, {}):
            return db[coll][name].get("role", "")
    return ""


def _resolve_cross_references(db):
    validation = []
    # ---- §2 pipettor-connected devices ----
    for pc in db["pipettor_connected"]:
        pc["device"] = resolve_name(db, pc["device_raw"])
        if pc["device"] is None:
            validation.append(f"§2: '{pc['device_raw']}' does not match any §5/§6 device "
                              f"name, so its pipettor connection cannot be used.")

    # ---- §7 pairings ----
    pairings = []
    for rp in db["raw_pairings"]:
        primary = resolve_name(db, rp["primary_raw"])
        if primary is None:
            validation.append(f"§7: primary device '{rp['primary_raw']}' does not match "
                              f"any DRD device name; pairing ignored.")
            continue
        for praw in rp["partners_raw"]:
            entry = {"primary": primary, "partner": None, "partner_raw": praw,
                     "connections": rp["connections"], "notes": rp["notes"],
                     "relation": "", "pipettor": ""}
            if praw.strip().lower().startswith("subsequent"):
                entry["relation"] = "daisy_chain"
                entry["partner"] = primary
                pairings.append(entry)
                continue
            pip = _is_pipettor_ref(praw)
            if pip:
                entry["relation"] = "pipettor"
                entry["pipettor"] = pip
                pairings.append(entry)
                continue
            partner = resolve_name(db, praw)
            if partner is None:
                validation.append(f"§7: paired device '{praw}' (with {primary}) does not "
                                  f"match any DRD device name; pairing ignored.")
                continue
            entry["partner"] = partner
            r1, r2 = _role_of(db, primary), _role_of(db, partner)
            if r1 == "child":
                entry["relation"] = "child_of"          # primary is child of partner
            elif r2 == "child":
                entry["relation"] = "parent_of"         # primary is parent of partner
            else:
                entry["relation"] = "link"              # two independently powered devices
            pairings.append(entry)
    db["pairings"] = pairings

    # ---- record-level validation ----
    for name, rec in db["devices"].items():
        if not rec.get("role"):
            validation.append(f"{name}: Role is '{rec.get('role_raw', '')}' "
                              f"(expected Powered or Child). It will be flagged in every analysis.")
        if rec.get("role") == "powered" and rec["amperage_status"] != "ok" \
                and name != HHS_CONTROL_BOX:
            validation.append(f"{name}: Powered, but amperage '{rec['amperage_raw']}' is "
                              f"not a number. An amperage must be entered when it is used.")
    for name, rec in db["pcs"].items():
        if rec["amperage_status"] != "ok":
            validation.append(f"{name} (PC): amperage '{rec['amperage_raw']}' is not a number.")
    db["validation"] = list(dict.fromkeys(validation))   # de-duplicate, keep order


# --------------------------------------------------------------------------
# High level DRD object
# --------------------------------------------------------------------------
class DRD:
    def __init__(self, db: dict):
        self.db = db
        self.version = db.get("version", "")
        self.pipettors = db.get("pipettors", {})
        self.pcs = db.get("pcs", {})
        self.hubs = db.get("hubs", {})
        self.devices = db.get("devices", {})
        self.pairings = db.get("pairings", [])
        self.pipettor_connected = db.get("pipettor_connected", [])
        self.validation = db.get("validation", [])

        # Part-number index: one part number -> MANY records (kits).
        self._part_index = {}
        self._part_wild = []
        for _coll, name, rec in _all_records(db):
            for pn in rec.get("part_numbers", []):
                npn = norm_part(pn)
                if not npn:
                    continue
                m = re.fullmatch(r"(.*?[0-9\-])(x+)", pn.strip().lower())
                if m and norm_part(m.group(1)):
                    stem = norm_part(m.group(1))
                    n = len(m.group(2))
                    pat = re.compile("^" + re.escape(stem) + r"[a-z0-9]{1,%d}$" % max(3, n))
                    self._part_wild.append((pat, name, pn))
                else:
                    self._part_index.setdefault(npn, [])
                    if name not in self._part_index[npn]:
                        self._part_index[npn].append(name)

    # ---- record access ----
    def get(self, name: str):
        if not name:
            return None
        for coll in (self.devices, self.pcs, self.hubs):
            if name in coll:
                return coll[name]
        return None

    def all_names(self):
        return sorted(set(self.devices) | set(self.pcs) | set(self.hubs))

    def role(self, name):
        rec = self.get(name)
        return (rec or {}).get("role", "")

    def is_hhs_family(self, name):
        return name in HHS_FAMILY

    # ---- part-number matching (the ONLY automatic order matching) ----
    def find_part(self, part_number: str):
        """Return a list of (device_name, how) for an order part number.
        exact + wildcard ('96160-xx') matches are combined, because a DRD
        wildcard states that every variant includes that device. If neither
        hits, the model before ':' is tried ('VANTAGE 2.0 : 009741').
        Never matches on descriptions or names."""
        out = []
        for candidate, how_prefix in self._pn_candidates(part_number):
            key = norm_part(candidate)
            if not key:
                continue
            for name in self._part_index.get(key, []):
                if name not in [n for n, _h in out]:
                    out.append((name, how_prefix + "exact"))
            for pat, name, _pn in self._part_wild:
                if pat.match(key) and name not in [n for n, _h in out]:
                    out.append((name, how_prefix + "wildcard"))
            if out:
                return out
        return out

    @staticmethod
    def _pn_candidates(part_number):
        pn = (part_number or "").strip()
        cands = [(pn, "")]
        if ":" in pn:
            model = pn.split(":")[0].strip()
            if model:
                cands.append((model, "model:"))
        return cands

    # ---- pairing helpers ----
    def parents_of(self, name):
        """§7 parents of a Child device: list of (parent_name, connections)."""
        out = []
        for p in self.pairings:
            if p["relation"] == "child_of" and p["primary"] == name:
                out.append((p["partner"], p["connections"]))
            elif p["relation"] == "parent_of" and p["partner"] == name:
                out.append((p["primary"], p["connections"]))
        return out

    def children_of(self, name):
        out = []
        for p in self.pairings:
            if p["relation"] == "child_of" and p["partner"] == name:
                out.append((p["primary"], p["connections"]))
            elif p["relation"] == "parent_of" and p["primary"] == name:
                out.append((p["partner"], p["connections"]))
        return out

    def links_of(self, name):
        out = []
        for p in self.pairings:
            if p["relation"] != "link":
                continue
            if p["primary"] == name:
                out.append((p["partner"], p["connections"]))
            elif p["partner"] == name:
                out.append((p["primary"], p["connections"]))
        return out

    def pipettor_pairing(self, name):
        """§7 rows that send a device's cable to a pipettor port."""
        return [p for p in self.pairings
                if p["relation"] == "pipettor" and p["primary"] == name]

    def daisy_chain(self, name):
        for p in self.pairings:
            if p["relation"] == "daisy_chain" and p["primary"] == name:
                return p["connections"]
        return None

    def kit_mates(self, name):
        """Records that share at least one exact part number with `name`."""
        rec = self.get(name)
        mates = []
        for pn in (rec or {}).get("part_numbers", []):
            for other in self._part_index.get(norm_part(pn), []):
                if other != name and other not in mates:
                    mates.append(other)
        return mates

    def pipettor_connection(self, name, pip_type):
        """§2 entry for this device + pipettor type, or None."""
        for pc in self.pipettor_connected:
            if pc.get("device") == name and pc.get("pipettor") == pip_type:
                return pc
        return None

    # ---- loading ----
    @classmethod
    def load(cls, json_path=None, html_path=None):
        if json_path and os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                return cls(json.load(f))
        if html_path and os.path.exists(html_path):
            return cls(parse_drd_html(html_path))
        raise FileNotFoundError("No DRD JSON or HTML found to load.")


def version_tuple(v: str):
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except ValueError:
        return (0,)


def bundled_data_dir():
    """data/ folder next to the app, or inside the PyInstaller bundle."""
    import sys
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "data")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def user_data_dir():
    """Writable folder for a DRD rebuilt by the user and saved rules."""
    import sys
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "WireDiagramBuilder")
    return bundled_data_dir()


def styleguide_version(data_dir=None):
    """Version of the newest StyleGuide_v*.html shipped in data/ ("" if none)."""
    return find_latest_html(data_dir or bundled_data_dir(), "StyleGuide")[1]


def find_latest_html(data_dir: str, prefix="DeviceReferenceDatabase"):
    """Return (path, version) of the newest DRD/StyleGuide HTML in data_dir."""
    import glob
    best = (None, "")
    for p in glob.glob(os.path.join(data_dir, prefix + "*.html")):
        m = re.search(r"_v([0-9]+(?:\.[0-9]+)+)\.html$", os.path.basename(p))
        v = m.group(1) if m else ""
        if best[0] is None or version_tuple(v) > version_tuple(best[1]):
            best = (p, v)
    return best


def build_and_cache(html_path: str, json_out: str) -> dict:
    db = parse_drd_html(html_path)
    os.makedirs(os.path.dirname(json_out) or ".", exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    return db


if __name__ == "__main__":
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    data = os.path.join(root, "data")
    src = sys.argv[1] if len(sys.argv) > 1 else find_latest_html(data)[0]
    if not src:
        sys.exit("No DeviceReferenceDatabase_v*.html found in data/.")
    out = os.path.join(data, "drd.json")
    db = build_and_cache(src, out)
    print(f"Parsed DRD v{db['version']} ({os.path.basename(src)}): "
          f"{len(db['devices'])} devices, {len(db['pcs'])} PCs, {len(db['hubs'])} hubs, "
          f"{len(db['pipettors'])} pipettors, {len(db['pairings'])} pairings; "
          f"{db['skipped_struck_rows']} struck rows skipped. Wrote {out}")
    if db["validation"]:
        print(f"\n{len(db['validation'])} DRD issue(s):")
        for v in db["validation"]:
            print("  - " + v)
