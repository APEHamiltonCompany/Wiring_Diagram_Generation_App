import re

import docx

from .models import CprData, CprDevice, CprPart, CprOption, CprSystem

SPEC_HEADING_RE = re.compile(r'Part\s*#\s*(\d+-\d+)')
PART_HEADER_MARKERS = ('SK Part Number', 'Price Per Each')
PART_COLUMN_NAMES = ('Quantity', 'Part Name', 'SharePoint', 'SK Part', 'Price')


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
        if not all(any(marker in text for _, _, text in header) for marker in PART_HEADER_MARKERS):
            continue
        columns = {}
        for name in PART_COLUMN_NAMES:
            columns[name] = next(((index, start, span) for index, (start, span, text) in enumerate(header) if name in text), None)
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
        devices.append(CprDevice(
            otp_number=otp_number,
            vendor=kv.get('Vendor') or kv.get('Manufacturer', ''),
            model_name=kv.get('Model Name', ''),
            availability=kv.get('Device Availability', ''),
        ))
    return devices


def _read_spec_names(doc):
    spec_names = {}
    current = None
    for child in doc.element.body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            match = SPEC_HEADING_RE.search(_full_text(child))
            if match:
                current = 'APE-' + match.group(1)
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
    return spec_names


def _clean_text(text):
    return ' '.join(text.replace('\u200b', '').replace('\xa0', ' ').split())


def _has_glyph(text):
    return '\u2610' in text or '\u2612' in text


PLACEHOLDER_RE = re.compile(r'^\[.*\]$')
GLYPH_ITEM_RE = re.compile(r'([\u2610\u2612])\s*([^\u2610\u2612]+)')


def _value_text(value):
    value = value.strip()
    if not value or PLACEHOLDER_RE.match(value):
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
        return None
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
            upgrade = '\u2612' in row[0]
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
            if any(cell in ('\u2610', '\u2612') for cell in row[1:]) and all(cell in ('', '\u2610', '\u2612') for cell in row[1:]):
                for index, cell in enumerate(row[1:]):
                    if cell and index < len(slot_headers):
                        options.append(CprOption(group, f'{row[0]} {slot_headers[index]}', cell == '\u2612'))
                continue
        if row[0] and not _has_glyph(row[0]) and not row[0].endswith(':'):
            found_items = []
            for cell in row[1:]:
                if not cell:
                    continue
                if cell[-1] not in ('\u2610', '\u2612'):
                    found_items = None
                    break
                found_items.append((cell[:-1].strip(), cell[-1]))
            if found_items:
                for label, glyph in found_items:
                    options.append(CprOption(group, f'{row[0]}: {label}', glyph == '\u2612'))
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
                checked = glyph == '\u2612'
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
    return CprData(
        parts=_find_parts(doc),
        devices=_find_devices(doc),
        spec_names=_read_spec_names(doc),
        system=_read_system(doc),
    )
