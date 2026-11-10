import re

import docx

from .models import CprData, CprDevice, CprPart, CprOption, CprSystem

SPEC_HEADING_RE = re.compile(r'Part\s*#\s*(\d+-\d+)')
NEW_SPEC_HEADING_RE = re.compile(r'^\s*Part\s+(\d+)\s*[\u2014\u2013-]\s*(?:[^:]*:\s*)?(.+?)\s*$')
PART_HEADER_MARKERS = ('SK Part Number', 'Price Per Each')
PART_HEADER_MARKERS_ALT = ('SK Part #', 'Unit Price')
PART_COLUMN_ALIASES = (
    ('Quantity', ('Quantity', 'Qty')),
    ('Part Name', ('Part Name',)),
    ('SharePoint', ('SharePoint', 'APE Part')),
    ('SK Part', ('SK Part',)),
    ('Price', ('Price',)),
)
CHECKED_GLYPHS = ('\u2612', '\u2611')
GLYPHS = ('\u2610', '\u2612', '\u2611')
SYSTEM_STOP_RE = re.compile(r'Conceptual Layout|Custom Parts|^Part\s+\d+\s*[\u2014\u2013-]')


def _full_text(element):
    parts = []
    for node in element.iter():
        tag = node.tag.split('}')[-1]
        if tag == 't':
            parts.append(node.text or '')
        elif tag == 'br':
            parts.append('\n')
    return ''.join(parts)


def _local(node):
    return node.tag.split('}')[-1]


def _row_tcs(tr):
    tcs = []
    for child in tr:
        tag = _local(child)
        if tag == 'tc':
            tcs.append(child)
        elif tag == 'sdt':
            nested = [node for node in child.iter() if _local(node) == 'tc']
            if nested:
                tcs.extend(nested)
            else:
                tcs.append(child)
    return tcs


def _grid_span(tc):
    for node in tc.iter():
        if _local(node) == 'gridSpan':
            for key, value in node.attrib.items():
                if key.split('}')[-1] == 'val':
                    return int(value)
    return 1


def _tr_grid(tr):
    cells = []
    column = 0
    for tc in _row_tcs(tr):
        span = _grid_span(tc)
        cells.append((column, span, _full_text(tc).strip()))
        column += span
    return cells


def _tr_cells(tr):
    return [text for _, _, text in _tr_grid(tr)]


def _table_rows(table):
    return [_tr_cells(row._tr) for row in table.rows]


def _cell_in_span(grid_row, column, width):
    found = ''
    for start, span, text in grid_row:
        if start < column + width and column < start + span and text:
            found = text
    return found


NUMBER_RE = re.compile(r'-?\d+(?:\.\d+)?')


def _parse_number(value):
    match = NUMBER_RE.search(str(value).replace(',', ''))
    if match is None:
        raise ValueError(f'Could not read a number from CPR cell {value!r}')
    return float(match.group())


def _find_parts(doc):
    for table in doc.tables:
        rows = [_tr_grid(row._tr) for row in table.rows]
        if not rows:
            continue
        header = rows[0]
        if not any(all(any(marker in text for _, _, text in header) for marker in markers)
                   for markers in (PART_HEADER_MARKERS, PART_HEADER_MARKERS_ALT)):
            continue
        columns = {}
        for name, aliases in PART_COLUMN_ALIASES:
            columns[name] = None
            for alias in aliases:
                columns[name] = next(((index, start, span) for index, (start, span, text) in enumerate(header) if alias in text), None)
                if columns[name] is not None:
                    break
        parts = []
        for row in rows[1:]:
            def _get(name):
                position = columns[name]
                if position is None:
                    return ''
                index, start, span = position
                if len(row) == len(header):
                    text = row[index][2]
                else:
                    text = _cell_in_span(row, start, span)
                return text.replace('\xa0', ' ').strip()
            part_name = _get('Part Name')
            sk_pn = _get('SK Part')
            if not part_name and not _get('Quantity') and not _get('Price'):
                continue
            parts.append(CprPart(
                sk_pn=sk_pn,
                sharepoint_id=_get('SharePoint'),
                part_name=part_name,
                quantity=_parse_number(_get('Quantity')) if _get('Quantity') else 0.0,
                price=_parse_number(_get('Price')) if _get('Price') else 0.0,
            ))
        return parts
    raise ValueError('No Custom Parts List table found in CPR document')


def _find_devices(doc):
    devices = []
    for table in doc.tables:
        rows = _table_rows(table)
        if not any(len(row) >= 2 and row[0] in ('OTP Number', 'Base Device OTP #', 'OTP #') and row[1] for row in rows):
            continue
        kv = {}
        for row in rows:
            if len(row) >= 2 and row[0] and row[1]:
                kv[row[0]] = row[1]
        otp_number = kv.get('OTP Number') or kv.get('Base Device OTP #') or kv.get('OTP #', '')
        if not otp_number:
            continue
        availability = kv.get('Device Availability', '')
        if not availability and 'Customer Supplied' in kv:
            availability = f"Customer Supplied: {kv['Customer Supplied']}"
        quantity = 1
        if 'OTP #' in kv and ('Customer Supplied' in kv or 'Device Category' in kv):
            match = NUMBER_RE.search(kv.get('Quantity', ''))
            if match is not None:
                value = int(float(match.group()))
                if value > 0:
                    quantity = value
        devices.append(CprDevice(
            otp_number=otp_number,
            vendor=kv.get('Vendor') or kv.get('Manufacturer', ''),
            model_name=kv.get('Model Name', ''),
            availability=availability,
            quantity=quantity,
        ))
    return devices


def _read_spec_names(doc):
    spec_names = {}
    heading_names = {}
    current = None
    for child in doc.element.body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            text = _full_text(child)
            match = SPEC_HEADING_RE.search(text)
            if match:
                current = 'APE-' + match.group(1)
            else:
                new_match = NEW_SPEC_HEADING_RE.match(text)
                if new_match:
                    heading_names[new_match.group(1)] = new_match.group(2)
        elif tag == 'tbl':
            if current is None:
                continue
            for tr in child:
                if tr.tag.split('}')[-1] != 'tr':
                    continue
                cells = _tr_cells(tr)
                if len(cells) >= 2 and cells[0] == 'Part Name':
                    spec_names[current] = cells[1]
                    break
    return spec_names, heading_names


def _clean_text(text):
    return ' '.join(text.replace('\u200b', '').replace('\xa0', ' ').split())


def _has_glyph(text):
    return any(glyph in text for glyph in GLYPHS)


PLACEHOLDER_RE = re.compile(r'^\[.*\]$')
PLACEHOLDER_VALUES = ('\u2014', '\u2013', '-', '\u00d7')
GLYPH_ITEM_RE = re.compile(r'([\u2610\u2612\u2611])\s*([^\u2610\u2612\u2611]+)')


def _value_text(value):
    value = value.strip()
    if not value or PLACEHOLDER_RE.match(value):
        return ''
    if all(char in PLACEHOLDER_VALUES for char in value):
        return ''
    return value


def _system_family(heading, options):
    if re.search(r'STAR\s*V\b', heading or '', re.IGNORECASE):
        return 'STAR V'
    upper = (heading or '').upper()
    if 'VANTAGE' in upper:
        return 'VANTAGE'
    if 'STAR' in upper:
        return 'STAR'
    labels = {option.label for option in options}
    if any('Pipettor' in label for label in labels):
        return 'VANTAGE'
    if labels & {'STAR23', 'STARlet23', 'STARlet', 'STAR', 'STARplus'}:
        return 'STAR'
    return ''


def _read_system_sections(doc):
    children = list(doc.element.body.iterchildren())
    heading = None
    start = 0
    for index, child in enumerate(children):
        if child.tag.split('}')[-1] != 'p':
            continue
        text = _clean_text(_full_text(child))
        if 'System Configuration' in text:
            heading = text
            start = index + 1
            break
    if heading is None:
        return None
    options = []
    upgrade = False
    serial_number = ''
    sales_order = ''
    parent = ''
    group = ''
    for child in children[start:]:
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            text = _clean_text(_full_text(child))
            if not text:
                continue
            if SYSTEM_STOP_RE.search(text):
                break
            if text.endswith(':'):
                group = f'{parent} - {text[:-1]}'
            else:
                parent = text
                group = text
            continue
        if tag != 'tbl':
            continue
        rows = []
        for tr in child:
            if tr.tag.split('}')[-1] != 'tr':
                continue
            rows.append([_clean_text(cell) for cell in _tr_cells(tr)])
        if group == 'Upgrade Status':
            for row in rows:
                for cell in row:
                    if 'Upgrade' in cell and any(glyph in cell for glyph in CHECKED_GLYPHS):
                        upgrade = True
                for index in range(len(row) - 1):
                    if row[index] == 'Serial Number':
                        serial_number = _value_text(row[index + 1])
                    elif row[index] in ('Sales Order Number', 'Sales Order #'):
                        sales_order = _value_text(row[index + 1])
            continue
        for row_index, row in enumerate(rows):
            if not any(row):
                continue
            if not any(_has_glyph(cell) for cell in row):
                if len([cell for cell in row if cell]) == 2 or (row[0] and len(row) > 1):
                    header_row = (row_index == 0 and len(rows) > 1 and rows[1][0]
                                  and _has_glyph(rows[1][0])
                                  and _value_text(row[1] if len(row) > 1 else ''))
                    if header_row:
                        continue
                    options.append(CprOption(group, row[0].rstrip(':'), None,
                                             _value_text(row[1] if len(row) > 1 else '')))
                continue
            items = GLYPH_ITEM_RE.findall(row[0])
            if len(items) == 1 and len(row) > 1 and not _has_glyph(row[1]):
                glyph, label = items[0]
                options.append(CprOption(group, ' '.join(label.split()), glyph in CHECKED_GLYPHS,
                                         _value_text(row[1])))
                continue
            for cell in row:
                for glyph, label in GLYPH_ITEM_RE.findall(cell):
                    options.append(CprOption(group, ' '.join(label.split()), glyph in CHECKED_GLYPHS))
    return CprSystem(
        family=_system_family(heading, options),
        heading=heading,
        upgrade=upgrade,
        serial_number=serial_number,
        sales_order=sales_order,
        options=options,
    )


def _read_system(doc):
    heading = None
    system_table = None
    for child in doc.element.body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            text = _full_text(child)
            if 'System Configuration' in text:
                heading = text
            elif heading is not None and 'Custom Parts' in text:
                break
        elif tag == 'tbl' and heading is not None:
            rows = []
            for tr in child:
                if tr.tag.split('}')[-1] != 'tr':
                    continue
                rows.append([_clean_text(cell) for cell in _tr_cells(tr)])
            for row in rows:
                non_empty = [cell for cell in row if cell]
                if len(non_empty) == 1 and non_empty[0] == 'Platform':
                    system_table = rows
                    break
            if system_table is not None:
                break
    if system_table is None:
        return _read_system_sections(doc)
    options = []
    group = ''
    slot_headers = None
    upgrade = False
    serial_number = ''
    sales_order = ''
    for row in system_table:
        non_empty = [cell for cell in row if cell]
        if not non_empty:
            continue
        if len(non_empty) == 1 and not _has_glyph(non_empty[0]):
            group = non_empty[0]
            slot_headers = None
            continue
        if group == 'Is this an Upgrade to an existing system?' and _has_glyph(row[0]) and 'Yes' in row[0]:
            upgrade = any(glyph in row[0] for glyph in CHECKED_GLYPHS)
            for index in range(len(row) - 1):
                if row[index] == 'Serial Number':
                    serial_number = _value_text(row[index + 1])
                elif row[index] == 'Sales Order #':
                    sales_order = _value_text(row[index + 1])
            continue
        if row[0] == '':
            others = [cell for cell in row[1:] if cell]
            if others and not any(_has_glyph(cell) for cell in others):
                slot_headers = others
                continue
        if slot_headers is not None and row[0] and not _has_glyph(row[0]):
            if any(cell in GLYPHS for cell in row[1:]) and all(cell in ('',) + GLYPHS for cell in row[1:]):
                for index, cell in enumerate(row[1:]):
                    if cell and index < len(slot_headers):
                        options.append(CprOption(group, f'{row[0]} {slot_headers[index]}', cell in CHECKED_GLYPHS))
                continue
        if row[0] and not _has_glyph(row[0]) and not row[0].endswith(':'):
            found_items = []
            for cell in row[1:]:
                if not cell:
                    continue
                if cell[-1] not in GLYPHS:
                    found_items = None
                    break
                found_items.append((cell[:-1].strip(), cell[-1]))
            if found_items:
                for label, glyph in found_items:
                    options.append(CprOption(group, f'{row[0]}: {label}', glyph in CHECKED_GLYPHS))
                continue
        start = 0
        if row[0].endswith(':') and not _has_glyph(row[0]):
            label = row[0][:-1]
            value = row[1] if len(row) > 1 else ''
            options.append(CprOption(group, label, None, _value_text(value)))
            start = 2
        index = start
        while index < len(row):
            cell = row[index]
            index += 1
            if not cell:
                continue
            items = GLYPH_ITEM_RE.findall(cell)
            for item_index, (glyph, label) in enumerate(items):
                checked = glyph in CHECKED_GLYPHS
                value = ''
                if len(items) == 1 and label.endswith(':'):
                    label = label[:-1]
                    if index < len(row):
                        value = row[index]
                        index += 1
                label = ' '.join(label.split())
                value = _value_text(value)
                options.append(CprOption(group, label, checked, value))
    return CprSystem(
        family=_system_family(heading, options),
        heading=heading or '',
        upgrade=upgrade,
        serial_number=serial_number,
        sales_order=sales_order,
        options=options,
    )


def read_cpr(path):
    doc = docx.Document(path)
    parts = _find_parts(doc)
    spec_names, heading_names = _read_spec_names(doc)
    for item_number, name in heading_names.items():
        for part in parts:
            key = part.sharepoint_id or part.sk_pn
            if not key or key in spec_names:
                continue
            if key.endswith('-' + item_number):
                spec_names[key] = name
    return CprData(
        parts=parts,
        devices=_find_devices(doc),
        spec_names=spec_names,
        system=_read_system(doc),
    )
