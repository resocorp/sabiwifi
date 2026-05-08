"""
Router hardware catalogue.

Maps `Router.board_name` (the string the announce endpoint records from
RouterOS `/system resource get board-name`) to a structured description of
the device's physical ports and radios. Drives the port-grid modal in the
dashboard and the explicit-topology branch of the provision template.

Adding a new SKU = add an entry to BOARDS. No migration, no UI change.

Roles:
  - 'wan'     : management + internet uplink (exactly one per router)
  - 'hotspot' : captive-portal VLAN, bridged to hotspot-br
  - 'pppoe'   : PPPoE-server VLAN, bridged to pppoe-br
  - 'lan'     : reserved for v2 (untagged passthrough)
"""

ROLE_WAN = 'wan'
ROLE_HOTSPOT = 'hotspot'
ROLE_PPPOE = 'pppoe'
ROLE_LAN = 'lan'

VALID_ROLES = (ROLE_WAN, ROLE_HOTSPOT, ROLE_PPPOE, ROLE_LAN)
RADIO_VALID_ROLES = (ROLE_HOTSPOT, ROLE_PPPOE, ROLE_LAN)


BOARDS = {
    'L009UiGS-2HaxD': {
        'label': 'L009UiGS-2HaxD',
        'ether_ports': [
            {'name': 'ether1', 'caption': '2.5G PoE-in', 'default_role': ROLE_WAN},
            {'name': 'ether2', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether3', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether4', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether5', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether6', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether7', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
            {'name': 'ether8', 'caption': '1G',          'default_role': ROLE_HOTSPOT},
        ],
        'radios': [
            {'name': 'wifi1', 'band': '2.4ghz', 'default_role': ROLE_HOTSPOT},
        ],
        'notes': 'Single 2.4GHz radio — never assign 5g config.',
    },
    'hAP ax²': {
        'label': 'hAP ax²',
        'ether_ports': [
            {'name': 'ether1', 'caption': '1G WAN',  'default_role': ROLE_WAN},
            {'name': 'ether2', 'caption': '1G',      'default_role': ROLE_HOTSPOT},
            {'name': 'ether3', 'caption': '1G',      'default_role': ROLE_HOTSPOT},
            {'name': 'ether4', 'caption': '1G',      'default_role': ROLE_HOTSPOT},
            {'name': 'ether5', 'caption': '1G',      'default_role': ROLE_HOTSPOT},
        ],
        'radios': [
            {'name': 'wifi1', 'band': '2.4ghz', 'default_role': ROLE_HOTSPOT},
            {'name': 'wifi2', 'band': '5ghz',   'default_role': ROLE_HOTSPOT},
        ],
        'notes': 'Dual-band ax. WiFi 6.',
    },
    'hAP ac²': {
        'label': 'hAP ac²',
        'ether_ports': [
            {'name': 'ether1', 'caption': '1G WAN', 'default_role': ROLE_WAN},
            {'name': 'ether2', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether3', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether4', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether5', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
        ],
        'radios': [
            {'name': 'wifi1', 'band': '2.4ghz', 'default_role': ROLE_HOTSPOT},
            {'name': 'wifi2', 'band': '5ghz',   'default_role': ROLE_HOTSPOT},
        ],
        'notes': 'Dual-band ac. WiFi 5.',
    },
    'hEX': {
        'label': 'hEX (RB750Gr3)',
        'ether_ports': [
            {'name': 'ether1', 'caption': '1G WAN', 'default_role': ROLE_WAN},
            {'name': 'ether2', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether3', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether4', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
            {'name': 'ether5', 'caption': '1G',     'default_role': ROLE_HOTSPOT},
        ],
        'radios': [],
        'notes': 'No WiFi — wired only.',
    },
    'CCR2004-1G-12S+2XS': {
        'label': 'CCR2004-1G-12S+2XS',
        'ether_ports': [
            {'name': 'ether1', 'caption': '1G WAN',  'default_role': ROLE_WAN},
            {'name': 'sfp-sfpplus1',  'caption': '10G SFP+', 'default_role': ROLE_HOTSPOT},
            {'name': 'sfp-sfpplus2',  'caption': '10G SFP+', 'default_role': ROLE_HOTSPOT},
        ],
        'radios': [],
        'notes': 'Cluster-class router. Most ports omitted from catalogue — '
                 'configure additional SFPs via SSH for v1.',
    },
}


def _synthesize_entry(board_name, ether_port_count, has_wifi, has_5ghz):
    """
    Build a fallback catalogue entry for an unknown board so the UI still
    renders something sensible. Defaults: ether1=WAN, the rest hotspot,
    one 2.4GHz radio (and one 5GHz if reported), all hotspot.
    """
    n = ether_port_count or 1
    ether_ports = []
    for i in range(1, n + 1):
        ether_ports.append({
            'name': f'ether{i}',
            'caption': '1G',
            'default_role': ROLE_WAN if i == 1 else ROLE_HOTSPOT,
        })
    radios = []
    if has_wifi:
        radios.append({'name': 'wifi1', 'band': '2.4ghz', 'default_role': ROLE_HOTSPOT})
        if has_5ghz:
            radios.append({'name': 'wifi2', 'band': '5ghz', 'default_role': ROLE_HOTSPOT})
    return {
        'label': board_name or 'Unknown board',
        'ether_ports': ether_ports,
        'radios': radios,
        'notes': 'Synthesized from announced capabilities. '
                 'Add an entry to routers/catalogue.py for accurate port labels.',
        'synthesized': True,
    }


def get_catalogue_entry(board_name, ether_port_count=None, has_wifi=None, has_5ghz=None):
    """
    Return the catalogue entry for a board, or a synthesized fallback.

    The fallback fields default to safe minima: 1 ether port, no WiFi —
    so a router that hasn't announced yet still gets a renderable shape.
    """
    if board_name and board_name in BOARDS:
        entry = dict(BOARDS[board_name])
        entry['synthesized'] = False
        return entry
    return _synthesize_entry(
        board_name or '',
        ether_port_count or 0,
        bool(has_wifi),
        bool(has_5ghz),
    )


def all_port_names(entry):
    """Helper: flat list of every port + radio name in an entry."""
    return [p['name'] for p in entry['ether_ports']] + [r['name'] for r in entry['radios']]


def is_radio(entry, port_name):
    """True if port_name is a radio (not ethernet)."""
    return any(r['name'] == port_name for r in entry['radios'])


def default_assignments(entry):
    """Build the default port_assignments dict from a catalogue entry."""
    out = {}
    for p in entry['ether_ports']:
        out[p['name']] = p['default_role']
    for r in entry['radios']:
        out[r['name']] = r['default_role']
    return out
