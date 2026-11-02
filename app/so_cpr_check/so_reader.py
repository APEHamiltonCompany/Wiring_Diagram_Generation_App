import os

import openpyxl

from .models import SoComponent, SoLine

CONFIRMATION_HEADERS = {'Line', 'Item number', 'Description', 'Estimated ship date', 'Qty', 'Unit', 'Net Price', 'Amount', 'Sales tax', 'Total'}


def _normalized(value):
    if value is None:
        return ''
    return ' '.join(str(value).split())


def _cell_value(values, index):
    if index is None or index >= len(values):
        return ''
    value = values[index]
    if value is None:
        return ''
    return str(value).strip()


def _cell_number(values, index):
    if index is None or index >= len(values):
        return 0.0
    value = values[index]
    if value is None:
        return 0.0
    return float(value)


def _line_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return None


def _is_header_row(row):
    normalized = [_normalized(value) for value in row]
    return 'Line' in normalized and 'Item number' in normalized and 'Net Price' in normalized


def is_confirmation(path):
    try:
        workbook = openpyxl.load_workbook(path, data_only=True)
    except Exception:
        return False
    try:
        for row in workbook.active.iter_rows(values_only=True):
            if _is_header_row(row):
                return True
        return False
    finally:
        workbook.close()


def read_so(path):
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        sheet = workbook.active
        columns = None
        header_row = 0
        for index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if _is_header_row(row):
                normalized = [_normalized(value) for value in row]
                columns = {name: normalized.index(name) for name in CONFIRMATION_HEADERS}
                header_row = index
                break
        if columns is None:
            raise ValueError(f'Not a SalesOrderConfirmation export (no Line / Item number table found): {os.path.basename(path)}')
        lines = []
        current = None
        for excel_row, values in enumerate(sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            first = next((value for value in values if value is not None and str(value).strip() != ''), None)
            if first is None:
                continue
            if str(first).strip() == 'Subtotal':
                break
            line_no = _line_number(_cell_value(values, columns['Line']))
            if line_no is not None:
                item_cell = _cell_value(values, columns['Item number'])
                item_parts = [part.strip() for part in item_cell.splitlines() if part.strip()]
                current = SoLine(
                    row=excel_row,
                    product_number=item_parts[0] if item_parts else '',
                    product_name=_cell_value(values, columns['Description']),
                    quantity=_cell_number(values, columns['Qty']),
                    unit=_cell_value(values, columns['Unit']),
                    unit_price=_cell_number(values, columns['Net Price']),
                    so_file=os.path.basename(path),
                    config_description=item_parts[2] if len(item_parts) > 2 else '',
                    line_number=line_no,
                    config_number=item_parts[1] if len(item_parts) > 1 else '',
                )
                lines.append(current)
            elif current is not None:
                description = _cell_value(values, columns['Description'])
                item = _cell_value(values, columns['Item number'])
                qty_value = values[columns['Qty']] if columns['Qty'] < len(values) else None
                has_qty = qty_value is not None and str(qty_value).strip() != ''
                if description.startswith('Configuration') and ':' in description:
                    current.config_id = description.split(':', 1)[1].strip()
                elif item and has_qty:
                    current.components.append(SoComponent(
                        item_number=item,
                        description=description,
                        quantity=float(qty_value),
                        unit=_cell_value(values, columns['Unit']),
                    ))
                elif description and not has_qty:
                    current.note = description
        return lines
    finally:
        workbook.close()
