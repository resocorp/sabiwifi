"""
VLAN ID assignments used by the explicit-topology provision template.

These are RouterOS bridge-VLAN tags, not user-visible. Hotspot and PPPoE
clients are isolated by VLAN when both roles coexist on the same router.
"""
HOTSPOT_VLAN = 10
PPPOE_VLAN = 20
