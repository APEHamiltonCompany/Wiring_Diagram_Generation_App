import re

from dataclasses import dataclass, field


@dataclass
class SoComponent:
    item_number: str
    description: str
    quantity: float
    unit: str = ''


@dataclass
class SoLine:
    row: int
    product_number: str
    product_name: str
    quantity: float
    unit: str
    unit_price: float
    so_file: str = ''
    config_description: str = ''
    line_number: int | None = None
    config_number: str = ''
    config_id: str = ''
    components: list[SoComponent] = field(default_factory=list)
    note: str = ''


@dataclass
class CprPart:
    sk_pn: str
    sharepoint_id: str
    part_name: str
    quantity: float
    price: float


@dataclass
class CprDevice:
    otp_number: str
    vendor: str
    model_name: str
    availability: str


@dataclass
class CprOption:
    group: str
    label: str
    checked: bool | None
    value: str = ''


@dataclass
class CprSystem:
    family: str
    heading: str
    upgrade: bool
    serial_number: str
    sales_order: str
    options: list[CprOption]

    def checked_options(self):
        return [option for option in self.options if option.checked is True]

    def is_checked(self, label_pattern):
        return any(re.search(label_pattern, option.label, re.IGNORECASE) for option in self.checked_options())


@dataclass
class CprData:
    parts: list[CprPart]
    devices: list[CprDevice]
    spec_names: dict[str, str]
    system: CprSystem | None = None


@dataclass
class ResultRow:
    type: str
    cpr_sk_pn: str = ''
    cpr_part_name: str = ''
    cpr_qty: float | None = None
    cpr_price: float | None = None
    so_row: int | None = None
    so_product_number: str = ''
    so_product_name: str = ''
    so_qty: float | None = None
    so_unit_price: float | None = None
    status: str = ''
    note: str = ''
    so_file: str = ''
