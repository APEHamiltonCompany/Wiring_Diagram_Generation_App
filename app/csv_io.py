"""
csv_io.py - Power Distribution CSV reader / writer (schema 2.0).

The CSV is the hand-off between the two app modes:

    MODE 1 (order review + analysis) ---writes---> Power Distribution CSV
    Power Distribution CSV ---read by---> MODE 2 ---> .drawio wiring diagram

The file is one .csv made of labelled sections. Each section starts with

    # [SECTION_NAME]

followed by its own header row and data rows. Other lines starting with '#'
are comments. See CSV_SCHEMA.md.

Sections
--------
# [META]        key,value pairs (project, rating, pipettors, DRD/StyleGuide versions)
# [DEVICES]     one row per physical unit that appears on the diagram
# [CIRCUITS]    one row per circuit
# [CONNECTIONS] every line the diagram draws (power and communication)

Schema 2.0 changes (from 1.0):
  * DEVICES has an ``id`` column (unique per unit). CONNECTIONS refer to ids.
  * DEVICES adds role, parent, drd_name, section, width, height, plugs.
  * One row per unit (qty is always 1). Excluded order lines are not written.
  * CONNECTIONS is required. Mode 2 draws only what is listed there.
Schema 1.0 files are still read; Mode 2 then rebuilds connections from the
old connect_power_to / connect_comm_to columns where it can.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field, asdict
from typing import Optional

SCHEMA_VERSION = "2.0"

DEVICE_COLUMNS = [
    "id", "name", "drd_name", "part_number", "qty", "amperage", "plugs",
    "power_cable", "comm_cable", "category", "role", "parent", "section",
    "width", "height", "circuit", "source", "powered", "unpowered_reason", "notes",
    # schema 1.0 columns kept for readability / backward compatibility
    "connect_power_to", "connect_comm_to",
]
CIRCUIT_COLUMNS = [
    "circuit", "rating", "voltage_outlet", "load_a", "headroom_a",
    "power_strip", "devices",
]
CONNECTION_COLUMNS = [
    "source", "source_port", "target", "target_port", "type", "label",
]
CONNECTION_TYPES = ("power", "usb", "serial", "ethernet", "periphery")


@dataclass
class DeviceRow:
    id: str = ""
    name: str = ""
    drd_name: str = ""
    part_number: str = ""
    qty: int = 1
    amperage: Optional[float] = None
    plugs: int = 0              # facility power cords (0 for children)
    power_cable: str = ""
    comm_cable: str = ""
    category: str = ""          # pipettor / pc / hub / on_deck / off_deck
    role: str = ""              # powered / child / pipettor
    parent: str = ""            # device id of the parent (children only)
    section: str = ""           # DRD section the record came from
    width: Optional[int] = None
    height: Optional[int] = None
    circuit: str = ""           # circuit number ("" for children / unpowered)
    source: str = "DRD"
    powered: bool = True
    unpowered_reason: str = ""
    notes: str = ""
    connect_power_to: str = ""
    connect_comm_to: str = ""

    def to_csv(self):
        d = asdict(self)
        d["amperage"] = "" if self.amperage is None else _fmt_amp(self.amperage)
        d["powered"] = "yes" if self.powered else "no"
        return {k: ("" if v is None else v) for k, v in d.items()}


@dataclass
class CircuitRow:
    circuit: str = ""
    rating: str = ""            # 15A / 20A / C19 10A
    voltage_outlet: str = ""    # 120V NEMA 5-15R / 208V L14-20R / 120V C19
    load_a: float = 0.0
    headroom_a: float = 0.0
    power_strip: str = "No"
    devices: str = ""           # "; "-joined device names

    def to_csv(self):
        d = asdict(self)
        d["load_a"] = _fmt_amp(self.load_a)
        d["headroom_a"] = _fmt_amp(self.headroom_a)
        return d


@dataclass
class ConnectionRow:
    source: str = ""            # device id, "Circuit N", "Power Strip N"
    source_port: str = ""
    target: str = ""            # device id (pipettor id + target_port for pipettor ports)
    target_port: str = ""
    type: str = ""              # power / usb / serial / ethernet / periphery
    label: str = ""             # cable name from the DRD


@dataclass
class PowerModel:
    meta: dict = field(default_factory=dict)
    devices: list = field(default_factory=list)
    circuits: list = field(default_factory=list)
    connections: list = field(default_factory=list)

    @property
    def pipettor(self):
        return self.meta.get("pipettor", "")

    @property
    def vantage_phase(self):
        return self.meta.get("vantage_phase", "")

    @property
    def facility_circuit(self):
        return self.meta.get("facility_circuit", "15A")

    @property
    def schema(self):
        return self.meta.get("schema_version", SCHEMA_VERSION)

    def device(self, dev_id):
        for d in self.devices:
            if d.id == dev_id:
                return d
        return None


def _fmt_amp(v):
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.1f}"


# --------------------------------------------------------------------------
def to_csv_text(model: PowerModel) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["# WireDiagram Power Distribution CSV"])
    w.writerow([f"# schema_version={SCHEMA_VERSION}"])
    w.writerow(["# Edit by hand or regenerate. Section markers are '# [SECTION]'."
                " Other '#' lines are ignored. Mode 2 draws only the CONNECTIONS rows."])
    w.writerow([])
    model.meta["schema_version"] = SCHEMA_VERSION
    w.writerow(["# [META]"])
    w.writerow(["key", "value"])
    for k, v in model.meta.items():
        w.writerow([k, v])
    w.writerow([])
    w.writerow(["# [DEVICES]"])
    w.writerow(DEVICE_COLUMNS)
    for d in model.devices:
        row = d.to_csv()
        w.writerow([_flat(row.get(c, "")) for c in DEVICE_COLUMNS])
    w.writerow([])
    w.writerow(["# [CIRCUITS]"])
    w.writerow(CIRCUIT_COLUMNS)
    for c in model.circuits:
        row = c.to_csv()
        w.writerow([_flat(row.get(col, "")) for col in CIRCUIT_COLUMNS])
    w.writerow([])
    w.writerow(["# [CONNECTIONS]"])
    w.writerow(CONNECTION_COLUMNS)
    for cn in model.connections:
        row = asdict(cn)
        w.writerow([_flat(row.get(col, "")) for col in CONNECTION_COLUMNS])
    w.writerow([])
    return buf.getvalue()


def _flat(v):
    """Keep every record on one line so the file stays easy to hand-edit."""
    if isinstance(v, str):
        return " / ".join(p.strip() for p in v.splitlines() if p.strip())
    return v


def write_csv(model: PowerModel, path: str):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(to_csv_text(model))


# --------------------------------------------------------------------------
def _section_of(line: str):
    s = line.strip()
    if s.startswith("#") and "[" in s and "]" in s:
        return s[s.index("[") + 1:s.index("]")].strip().upper()
    return None


def read_csv(path: str) -> PowerModel:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return read_csv_text(f.read())


def read_csv_text(text: str) -> PowerModel:
    """Parse the sectioned CSV. Uses csv.reader over the whole text so quoted
    cells that span several lines (hand edits in Excel) stay in one record."""
    model = PowerModel()
    cur = None
    header = None
    for row in csv.reader(io.StringIO(text)):
        first = row[0] if row else ""
        sec = _section_of(first) if len(row) <= 1 or not any(c.strip() for c in row[1:]) else None
        if sec is not None:
            cur, header = sec, None
            continue
        if not any(c.strip() for c in row):
            continue
        if first.strip().startswith("#"):
            continue
        row = [c.replace("\r", "").replace("\n", " / ") for c in row]
        if header is None:
            header = [c.strip() for c in row]
            continue
        if cur == "META":
            if len(row) >= 2 and row[0].strip():
                model.meta[row[0].strip()] = row[1].strip()
            continue
        rec = dict(zip(header, row))
        if cur == "DEVICES":
            model.devices.append(_device_from_rec(rec))
        elif cur == "CIRCUITS":
            model.circuits.append(_circuit_from_rec(rec))
        elif cur == "CONNECTIONS":
            model.connections.append(ConnectionRow(
                **{k: (rec.get(k) or "").strip() for k in CONNECTION_COLUMNS}))
    _fill_ids(model)
    return model


def _fill_ids(model):
    """Schema 1.0 files have no ids: give each row one and expand qty."""
    if all(d.id for d in model.devices):
        return
    out, used = [], set()
    for d in model.devices:
        n = max(1, int(d.qty or 1))
        for i in range(n):
            nd = DeviceRow(**{**asdict(d), "qty": 1})
            base = nd.id or nd.name or "Device"
            cand = base if (n == 1 and base not in used) else f"{base} #{i + 1}"
            k = 2
            while cand in used:
                cand = f"{base} #{k}"
                k += 1
            nd.id = cand
            used.add(cand)
            out.append(nd)
    model.devices = out


def _to_float(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s, default=None):
    f = _to_float(s)
    return default if f is None else int(f)


def _to_bool(s, default=True):
    s = (s or "").strip().lower()
    if s in ("yes", "true", "1", "y"):
        return True
    if s in ("no", "false", "0", "n"):
        return False
    return default


def _device_from_rec(rec) -> DeviceRow:
    g = lambda k: (rec.get(k) or "").strip()
    return DeviceRow(
        id=g("id"), name=g("name"), drd_name=g("drd_name") or g("name"),
        part_number=g("part_number"), qty=_to_int(g("qty"), 1) or 1,
        amperage=_to_float(g("amperage")), plugs=_to_int(g("plugs"), 0) or 0,
        power_cable=g("power_cable"), comm_cable=g("comm_cable"),
        category=g("category"), role=g("role"), parent=g("parent"),
        section=g("section"), width=_to_int(g("width")), height=_to_int(g("height")),
        circuit=g("circuit"), source=g("source") or "DRD",
        powered=_to_bool(g("powered")), unpowered_reason=g("unpowered_reason"),
        notes=g("notes"), connect_power_to=g("connect_power_to"),
        connect_comm_to=g("connect_comm_to"),
    )


def _circuit_from_rec(rec) -> CircuitRow:
    g = lambda k: (rec.get(k) or "").strip()
    return CircuitRow(circuit=g("circuit"), rating=g("rating"),
                      voltage_outlet=g("voltage_outlet"),
                      load_a=_to_float(g("load_a")) or 0.0,
                      headroom_a=_to_float(g("headroom_a")) or 0.0,
                      power_strip=g("power_strip") or "No", devices=g("devices"))
