# Wire Diagram Builder (v2.1.0)

Offline Windows app that turns a Hamilton Sales Order / Quote into a power
distribution analysis and a draw.io wiring diagram, following the StyleGuide
and the Device Reference Database (DRD).

Current data: **DRD v1.1.1**, **StyleGuide v2.2.3**, **CSV schema 2.0**.

## Workflow

### 1. Review order
1. **Browse** to the `.xlsx` and click **Load order**. (Or use **Start without order** to enter devices by hand.)
2. The order is split into two lists, without any prompts:
   * **Relevant for diagram** – lines whose **part number** matches the DRD (exact or wildcard such as `199xxx`). A kit part number becomes every DRD device that shares it.
   * **Not part of diagram** – everything else (carriers, tips, services, APE MISC, cables, pipettor lines…).
   Descriptions are **never** used to match a device.
3. Fix the lists:
   * **Move to Not part >** – take a device out. Tick **Remember for this part #** to make it permanent.
   * **< Map to DRD device…** – turn a line into one or more DRD devices (several = a kit). Can be remembered.
   * **< Map as custom…** – a CPR custom item that is not in the DRD.
   * **< Restore device** – undo a move (blue rows).
   * **Always ignore this part #** – the line always goes to *Not part of diagram*.
   * **Add customer-supplied (DRD)…** – only DRD devices can be picked.
   * **Add custom device…** – amperage (or watts), cables, category, role, block size. Saved outside the DRD for reuse.
   * **Edit qty / amps** (or double-click) – amperage override for "Varies", CPR values, or the largest draw between swappable devices.
4. **Pipettors** are always picked by hand (**Add…**): STARline (C13) or VANTAGEline (L14-20 / L21-20 dedicated, or C19 single-phase at 10 A). A system can have several, or none.
5. **Apply DRD §7 pairing** adds required parents/children (purple rows, e.g. BioTek 405 → BioTek Pump). Remove any you don't want; they won't be re-added.

Yellow rows still need an amperage.

### 2. Power analysis
Options: project name, 15 A / 20 A circuits, main PC (from order, or pick one; no PC = Customer-supplied PC values), HHS connection method (automatic, HHS Control Box, STAR TCC ports, VANTAGE Periphery Box), keep a C19 VANTAGE on its own circuit, approve going over 80 %.

**Run analysis** applies the rules:
* Child devices get no circuit and no load. Only a DRD note such as "Adds 1A to the HHS Control Box" adds load to the parent.
* HHS Control Box = 0.3 A + 1 A per unit, max 8 units per box; HHCs take the box's USB line first.
* Main PC on Circuit 1; L14-20/L21-20 VANTAGE on its own dedicated circuit, last.
* First-fit at 80 % of the rating, max 2 plugs per circuit before a power strip is needed. With over-80 % approved, the two lightest circuits are merged if they fit within 100 %.
* A hub is added when the main PC runs out of ports (never for a Customer-supplied PC).
* DRD §2 required pipettor ports (e.g. Centrifuge → STAR Div2) are connected.

**Move selected load to circuit** pins a load; the rest re-pack around it. **Clear moves** undoes it.

**Blocking issues** (missing/"Varies" amperage, missing parent, overloaded circuit, too few HHS boxes…) must be fixed before **Export CSV…** is enabled. **Save report (.txt)** saves the full analysis text.

### 3. Wiring diagram
Generate the `.drawio` from the current analysis, or from any Power Distribution CSV (including one you edited by hand). Only the rows in the CSV's `[CONNECTIONS]` section are drawn. The DRD/StyleGuide/schema versions are stored as a hidden comment in the file.

**Layout and routing (StyleGuide 9.1)**
* All blocks sit on the 10px grid. Paired devices are placed one after another: a control box's children start level with the box, and HHSs on a VANTAGE line up with the port they use.
* Circuits are placed level with the devices they power (at least 20px apart). Power strips are centred on their circuit.
* Every line is routed by `app/router.py` and written with waypoints. Lines stay 10px clear of blocks, never share a lane (parallel lines are 10px+ apart), cross only at right angles, never bend where another line passes, and enter/leave a block straight through one face. Pipettor ports are entered from the left.
* Line ends still float (no fixed exit/entry points), so lines can be dragged along a face in draw.io.
* If a line cannot be routed, the column gap it needs is widened by 20px and the layout is redone (up to 8 tries; row spacing also grows after 4). Any line still unrouted is reported as a warning.

### DRD tab
Shows the loaded DRD and StyleGuide versions, issues found while importing the DRD, and the saved part # rules (with delete). **Rebuild from DRD HTML…** parses a new `DeviceReferenceDatabase_vX.Y.Z.html` into the user data folder; the app then uses whichever DRD is newest.

## Files and folders

| Path | Purpose |
|---|---|
| `main.py` | Entry point |
| `app/gui.py` | Tkinter window (widgets only) |
| `app/session.py` | Controller used by the GUI and the tests |
| `app/review.py` | Order sorting, user actions, DRD §7 pairing |
| `app/power_engine.py` | Power distribution analysis |
| `app/drawio_gen.py` | `.drawio` generator |
| `app/csv_io.py` | CSV schema 2.0 reader/writer (reads 1.0 too) |
| `app/drd.py` | DRD HTML parser and lookups |
| `app/xlsx_parser.py` | Sales Order / Quote reader |
| `app/pn_overrides.py` | Saved part # rules |
| `app/user_devices.py` | Saved custom devices |
| `data/` | Shipped (read-only): `drd.json`, newest DRD + StyleGuide HTML |
| `tools/validate_diagram.py` | StyleGuide checks for a `.drawio` |
| `tests/` | Regression tests (Sanofi, AstraZeneca, Exact) |

**User data** (writable) is kept in `%APPDATA%\WireDiagramBuilder\` when running the `.exe`: `pn_overrides.json`, `user_devices.json` and a rebuilt `drd.json`. When running from source these go in `data/`.

## Updating the DRD or StyleGuide
1. Put the new `DeviceReferenceDatabase_vX.Y.Z.html` and/or `StyleGuide_vX.Y.Z.html` in `data/` (older versions can stay or be deleted).
2. Run `build_exe.bat`. It rebuilds `data\drd.json` from the newest DRD and bundles only the newest DRD and StyleGuide.

Or, without rebuilding the app: **DRD tab → Rebuild from DRD HTML…**

## Build (Windows)
Double-click `build_exe.bat` → `dist\WireDiagramBuilder.exe`.

From source: `pip install openpyxl` then `python main.py`.

## Tests
```
python -m pytest tests -q
```
or `python tests/test_examples.py`. They need `tests/fixtures/` (the three orders) and `data/drd.json`.

Check a diagram: `python tools/validate_diagram.py file.drawio`

The checker rebuilds every line the way draw.io draws it and fails on: bent lines with no waypoints, diagonal segments, lines through a block that is not one of their ends, lines drawn on top of each other, and parallel lines closer than 10px. 10px zig-zags are a warning.

## Changes in 2.1.0
* New orthogonal grid router; all lines get waypoints (10px lanes, no overlaps, no lines through blocks).
* Paired devices placed together; circuits level with their devices; legend placed before routing.
* Port fills by port type: P INTER ports green; STAR Div2/Div3/P1/P2 grey.
* Power strip gradient restored (`gradientColor=#000000`).
* PC block lists each power cable on its own line.
* HHS/HHC blocks list USB only on the unit that has a USB line; child devices show their own power cable.
* VANTAGE External USB B is only used when something is connected to the Periphery Box.
* "(C19 cord)" removed from circuit labels.
* Validator: real routing check (replaces the "open in draw.io" skip). 4 new validator tests (19 total).
