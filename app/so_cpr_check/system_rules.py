INSTRUMENT_PRODUCT_RE = r'^(STAR|STARLET|STARPLUS|VANTAGE|NIMBUS|MICROLAB)\b(?!\s*MISC\b)'

OUT_OF_SCOPE_PRODUCT_PATTERNS = (r'^INDUSTRY_', r'^APPLICATION_', r'^APAE\d*$', r'^APPS\d*$', r'^APME\d*$', r'^MISCITEM$')

PLATFORM_PATTERNS = {
    'STARlet23': r'\bSTARLET-?2\b',
    'STAR23': r'\bSTAR-?2\b|\bSTAR FMA\b',
    'STARlet': r'\bSTARLET\b',
    'STAR': r'\bSTAR\b',
    'STARplus': r'\bSTARPLUS\b',
    '1.3m Pipettor': r'\b1\.3\b',
    '2.0m Pipettor': r'\b2\.0\b',
}

PIPETTOR_FEATURES = (
    ('Autoload', r'^Autoload$', r'\bAL\b', r'^AL$'),
    ('iSWAP', r'^iSWAP(/IPG)?$', r'\bISWAP\b', r'^ISWAP$'),
    ('96 MPH', r'^96 MPH$', r'\b96X\b|\b96 ?(MPH|HEAD)\b', r'^96$'),
    ('384 MPH', r'^384 MPH$', r'\b384X?\b', r'^384$'),
    ('96 TADM', r'^96 TADM$', r'\bTADM\b', None),
    ('Fluid Motion Arm', r'^Fluid Motion Arm$', r'\bFMA\b', r'^FMA$'),
)

CHANNEL_LABEL_RE = r'Channels'

ACCESSORY_RULES = [
    {'option': r'^Standard table PN$', 'value_is_part_number': True},
    {'option': r'^Front Chute$', 'name': r'CHUTE.*FRONT|FRONT.*CHUTE'},
    {'option': r'^Rear Chute$', 'name': r'CHUTE.*REAR|REAR.*CHUTE'},
    {'option': r'^CO-RE grips$', 'name': r'CO-?RE GRIP'},
    {'option': r'^(HEPA|CAP \(HEPA\))$', 'name': r'\bHEPA\b|\bHOOD KIT\b'},
    {'option': r'^(UV|UV Light Kit)$', 'name': r'\bUV\b'},
    {'option': r'^LHS Enclosure$', 'name': r'ENCL.*\bLHS\b'},
    {'option': r'^RHS Enclosure$', 'name': r'ENCL.*\bRHS\b'},
    {'option': r'^LHS extension shelf$', 'name': r'TABLE EXT.*\bLHS\b'},
    {'option': r'^RHS extension shelf$', 'name': r'TABLE EXT.*\bRHS\b'},
    {'option': r'^Left side extension$', 'name': r'\bLHS EXTENSION\b'},
    {'option': r'^AC Box: 1 Phase$', 'name': r'AC POWER CABLE.*1-PHASE'},
    {'option': r'^AC Box: 2 Phase$', 'name': r'AC POWER CABLE.*2-PHASE'},
    {'option': r'^AC Box: 3 Phase$', 'name': r'AC POWER CABLE.*3-PHASE'},
    {'option': r'^Other Channels$', 'value': r'TUBE GRIPPER', 'name': r'TUBE GRIPPER'},
]
