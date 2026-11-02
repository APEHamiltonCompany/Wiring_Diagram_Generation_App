import re

from .models import CprData, ResultRow, SoLine
from .system_rules import (
    ACCESSORY_RULES,
    CHANNEL_LABEL_RE,
    INSTRUMENT_PRODUCT_RE,
    OUT_OF_SCOPE_PRODUCT_PATTERNS,
    PIPETTOR_FEATURES,
    PLATFORM_PATTERNS,
)

CUSTOM_PART_PRODUCT_NUMBERS = {'APE MISC'}
PRICE_TOLERANCE = 0.01
ARM_PN_RE = re.compile(r'\(P/N\s*([^)]+)\)')
CHANNEL_COMPONENT_RE = re.compile(r'\bCHANNEL\b')
VENUS_RE = re.compile(r'VENUS\s*([\d.]+)', re.IGNORECASE)


def _normalize(name):
    return ' '.join(name.upper().split())


def _pn_normalize(value):
    return ''.join(value.upper().split())


def _so_text(line):
    parts = [line.product_number, line.product_name, line.config_description]
    parts.extend(component.description for component in line.components)
    return ' '.join(part for part in parts if part)


def _find_instrument_line(so, used_so):
    for line in so:
        if id(line) in used_so:
            continue
        if line.quantity <= 0:
            continue
        if line.product_number.strip().upper().startswith('LH TRADE'):
            continue
        product_number = line.product_number.strip()
        if not line.components and not re.search(INSTRUMENT_PRODUCT_RE, product_number, re.IGNORECASE):
            continue
        return line
    return None


def _platform_text_matches(option, text, family):
    if not re.search(PLATFORM_PATTERNS[option.label], text, re.IGNORECASE):
        return False
    if family == 'VANTAGE':
        return 'VANTAGE' in text.upper()
    if family == 'STAR V':
        return bool(re.search(r'STAR\s*V', text, re.IGNORECASE))
    return True


def _system_row(line, cpr_part_name, status, note, cpr_qty=None, so_product_number=None, so_product_name=None, so_qty=None):
    kwargs = {}
    if line is not None:
        kwargs.update(
            so_row=line.row,
            so_file=line.so_file,
            so_product_number=so_product_number if so_product_number is not None else line.product_number,
            so_product_name=so_product_name if so_product_name is not None else line.product_name,
            so_qty=so_qty if so_qty is not None else line.quantity,
            so_unit_price=line.unit_price,
        )
    return ResultRow(
        'System',
        cpr_part_name=cpr_part_name,
        cpr_qty=cpr_qty,
        status=status,
        note=note,
        **kwargs,
    )


def _option_part_name(option):
    part_name = f'{option.group}: {option.label}'
    if option.value:
        part_name += f' = {option.value}'
    return part_name


def _format_number(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _arm_pn_rows(instrument, system, used_options, used_components):
    rows = []
    for option in system.options:
        match = ARM_PN_RE.search(option.label)
        if match is None:
            continue
        pn = match.group(1)
        component = next(
            (
                comp for comp in instrument.components
                if _pn_normalize(comp.item_number) == _pn_normalize(pn)
            ),
            None,
        )
        part_name = _option_part_name(option)
        if component is not None:
            used_components.add(id(component))
            if option.checked is True:
                used_options.add(id(option))
                rows.append(_system_row(
                    instrument,
                    part_name,
                    'Match',
                    f'Arm part number {pn} found in SO configuration',
                ))
            elif option.checked is False:
                rows.append(_system_row(
                    instrument,
                    part_name,
                    'Only on SO',
                    f'SO configuration includes {pn} but CPR leaves it unchecked',
                ))
        elif option.checked is True:
            used_options.add(id(option))
            rows.append(_system_row(
                instrument,
                part_name,
                'Only in CPR',
                f'Arm part number {pn} selected in CPR but not in SO configuration',
            ))
    return rows


def _feature_rows(instrument, system, used_options, used_components):
    rows = []
    config_tokens = (instrument.config_description or instrument.product_name).upper().split()
    for feature, option_pattern, component_pattern, token_pattern in PIPETTOR_FEATURES:
        component = None
        if component_pattern is not None:
            component = next(
                (
                    comp for comp in instrument.components
                    if id(comp) not in used_components and re.search(component_pattern, comp.description, re.IGNORECASE)
                ),
                None,
            )
        token_match = token_pattern is not None and any(re.search(token_pattern, token) for token in config_tokens)
        so_has = component is not None or token_match
        cpr_has = system.is_checked(option_pattern)
        if component is not None:
            used_components.add(id(component))
        if so_has and cpr_has:
            if component is not None:
                note = f'SO configuration: {component.item_number} {component.description}'
            else:
                note = f'SO config includes {feature}'
            rows.append(_system_row(instrument, feature, 'Match', note))
        elif cpr_has:
            rows.append(_system_row(
                instrument,
                feature,
                'Only in CPR',
                f'{feature} selected in CPR but not in SO configuration',
            ))
        elif so_has:
            rows.append(_system_row(
                instrument,
                feature,
                'Only on SO',
                f'SO configuration includes {feature} but CPR leaves it unchecked',
            ))
        if cpr_has:
            for option in system.checked_options():
                if re.search(option_pattern, option.label, re.IGNORECASE):
                    used_options.add(id(option))
    return rows


def _channel_type_from_label(label):
    upper = label.upper()
    if re.search(r'\b1\s?ML\b', upper):
        return '1mL'
    if re.search(r'\b5\s?ML\b', upper):
        return '5mL'
    match = re.match(r'^(.*?)\s*CHANNEL', upper)
    if match and match.group(1):
        return match.group(1).strip()
    return label


def _channel_type_from_description(description):
    upper = description.upper()
    if '1000UL' in upper or '1ML' in upper:
        return '1mL'
    if '5000UL' in upper or '5ML' in upper:
        return '5mL'
    return 'Other'


def _channel_rows(instrument, system, used_options, used_components):
    rows = []
    so_types = {}
    so_components = {}
    for component in instrument.components:
        if not CHANNEL_COMPONENT_RE.search(component.description):
            continue
        used_components.add(id(component))
        channel_type = _channel_type_from_description(component.description)
        count = component.quantity * 2 if 'PAIR' in component.description.upper() else component.quantity
        so_types[channel_type] = so_types.get(channel_type, 0) + count
        so_components.setdefault(channel_type, []).append(component)
    cpr_types = {}
    cpr_options = {}
    for option in system.options:
        if not re.search(CHANNEL_LABEL_RE, option.label) or not re.fullmatch(r'\d+', option.value or ''):
            continue
        channel_type = _channel_type_from_label(option.label)
        cpr_types[channel_type] = cpr_types.get(channel_type, 0) + int(option.value)
        cpr_options.setdefault(channel_type, []).append(option)
    for channel_type in sorted(set(cpr_types) | set(so_types)):
        so_count = so_types.get(channel_type, 0)
        cpr_count = cpr_types.get(channel_type)
        if cpr_count is not None and so_count:
            for option in cpr_options.get(channel_type, []):
                used_options.add(id(option))
            pieces = []
            for component in so_components.get(channel_type, []):
                quantity_text = f'x{_format_number(component.quantity)}'
                if 'PAIR' in component.description.upper():
                    quantity_text += ' pairs'
                pieces.append(f'{component.item_number} {component.description} {quantity_text}')
            note = f'CPR {channel_type} channels {cpr_count} vs SO configuration {_format_number(so_count)} ({", ".join(pieces)})'
            rows.append(_system_row(
                instrument,
                cpr_options.get(channel_type, [None])[0].label,
                'Match' if so_count == cpr_count else 'Qty mismatch',
                note,
                cpr_qty=cpr_count,
                so_qty=so_count,
            ))
        elif cpr_count is not None:
            for option in cpr_options.get(channel_type, []):
                used_options.add(id(option))
            rows.append(_system_row(
                instrument,
                cpr_options.get(channel_type, [None])[0].label,
                'Only in CPR',
                f'{channel_type} channels selected in CPR but not found in SO configuration',
                cpr_qty=cpr_count,
            ))
        else:
            rows.append(_system_row(
                instrument,
                '',
                'Only on SO',
                f'SO configuration includes {_format_number(so_count)} {channel_type} channels but CPR does not itemise them',
                so_qty=so_count,
            ))
    return rows


def _fallback_channel_row(instrument, system, used_options):
    rows = []
    config = instrument.config_description or instrument.product_name
    tokens = config.upper().split()
    so_channels = None
    for token in tokens[1:]:
        match = re.match(r'^(\d+)', token)
        if match and match.group(1) not in ('96', '384'):
            so_channels = int(match.group(1))
            break
    channel_options = [
        option for option in system.checked_options()
        if re.search(CHANNEL_LABEL_RE, option.label) and re.fullmatch(r'\d+', option.value)
    ]
    cpr_channels = sum(int(option.value) for option in channel_options)
    if so_channels is not None and cpr_channels:
        rows.append(_system_row(
            instrument,
            'Channel count',
            'Match' if so_channels == cpr_channels else 'Qty mismatch',
            f'CPR channels {cpr_channels} vs SO config {so_channels}',
            cpr_qty=cpr_channels,
        ))
        for option in channel_options:
            used_options.add(id(option))
    return rows


def _component_info_rows(instrument, used_components):
    rows = []
    for component in instrument.components:
        if id(component) in used_components:
            continue
        match = VENUS_RE.search(component.description)
        if match:
            note = f'SO configuration: VENUS software version {match.group(1)}'
        else:
            note = 'SO configuration component (not checked against CPR)'
        rows.append(_system_row(
            instrument,
            '',
            'Info',
            note,
            so_product_number=component.item_number,
            so_product_name=component.description,
            so_qty=component.quantity,
        ))
    return rows


def _compare_system(system, so, used_so):
    if system is None:
        return []
    rows = []
    instrument = _find_instrument_line(so, used_so)
    used_options = set()
    if system.upgrade:
        rows.append(ResultRow(
            'System',
            cpr_part_name=(
                f'Upgrade of existing system (serial {system.serial_number or "?"}, '
                f'SO {system.sales_order or "?"})'
            ),
            status='Info',
            note='Upgrade: no new instrument expected on SO',
        ))
        if instrument is not None:
            rows.append(_system_row(
                instrument,
                'Platform',
                'Only on SO',
                'CPR marks this as an upgrade but SO has an instrument line',
            ))
            used_so.add(id(instrument))
    else:
        platform_options = [
            option for option in system.checked_options()
            if option.group == 'Platform' and option.label in PLATFORM_PATTERNS
        ]
        labels = [option.label for option in platform_options]
        part_name = 'Platform: ' + (', '.join(labels) if labels else 'none selected')
        if instrument is not None:
            so_text = _so_text(instrument)
            matched = next(
                (option for option in platform_options if _platform_text_matches(option, so_text, system.family)),
                None,
            )
            if matched is not None:
                rows.append(_system_row(
                    instrument,
                    f'Platform: {matched.label}',
                    'Match',
                    f'{system.family} {matched.label} matches SO instrument',
                ))
            else:
                rows.append(_system_row(
                    instrument,
                    part_name,
                    'Mismatch',
                    f'CPR platform {", ".join(labels) or "none selected"} vs SO instrument {instrument.product_name}',
                ))
            used_so.add(id(instrument))
        else:
            rows.append(ResultRow(
                'System',
                cpr_part_name=part_name,
                status='Only in CPR',
                note='No instrument line on SO',
            ))
        for option in platform_options:
            used_options.add(id(option))
        if instrument is not None:
            used_components = set()
            rows.extend(_arm_pn_rows(instrument, system, used_options, used_components))
            rows.extend(_feature_rows(instrument, system, used_options, used_components))
            if instrument.components:
                rows.extend(_channel_rows(instrument, system, used_options, used_components))
                rows.extend(_component_info_rows(instrument, used_components))
            else:
                rows.extend(_fallback_channel_row(instrument, system, used_options))
    for option in system.options:
        rule = None
        for candidate in ACCESSORY_RULES:
            if re.search(candidate['option'], option.label, re.IGNORECASE):
                rule = candidate
                break
        if rule is None:
            continue
        if 'value' in rule and not re.search(rule['value'], option.value, re.IGNORECASE):
            continue
        if rule.get('value_is_part_number'):
            if not option.value:
                continue
            matched = next(
                (
                    line for line in so
                    if id(line) not in used_so and line.quantity > 0 and line.product_number.strip().upper() == option.value.strip().upper()
                ),
                None,
            )
        else:
            matched = next(
                (
                    line for line in so
                    if id(line) not in used_so and line.quantity > 0 and re.search(rule['name'], _normalize(line.product_name), re.IGNORECASE)
                ),
                None,
            )
        part_name = _option_part_name(option)
        if option.checked is True:
            used_options.add(id(option))
            if matched is not None:
                used_so.add(id(matched))
                rows.append(_system_row(matched, part_name, 'Match', 'Selected in CPR and found on SO'))
            else:
                rows.append(ResultRow(
                    'System',
                    cpr_part_name=part_name,
                    status='Only in CPR',
                    note='Selected in CPR but no matching SO line',
                ))
        elif option.checked is False and matched is not None:
            used_so.add(id(matched))
            rows.append(_system_row(matched, part_name, 'Only on SO', 'On SO but not selected in CPR'))
    for option in system.checked_options():
        if id(option) in used_options:
            continue
        rows.append(ResultRow(
            'System',
            cpr_part_name=_option_part_name(option),
            status='Info',
            note='Selected in CPR; not itemised on SO (part of instrument configuration)',
        ))
    for option in system.options:
        if option.checked is None and option.value and id(option) not in used_options:
            rows.append(ResultRow(
                'System',
                cpr_part_name=_option_part_name(option),
                status='Info',
                note='Selected in CPR; not itemised on SO (part of instrument configuration)',
            ))
    return rows


def compare(so, cpr):
    warnings = []
    used_so = set()

    so_custom = [line for line in so if line.product_number.strip().upper() in CUSTOM_PART_PRODUCT_NUMBERS]
    used_custom = set()
    custom_index = {id(line): index for index, line in enumerate(so_custom)}
    outcome = {}
    sk_lines = []

    for part_index, part in enumerate(cpr.parts):
        if not part.sk_pn.strip():
            continue
        target = _pn_normalize(part.sk_pn)
        for line in so:
            if id(line) in used_so:
                continue
            if _pn_normalize(line.product_number) != target:
                continue
            used_so.add(id(line))
            if id(line) in custom_index:
                used_custom.add(custom_index[id(line)])
            sk_lines.append(line)
            price_diff = ''
            if abs(line.unit_price - part.price) > PRICE_TOLERANCE:
                price_diff = f'; price differs: CPR {part.price} vs SO {line.unit_price}'
            if line.quantity == part.quantity:
                outcome[part_index] = ('match', line, 'Matched on SK part number' + price_diff)
            else:
                outcome[part_index] = ('qty_mismatch', line, f'CPR qty {part.quantity} vs SO qty {line.quantity}' + price_diff)
            break

    for part_index, part in enumerate(cpr.parts):
        if part_index in outcome:
            continue
        for so_index, line in enumerate(so_custom):
            if so_index in used_custom:
                continue
            if abs(line.unit_price - part.price) <= PRICE_TOLERANCE and line.quantity == part.quantity:
                used_custom.add(so_index)
                used_so.add(id(line))
                outcome[part_index] = ('match', line, 'Matched on unit price and quantity (SO has no part number)')
                break

    for part_index, part in enumerate(cpr.parts):
        if part_index in outcome:
            continue
        for so_index, line in enumerate(so_custom):
            if so_index in used_custom:
                continue
            if abs(line.unit_price - part.price) <= PRICE_TOLERANCE:
                used_custom.add(so_index)
                used_so.add(id(line))
                outcome[part_index] = ('qty_mismatch', line, f'CPR qty {part.quantity} vs SO qty {line.quantity}')
                break

    custom_rows = []
    for part_index, part in enumerate(cpr.parts):
        result = outcome.get(part_index)
        if result is None:
            custom_rows.append(ResultRow(
                'Custom Part',
                cpr_sk_pn=part.sk_pn,
                cpr_part_name=part.part_name,
                cpr_qty=part.quantity,
                cpr_price=part.price,
                status='Only in CPR',
                note='No SO APE MISC line at this unit price',
            ))
        elif result[0] == 'match':
            line = result[1]
            custom_rows.append(ResultRow(
                'Custom Part',
                cpr_sk_pn=part.sk_pn,
                cpr_part_name=part.part_name,
                cpr_qty=part.quantity,
                cpr_price=part.price,
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Match',
                note=result[2],
            ))
        else:
            line = result[1]
            custom_rows.append(ResultRow(
                'Custom Part',
                cpr_sk_pn=part.sk_pn,
                cpr_part_name=part.part_name,
                cpr_qty=part.quantity,
                cpr_price=part.price,
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Qty mismatch',
                note=result[2],
            ))

    for so_index, line in enumerate(so_custom):
        if so_index not in used_custom:
            custom_rows.append(ResultRow(
                'Custom Part',
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Only on SO',
                note='No CPR custom part at this unit price',
            ))
            used_so.add(id(line))

    device_rows = []
    for device in cpr.devices:
        matched = None
        for line in so:
            if id(line) in used_so:
                continue
            if line.product_number and line.product_number.strip().upper() == device.otp_number.strip().upper():
                matched = line
                break
        part_name = f'{device.vendor} {device.model_name}'.strip()
        if matched is not None:
            used_so.add(id(matched))
            device_rows.append(ResultRow(
                'Device',
                cpr_sk_pn=device.otp_number,
                cpr_part_name=part_name,
                so_row=matched.row,
                so_file=matched.so_file,
                so_product_number=matched.product_number,
                so_product_name=matched.product_name,
                so_qty=matched.quantity,
                so_unit_price=matched.unit_price,
                status='Match',
            ))
        elif 'on sales order' in device.availability.strip().lower():
            device_rows.append(ResultRow(
                'Device',
                cpr_sk_pn=device.otp_number,
                cpr_part_name=part_name,
                status='Only in CPR',
                note="CPR marks device 'On Sales Order' but it is not on the SO",
            ))

    system_rows = _compare_system(cpr.system, so, used_so)

    remaining_device_rows = []
    remaining_other_rows = []
    for line in so:
        if id(line) in used_so:
            continue
        if any(re.search(pattern, line.product_number.strip(), re.IGNORECASE) for pattern in OUT_OF_SCOPE_PRODUCT_PATTERNS):
            remaining_other_rows.append(ResultRow(
                'Other',
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Out of scope',
                note='Not covered by CPR',
            ))
        elif line.product_number.strip().upper().startswith('OTP-'):
            remaining_device_rows.append(ResultRow(
                'Device',
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Only on SO',
                note='SO device not referenced in CPR',
            ))
        else:
            remaining_other_rows.append(ResultRow(
                'Other',
                so_row=line.row,
                so_file=line.so_file,
                so_product_number=line.product_number,
                so_product_name=line.product_name,
                so_qty=line.quantity,
                so_unit_price=line.unit_price,
                status='Only on SO',
            ))

    rows = custom_rows + device_rows + system_rows + remaining_device_rows + remaining_other_rows

    for part in cpr.parts:
        spec_name = cpr.spec_names.get(part.sharepoint_id)
        if spec_name is None:
            continue
        if _normalize(part.part_name) != _normalize(spec_name):
            warnings.append(
                f"{part.sharepoint_id}: list name '{part.part_name}' differs from spec name '{spec_name}'"
            )

    cpr_total = sum(part.quantity * part.price for part in cpr.parts)
    so_custom_total = sum(line.quantity * line.unit_price for line in so_custom)
    so_custom_total += sum(line.quantity * line.unit_price for line in sk_lines)
    if abs(cpr_total - so_custom_total) > PRICE_TOLERANCE:
        warnings.append(
            f'Total value mismatch: CPR total {cpr_total:.2f} vs SO custom total {so_custom_total:.2f}'
        )

    return rows, warnings
