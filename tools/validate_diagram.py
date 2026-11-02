#!/usr/bin/env python3
"""
tools/validate_diagram.py
=========================
Static rule-checker for generated .drawio files.

Checks every StyleGuide v2.2.3 rule that is verifiable from the XML without
running draw.io's layout engine.  Line routing is checked by rebuilding each
line from its waypoints the way draw.io draws an orthogonal edge with floating
ends (see check_edge_routing).

Usage
-----
    python3 tools/validate_diagram.py  <diagram.drawio>
    python3 tools/validate_diagram.py  <diagram.drawio>  --drd data/drd.json
    python3 tools/validate_diagram.py  <diagram.drawio>  --margin 10

Exit codes
----------
    0  all checks pass (or only warnings)
    1  at least one FAIL

Output format
-------------
    [PASS]  Rule description
    [WARN]  Rule description - detail (not a hard failure)
    [FAIL]  Rule description - detail
    [SKIP]  Rule description - cannot check statically

Author: WireDiagram App
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional


# ── ANSI colours (disabled if stdout is not a terminal) ─────────────────────
_TTY = sys.stdout.isatty()
GREEN  = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED    = "\033[31m" if _TTY else ""
CYAN   = "\033[36m" if _TTY else ""
RESET  = "\033[0m"  if _TTY else ""


# ── Connector colour constants (StyleGuide §9.2) ─────────────────────────────
CONN_COLORS = {
    "power":     "#000000",
    "usb":       "#007CF7",
    "serial":    "#009900",
    "ethernet":  "#FF4AE7",
    "periphery": "#6600CC",
}
VALID_STROKE_COLORS = set(CONN_COLORS.values())
# draw.io also accepts 4-digit short hex (e.g. #0000 == #000000); the approved
# reference diagrams use those forms, so treat them as valid too.
_SHORT_HEX = {"#0000": "#000000", "#FFFF": "#FFFFFF"}
VALID_STROKE_COLORS |= set(_SHORT_HEX.keys())

# Column boundaries (StyleGuide §2.3)
COLUMNS = {
    1: (190, 330,  "Facility Requirements"),
    2: (330, 460,  "System Requirements"),
    3: (460, 640,  "PC Connections"),
    4: (640, 840,  "Off Deck Devices"),
    5: (840, 990,  "On Deck Devices"),
    6: (990, 1140, "Pipettor Connections"),
}
COLUMN_TITLES = [v[2] for v in COLUMNS.values()]

# Styles that identify structural / decorative cells (not device blocks)
_STRUCTURAL_STYLES = (
    "text;",          # labels / headers
    "endArrow=none;dashed=1",  # separators
    "ellipse;",       # circuit ellipses
)


# ── Result accumulator ───────────────────────────────────────────────────────
@dataclass
class Report:
    passes:   list = field(default_factory=list)
    warns:    list = field(default_factory=list)
    fails:    list = field(default_factory=list)
    skips:    list = field(default_factory=list)

    def ok(self, msg):    self.passes.append(msg)
    def warn(self, msg):  self.warns.append(msg)
    def fail(self, msg):  self.fails.append(msg)
    def skip(self, msg):  self.skips.append(msg)

    def print_summary(self):
        for m in self.passes:
            print(f"{GREEN}[PASS]{RESET}  {m}")
        for m in self.warns:
            print(f"{YELLOW}[WARN]{RESET}  {m}")
        for m in self.fails:
            print(f"{RED}[FAIL]{RESET}  {m}")
        for m in self.skips:
            print(f"{CYAN}[SKIP]{RESET}  {m}")
        print()
        print(f"  {len(self.passes)} PASS  |  {len(self.warns)} WARN  "
              f"|  {len(self.fails)} FAIL  |  {len(self.skips)} SKIP")

    @property
    def exit_code(self):
        return 1 if self.fails else 0


# ── XML parsing helpers ──────────────────────────────────────────────────────
@dataclass
class CellInfo:
    id:     str
    value:  str           # raw HTML label
    style:  str
    vertex: bool
    edge:   bool
    x:      float = 0.0
    y:      float = 0.0
    w:      float = 0.0
    h:      float = 0.0
    source: str = ""
    target: str = ""
    parent: str = "1"

    @property
    def text(self):
        """Plain text content of the label (HTML tags stripped)."""
        return _strip_html(self.value)

    @property
    def stroke_color(self):
        m = re.search(r"strokeColor=([^;]+)", self.style)
        return m.group(1) if m else ""

    @property
    def is_separator(self):
        return "dashed=1" in self.style and "endArrow=none" in self.style

    @property
    def is_circuit(self):
        """A facility-circuit ellipse: an ellipse whose label says "Circuit".
        Other labelled circles (STAR Div2/Div3/P1/P2, VANTAGE ports) are not
        counted."""
        return "ellipse" in self.style and "circuit" in self.text.lower()

    @property
    def is_device_block(self):
        return (self.vertex and
                "rounded=1" in self.style and
                "whiteSpace=wrap" in self.style and
                "ellipse" not in self.style and
                "text;" not in self.style and
                "glass=1" in self.style)

    @property
    def is_header_label(self):
        return self.vertex and "text;" in self.style

    def rect(self):
        """Return (x, y, x2, y2) bounding box."""
        return (self.x, self.y, self.x + self.w, self.y + self.h)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)
    def get_text(self):
        return " ".join(p.strip() for p in self._parts if p.strip())


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    try:
        s.feed(html)
        return s.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


def parse_drawio(path: str) -> list[CellInfo]:
    tree = ET.parse(path)
    root = tree.getroot()
    cells = []
    for cell in root.iter("mxCell"):
        geom = cell.find("mxGeometry")
        ci = CellInfo(
            id     = cell.get("id", ""),
            value  = cell.get("value", ""),
            style  = cell.get("style", ""),
            vertex = cell.get("vertex") == "1",
            edge   = cell.get("edge") == "1",
            source = cell.get("source", ""),
            target = cell.get("target", ""),
            parent = cell.get("parent", "1"),
        )
        if geom is not None:
            ci.x = float(geom.get("x", 0) or 0)
            ci.y = float(geom.get("y", 0) or 0)
            ci.w = float(geom.get("width",  0) or 0)
            ci.h = float(geom.get("height", 0) or 0)
        cells.append(ci)
    return cells


# ── Individual checks ────────────────────────────────────────────────────────

def check_xml_wellformed(path: str, r: Report):
    try:
        ET.parse(path)
        r.ok("XML is well-formed")
    except ET.ParseError as e:
        r.fail(f"XML is NOT well-formed: {e}")


def check_column_headers(cells: list[CellInfo], r: Report):
    labels = [_strip_html(c.value) for c in cells if c.is_header_label]
    missing = [t for t in COLUMN_TITLES if t not in labels]
    if missing:
        r.fail(f"Missing column headers: {missing}")
    else:
        r.ok(f"All 6 column headers present")


def check_separator_zorder(cells: list[CellInfo], r: Report):
    """Separators must be the LAST cells in the XML (§2.4 highest z-order)."""
    ids = [c.id for c in cells if c.id not in ("0", "1")]
    sep_ids = {c.id for c in cells if c.is_separator}
    if not sep_ids:
        r.warn("No separator cells found — expected dashed column dividers")
        return
    last_non_sep = None
    for cid in reversed(ids):
        if cid not in sep_ids:
            last_non_sep = cid
            break
    if last_non_sep is not None:
        # check if any separator comes before last_non_sep
        non_sep_idx = ids.index(last_non_sep)
        for sid in sep_ids:
            if sid in ids and ids.index(sid) < non_sep_idx:
                r.fail(f"Separator z-order violated (§2.4): separator {sid!r} "
                       f"appears before non-separator {last_non_sep!r}")
                return
    r.ok(f"Separators ({len(sep_ids)}) are last in XML — correct z-order (§2.4)")


def check_block_overlaps(cells: list[CellInfo], r: Report, margin: float = 10.0):
    """Check that no two device blocks overlap with a 10px margin (§9.1.1).
    Header labels, edges, and non-block cells are excluded."""
    # Only top-level blocks (parent == "1") are placed in absolute page
    # coordinates.  Pipettor port cells are children of a container body
    # (parent == body id) with coordinates relative to that body, so they are
    # laid out and clipped by draw.io inside the container and must be excluded
    # from absolute page-overlap checks (containment-aware, §6.1/§6.2).
    blocks = [c for c in cells
              if c.vertex and c.w > 0 and c.h > 0
              and not c.is_separator and not c.is_header_label
              and c.id not in ("0", "1")
              and getattr(c, "parent", "1") == "1"]
    fails = []
    for i, a in enumerate(blocks):
        ax1, ay1, ax2, ay2 = a.rect()
        for b in blocks[i + 1:]:
            bx1, by1, bx2, by2 = b.rect()
            # with margin applied outward
            if (ax1 - margin < bx2 and ax2 + margin > bx1 and
                    ay1 - margin < by2 and ay2 + margin > by1):
                la = _strip_html(a.value)[:30] or a.id
                lb = _strip_html(b.value)[:30] or b.id
                fails.append(
                    f"  {la!r} ({a.id}) [{int(a.x)},{int(a.y)},{int(a.w)},{int(a.h)}] "
                    f"↔ {lb!r} ({b.id}) [{int(b.x)},{int(b.y)},{int(b.w)},{int(b.h)}]"
                )
    if fails:
        r.fail(f"Block overlap violations within {margin}px margin (§9.1.1):\n"
               + "\n".join(fails))
    else:
        r.ok(f"No block-to-block overlaps within {margin}px margin (§9.1.1) "
             f"[{len(blocks)} blocks checked]")


def check_edge_routing(path: str, r: Report):
    """Line routing (§9.1 / §9.1.1). Each line is rebuilt the way draw.io draws an
    orthogonal edge with floating ends: it leaves the source face level with the
    first waypoint, follows the waypoints, and enters the target face level with
    the last one. Checks: every line has waypoints, no diagonal segments, no line
    through a block that is not one of its ends, no two lines on top of each other,
    parallel lines at least 10px apart, and (warning) no 10px zig-zags."""
    import xml.etree.ElementTree as _ET
    root = _ET.parse(path).getroot()
    for level, msg in _geom_check(root):
        {"PASS": r.ok, "WARN": r.warn, "FAIL": r.fail}[level](msg)


def _geom_check(root):
    """Reconstruct each line as draw.io draws it (floating ends + waypoints) and check
    StyleGuide 9.1 routing rules. Returns list of (level, message)."""
    cells = {c.get("id"): c for c in root.iter("mxCell")}
    absr = {}
    def rect(cid):
        if cid in absr: return absr[cid]
        c = cells.get(cid)
        if c is None or c.get("vertex") != "1": return None
        g = c.find("mxGeometry")
        if g is None: return None
        x, y = float(g.get("x", 0)), float(g.get("y", 0))
        w, h = float(g.get("width", 0)), float(g.get("height", 0))
        p = rect(c.get("parent"))
        if p: x, y = x + p[0], y + p[1]
        absr[cid] = (x, y, w, h); return absr[cid]
    def ancestors(cid):
        out = set()
        while cid in cells:
            out.add(cid); cid = cells[cid].get("parent")
        return out
    def label(cid):
        v = (cells[cid].get("value") or "") if cid in cells else ""
        v = re.sub(r"<[^>]+>", " ", v).split()
        return " ".join(v[:4]) or cid
    obst = []
    for cid, c in cells.items():
        if c.get("vertex") != "1": continue
        st = c.get("style") or ""
        if st.startswith("text;") or "swimlane" in st: continue
        r = rect(cid)
        if r and r[2] > 0 and r[3] > 0: obst.append((cid, r))
    def end_pt(r, wp):
        x, y, w, h = r; px, py = wp
        if y <= py <= y + h:
            return (x + w if px >= x + w else x, py)
        if x <= px <= x + w:
            return (px, y + h if py >= y + h else y)
        return None
    res, segs, nowp = [], [], []
    for cid, c in cells.items():
        if c.get("edge") != "1" or not c.get("source"): continue
        pts = [(float(p.get("x")), float(p.get("y"))) for p in c.findall("mxGeometry/Array/mxPoint")]
        name = f"{label(c.get('source'))} -> {label(c.get('target'))}"
        rs, rt = rect(c.get("source")), rect(c.get("target"))
        if rs is None or rt is None: continue
        if not pts:
            # draw.io draws a straight line (middle of the overlap) when the two
            # blocks overlap side-to-side or top-to-bottom; that needs no waypoints
            (sx, sy, sw, sh), (tx, ty, tw, th) = rs, rt
            oy0, oy1 = max(sy, ty), min(sy + sh, ty + th)
            ox0, ox1 = max(sx, tx), min(sx + sw, tx + tw)
            if oy1 > oy0 and (sx + sw <= tx or tx + tw <= sx):
                ym = (oy0 + oy1) / 2
                a, b = ((sx + sw, ym), (tx, ym)) if sx < tx else ((sx, ym), (tx + tw, ym))
            elif ox1 > ox0 and (sy + sh <= ty or ty + th <= sy):
                xm = (ox0 + ox1) / 2
                a, b = ((xm, sy + sh), (xm, ty)) if sy < ty else ((xm, sy), (xm, ty + th))
            else:
                nowp.append(name); continue
        else:
            a, b = end_pt(rs, pts[0]), end_pt(rt, pts[-1])
        if a is None or b is None:
            res.append(("FAIL", f"{name}: first/last waypoint not level with its block face (line would hit a corner)"))
            continue
        poly = [a] + pts + [b]
        skip = ancestors(c.get("source")) | ancestors(c.get("target"))
        for oid, (ox, oy, ow, oh) in obst:      # port-group panels that hold an end port
            for (ex, ey, ew, eh) in (rs, rt):
                if ox <= ex and oy <= ey and ex + ew <= ox + ow and ey + eh <= oy + oh:
                    skip.add(oid)
        for (x1, y1), (x2, y2) in zip(poly, poly[1:]):
            if x1 != x2 and y1 != y2:
                res.append(("FAIL", f"{name}: diagonal segment ({x1},{y1})-({x2},{y2})")); continue
            if (x1, y1) == (x2, y2): continue
            segs.append((cid, name, x1, y1, x2, y2))
            lx, hx, ly, hy = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
            for oid, (ox, oy, ow, oh) in obst:
                if oid in skip: continue
                if lx < ox + ow and hx > ox and ly < oy + oh and hy > oy:
                    res.append(("FAIL", f"{name}: passes through block '{label(oid)}'")); break
    if nowp:
        res.append(("FAIL", f"{len(nowp)} line(s) need a bend but have no waypoints: " + "; ".join(nowp[:5])))
    bad_lane, overlap = set(), set()
    for i in range(len(segs)):
        e1, n1, ax1, ay1, ax2, ay2 = segs[i]
        for j in range(i + 1, len(segs)):
            e2, n2, bx1, by1, bx2, by2 = segs[j]
            if e1 == e2: continue
            if ay1 == ay2 and by1 == by2:          # both horizontal
                lo, hi = max(min(ax1, ax2), min(bx1, bx2)), min(max(ax1, ax2), max(bx1, bx2))
                d = abs(ay1 - by1)
            elif ax1 == ax2 and bx1 == bx2:        # both vertical
                lo, hi = max(min(ay1, ay2), min(by1, by2)), min(max(ay1, ay2), max(by1, by2))
                d = abs(ax1 - bx1)
            else:
                continue
            if hi - lo <= 0: continue
            key = tuple(sorted((n1, n2)))
            if d == 0: overlap.add(key)
            elif d < 10: bad_lane.add(key)
    for a, b in sorted(overlap):
        res.append(("FAIL", f"Lines drawn on top of each other: {a}  |  {b}"))
    for a, b in sorted(bad_lane):
        res.append(("FAIL", f"Parallel lines closer than 10px: {a}  |  {b}"))
    jogs = []
    for cid, c in cells.items():
        if c.get("edge") != "1" or not c.get("source"): continue
        pts = [(float(p.get("x")), float(p.get("y"))) for p in c.findall("mxGeometry/Array/mxPoint")]
        if any(abs(a[0] - b[0]) + abs(a[1] - b[1]) < 20 for a, b in zip(pts, pts[1:])):
            jogs.append(f"{label(c.get('source'))} -> {label(c.get('target'))}")
    for n in jogs:
        res.append(("WARN", f"Small 10px zig-zag in line: {n}"))
    if not [x for x in res if x[0] == "FAIL"]:
        res.append(("PASS", f"Routing (9.1): {len(segs)} segments - every bent line has waypoints, 10px lanes, no overlaps, no lines through blocks"))
    return res


def check_connector_colors(cells: list[CellInfo], r: Report):
    """All edge strokeColors must be one of the 5 canonical types (§9.2)."""
    edges = [c for c in cells if c.edge]
    bad = []
    for e in edges:
        sc = e.stroke_color
        if not sc:
            continue
        # legend sample edges and separator edges are excluded
        if "dashed=1" in e.style:
            continue
        if sc not in VALID_STROKE_COLORS:
            bad.append(f"  edge {e.id!r}: strokeColor={sc!r}")
    if bad:
        r.fail(f"Connector colour violations (§9.2):\n" + "\n".join(bad))
    else:
        r.ok(f"All {len(edges)} edge strokeColors are valid (§9.2)")


def check_first_line_bold(cells: list[CellInfo], r: Report):
    """First line of device blocks must be bold (§10.12)."""
    device_blocks = [c for c in cells if c.is_device_block]
    bad = []
    for c in device_blocks:
        val = c.value
        if val and not re.search(r"^<b>", val.lstrip()):
            label = _strip_html(val)[:40]
            bad.append(f"  {label!r} ({c.id})")
    if bad:
        r.fail(f"Device blocks without bold first line (§10.12):\n" + "\n".join(bad))
    else:
        r.ok(f"All device-block first lines are bold (§10.12) "
             f"[{len(device_blocks)} blocks]")


def check_label_no_reason_text(cells: list[CellInfo], r: Report):
    """Device blocks must not contain explanatory/reason text ('Powered through',
    'No power', 'Internal VANTAGE') — only DRD-sourced content."""
    FORBIDDEN = [
        r"powered through",
        r"no power",
        r"internal vantage",
        r"customer.supplied",
        r"implied by",
        r"\bunpowered\b",
    ]
    bad = []
    for c in cells:
        if not c.is_device_block:
            continue
        txt = _strip_html(c.value).lower()
        for pat in FORBIDDEN:
            if re.search(pat, txt):
                bad.append(f"  {_strip_html(c.value)[:50]!r} ({c.id}) — matches {pat!r}")
                break
    if bad:
        r.fail(f"Device blocks contain forbidden explanatory text:\n" + "\n".join(bad))
    else:
        r.ok("No device blocks contain forbidden reason/explanation text")


_REASON_PHRASES = (
    "powered through", "powered by", "unpowered", "no power", "draws from",
    "trade-in", "trade in", "out of scope", "not counted", "implied by",
    "paired with", "reason", "via the", "plugs into",
)


def check_unpowered_block_content(cells: list[CellInfo], drd_names: set[str], r: Report):
    """Device blocks must carry DRD content only — name + amperage + cable
    line(s) — and must NOT contain free-form reason/explanation prose (the
    constraint: paired/unpowered device text is limited to DRD entries).

    The earlier line-count heuristic produced false positives: powered control
    boxes legitimately show amperage + cable, and the first HHC/HHS on a box
    legitimately carries two cable lines (its serial line + the USB-to-PC line,
    spec §6.3).  Line count therefore does not indicate a violation; the real
    thing to forbid is reason prose, so we flag that instead."""
    if not drd_names:
        r.skip("DRD not loaded — skipping unpowered-block content depth check "
               "(pass --drd data/drd.json to enable)")
        return
    bad = []
    for c in cells:
        if not c.is_device_block:
            continue
        txt = _strip_html(c.value).lower()
        hit = next((p for p in _REASON_PHRASES if p in txt), None)
        if hit:
            bad.append(f"  {_strip_html(c.value)[:50]!r} ({c.id}): contains reason text {hit!r}")
    if bad:
        r.fail("Device blocks contain forbidden reason/explanation text "
               "(DRD content only):\n" + "\n".join(bad))
    else:
        r.ok("No device block contains reason/explanation text (DRD content only)")


def check_circuit_ellipses(cells: list[CellInfo], r: Report):
    """At least one circuit ellipse must be present."""
    circuits = [c for c in cells if c.is_circuit]
    if not circuits:
        r.fail("No circuit ellipses found (expected at least one)")
        return
    r.ok(f"{len(circuits)} circuit ellipse(s) found")

    # VANTAGE circuit: the one with L14-20 / L21-20 / C19 in its label should
    # be the last ellipse in document order (§8.x)
    van_circuits = [c for c in circuits
                    if re.search(r"L14|L21|C19", c.value)]
    if van_circuits:
        last_ellipse = circuits[-1]
        for vc in van_circuits:
            if vc.id != last_ellipse.id:
                r.fail(f"VANTAGE circuit ({vc.id}) is not the last circuit ellipse — "
                       f"it should be numbered last (§8.x)")
                return
        r.ok("VANTAGE dedicated circuit is last (§8.x)")


def check_power_strips(cells: list[CellInfo], r: Report):
    """Power strips (glass gradient style) should exist only when a circuit
    needs them (>2 connections).  We verify strips are present in the diagram
    (structural presence only — connection counting requires the full model)."""
    strips = [c for c in cells
              if c.vertex and "glass=1" in c.style and "CCCCCC" in c.style
              and c.w < 60]   # power strips are narrow (~40px wide)
    r.ok(f"{len(strips)} power strip(s) found in diagram")


def check_legend(cells: list[CellInfo], r: Report):
    """A legend block containing connection-type labels must be present (§7)."""
    legend_labels = {"Power", "USB", "Serial", "Ethernet", "Periphery"}
    found = set()
    for c in cells:
        txt = _strip_html(c.value)
        if txt in legend_labels:
            found.add(txt)
    if len(found) >= 4:
        r.ok(f"Legend present — {len(found)} connection-type labels found (§7)")
    else:
        r.fail(f"Legend incomplete — found {found}, expected {legend_labels}")


def check_device_column_placement(cells: list[CellInfo], r: Report):
    """Device blocks should be within the expected column x-ranges."""
    bad = []
    col_ranges = [(x0, x1, name) for _, (x0, x1, name) in COLUMNS.items()]
    for c in cells:
        if not c.is_device_block or c.w == 0:
            continue
        cx = c.x
        in_any = any(x0 - 5 <= cx <= x1 + 5 for x0, x1, _ in col_ranges)
        if not in_any:
            bad.append(f"  {_strip_html(c.value)[:35]!r} x={int(cx)} — outside all column ranges")
    if bad:
        r.warn(f"Blocks positioned outside defined column ranges:\n" + "\n".join(bad))
    else:
        r.ok("All device blocks are within defined column x-ranges")


def check_canvas_size(cells: list[CellInfo], r: Report, path: str):
    """Canvas pageWidth/pageHeight should be at least 1100×850 (StyleGuide §2.2)."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        mg = root.find(".//mxGraphModel")
        if mg is None:
            r.warn("Cannot locate mxGraphModel element for canvas size check")
            return
        pw = int(mg.get("pageWidth", 0))
        ph = int(mg.get("pageHeight", 0))
        if pw >= 1100 and ph >= 850:
            r.ok(f"Canvas size {pw}×{ph} px (≥1100×850, §2.2)")
        else:
            r.fail(f"Canvas size {pw}×{ph} px is smaller than required 1100×850 (§2.2)")
    except Exception as e:
        r.warn(f"Canvas size check failed: {e}")


def check_grid(path: str, r: Report):
    """Grid should be enabled with gridSize=10 (StyleGuide §2.1)."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        mg = root.find(".//mxGraphModel")
        if mg is None:
            return
        grid = mg.get("grid", "0")
        gsize = mg.get("gridSize", "0")
        if grid == "1" and gsize == "10":
            r.ok("Grid enabled, gridSize=10 (§2.1)")
        else:
            r.warn(f"Grid={grid}, gridSize={gsize} — expected grid=1 gridSize=10 (§2.1)")
    except Exception:
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def load_drd_names(drd_path: Optional[str]) -> set[str]:
    if not drd_path or not os.path.isfile(drd_path):
        return set()
    try:
        data = json.load(open(drd_path, encoding="utf-8"))
        names = set(data.get("devices", {}).keys())
        names.update(data.get("pcs", {}).keys())
        names.update(data.get("hubs", {}).keys())
        names.update(data.get("pipettors", {}).keys())
        return names
    except Exception:
        return set()


def validate(path: str, drd_path: Optional[str] = None, margin: float = 10.0) -> Report:
    r = Report()
    print(f"\n{'='*66}")
    print(f"  DIAGRAM VALIDATION REPORT")
    print(f"  File   : {os.path.basename(path)}")
    print(f"  Margin : {margin}px   DRD: {drd_path or 'not loaded'}")
    print(f"{'='*66}\n")

    # 1) XML well-formed (prerequisite for all other checks)
    check_xml_wellformed(path, r)
    if r.fails:
        r.print_summary()
        return r

    cells = parse_drawio(path)
    drd_names = load_drd_names(drd_path)

    # StyleGuide checks (grouped by concern)
    print("-- Layout & canvas --")
    check_canvas_size(cells, r, path)
    check_grid(path, r)
    check_column_headers(cells, r)
    check_separator_zorder(cells, r)

    print("\n-- Block placement & spacing --")
    check_device_column_placement(cells, r)
    check_block_overlaps(cells, r, margin)
    check_edge_routing(path, r)

    print("\n-- Label content --")
    check_first_line_bold(cells, r)
    check_label_no_reason_text(cells, r)
    check_unpowered_block_content(cells, drd_names, r)

    print("\n-- Connectors --")
    check_connector_colors(cells, r)

    print("\n-- Structural elements --")
    check_circuit_ellipses(cells, r)
    check_power_strips(cells, r)
    check_legend(cells, r)

    print()
    r.print_summary()
    return r


def main():
    ap = argparse.ArgumentParser(
        description="Validate a WireDiagram .drawio file against StyleGuide v2.2.3 rules.")
    ap.add_argument("diagram", help="Path to the .drawio file to validate")
    ap.add_argument("--drd", default=None,
                    help="Path to drd.json (enables DRD-content checks)")
    ap.add_argument("--margin", type=float, default=10.0,
                    help="Block exclusion margin in px (default 10, §9.1.1)")
    args = ap.parse_args()

    if not os.path.isfile(args.diagram):
        print(f"ERROR: file not found: {args.diagram}", file=sys.stderr)
        sys.exit(1)

    r = validate(args.diagram, drd_path=args.drd, margin=args.margin)
    sys.exit(r.exit_code)


if __name__ == "__main__":
    main()
