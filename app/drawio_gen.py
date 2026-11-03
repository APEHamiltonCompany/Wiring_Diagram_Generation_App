"""
drawio_gen.py - Mode 2: Power Distribution CSV (schema 2.0) -> .drawio diagram.

Rules followed (StyleGuide v2.2.3):
* Six columns: Facility Requirements, System Requirements, PC Connections,
  Off Deck Devices, On Deck Devices, Pipettor Connections.
* Only the CONNECTIONS rows are drawn. Colours: power black, USB blue, serial
  green, Ethernet pink, anything else purple (catch-all).
* Lines attach to the blocks with floating ends (no fixed exit/entry points)
  so they are easy to adjust in draw.io. Every line gets waypoints from the
  grid router (router.py): 10px lanes, 10px clear of every block, around
  blocks instead of through them (StyleGuide 9.1).
* Block sizes come from the DRD Dimensions carried in the CSV. All blocks sit
  on the 10px grid.
* Paired devices are placed together: a control box's on-deck devices start
  at the control box's height; off-deck children sit beside their parent.
* Circuits sit next to the devices they feed (spacing follows the content).
* Columns widen when a gap needs more 10px lanes (Column Expansion Rule).
* Pipettor ports are filled by PORT TYPE (StyleGuide 6.1.5 / 6.2.2) when used.
* Power strips use the StyleGuide 10.6 style (gradientColor=#000000).
* The DRD / StyleGuide versions are written into the file as an XML comment.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

from .csv_io import PowerModel, ConnectionRow
from .drd import split_cables, cable_kind, PIPETTOR_VARIANTS
from .router import Router, Rect, Terminal

# --------------------------------------------------------------------------
# Line / connector colours (spec sec 8.13).  Reference diagrams use draw.io's
# short hex forms (#0000, #FFFF, ...) which draw.io expands automatically; we
# keep them verbatim for byte-level fidelity with the approved files.
# --------------------------------------------------------------------------
C_POWER = "#000000"   # full 6-digit: '#0000' is parsed as transparent-alpha by
                      # browser/SVG renderers, making power lines invisible.
C_USB = "#007CF7"
C_SERIAL = "#009900"
C_ETHERNET = "#FF4AE7"
C_PERIPHERY = "#6600CC"

LINE_COLORS = {
    "power": C_POWER, "usb": C_USB, "serial": C_SERIAL,
    "ethernet": C_ETHERNET, "periphery": C_PERIPHERY,
}

# Port / section styles (spec sec 8.11, verbatim from the reference diagrams) ----
PS_USB = "rounded=1;whiteSpace=wrap;html=1;fillColor=#C4E4FF;gradientColor=#6600CC;fontColor=#FFFFFF;strokeColor=#314354;"
PS_SERIAL = "rounded=1;whiteSpace=wrap;html=1;fillColor=#00FF00;gradientColor=#009900;fontColor=#FFFFFF;strokeColor=#314354;"
PS_ETHERNET = "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFCCFA;gradientColor=#FF4AE7;fontColor=#FFFFFF;strokeColor=#314354;"
PS_POWER = "rounded=1;whiteSpace=wrap;html=1;fillColor=#CCCCCC;gradientColor=default;fontColor=#FFFFFF;strokeColor=#314354;"
PS_UNFILLED = "rounded=1;whiteSpace=wrap;html=1;fillColor=none;gradientColor=none;fontColor=#000000;strokeColor=#314354;"
PS_SECTION = "rounded=1;whiteSpace=wrap;html=1;fillColor=#F5F5F5;strokeColor=#666666;"

PORT_FILL = {"usb": PS_USB, "serial": PS_SERIAL, "ethernet": PS_ETHERNET, "power": PS_POWER}

# Fill of a used port comes from the PORT TYPE, not from the line that uses it
# (StyleGuide 6.1.5 STAR, 6.2.2 VANTAGE Periphery Box / P INTER).
STAR_PORT_KIND = {
    "TCC1": "serial", "TCC2": "serial", "Ext. Can": "serial", "EX IO": "serial", "Div1": "serial",
    "Div2": "power", "Div3": "power", "P1": "power", "P2": "power",
    "USB B": "usb", "C13": "power",
}
VANTAGE_PORT_KIND = {
    "USB A": "usb", "DB9": "serial", "RJ45": "ethernet",
    "High": "serial", "Medium": "serial", "Low": "serial", "General": "serial", "Limited": "serial",
    "Ext RJ45": "ethernet", "Ext USB B": "usb", "power": "power", "P SKT": "power",
}

PERIPH_INVENTORY = {"USB A": 3, "DB9": 4, "RJ45": 4, "High": 1,
                    "Medium": 2, "Low": 1, "General": 2, "Limited": 1}
PERIPH_DEFAULT = ("USB A", "DB9", "RJ45", "High", "Medium", "Low", "General")
_PERIPH_RE = re.compile(r"(USB A|DB9|RJ45|High|Medium|Low|General|Limited)[\s_-]*(\d*)", re.I)


def _periph_key(key):
    """(base, index) of a VANTAGE Periphery Box port key ("USB A2" -> ("USB A", 2)), else None."""
    m = _PERIPH_RE.fullmatch((key or "").strip())
    if not m:
        return None
    base = next(b for b in PERIPH_INVENTORY if b.lower() == m.group(1).lower())
    return base, max(1, int(m.group(2) or 1))


def _periph_name(base, idx):
    return base if idx == 1 else f"{base}{idx}"


def _port_kind(ptype, key):
    if ptype == "STAR":
        return STAR_PORT_KIND.get(key, "")
    if key.startswith("PWR"):
        return "power"
    pk = _periph_key(key)
    return VANTAGE_PORT_KIND.get(pk[0] if pk else key, "")

# Ellipse variants (STAR Div/P power ports are circles) ---------------------
def _ellipse(style):
    return "ellipse;" + style[len("rounded=1;"):] if style.startswith("rounded=1;") else style

# Block styles (spec sec 8.4-8.10) -----------------------------------------
STYLE_CIRCUIT = "ellipse;whiteSpace=wrap;html=1;aspect=fixed;fillColor=#FFFFFF;gradientColor=#FF4545;strokeColor=#B85450;"
STYLE_POWERSTRIP = ("rounded=1;whiteSpace=wrap;html=1;strokeWidth=2;gradientColor=#000000;"
                    "gradientDirection=south;glass=1;fillColor=#CCCCCC;fontColor=#FFFFFF;")
STYLE_HUB = ("rounded=1;whiteSpace=wrap;html=1;shadow=0;glass=1;sketch=0;strokeWidth=2;"
             "fillColor=#F8CECC;gradientColor=#FFCCE6;strokeColor=#B85450;")
STYLE_DEVICE = "rounded=1;whiteSpace=wrap;html=1;shadow=0;strokeWidth=2;fillColor=#FFFFFF;gradientColor=#D5E8D4;glass=1;"
STYLE_PC = "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6c8ebf;gradientColor=#B3B3B3;"
STYLE_STAR = ("rounded=1;whiteSpace=wrap;html=1;strokeWidth=2;shadow=0;sketch=0;align=center;"
              "fillColor=#CCE5FF;gradientColor=#0000FF;strokeColor=#36393d;arcSize=7;glass=1;container=1;")
STYLE_VANTAGE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=default;strokeColor=#10739e;arcSize=7;"
                 "gradientColor=#0000FF;glass=1;container=1;")
STYLE_HEADER = ("text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;"
                "fontFamily=Helvetica;fontSize=11;")
STYLE_SECTION_TITLE = "text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontStyle=1;"
STYLE_PIP_TITLE = "text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontStyle=1;fontSize=13;fontColor=#000000;"
STYLE_AMP_LABEL = "text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;"
STYLE_LEGEND_BOX = "rounded=1;whiteSpace=wrap;html=1;"
STYLE_LEGEND_TITLE = "text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;"
STYLE_LEGEND_LABEL = "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;"
STYLE_SEPARATOR = "endArrow=none;dashed=1;html=1;dashPattern=1 3;strokeWidth=2;rounded=1;strokeColor=default;"
STYLE_LEGEND_LINE = "endArrow=none;html=1;strokeWidth=3;endFill=0;jumpStyle=arc;edgeStyle=orthogonalEdgeStyle;strokeColor={c};"

# Connector base (spec sec 9.1) - no exitX/entryX, source/target only --------
EDGE_BASE = "edgeStyle=orthogonalEdgeStyle;rounded=1;curved=0;jumpStyle=arc;html=1;strokeWidth=2;endArrow=none;"

# Default column layout (spec sec 8.2): (title, default_width) in order -------
COLS = [
    ("Facility Requirements", 140),
    ("System Requirements", 130),
    ("PC Connections", 180),
    ("Off Deck Devices", 200),
    ("On Deck Devices", 150),
    ("Pipettor Connections", 150),
]
COL_LEFT0 = 190  # left edge of column 1


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _comm_kind(comm: str) -> str:
    low = (comm or "").lower()
    if not low or low == "-":
        return ""
    if "can-bus" in low or "odtc connector" in low:
        return "periphery"
    if "usb" in low:
        return "usb"
    if "ethernet" in low or "rj45" in low or "tcp" in low or "poe" in low:
        return "ethernet"
    if any(k in low for k in ["rs-232", "rs232", "db9", "db15", "db25", "d15", "serial"]):
        return "serial"
    if "barrel" in low:
        return "serial"
    return "periphery"


def _fmt(v):
    if v is None:
        return ""
    f = float(v)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else f"{f:.1f}"


def _power_cables(spec):
    spec = (spec or "").strip()
    if not spec or spec == "-":
        return []
    m = re.match(r"\s*(\d+)\s*x\s*(.+)", spec, re.I)
    if m:
        return [m.group(2).strip()] * int(m.group(1))
    return [spec]


def _comm_lines(spec):
    """Split a comm_cable field into individual cable lines (e.g. 'Ethernet, USB')."""
    spec = (spec or "").strip()
    if not spec or spec == "-":
        return []
    return [p.strip() for p in spec.split(",") if p.strip()]


def _is_hhx(name):
    low = (name or "").lower()
    return "hhs" in low or "hhc" in low or "heater shaker" in low or "heater cooler" in low


def _is_hhc(name):
    low = (name or "").lower()
    return "hhc" in low or "heater cooler" in low



def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _int(v, default=0):
    try:
        return int(float(str(v).split(",")[0]))
    except (TypeError, ValueError):
        return default


def _w(d, default=120):
    return int(d.width) if getattr(d, "width", None) else default


def _h(d, default=60):
    return int(d.height) if getattr(d, "height", None) else default


def _is_star_v(row):
    return "STAR-V" in f"{row.name} {row.id} {row.drd_name}".upper()


def _pip_title(row, ptype):
    name = (row.name or "").strip()
    return name if name in PIPETTOR_VARIANTS.get(ptype, []) else ptype


def _is_feeder(ref):
    return ref.startswith("Circuit") or ref.startswith("Power Strip")


def _pip_type(row, model):
    txt = f"{row.name} {row.id} {row.drd_name}".upper()
    if "VANTAGE" in txt or "STAR-V" in txt:
        return "VANTAGE"
    if "STAR" in txt:
        return "STAR"
    return (model.pipettor or "STAR").split("+")[0].upper()


def _legacy_connections(model):
    """Schema 1.0 CSVs: rebuild connections from connect_power_to / connect_comm_to."""
    pcs = [d for d in model.devices if d.category == "pc"]
    main = pcs[0].id if pcs else ""
    if main and "main_pc_id" not in model.meta:
        model.meta["main_pc_id"] = main
    strips = {c.circuit for c in model.circuits if (c.power_strip or "").lower().startswith("y")}
    for c in strips:
        model.connections.append(ConnectionRow(f"Power Strip {c}", "", f"Circuit {c}", "", "power", ""))
    names = {d.name: d.id for d in model.devices}
    for d in model.devices:
        if d.powered and d.circuit:
            src = f"Power Strip {d.circuit}" if d.circuit in strips else f"Circuit {d.circuit}"
            n = max(1, len(_power_cables(d.power_cable)))
            port = ("C13" if _pip_type(d, model) == "STAR" else "power") if d.category == "pipettor" else ""
            for _ in range(n if d.category != "pipettor" else 1):
                model.connections.append(ConnectionRow(src, "", d.id, port, "power", d.power_cable))
        elif d.connect_power_to and d.connect_power_to in names:
            d.parent = names[d.connect_power_to]
        tgt = (d.connect_comm_to or "").strip()
        if d.category in ("pc",) or not d.comm_cable:
            continue
        tid = main if tgt.lower() in ("pc", "") else names.get(tgt, "")
        for cab in split_cables(d.comm_cable):
            k = cable_kind(cab)
            if k and k != "power" and tid and tid != d.id:
                model.connections.append(ConnectionRow(d.id, "", tid, "", k, cab))

# --------------------------------------------------------------------------
# Lightweight element / edge records
# --------------------------------------------------------------------------
class Node:
    __slots__ = ("id", "value", "style", "x", "y", "w", "h", "parent", "html")

    def __init__(self, id, value, style, x, y, w, h, parent="1", html=True):
        self.id, self.value, self.style = id, value, style
        self.x, self.y, self.w, self.h = x, y, w, h
        self.parent, self.html = parent, html


class Edge:
    __slots__ = ("id", "src", "tgt", "color", "points")

    def __init__(self, id, src, tgt, color, points=None):
        self.id, self.src, self.tgt, self.color = id, src, tgt, color
        self.points = points or []


# --------------------------------------------------------------------------
GAP_Y = 20          # vertical gap between stacked blocks
MAX_TRIES = 8       # layout / route attempts (columns widen between tries)


class DrawioGenerator:
    """Draws a PowerModel (CSV schema 2.0).

    Only the rows in model.connections are drawn - Mode 2 never invents a
    connection. Blocks use the DRD Dimensions carried in the DEVICES rows.
    Lines attach to the block (floating source/target, no fixed exit/entry
    points) and get router waypoints, so they stay easy to move in draw.io."""

    def __init__(self, model: PowerModel):
        self.m = model
        self.warnings = []
        self.extra = [0] * len(COLS)      # extra width per column (lane room)
        self.gap_y = GAP_Y
        self.face_pref = True
        self.split = set()                # device columns drawn as two halves
        self._reset()

    def _reset(self):
        self._id = 0
        self.nodes = []
        self.edges = []
        self.free_edges = []
        self.separators = []
        self.cols = []
        self.port_id = {}       # (pipettor id, port key) -> cell id
        self.port_rect = {}     # (pipettor id, port key) -> absolute Rect
        self.rect = {}          # cell id -> absolute Rect (top-level shapes)
        self.col_of = {}        # cell id -> column index
        self.cell = {}          # device id / "Circuit N" / "Power Strip N" -> cell id
        self.route_fail = []

    # ---- id + node factories ----------------------------------------
    def nid(self):
        self._id += 1
        return f"c{self._id}"

    def node(self, value, style, x, y, w, h, parent="1", html=True, col=None):
        n = Node(self.nid(), value, style, int(round(x)), round(y, 2), w, h, parent, html)
        self.nodes.append(n)
        if parent == "1":
            self.rect[n.id] = Rect(n.x, n.y, w, h)
            if col is not None:
                self.col_of[n.id] = col
        return n.id

    def edge(self, src, tgt, color, points=None):
        if not src or not tgt:
            return None
        e = Edge(self.nid(), src, tgt, color, points)
        self.edges.append(e)
        return e.id

    # =================================================================
    # PHASE 1 - plan
    # =================================================================
    def _plan(self):
        m = self.m
        if not m.connections:
            _legacy_connections(m)
        self.devs = {d.id: d for d in m.devices}
        self.pip_rows = [d for d in m.devices if d.category == "pipettor"]
        self.pip_ids = {p.id for p in self.pip_rows}
        pc_main = m.meta.get("main_pc_id", "")
        self.pcs = sorted([d for d in m.devices if d.category == "pc"],
                          key=lambda d: d.id != pc_main)
        self.hubs = [d for d in m.devices if d.category == "hub"]
        self.pc_ids = {d.id for d in self.pcs}
        self.hub_ids = {d.id for d in self.hubs}
        self.main_pc = self.pcs[0].id if self.pcs else ""
        rest = [d for d in m.devices if d.category not in ("pc", "hub", "pipettor")]
        self.children = {}
        for d in rest:
            if d.parent and d.parent in self.devs:
                self.children.setdefault(d.parent, []).append(d)
        # off-deck: stand-alone + parents; their off-deck children sit beside them
        self.offdeck = [d for d in rest if d.category != "on_deck"
                        and not (d.parent in self.devs and self.devs[d.parent].category == "off_deck"
                                 and d.category == "off_deck")]
        self.beside = {p: [c for c in kids if c.category == "off_deck"
                           and self.devs[p].category == "off_deck"]
                       for p, kids in self.children.items()}
        self.ondeck = [d for d in rest if d.category == "on_deck"]
        # devices with a USB line (HHS/HHC labels list USB only on those)
        self.usb_ends = set()
        for c in m.connections:
            if c.type == "usb":
                self.usb_ends.update((c.source, c.target))
        # pipettor ports: filled when used, colour by PORT TYPE
        self.ptype = {p.id: _pip_type(p, m) for p in self.pip_rows}
        self.star_v = {p.id for p in self.pip_rows if self.ptype[p.id] == "VANTAGE" and _is_star_v(p)}
        self.filled = {}
        for c in m.connections:
            for end, port in ((c.target, c.target_port), (c.source, c.source_port)):
                if end in self.pip_ids and port:
                    key = self._pkey(end, port)
                    self.filled[(end, key)] = _port_kind(self.ptype[end], key) or c.type or "power"
        self.links = {}
        for c in m.connections:
            self.links.setdefault(c.source, []).append((c.target, c.target_port, c.type))
            self.links.setdefault(c.target, []).append((c.source, c.source_port, c.type))
        self.pc_target = {}
        for pc in self.pcs[1:]:
            ends = {o for o, _p, k in self.links.get(pc.id, []) if k != "power"}
            if len(ends) == 1:
                o = next(iter(ends))
                if o in self.devs and self.devs[o].category in ("off_deck", "on_deck"):
                    self.pc_target[pc.id] = o
        for p in self.pip_rows:
            if self.ptype[p.id] == "STAR":
                self.filled.setdefault((p.id, "EX IO"), "serial")     # always filled (6.1.5)
            elif p.id not in self.star_v:
                self.filled.setdefault((p.id, "PWR 1"), "power")
                self.filled.setdefault((p.id, "P SKT"), "power")

    def _pkey(self, ref, port):
        if port and self.ptype.get(ref) == "VANTAGE":
            pk = _periph_key(port)
            if pk:
                return _periph_name(*pk)
        return port

    # =================================================================
    # PHASE 2 - layout
    # =================================================================
    def _col_x(self, idx):
        return self.cols[idx][1]

    def _compute_columns(self):
        widths = [w for _, w in COLS]
        need = 0
        for p in self.offdeck:
            kids = self.beside.get(p.id, [])
            if kids:
                w = 20 + _w(p) + sum(20 + _w(k) for k in kids) + 40
                need = max(need, w)
        widths[3] = max(widths[3], need, max([_w(d) + 60 for d in self.offdeck] + [0]))
        widths[4] = max(widths[4], max([_w(d) + 50 for d in self.ondeck] + [0]))
        row = [d for d in self.pcs if d.id not in self.pc_target]
        widths[2] = max([widths[2], 180, 20 + sum(_w(d) + 40 for d in row)]
                        + [_w(d) + 60 for d in self.hubs]
                        + [_w(d) + 60 for d in self.pcs if d.id in self.pc_target])
        lane_w = {3: max([_w(p) + sum(20 + _w(k) for k in self.beside.get(p.id, []))
                          for p in self.offdeck] + [120]),
                  4: max([_w(d) for d in self.ondeck] + [80])}
        for c in (3, 4):
            if c in self.split:
                widths[c] = max(widths[c], 40 + 2 * lane_w[c] + self._mid_gap(c))
        widths = [_g(w + e) for w, e in zip(widths, self.extra)]
        self.cols = []
        left = COL_LEFT0
        for i, (title, _w0) in enumerate(COLS):
            self.cols.append((title, left, widths[i]))
            left += widths[i]
        self.page_right = left
        self.lane_x = {}
        for c in (3, 4):
            self.lane_x[(c, 0)] = self.cols[c][1] + 20
            if c in self.split:
                self.lane_x[(c, 1)] = self.cols[c][1] + widths[c] - 20 - lane_w[c]

    def _too_tall(self):
        """First device column whose stack is much taller than the PC/hub stack and
        the pipettors (1.5x) or above 1400px; it is split into two halves."""
        hubs = max([_h(p, 80) for p in self.pcs if p.id not in self.pc_target] + [0]) \
            + sum(_h(hb, 50) + 60 for hb in self.hubs)
        limit = min(1400, 1.5 * max(hubs, self.pip_bottom - 90))
        for c in (3, 4):
            rows = {(r.y, r.y2) for cid, r in self.rect.items() if self.col_of.get(cid) == c}
            if c not in self.split and sum(b - a + self.gap_y for a, b in rows) > limit:
                return c
        return None

    def _inner(self, d):
        """Devices that go in the half of a split column nearer the pipettor."""
        to_pip = any(o in self.pip_ids for o, _p, _k in self.links.get(d.id, []))
        return to_pip if d.category == "on_deck" else to_pip or bool(self._kids_on_deck(d))

    def _mid_gap(self, col):
        """Gap between the two halves: room for the lines the inner half sends left."""
        left = self.pc_ids | self.hub_ids
        n = 0
        for d in (self.offdeck if col == 3 else self.ondeck):
            if self._inner(d) or col == 4:
                n += sum(1 for o, _p, _k in self.links.get(d.id, []) if o in left or _is_feeder(o)
                         or (col == 4 and o in self.devs and self.devs[o].category == "off_deck"))
        return _g(max(40, (n + 1) * 10 + 20))

    def _layout(self):
        self._compute_columns()
        for title, left, w in self.cols:
            self.node(title, STYLE_HEADER, left, 20, w, 40)
        self._layout_pipettors()
        self._layout_devices()
        col = self._too_tall()
        if col is not None:
            self.split.add(col)
            self._reset()
            return self._layout()
        self._layout_pcs()
        self._layout_circuits()
        self._layout_legend()
        self._wire()
        self._finalise_frame()

    # ---- pipettors ---------------------------------------------------
    def _port(self, pip_id, body_id, key, label, ox, oy, w, h, ellipse=False, origin=(0, 0)):
        kind = self.filled.get((pip_id, key))
        style = PORT_FILL.get(kind, PS_POWER) if kind else PS_UNFILLED
        if ellipse:
            style = _ellipse(style)
        cid = self.node(label, style, ox, oy, w, h, parent=body_id)
        self.port_id[(pip_id, key)] = cid
        self.port_rect[(pip_id, key)] = Rect(origin[0] + ox, origin[1] + oy, w, h)
        return cid

    def _layout_pipettors(self):
        px = self._col_x(5) + 30
        py = 90
        bottom = py
        for p in self.pip_rows:
            if self.ptype[p.id] == "STAR":
                h = self._build_star(p, px, py)
            else:
                h = self._build_vantage(p, px, py)
            bottom = py + h
            py += h + 40
        self.pip_bottom = bottom

    def _build_star(self, pip, bx, by):
        H = 410
        body = self.node("", STYLE_STAR, bx, by, 90, H, col=5)
        self.cell[pip.id] = body
        P, o = pip.id, (bx, by)
        self.node(_pip_title(pip, "STAR"), STYLE_PIP_TITLE, 0, 0, 90, 20, parent=body)
        self.node("", PS_SECTION, 10, 20, 70, 250, parent=body)
        self.node("Periphery", STYLE_SECTION_TITLE, 10, 20, 70, 20, parent=body)
        for key, oy in [("TCC1", 40), ("TCC2", 60), ("Ext. Can", 80), ("EX IO", 100), ("Div1", 120)]:
            self._port(P, body, key, key, 20, oy, 50, 20, origin=o)
        for key, oy in [("Div2", 140.87), ("Div3", 170.87), ("P1", 200.87), ("P2", 230.87)]:
            self._port(P, body, key, key, 30, oy, 30, 30, ellipse=True, origin=o)
        ib = self.node("Int. Board", PS_SECTION, 10, 280, 70, 30, parent=body)
        self.port_id[(P, "Int. Board")] = ib
        self.port_rect[(P, "Int. Board")] = Rect(bx + 10, by + 280, 70, 30)
        self.node("", PS_SECTION, 10, 320, 70, 80, parent=body)
        self.node("External", STYLE_SECTION_TITLE, 10, 320, 70, 20, parent=body)
        self._port(P, body, "USB B", "USB B", 20, 340, 50, 20, origin=o)
        self._port(P, body, "C13", "C13", 20, 360, 50, 20, origin=o)
        amp = f"{_fmt(pip.amperage)}A" if pip.amperage else "6.3A"
        self.node(amp, STYLE_AMP_LABEL, 10, 380, 70, 20, parent=body)
        return H

    def _build_vantage(self, pip, bx, by):
        P, o = pip.id, (bx, by)
        phase = (pip.power_cable or self.m.vantage_phase or "L14-20").strip()
        used = {}
        for (pid, k) in self.filled:
            pk = _periph_key(k) if pid == P else None
            if pk:
                used[pk[0]] = max(used.get(pk[0], 0), pk[1])
        count = {b: min(n, max(int(b in PERIPH_DEFAULT), used.get(b, 0)))
                 for b, n in PERIPH_INVENTORY.items()}
        periph_ports = sum(count.values())
        ac_box = P not in self.star_v
        periph_h = 60 + periph_ports * 20
        H = 200 + periph_ports * 20 + (170 if ac_box else 0)
        body = self.node("", STYLE_VANTAGE, bx, by, 90, H, col=5)
        self.cell[P] = body
        self.node(_pip_title(pip, "VANTAGE"), STYLE_PIP_TITLE, 0, 0, 90, 20, parent=body)
        self.node("", PS_SECTION, 10, 20, 70, periph_h, parent=body)
        self.node("Periphery Box", STYLE_SECTION_TITLE, 10, 20, 70, 30, parent=body)
        oy = 50
        for base in PERIPH_INVENTORY:
            if base == "High":
                self.node("P INTER", STYLE_SECTION_TITLE, 10, oy, 70, 20, parent=body); oy += 20
            for i in range(1, count[base] + 1):
                self._port(P, body, _periph_name(base, i), base, 20, oy, 50, 20, origin=o); oy += 20
        ext_y = oy + 20
        self.node("", PS_SECTION, 10, ext_y, 70, 90, parent=body)
        self.node("External", STYLE_SECTION_TITLE, 10, ext_y, 70, 20, parent=body)
        self._port(P, body, "Ext RJ45", "RJ45", 20, ext_y + 20, 50, 20, origin=o)
        self._port(P, body, "Ext USB B", "USB B", 20, ext_y + 40, 50, 20, origin=o)
        self._port(P, body, "power", _esc(phase), 20, ext_y + 60, 50, 20, origin=o)
        if ac_box:
            ac_y = ext_y + 100
            self.node("", PS_SECTION, 10, ac_y, 70, 170, parent=body)
            self.node("AC Box", STYLE_SECTION_TITLE, 10, ac_y, 70, 20, parent=body)
            for i, lab in enumerate(["PWR 1", "PWR 2", "PWR 3", "PWR 4", "PWR 5", "PWR 6"]):
                self._port(P, body, lab, lab, 20, ac_y + 20 + i * 20, 50, 20, origin=o)
            self._port(P, body, "P SKT", "P SKT", 20, ac_y + 140, 50, 20, origin=o)
        return H

    # ---- PCs + hubs ------------------------------------------------------
    def _pc_block(self, pc, x, y):
        w, h = _w(pc), _h(pc, 80)
        name = "PC &amp; Peripheries" if pc.id == self.m.meta.get("main_pc_id", "") \
            else _esc(pc.name)
        lines = [f"<b>{name}</b>"]
        if pc.amperage:
            lines.append(f"{_fmt(pc.amperage)}A")
        # every power cable on its own line (a 2-plug PC lists two)
        lines += [_esc(c) for c in self._power_lines(pc)]
        h = max(h, _g(12 + 16 * len(lines)))
        self.cell[pc.id] = self.node("<br>".join(lines), STYLE_PC, x, _g(y), w, h, col=2)
        return w, h

    def _end_y(self, ref, port):
        r = self._end_rect(ref, port)
        return None if r is None else r.y + r.h / 2

    def _layout_pcs(self):
        """PCs side by side at the top, hubs stacked below with the gaps sized from
        the lines that leave between them (busiest hub last), and a PC wired to a
        single device level with that device."""
        x = self._col_x(2) + 20
        y, bx, row_h = 90, x, 0
        for pc in self.pcs:
            if pc.id not in self.pc_target:
                w, h = self._pc_block(pc, bx, y)
                bx += w + 40
                row_h = max(row_h, h)
        main_down = sum(1 for o, _p, k in self.links.get(self.main_pc, [])
                        if k != "power" and o not in self.hub_ids)
        y += row_h
        above = main_down
        hubs = sorted(self.hubs, key=lambda hb: (len(self.links.get(hb.id, [])), self.hubs.index(hb)))
        for hb in hubs:
            w, h = _w(hb, 120), _h(hb, 50)
            mid = y + 60 + h / 2
            ys = [self._end_y(o, p) for o, p, _k in self.links.get(hb.id, [])]
            up = sum(1 for v in ys if v is None or v < mid)
            y += max(60, (up + above + 1) * 10 + 20)
            lines = [f"<b>{_esc(hb.name)}</b>"] + [_esc(c) for c in split_cables(hb.comm_cable)]
            self.cell[hb.id] = self.node("<br>".join(lines), STYLE_HUB, x, y, w, h, col=2)
            y += h
            above = len(ys) - up
        y += 40
        for pc in self.pcs:
            if pc.id in self.pc_target:
                want = self.rect[self.cell[self.pc_target[pc.id]]].y
                _w2, h = self._pc_block(pc, x, max(want, y))
                y = max(want, y) + h + 40
        self.pc_bottom = y
        self.devices_bottom = max(self.devices_bottom, y)

    def _power_lines(self, d):
        """Power cables of a device, one entry per physical cable."""
        out = []
        for cab in split_cables(d.power_cable):
            out += _power_cables(cab)
        n_conn = sum(1 for c in self.m.connections
                     if c.type == "power" and c.target == d.id and not c.source == d.id)
        if len(out) == 1 and n_conn > 1:
            out = out * n_conn
        return out

    # ---- devices ---------------------------------------------------------
    def _dev_label(self, d):
        lines = [f"<b>{_esc(d.name)}</b>"]
        if d.role != "child" and d.amperage:
            lines.append(f"{_fmt(d.amperage)}A")
        if d.role != "child":
            for cab in self._power_lines(d):
                lines.append(_esc(cab))
        else:
            # a child that has its own power cable (e.g. BioTek Pump C13R to C13P)
            for cab in split_cables(d.power_cable):
                if cab and cab != "-":
                    lines.append(_esc(cab))
        hhx = _is_hhx(d.name)
        for cab in split_cables(d.comm_cable):
            if cab.lower().startswith("see "):
                continue
            # StyleGuide 5.4 HHS table: USB only on units that have a USB line
            if hhx and cable_kind(cab) == "usb" and d.id not in self.usb_ends:
                continue
            lines.append(_esc(cab))
        return "<br>".join(lines)

    def _block(self, d, x, y, col):
        w, h = _w(d), self._dev_h(d)
        self.cell[d.id] = self.node(self._dev_label(d), STYLE_DEVICE, x, _g(y), w, h, col=col)
        return w, h

    def _dev_h(self, d):
        return _h(d, 0) or _g(max(60, 12 + 16 * self._dev_label(d).count("<br>") + 16))

    def _kids_on_deck(self, d):
        return [k for k in self.children.get(d.id, []) if k.category == "on_deck"]

    def _port_ys(self, d):
        """Centre y of every pipettor port the device is wired to."""
        out = []
        for o, p, _k in self.links.get(d.id, []):
            key = self._pkey(o, p)
            if o in self.pip_ids and (o, key) in self.port_rect:
                r = self.port_rect[(o, key)]
                out.append(r.y + r.h / 2)
        return out

    def _port_only(self, d):
        """Port ys of a device wired only to pipettor ports (plus PCs / hubs)."""
        ys = self._port_ys(d)
        if not ys or any(o not in self.pip_ids and o not in self.pc_ids and o not in self.hub_ids
                         for o, _p, _k in self.links.get(d.id, [])):
            return None
        return ys

    def _channels(self):
        """Clusters of pipettor-port ys wired from the left of the device columns
        (PCs, hubs, power strips, circuits); those lanes cross both device columns."""
        ys = []
        for c in self.m.connections:
            for p, port, o in ((c.target, c.target_port, c.source), (c.source, c.source_port, c.target)):
                key = self._pkey(p, port)
                left = o in self.pc_ids or o in self.hub_ids or _is_feeder(o)
                if p in self.pip_ids and left and (p, key) in self.port_rect:
                    r = self.port_rect[(p, key)]
                    ys.append(r.y + r.h / 2)
        out = []
        for v in sorted(ys):
            if out and v - out[-1][1] <= 30:
                out[-1][1] = v
            else:
                out.append([v, v])
        return [(a - 10, b + 10) for a, b in out]

    def _reserve_ports(self, ports):
        """Port-only devices: block tops inside the straight-line window of their
        ports, stacked in port order and centred on the group."""
        top0 = min(self.rect[self.cell[p.id]].y for p in self.pip_rows)
        bot0 = self.pip_bottom
        out = []
        for d, ys in sorted(ports, key=lambda t: (t[0].category == "on_deck", min(t[1]), t[0].id)):
            h = self._dev_h(d)
            lo, hi = max(ys) - h + 10, min(ys) - 10
            want = (min(ys) + max(ys)) / 2 - h / 2
            if lo <= hi:
                want = min(max(want, lo), hi)
            out.append([d, want, h, lo, hi])
        groups = []
        for item in out:
            g = groups[-1] if groups else None
            if g and g[-1][0].category == item[0].category and g[-1][1] + g[-1][2] + self.gap_y > item[1]:
                g.append(item)
            else:
                groups.append([item])
        for g in groups:
            span = sum(i[2] for i in g) + self.gap_y * (len(g) - 1)
            mid = sum(i[1] + i[2] / 2 for i in g) / len(g)
            y = min(max(mid - span / 2, top0), max(top0, bot0 - span))
            for i in g:
                i[1] = _g(y)
                y += i[2] + self.gap_y
        return [(d, y, h, max(top0, min(y, lo) - h), min(bot0, max(y + h, hi + h) + h))
                for d, y, h, lo, hi in out]

    def _layout_devices(self):
        gy = self.gap_y
        occ = {k: [] for k in self.lane_x}
        placed = set()
        last = {c: max(k for cc, k in self.lane_x if cc == c) for c in (3, 4)}

        def reserve(col, a, b, lanes=None):
            for k in occ:
                if k[0] == col and (lanes is None or k[1] in lanes):
                    occ[k].append((a + gy, b - gy))

        def fits(col, y, h):
            return y >= 90 and all(y + h + gy <= a or b + gy <= y for a, b in occ[col])

        def near(col, h, want, lo=90):
            want = max(lo, _g(want))
            cands = [want] + [_g(b + gy) for _a, b in occ[col]] + [_g(a - gy - h) for a, _b in occ[col]]
            return min((c for c in cands if c >= lo and fits(col, c, h)),
                       key=lambda c: (abs(c - want), c))

        def put(d, lane, y, dx=0):
            w, h = self._block(d, self.lane_x[lane] + dx, y, lane[0])
            occ[lane].append((_g(y), _g(y) + h))
            placed.add(d.id)
            return w, h

        def kids_h(d):
            return sum(self._dev_h(k) + gy for k in self._kids_on_deck(d))

        def key(d):
            circ = [_int(c) for c in (d.circuit or "").split(",") if c.strip()]
            return (min(circ) if circ else 99, kids_h(d))

        def put_kids(d, y):
            kids = [k for k in self._kids_on_deck(d) if k.id not in placed]
            if kids:
                ky = near((4, 0), kids_h(d) - gy, y)
                for k in kids:
                    ky += put(k, (4, 0), ky)[1] + gy
        for a, b in self._channels():
            reserve(3, a, b)
            reserve(4, a, b)
        ports = []
        for d in self.offdeck + self.ondeck:
            ys = self._port_only(d)
            par = self.devs.get(d.parent)
            if ys and not (par and par.category in ("off_deck", "on_deck")):
                ports.append((d, ys))
        port_ys = {d.id: ys for d, ys in ports}
        for d, y, h, a, b in self._reserve_ports(ports):
            col = 4 if d.category == "on_deck" else 3
            put(d, (col, last[col]), y)
            reserve(col, a, b, (last[col],))
            if col == 3:
                reserve(4, min(port_ys[d.id]) - 10, max(port_ys[d.id]) + 10)
        cursor = {k: 90 for k in occ}
        for d in sorted(self.offdeck, key=key):
            if d.id in placed:
                put_kids(d, self.rect[self.cell[d.id]].y)
                continue
            row = [d] + self.beside.get(d.id, [])
            row_h = max(self._dev_h(k) for k in row)
            lanes = [(3, last[3])] if 3 in self.split and self._inner(d) else \
                [k for k in occ if k[0] == 3]
            spots = [(near(k, row_h, cursor[k], cursor[k]), k[1], k) for k in lanes]
            y, _i, lane = min(spots)
            dx = 0
            for k in row:
                dx += put(k, lane, y, dx)[0] + 20
            occ[lane][-len(row):] = [(y, y + row_h)]
            cursor[lane] = y + row_h + gy
            put_kids(d, y)
        for d in self.ondeck:
            if d.id not in placed:
                ys = self._port_ys(d)
                h = self._dev_h(d)
                lane = (4, last[4] if ys else 0)
                put(d, lane, near(lane, h, sum(ys) / len(ys) - h / 2 if ys else 90))
        self.devices_bottom = max([self.rect[self.cell[i]].y2 for i in placed] + [self.pip_bottom])

    # ---- circuits -----------------------------------------------------
    def _end_rect(self, ref, port):
        port = self._pkey(ref, port)
        if port and (ref, port) in self.port_rect:
            return self.port_rect[(ref, port)]
        cid = self.cell.get(ref)
        return self.rect.get(cid) if cid else None

    def _layout_circuits(self):
        def dedicated(c):
            v = c.voltage_outlet or ""
            return "L14" in v or "L21" in v
        def isolated(c):
            return dedicated(c) or any(self.devs.get(x.strip()) and self.devs[x.strip()].category
                                       == "pipettor" for x in (c.devices or "").split(";")) \
                and len([x for x in (c.devices or "").split(";") if x.strip()]) == 1
        ordered = sorted(self.m.circuits, key=lambda c: (isolated(c), _int(c.circuit)))
        strip_conns = {}
        targets = {}
        for c in self.m.connections:
            if c.type != "power" or _is_feeder(c.source) == _is_feeder(c.target):
                continue
            if _is_feeder(c.source):
                feed, dev, port = c.source, c.target, c.target_port
            else:
                feed, dev, port = c.target, c.source, c.source_port
            if feed.startswith("Power Strip"):
                strip_conns[feed] = strip_conns.get(feed, 0) + 1
                feed = "Circuit " + feed.split()[-1]
            r = self._end_rect(dev, port)
            if r is not None:
                targets.setdefault(feed, []).append(r.y + r.h / 2)
        prev_bottom = 70
        self.circuits_bottom = 90
        for c in ordered:
            ded = dedicated(c)
            size = 90 if ded else 80
            name = f"Circuit {c.circuit}"
            sname = f"Power Strip {c.circuit}"
            has_strip = (c.power_strip or "").lower().startswith("y") or sname in strip_conns
            sh = max(90, 10 + strip_conns.get(sname, 3) * 10) if has_strip else 0
            ext = max(size, sh)
            ys = targets.get(name)
            want_c = sum(ys) / len(ys) if ys else prev_bottom + self.gap_y + ext / 2
            top = max(want_c - ext / 2, prev_bottom + self.gap_y)
            if has_strip:
                sy = _g(top + (ext - sh) / 2)
                cy = sy + sh / 2 - size / 2
            else:
                cy = _g(top + (ext - size) / 2)
            cx = 215 if ded else 220
            label = f"<b>Circuit {c.circuit}</b><br>{c.rating}<br>{c.voltage_outlet}"
            self.cell[name] = self.node(label, STYLE_CIRCUIT, cx, cy, size, size, col=0)
            bottom = cy + size
            if has_strip:
                sx = self._col_x(1) + 20
                self.cell[sname] = self.node("<b>Power Strip</b>", STYLE_POWERSTRIP, sx, sy, 40, sh, col=1)
                bottom = max(bottom, sy + sh)
            prev_bottom = bottom
            self.circuits_bottom = max(self.circuits_bottom, bottom)

    # ---- legend ---------------------------------------------------------
    def _all_rects(self):
        return [(r.x, r.y, r.w, r.h) for r in self.rect.values()]

    def _overlaps(self, r, rects, pad=10):
        x, y, w, h = r
        for (ox, oy, ow, oh) in rects:
            if not (x + w + pad <= ox or ox + ow + pad <= x or
                    y + h + pad <= oy or oy + oh + pad <= y):
                return True
        return False

    def _layout_legend(self):
        items = [("Power", C_POWER), ("USB", C_USB), ("Serial", C_SERIAL),
                 ("Ethernet", C_ETHERNET), ("Periphery", C_PERIPHERY)]
        w, h = 140, 50 + len(items) * 20
        rects = [(r.x, r.y, r.w, r.h) for cid, r in self.rect.items()
                 if self.col_of.get(cid) is not None]
        bottom = max([ry + rh for (_rx, ry, _rw, rh) in rects] + [self.devices_bottom])
        # below the pipettors first (out of the routing lanes), then the corners
        cands = [
            (self._col_x(5) + 10, _g(self.pip_bottom + 30)),
            (self.page_right + 10, 70),
            (self.page_right - w - 10, _g(bottom + 30)),
            (200, _g(bottom + 30)),
        ]
        chosen = None
        for (x, y) in cands:
            if not self._overlaps((x, y, w, h), rects):
                chosen = (x, y)
                break
        if chosen is None:
            chosen = (200, _g(bottom + 30))
        x, y = chosen
        self.legend_rect = (x, y, w, h)
        self.node("", STYLE_LEGEND_BOX, x, y, w, h, col=-1)
        self.node("<b><u>Legend</u></b>", STYLE_LEGEND_TITLE, x + 50, y + 5, 40, 23)
        ly = y + 30
        for label, color in items:
            self.free_edges.append(
                ("legend", STYLE_LEGEND_LINE.format(c=color), x + 15, ly + 10, x + 55, ly + 10))
            self.node(f"<b>{label}</b>", STYLE_LEGEND_LABEL, x + 65, ly, 70, 20)
            ly += 23

    # ---- wiring ---------------------------------------------------------
    def _terminal(self, ref, port, other="", kind=""):
        """(cell id, Terminal, column) for one end of a connection."""
        port = self._pkey(ref, port)
        if port and (ref, port) in self.port_id:
            body = self.rect[self.cell[ref]]
            return self.port_id[(ref, port)], Terminal(self.port_rect[(ref, port)], body, ("W",)), 5
        if port and ref in self.pip_ids:
            self.warnings.append(f"Port '{port}' not found on {ref}; line attached to the body.")
        cid = self.cell.get(ref)
        if not cid:
            return None, None, None
        r = self.rect[cid]
        col = self.col_of.get(cid, 0)
        if ref in self.pip_ids:
            return cid, Terminal(r, r, ("W",)), 5
        if ref.startswith("Power Strip"):
            return cid, Terminal(r, faces=("E", "W")), col
        if ref in self.hub_ids:
            return cid, Terminal(r, faces=("N", "S")), col
        prefer = None
        if ref in self.pc_ids and self.face_pref:
            if kind == "power" and other.startswith("Power Strip"):
                prefer = ("W",)
            elif kind != "power" and ref == self.main_pc:
                prefer = ("S",)
        return cid, Terminal(r, prefer=prefer), col

    def _sep_xs(self):
        return [COL_LEFT0] + [left + w for (_t, left, w) in self.cols]

    def _wire(self):
        obstacles = [r for cid, r in self.rect.items() if self.col_of.get(cid) is not None]
        bottom = max([r.y2 for r in obstacles] + [self.devices_bottom])
        router = Router(obstacles, (COL_LEFT0 + 10, 70, self.page_right - 10, bottom + 200),
                        vlines=self._sep_xs())
        jobs = []
        for i, c in enumerate(self.m.connections):
            s, st, sc = self._terminal(c.source, c.source_port, c.target, c.type)
            t, tt, tc = self._terminal(c.target, c.target_port, c.source, c.type)
            if not s or not t:
                self.warnings.append(f"Connection {c.source} -> {c.target} skipped: "
                                     f"{'source' if not s else 'target'} not on the diagram.")
                continue
            a, b = st.rect, tt.rect
            dist = abs((a.x + a.w / 2) - (b.x + b.w / 2)) + abs((a.y + a.h / 2) - (b.y + b.h / 2))
            jobs.append((dist, i, c, s, st, sc, t, tt, tc))
        # short lines first: they take the inner lanes and the longer ones nest outside
        jobs.sort(key=lambda j: (j[0], j[1]))
        pts = {}
        for j in jobs:
            p = router.route(j[4], j[7], lid=j[1])
            if p is not None:
                pts[j[1]] = p
        self._rip_up(router, jobs, pts)
        self.diagnosis = []
        for dist, i, c, s, st, sc, t, tt, tc in jobs:
            if i not in pts:
                self.route_fail.append((c, sc, tc))
                self.diagnosis.append(router.blockers(st, tt))
            self.edge(s, t, LINE_COLORS.get(c.type, C_PERIPHERY), pts.get(i, []))
        self.lines_bottom = max([p[1] for e in self.edges for p in e.points] + [0])

    def _rip_up(self, router, jobs, pts, rounds=3, max_lines=4):
        """Rip-up and reroute: free the lines that block a failed one, route the
        failed line first, then put the removed lines back. Undone if any of them
        then fails."""
        by_id = {j[1]: j for j in jobs}
        for _round in range(rounds):
            failed = [j for j in jobs if j[1] not in pts]
            if not failed:
                return
            progress = False
            for j in failed:
                p = router.route(j[4], j[7], lid=j[1])
                if p is not None:
                    pts[j[1]] = p
                    progress = True
                    continue
                diag = router.blockers(j[4], j[7])
                if not diag or not diag[0] or len(diag[0]) > max_lines or diag[1]:
                    continue
                lids = [k for k in diag[0] if k in pts]
                old = {k: (router.uncommit(k), pts.pop(k)) for k in lids}
                new = {}
                p = router.route(j[4], j[7], lid=j[1])
                ok = p is not None
                if ok:
                    new[j[1]] = p
                    for k in sorted(lids, key=lambda k: (by_id[k][0], k)):
                        q = router.route(by_id[k][4], by_id[k][7], lid=k)
                        if q is None:
                            ok = False
                            break
                        new[k] = q
                if ok:
                    pts.update(new)
                    progress = True
                    continue
                for k in new:
                    router.uncommit(k)
                for k, (nodes, q) in old.items():
                    router.commit(nodes, k)
                    pts[k] = q
            if not progress:
                return

    # =================================================================
    # Frame
    # =================================================================
    def _finalise_frame(self):
        rects = self._all_rects()
        max_y = max([ry + rh for (_rx, ry, _rw, rh) in rects] + [850, self.lines_bottom + 20])
        max_y = int(max_y + 30)
        self.frame_bottom = max_y
        for x in self._sep_xs():
            self.separators.append(("v", x, 50, x, max_y))
        self.separators.append(("h", COL_LEFT0, 50, self.page_right, 50))
        self.separators.append(("h", COL_LEFT0, max_y, self.page_right, max_y))

    # =================================================================
    # PHASE 3 - emit XML
    # =================================================================
    def _emit(self):
        parts = []
        width = int(max(1100, self.page_right + 50,
                        self.legend_rect[0] + self.legend_rect[2] + 10))
        height = int(max(850, self.frame_bottom + 10))
        parts.append('<mxGraphModel dx="1422" dy="794" grid="1" gridSize="10" guides="1" '
                     'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
                     f'pageWidth="{width}" pageHeight="{height}" math="0" shadow="0">')
        parts.append("<root>")
        parts.append('<mxCell id="0" />')
        parts.append('<mxCell id="1" parent="0" />')
        for n in self.nodes:
            val = quoteattr(n.value or "")
            parts.append(
                f'<mxCell id="{n.id}" value={val} style="{n.style}" vertex="1" '
                f'parent="{n.parent}"><mxGeometry x="{_num(n.x)}" y="{_num(n.y)}" '
                f'width="{n.w}" height="{n.h}" as="geometry" /></mxCell>')
        for e in self.edges:
            pts = ""
            if e.points:
                inner = "".join(f'<mxPoint x="{_num(px)}" y="{_num(py)}" />' for px, py in e.points)
                pts = f'<Array as="points">{inner}</Array>'
            parts.append(
                f'<mxCell id="{e.id}" style="{EDGE_BASE}strokeColor={e.color};" edge="1" '
                f'parent="1" source="{e.src}" target="{e.tgt}" value="">'
                f'<mxGeometry relative="1" as="geometry">{pts}</mxGeometry></mxCell>')
        for (_kind, style, x1, y1, x2, y2) in self.free_edges:
            parts.append(
                f'<mxCell id="{self.nid()}" style="{style}" edge="1" parent="1" value="">'
                f'<mxGeometry relative="1" as="geometry">'
                f'<mxPoint x="{_num(x1)}" y="{_num(y1)}" as="sourcePoint" />'
                f'<mxPoint x="{_num(x2)}" y="{_num(y2)}" as="targetPoint" />'
                f'</mxGeometry></mxCell>')
        # dashed separators LAST (highest z-order, spec 8.3 / check 12)
        for (_o, x1, y1, x2, y2) in self.separators:
            parts.append(
                f'<mxCell id="{self.nid()}" style="{STYLE_SEPARATOR}" edge="1" parent="1" value="">'
                f'<mxGeometry relative="1" as="geometry">'
                f'<mxPoint x="{_num(x1)}" y="{_num(y1)}" as="sourcePoint" />'
                f'<mxPoint x="{_num(x2)}" y="{_num(y2)}" as="targetPoint" />'
                f'</mxGeometry></mxCell>')
        parts.append("</root></mxGraphModel>")
        body = "".join(parts)
        meta = self.m.meta
        def _safe(v):
            v = str(v or "?").replace("-->", "").replace("--", "-")
            return v.rstrip("-") or "?"
        stamp = ("<!-- WireDiagram App | DRD v" + _safe(meta.get("drd_version")) +
                 " | StyleGuide v" + _safe(meta.get("styleguide_version")) +
                 " | CSV schema " + _safe(meta.get("schema_version")) +
                 " | " + _safe(meta.get("generated_on")) + " -->\n")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + stamp +
                '<mxfile host="WireDiagramApp" type="device">\n'
                f'<diagram id="wirediagram" name={quoteattr(self.m.meta.get("project_name") or "Wiring Diagram")}>'
                + body + '</diagram>\n</mxfile>\n')

    def _expand(self, fails, diagnosis):
        """Column Expansion Rule: widen the column where the diagnostic route had to
        cut through blocks; without a diagnosis, the gaps along the failed span."""
        grow = set()
        for (_c, sc, tc), diag in zip(fails, diagnosis):
            cols = set()
            for ix, _iy in (diag[1] if diag else []):
                for k, (_t, left, w) in enumerate(self.cols):
                    if left <= ix * 10 < left + w:
                        cols.add(k)
            if not cols:
                lo, hi = sorted((sc if sc is not None else 0, tc if tc is not None else 0))
                cols = {k for k in {lo, max(lo, hi - 1)} if 0 <= k < len(self.extra) - 1}
            grow |= cols
        for k in grow:
            self.extra[k] += 20

    # ---- public ----------------------------------------------------
    def generate(self) -> str:
        self._plan()
        base_warn = list(self.warnings)
        best, stale, fallback = None, 0, True

        def run(pref):
            nonlocal best
            self.face_pref = pref
            self._reset()
            self.warnings = list(base_warn)
            self._layout()
            self.face_pref = True
            if best is None or len(self.route_fail) < len(best["route_fail"]):
                best = dict(self.__dict__)
                return True
            return False
        for attempt in range(MAX_TRIES):
            improved = run(True)
            if not self.route_fail:
                break
            diagnosis = self.diagnosis
            fails = self.route_fail
            if fallback:
                fallback = False
                improved = run(False) or improved
                if not self.route_fail:
                    break
            stale = 0 if improved else stale + 1
            if stale >= 2 or attempt == MAX_TRIES - 1:
                break
            self._expand(fails, diagnosis)
            if attempt >= 3:
                self.gap_y = GAP_Y + 10
        self.__dict__.update(best)
        for c, _sc, _tc in self.route_fail:
            self.warnings.append(f"Line {c.source} -> {c.target} could not be routed on the 10px "
                                 f"grid; drawn without waypoints, adjust it in draw.io.")
        return self._emit()


def _g(v):
    """Snap to the 10px grid."""
    return int(round(float(v) / 10.0)) * 10


def _num(v):
    f = float(v)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else f"{f:g}"


def generate_drawio(model: PowerModel, out_path: str) -> str:
    gen = DrawioGenerator(model)
    xml = gen.generate()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    return xml
