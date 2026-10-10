"""The endpoints report: each ARP/ND entry placed by its node's services.

The release recordings exercise the join on a real fabric; these pin the
cases that fabric does not have.
"""

from __future__ import annotations

from typing import Any, Dict, List

from nornir_srl.aliases import alias_index
from nornir_srl.records import (
    BridgeTable,
    EthernetSegment,
    Lag,
    LagMember,
    LldpInterface,
    LldpNeighbor,
    MacEntry,
    NeighborCache,
    NeighborEntry,
)
from nornir_srl.reports import ENDPOINTS_TABLE
from nornir_srl.server.devices import MixinDevice

TYPES = {"ipvrf-1": "ip-vrf", "macvrf-1": "mac-vrf", "default": "default"}


class Tables(MixinDevice):
    """A device whose ARP, ND, bridge and segment tables are given."""

    def __init__(
        self,
        arp: List[NeighborCache],
        macs: List[BridgeTable],
        segments: List[EthernetSegment],
        lldp: Dict[str, str] = {},
    ) -> None:
        self.arp, self.macs, self.segments = arp, macs, segments
        #: port -> the system-name LLDP hears on it.
        self.lldp = lldp

    def get_lldp_sum(self, interface: str = "*") -> Dict[str, Any]:
        return {"lldp_nbrs": [LldpInterface(port, (LldpNeighbor(name, "ethernet-1/1"),)) for port, name in self.lldp.items()]}

    def get_lag(self, lag_id: str = "*") -> Dict[str, Any]:
        return {"lag": [Lag("lag1", members=(LagMember("ethernet-1/20"), LagMember("ethernet-1/21")))]}

    def get_arp(self) -> Dict[str, Any]:
        return {"arp": self.arp}

    def get_nd(self) -> Dict[str, Any]:
        return {"nd": []}

    def get_mac_table(self, network_instance: str = "*") -> Dict[str, Any]:
        return {"mac_table": self.macs}

    def get_es(self) -> Dict[str, Any]:
        return {"es": self.segments}

    def _ni_types(self) -> Dict[str, str]:
        return TYPES


IRB = ("macvrf-1", "ipvrf-1")
ES1 = EthernetSegment("ES-1", "00:11:11:11:11:11:11:11:11:11", "", "all-active", "up", interfaces=("lag1",))
ES2 = EthernetSegment("ES-2", "00:22:22:22:22:22:22:22:22:22", "", "all-active", "up", interfaces=("ethernet-1/3",))


def _endpoints(*entries: NeighborEntry, macs: tuple = (), nis: tuple = IRB, interface: str = "irb0.1") -> list:
    device = Tables([NeighborCache(interface, nis, entries)], [BridgeTable("macvrf-1", macs)], [ES1, ES2])
    return device.get_endpoints()["endpoints"]


def test_a_host_behind_an_irb_is_placed_on_its_access_port_and_segment():
    (host,) = _endpoints(
        NeighborEntry("10.0.0.5", "aa:bb:cc:00:00:05", "dynamic"),
        # The bridge table spells the MAC in capitals.
        macs=(MacEntry.read("AA:BB:CC:00:00:05", "lag1.100", "learnt"),),
    )
    assert (host.ip_vrf, host.mac_vrf, host.subinterface) == ("ipvrf-1", "macvrf-1", "irb0.1")
    assert (host.learned, host.learned_on, host.es, host.esi) == ("local", "lag1.100", "ES-1", ES1.esi)


def test_a_remote_host_is_only_an_endpoint_behind_a_segment_of_this_node():
    (segment,) = _endpoints(
        # Behind another node's VTEP: that node's endpoint.
        NeighborEntry("10.0.0.6", "AA:BB:CC:00:00:06", "evpn"),
        # Behind a segment this node has too: its endpoint as well.
        NeighborEntry("10.0.0.7", "AA:BB:CC:00:00:07", "evpn"),
        # Behind a segment only other nodes have.
        NeighborEntry("10.0.0.9", "AA:BB:CC:00:00:09", "evpn"),
        macs=(
            MacEntry.read("AA:BB:CC:00:00:06", "vxlan-interface:vxlan1.1 vtep:192.0.2.2 vni:1", "evpn"),
            MacEntry.read("AA:BB:CC:00:00:07", f"vxlan-interface:vxlan1.1 esi:{ES2.esi}", "evpn"),
            MacEntry.read("AA:BB:CC:00:00:09", "vxlan-interface:vxlan1.1 esi:00:99:99:99:99:99:99:99:99:99", "evpn"),
        ),
    )
    assert (segment.address, segment.learned, segment.esi, segment.es) == ("10.0.0.7", "remote", ES2.esi, "ES-2")


def test_a_host_the_bridge_table_does_not_have_is_left_unplaced():
    (host,) = _endpoints(
        NeighborEntry("10.0.0.8", "AA:BB:CC:00:00:08", "dynamic"),
        # Learned over EVPN and nowhere in the bridge table: not this node's.
        NeighborEntry("10.0.0.10", "AA:BB:CC:00:00:0A", "evpn"),
    )
    assert (host.address, host.mac_vrf, host.learned, host.learned_on, host.es) == ("10.0.0.8", "macvrf-1", "", "", "")


def test_a_neighbour_on_a_routed_port_is_on_that_port_and_its_segment():
    (host,) = _endpoints(
        NeighborEntry("192.168.0.1", "AA:BB:CC:00:00:09", "dynamic"),
        nis=("default",),
        interface="ethernet-1/3.0",
    )
    assert (host.ip_vrf, host.mac_vrf, host.learned_on, host.es) == ("default", "", "ethernet-1/3.0", "ES-2")


def test_a_neighbour_on_the_management_port_is_not_an_endpoint():
    management = _endpoints(
        NeighborEntry("172.20.20.1", "AA:BB:CC:00:00:0A", "dynamic"),
        nis=("mgmt",),
        interface="mgmt0.0",
    )
    assert management == []


#: The inventory: two leaves and a spine, as containerlab names them.
FABRIC = alias_index([("leaf1", "clab-dc-leaf1"), ("leaf2", "clab-dc-leaf2"), ("spine1", "clab-dc-spine1")])


def _isl_device(lldp: Dict[str, str]) -> Tables:
    """A leaf with a routed uplink, an irb host on lag1 and one on ethernet-1/3."""
    return Tables(
        [
            NeighborCache("ethernet-1/49.0", ("default",), (NeighborEntry("100.64.0.1", "AA:00:00:00:00:01", "dynamic"),)),
            NeighborCache("irb0.1", IRB, (
                NeighborEntry("10.0.0.5", "AA:BB:CC:00:00:05", "dynamic"),
                NeighborEntry("10.0.0.6", "AA:BB:CC:00:00:06", "dynamic"),
            )),
        ],
        [BridgeTable("macvrf-1", (
            MacEntry.read("AA:BB:CC:00:00:05", "lag1.1", "learnt"),
            MacEntry.read("AA:BB:CC:00:00:06", "ethernet-1/3.1", "learnt"),
        ))],
        [ES1, ES2],
        lldp,
    )


def test_a_link_to_another_node_of_the_fabric_is_not_an_endpoint():
    device = _isl_device({"ethernet-1/49": "spine1", "ethernet-1/21": "leaf2", "ethernet-1/3": "server6"})
    # The spine on the uplink and the host behind the LAG to leaf2 go; a
    # server that runs LLDP is not a node of the inventory, and stays.
    kept = device.get_endpoints(fabric=FABRIC)["endpoints"]
    assert [e.address for e in kept] == ["10.0.0.6"]
    assert kept[0].lldp == ("server6",)
    # Without the inventory, nothing says which neighbours are the fabric's.
    assert len(device.get_endpoints()["endpoints"]) == 3


def test_a_host_is_named_by_what_lldp_hears_on_its_port():
    device = _isl_device({"ethernet-1/20": "host1", "ethernet-1/21": "host1", "ethernet-1/3": "host2"})
    by_address = {e.address: e for e in device.get_endpoints()["endpoints"]}
    # On a LAG, from its members, once.
    assert by_address["10.0.0.5"].lldp == ("host1",)
    assert by_address["10.0.0.6"].lldp == ("host2",)
    # A routed neighbour that does not run LLDP has no name.
    assert by_address["100.64.0.1"].lldp == ()


def test_a_host_its_segment_peer_learned_is_named_on_this_side_of_the_segment():
    device = Tables(
        [NeighborCache("irb0.1", IRB, (NeighborEntry("10.0.0.7", "AA:BB:CC:00:00:07", "evpn"),))],
        [BridgeTable("macvrf-1", (MacEntry.read("AA:BB:CC:00:00:07", f"vxlan-interface:vxlan1.1 esi:{ES1.esi}", "evpn"),))],
        [ES1],
        {"ethernet-1/20": "host1"},
    )
    (host,) = device.get_endpoints()["endpoints"]
    assert (host.learned, host.es, host.lldp) == ("remote", "ES-1", ("host1",))


def test_an_endpoint_reads_as_one_row():
    (host,) = _endpoints(
        NeighborEntry("10.0.0.5", "AA:BB:CC:00:00:05", "dynamic"),
        macs=(MacEntry.read("AA:BB:CC:00:00:05", "lag1.100", "learnt"),),
    )
    (row,) = ENDPOINTS_TABLE.rows(host)
    assert {k: row.values[k] for k in ("IP", "Subinterface", "IP-VRF", "MAC-VRF", "Learned-on", "ES")} == {
        "IP": "10.0.0.5",
        "Subinterface": "irb0.1",
        "IP-VRF": "ipvrf-1",
        "MAC-VRF": "macvrf-1",
        "Learned-on": "lag1.100",
        "ES": "ES-1",
    }
