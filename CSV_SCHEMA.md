# Power Distribution CSV – schema 2.0

The CSV is the hand-off between the analysis (tab 2) and the diagram (tab 3). You can edit it in Excel or a text editor and then generate the diagram from it.

## Layout
One `.csv` file made of sections. A section starts with a line `# [NAME]`, then a header row, then data rows. Other lines starting with `#` are comments. Blank lines are ignored.

```
# [META]
# [DEVICES]
# [CIRCUITS]
# [CONNECTIONS]
```

Keep each record on one line. Where a cell holds several values (e.g. two comm cables) separate them with ` / `. Quoted cells that span lines are still read, in case Excel adds them.

## [META]
`key,value` rows.

| key | Example | Notes |
|---|---|---|
| schema_version | 2.0 | |
| project_name | Exact | |
| facility_circuit | 15A | 15A or 20A |
| pipettor | STAR / VANTAGE / STAR+VANTAGE | |
| pipettors | `STAR=STARline\|STAR\|C13\|6.3` | `id=model\|type\|power\|amps`, `; ` between pipettors |
| vantage_phase | L14-20 / L21-20 / C19 | blank if no VANTAGE |
| pc_type, main_pc_id | Standard PC | |
| hhs_mode | control_box / tcc / periphery / none | |
| over_80_approved | yes / no | |
| drd_version, styleguide_version | 1.1.1 / 2.2.3 | stamped into the diagram |
| generated_by | WireDiagram App 2.1.0 | |

## [DEVICES]
One row per physical unit (four Heater Shakers = four rows). Order lines that are not part of the diagram are not written.

| Column | Meaning |
|---|---|
| id | Unique name, used by CONNECTIONS (e.g. `Hamilton Heater Shaker #2`) |
| name / drd_name | Display name / DRD record name |
| part_number | Order part number(s) it came from |
| qty | Always 1 |
| amperage | Load in A (blank for children and dedicated L14-20/L21-20 VANTAGE) |
| plugs | Facility plugs the unit uses |
| power_cable / comm_cable | DRD cable names, ` / ` between several |
| category | `pc`, `hub`, `off_deck`, `on_deck`, `pipettor` (sets the column) |
| role | `powered`, `child`, `pipettor`, `none` |
| parent | id of the parent unit for children (placed beside it) |
| section | DRD section |
| width / height | Block size in px from the DRD (blank = default) |
| circuit | Circuit number, blank if none |
| source | Sales Order, Customer-supplied, Added by pairing, Mapped by user, Custom device… |
| powered / unpowered_reason / notes | Information only |
| connect_power_to / connect_comm_to | Information only in 2.0 (used to rebuild connections for 1.0 files) |

## [CIRCUITS]
| Column | Meaning |
|---|---|
| circuit | Circuit number (`1`, `2`…) |
| rating | 15A / 20A |
| voltage_outlet | e.g. `120V NEMA 5-15R`, `208V L14-20R` |
| load_a / headroom_a | Load and headroom in A |
| power_strip | Yes / No (more than 2 plugs) |
| devices | Loads on the circuit |

## [CONNECTIONS]
Every line the diagram draws. **Nothing else is drawn**, so add, remove or change rows here to change the wiring.

| Column | Meaning |
|---|---|
| source / target | Device `id`, or `Circuit N` for power |
| source_port / target_port | Optional port on a pipettor or box (e.g. `Div2`, `C13`, `USB A`, `USB A2`, `Ext USB B`, `Ext RJ45`); blank = the block itself |
| type | `power` (grey), `usb` (blue), `serial` (green), `ethernet` (pink), `periphery` (purple; also the catch-all for other cable types) |
| label | Cable name shown on the line |

## Schema 1.0 files
Still read. 1.0 had no ids or CONNECTIONS section, so the diagram rebuilds the wiring from `connect_power_to` / `connect_comm_to` where it can. Re-run the analysis to get a 2.0 file.
