"""
xlsx_parser.py - Sales Order / Quote .xlsx parser (Mode 1 input).

No sample XLSX was provided, so the parser is deliberately tolerant:
  * It scans the first worksheet(s) for a header row by looking for known column
    keywords (part number, description/name, quantity, amperage, etc.).
  * It then reads every data row below the header into a LineItem.
  * It copes with merged title rows, blank rows and extra columns.

The output is a list of LineItem which Mode 1 turns into DeviceInput objects
(after DRD matching) in power_engine.

Because the exact Sales Order layout is unknown, the GUI always lets the user
review and correct the parsed line items before running the analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


# Canonical field -> ordered list of acceptable header names (highest priority
# first).  Matching is EXACT on the normalised header text (and, only as a
# fallback, a whole-word/token match) so that wide Sales Order sheets with noise
# columns like "Config description", "Net amount" or "Unit price" can never be
# mistaken for the Device / Part # / Qty columns.
#
# Part # intentionally prefers "Product number" (standard SO export) and falls
# back to "Item" (the QTE export, which has no Product number column).
FIELD_HEADERS = {
    "part_number": [
        "product number", "item", "part number", "part #", "part no.",
        "part no", "part", "item number", "material number", "material",
        "sku", "model number",
    ],
    "name": [
        "product name", "description", "device name", "device",
        "item name", "line item", "name",
    ],
    "qty": [
        "quantity", "qty", "qty.", "count",
    ],
    "amperage": [
        "amperage", "amps", "amp", "current (a)", "current",
    ],
    "watts": [
        "wattage", "watts", "watt", "power (w)", "power(w)",
    ],
}

QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


@dataclass
class LineItem:
    name: str = ""
    part_number: str = ""
    qty: int = 1
    amperage: Optional[float] = None
    watts: Optional[float] = None
    raw: dict = field(default_factory=dict)


def _norm_header(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _tokens(h):
    """Split a normalised header into alphanumeric tokens (keeps '#')."""
    return [t for t in re.split(r"[^a-z0-9#]+", h) if t]


def _match_columns(header_cells):
    """Map columns -> canonical fields for one candidate header row.

    Two passes, both prioritised by FIELD_HEADERS order and each column used at
    most once:
      1. EXACT match on the normalised header text.
      2. Whole-token fallback for any field still unassigned (e.g. a header of
         "Part Number (EU)" whose exact text didn't match).  Noise columns like
         "Net amount" never qualify because "amount" is not a candidate token.
    Returns {col_index: field}.
    """
    norm = {ci: _norm_header(c) for ci, c in enumerate(header_cells)}
    norm = {ci: h for ci, h in norm.items() if h}
    assigned = {}   # field -> ci
    used = set()

    # Pass 1: exact match.
    for field, candidates in FIELD_HEADERS.items():
        for cand in candidates:
            hit = next((ci for ci, h in norm.items()
                        if ci not in used and h == cand), None)
            if hit is not None:
                assigned[field] = hit
                used.add(hit)
                break

    # Pass 2: token fallback only for fields that are still unassigned.
    for field, candidates in FIELD_HEADERS.items():
        if field in assigned:
            continue
        for cand in candidates:
            hit = next((ci for ci, h in norm.items()
                        if ci not in used and cand in _tokens(h)), None)
            if hit is not None:
                assigned[field] = hit
                used.add(hit)
                break

    return {ci: field for field, ci in assigned.items()}


def _detect_header_row(rows):
    """Return (header_row_index, {col_index: field}) or (None, {})."""
    best = None
    for ri, row in enumerate(rows[:40]):  # header is near the top
        mapping = _match_columns(row)
        fields = set(mapping.values())
        # a valid header needs at least a name OR part_number column
        if "name" in fields or "part_number" in fields:
            score = len(fields)
            if best is None or score > best[0]:
                best = (score, ri, mapping)
    if best:
        return best[1], best[2]
    return None, {}


def _to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


def _to_qty(v):
    if v is None:
        return 1
    if isinstance(v, (int, float)):
        return max(1, int(round(v)))
    m = QTY_RE.match(str(v))
    if m:
        return max(1, int(round(float(m.group(1)))))
    m2 = re.search(r"(\d+)", str(v))
    return max(1, int(m2.group(1))) if m2 else 1


def parse_xlsx(path: str) -> list:
    """Parse a Sales Order / Quote workbook into a list of LineItem."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse .xlsx files.")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    items = []
    for ws in wb.worksheets:
        rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
        if not rows:
            continue
        hdr_idx, mapping = _detect_header_row(rows)
        if hdr_idx is None:
            continue
        for row in rows[hdr_idx + 1:]:
            if row is None:
                continue
            rec = {}
            for ci, fld in mapping.items():
                if ci < len(row):
                    rec[fld] = row[ci]
            name = str(rec.get("name") or "").strip()
            part = str(rec.get("part_number") or "").strip()
            # Trim trailing separator noise from SO "Product number" values
            # like "STAR-2 : 006272 :  :  :" -> "STAR-2 : 006272".
            part = re.sub(r"[\s:]+$", "", part)
            if not name and not part:
                continue
            # skip obvious non-line rows (totals, subtotals)
            low = (name + " " + part).lower()
            if any(k in low for k in ["subtotal", "grand total", "total:", "tax",
                                      "shipping", "freight"]):
                continue
            li = LineItem(
                name=name or part,
                part_number=part,
                qty=_to_qty(rec.get("qty")),
                amperage=_to_float(rec.get("amperage")),
                watts=_to_float(rec.get("watts")),
                raw=rec,
            )
            items.append(li)
        if items:
            break  # use the first sheet that yielded line items
    wb.close()
    return items


def make_sample_xlsx(path: str, rows: list, title: str = "Sales Order"):
    """Create a small sample Sales Order workbook for testing the parser.

    `rows` is a list of (name, part_number, qty) tuples.
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl is required.")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Order"
    ws.append([title])
    ws.append([])
    ws.append(["Line", "Part Number", "Description", "Quantity"])
    for i, (name, part, qty) in enumerate(rows, 1):
        ws.append([i, part, name, qty])
    wb.save(path)
    return path
