"""The reports that return records, and the tables that render them.

A getter that returns records says what it found; the table declared next to
its spec says what that is called on a screen. These tests cover the seam:
that a record reads a device leaf apart correctly, that a table makes the
rows the old flattening made of the same data, that a field filter narrows a
record to the sub-records whose rows passed, and that both surfaces get the
records where they want objects and the rows where they want a table.
"""

from __future__ import annotations

import json

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

from nornir_srl import cli, mcp_server
from nornir_srl.connections.helpers import clean_structured_key
from nornir_srl.records import (
    Association,
    BgpPeers,
    BgpVpnInstance,
    BridgeTable,
    Candidate,
    EsDestination,
    EsDestinations,
    EthernetSegment,
    Family,
    HostRouteRule,
    Interface,
    InterfaceStats,
    IrbAddress,
    IrbArp,
    IrbInterface,
    IrbNd,
    Lag,
    LagMember,
    LldpInterface,
    LldpNeighbor,
    MacEntry,
    Neighbor,
    NeighborCache,
    NeighborEntry,
    NetworkInstance,
    NextHop,
    StaticNextHop,
    StaticRoute,
    StaticRouteTable,
    Subinterface,
    SubinterfaceState,
    SystemInfo,
    VxlanDestination,
    VxlanInterface,
    as_dict,
)
from nornir_srl.reports import (
    ARP_TABLE,
    BGP_PEERS_TABLE,
    ES_DEST_TABLE,
    ES_TABLE,
    IFSTATS_TABLE,
    IRB_TABLE,
    LAG_TABLE,
    LLDP_TABLE,
    MAC_TABLE,
    ND_TABLE,
    NI_TABLE,
    REPORTS,
    STATIC_ROUTES_TABLE,
    SUBIF_TABLE,
    SYS_INFO_TABLE,
    TUNNEL_TABLE,
    VXLAN_TABLE,
    get_report,
)
from nornir_srl.rows import Column, Table, countdown, extract, flatten


def _aggregated(resource, per_host, failed=()):
    aggregated = AggregatedResult(resource)
    for name, items in per_host.items():
        result = Result(host=Host(name=name, hostname=name), result={resource: items})
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    for name, exception in failed:
        result = Result(host=Host(name=name, hostname=name), exception=exception)
        result.failed = True
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    return aggregated


# --------------------------------------------------------------------------- #
# reading a device leaf apart
# --------------------------------------------------------------------------- #


def test_a_local_mac_entry_names_its_subinterface():
    entry = MacEntry.read("00:C1:AB:00:01:21", "lag1.100", "learnt")
    assert entry.local
    assert (entry.interface, entry.overlay, entry.vtep, entry.esi) == ("lag1.100", "", "", "")
    assert entry.destination == "lag1.100"


def test_the_gateway_mac_is_local_too():
    assert MacEntry.read("00:00:5E:00:01:01", "irb-interface", "irb-interface-anycast").local


def test_a_mac_behind_a_vtep_reads_the_overlay_and_vni():
    entry = MacEntry.read(
        "1A:09:0C:FF:00:42", "vxlan-interface:vxlan1.101 vtep:192.168.255.4 vni:101", "evpn-static"
    )
    assert not entry.local
    assert (entry.overlay, entry.vtep, entry.vni, entry.esi) == ("vxlan1.101", "192.168.255.4", 101, "")
    assert entry.interface == ""


def test_a_mac_behind_a_segment_reads_the_esi():
    entry = MacEntry.read(
        "00:C1:AB:00:03:43", "vxlan-interface:vxlan1.101 esi:00:01:03:00:00:00:66:00:01:03", "evpn"
    )
    assert not entry.local
    assert entry.esi == "00:01:03:00:00:00:66:00:01:03"
    assert entry.vtep == "" and entry.vni is None


def test_a_mac_learned_over_evpn_mpls_is_remote_too():
    """A gateway's WAN side learns MACs from a far-end PE with a label, not a VTEP."""
    entry = MacEntry.read("00:00:00:00:01:14", "far-end:192.0.2.7 nh-tag:172799368578 label:310001", "evpn")
    assert not entry.local
    assert (entry.far_end, entry.label, entry.vtep, entry.interface) == ("192.0.2.7", 310001, "", "")


def test_an_empty_destination_is_still_an_entry():
    assert MacEntry.read("00:00:00:00:00:01", None, None) == MacEntry("00:00:00:00:00:01", "", "")


def test_an_association_knows_its_designated_forwarder():
    elected = Association("subnet-1", (Candidate("192.168.255.1"), Candidate("192.168.255.2", True)))
    assert elected.designated == "192.168.255.2"
    assert Association("subnet-1", (Candidate("192.168.255.1"),)).designated is None
    assert Association("subnet-1").designated is None


def test_a_neighbor_finds_a_family_by_name():
    peer = Neighbor("10.0.0.2", "established", families=(Family("evpn", received=3),))
    assert peer.family("evpn").received == 3
    assert peer.family("ipv4-unicast") is None


# --------------------------------------------------------------------------- #
# a table makes rows of records
# --------------------------------------------------------------------------- #

LEAF_NI = NetworkInstance(
    name="subnet-1",
    type="mac-vrf",
    oper="up",
    overlays=("vxlan1.101",),
    evis=("101",),
    instances=(BgpVpnInstance(1, ("100:101",), ("100:101",)),),
    interfaces=(
        Subinterface("irb1.101", "up", prefixes=(), mtu=9000, associated=("ipvrf-1",)),
        Subinterface("lag1.100", "up", vlan=100, mtu=9214),
    ),
)


def test_a_column_shows_a_field_or_composes_a_cell():
    assert Column("NI", "name").of(LEAF_NI) == "subnet-1"
    assert Column("VNI", lambda ni: ", ".join(ni.evis)).of(LEAF_NI) == "101"


def test_a_cell_is_never_none():
    assert Column("mtu", "mtu").of(Subinterface("system0.0", "up")) == ""


def test_a_record_with_sub_records_is_one_row_each_inheriting_its_own_columns():
    rows = NI_TABLE.rows(LEAF_NI)
    assert [r.values["Subitf"] for r in rows] == ["irb1.101", "lag1.100"]
    assert all(r.values["NI"] == "subnet-1" for r in rows)
    # The first row carries the instance; the next continues it, and drops the
    # inherited cells when the table groups them.
    assert not rows[0].continues and rows[1].continues
    assert "NI" not in rows[1].cells(group=True)
    assert rows[1].cells(group=True)["vlan"] == 100
    assert rows[0].values["assoc-ni"] == "ipvrf-1"
    assert rows[1].values["assoc-ni"] == ""


def test_a_gateway_s_route_targets_are_kept_per_instance_and_shown_together():
    gateway = NetworkInstance(
        "ipvrf-l3dci", "ip-vrf", "up",
        instances=(
            BgpVpnInstance(1, ("3000:3000",), ("3000:3000",)),
            BgpVpnInstance(2, ("65000:3000",), ("65000:3000",), rd="192.0.2.8:13000"),
        ),
    )
    assert gateway.import_rts == ("3000:3000", "65000:3000")
    assert NI_TABLE.rows(gateway)[0].values["In-RT"] == "3000:3000, 65000:3000"


def test_a_record_without_sub_records_is_still_a_row():
    empty = NetworkInstance("default", "default", "up", router_id="192.168.255.1")
    rows = NI_TABLE.rows(empty)
    assert len(rows) == 1
    assert rows[0].values["router-id"] == "192.168.255.1"
    # Only its own columns: a missing cell renders as an empty one everywhere.
    assert "Subitf" not in rows[0].values and not rows[0].continues


def test_the_columns_are_named_once_in_the_order_they_read():
    assert NI_TABLE.column_names[:2] == ["NI", "oper"]
    assert NI_TABLE.column_names[-6:] == ["Subitf", "assoc-ni", "if-oper", "ip-prefix", "mtu", "vlan"]
    for table in (
        NI_TABLE, BGP_PEERS_TABLE, MAC_TABLE, ES_TABLE, VXLAN_TABLE,
        SUBIF_TABLE, IFSTATS_TABLE, LLDP_TABLE, ARP_TABLE, ND_TABLE,
        SYS_INFO_TABLE, LAG_TABLE, STATIC_ROUTES_TABLE, TUNNEL_TABLE, IRB_TABLE, ES_DEST_TABLE,
    ):
        names = table.column_names
        assert len(set(names)) == len(names)


def test_a_family_cell_says_why_it_shows_no_counts():
    def cell(neighbor):
        return {r.values["evpn\nRx/Act/Tx"] for r in BGP_PEERS_TABLE.rows(BgpPeers("default", (neighbor,)))}.pop()

    assert cell(Neighbor("p", "established")) == "-"
    assert cell(Neighbor("p", "established", families=(Family("evpn", enabled=False),))) == "disabled"
    assert cell(Neighbor("p", "established", families=(Family("evpn", oper="down"),))) == "down"
    assert cell(Neighbor("p", "established", families=(Family("evpn", received=8, active=7, sent=2),))) == "8/7/2"


def test_the_flags_cell_spells_dynamic_bfd_and_fast_failover():
    peer = Neighbor("p", "established", bfd=True, fast_failover=True)
    row = BGP_PEERS_TABLE.rows(BgpPeers("default", (peer,)))[0].values
    assert row["flags"] == "-BF"
    assert row["local-as"] == "-", "an unknown local AS reads as a dash"


def test_an_established_session_reads_as_up_and_the_rest_as_bgp_says():
    def state(value):
        return BGP_PEERS_TABLE.rows(BgpPeers("default", (Neighbor("p", value),)))[0].values["state"]

    assert state("established") == "up"
    assert state("active") == "active" and state("idle") == "idle"
    # The record itself keeps the device's word: a check and the JSON see it.
    assert as_dict(Neighbor("p", "established"))["state"] == "established"
    assert BGP_PEERS_TABLE.filter([BgpPeers("default", (Neighbor("p", "established"),))], {"state": "up"})


def test_the_es_table_writes_the_attachment_evi_and_df_election():
    virtual = EthernetSegment(
        "L3-ES-1", "00:01:..", "virtual", "all-active", "up",
        next_hops=(NextHop("10.1.100.254", ("2",)), NextHop("10.1.200.254", ("3",))),
        associations=(Association("subnet-1", (Candidate("192.168.255.1"), Candidate("192.168.255.2", True))),),
    )
    row = ES_TABLE.rows(virtual)[0].values
    assert row["itf/nh"] == "10.1.100.254 10.1.200.254"
    assert row["evi"] == "10.1.100.254:2 10.1.200.254:3"
    assert row["ni-peers"] == "subnet-1:[192.168.255.1 192.168.255.2(DF)]"


def test_the_vxlan_table_writes_the_destination_list():
    vxlan = VxlanInterface("vxlan1.101", "subnet-1", 101, (VxlanDestination("192.168.255.2", 101),))
    row = VXLAN_TABLE.rows(vxlan)[0].values
    assert row["ing-vni"] == 101
    assert row["destinations"] == "(192.168.255.2, 101)"
    bare = VXLAN_TABLE.rows(VxlanInterface("vxlan1.102", "subnet-2"))[0].values
    assert (bare["ing-vni"], bare["destinations"]) == ("-", "-")


def test_the_subif_table_lists_addresses_and_leaves_an_unset_mtu_blank():
    port = Interface(
        "ethernet-1/10",
        (
            SubinterfaceState("ethernet-1/10.0", "routed", "enable", "up", ip_mtu=9214, ipv4=("192.168.1.1/30",)),
            SubinterfaceState("ethernet-1/10.100", "bridged", "enable", "down", down_reason="port-down", vlan=100),
        ),
    )
    rows = SUBIF_TABLE.rows(port)
    assert [r.values["Subitf"] for r in rows] == ["ethernet-1/10.0", "ethernet-1/10.100"]
    assert rows[0].values["Itf"] == "ethernet-1/10" and rows[1].continues
    assert (rows[0].values["ipv4"], rows[0].values["ipv6"]) == (["192.168.1.1/30"], "")
    assert (rows[1].values["ip-mtu"], rows[1].values["vlan"], rows[1].values["down-reason"]) == ("", 100, "port-down")


def test_the_ifstats_table_shows_the_port_state_only_where_the_sample_had_it():
    """The CLI's two samples read counters alone; the server streams the state too."""
    cli = IFSTATS_TABLE.rows(InterfaceStats("ethernet-1/1", in_kbps=2.7, in_errors=1))[0].values
    assert (cli["oper-state"], cli["down-reason"], cli["in-Kbps"], cli["in-err"]) == ("", "", 2.7, 1)
    live = IFSTATS_TABLE.rows(InterfaceStats("lag2", oper="down/standby", down_reason="standby-signaling"))[0].values
    assert (live["oper-state"], live["down-reason"]) == ("down/standby", "standby-signaling")


def test_the_lldp_table_is_one_row_per_neighbour():
    port = LldpInterface("ethernet-1/49", (LldpNeighbor("s1", "ethernet-1/1"), LldpNeighbor("s2", "ethernet-1/1", "to l1")))
    rows = LLDP_TABLE.rows(port)
    assert [(r.values["Nbr-System"], r.values["Nbr-port"], r.values["Nbr-port-desc"]) for r in rows] == [
        ("s1", "ethernet-1/1", ""),
        ("s2", "ethernet-1/1", "to l1"),
    ]
    assert LLDP_TABLE.rows(LldpInterface("ethernet-1/50"))[0].values == {"interface": "ethernet-1/50"}


def test_the_arp_and_nd_tables_write_the_time_left_and_join_the_instances():
    cache = NeighborCache(
        "irb1.101",
        ("ipvrf-1", "subnet-1"),
        (
            NeighborEntry("10.0.1.2", "00:C1:AB:00:01:21", "dynamic", expires_in=14332),
            NeighborEntry("10.0.1.254", "00:00:5E:00:01:01", "static"),
        ),
    )
    rows = ARP_TABLE.rows(cache)
    assert rows[0].values["NI"] == "ipvrf-1, subnet-1"
    assert [(r.values["IPv4"], r.values["Type"], r.values["expiry"]) for r in rows] == [
        ("10.0.1.2", "dynamic", "3:58:52s"),
        ("10.0.1.254", "static", "-"),
    ]
    nd = ND_TABLE.rows(
        NeighborCache("irb1.101", ("ipvrf-1",), (NeighborEntry("2001:db8::10", "00:11:22:33:44:55", "dynamic", "reachable", 25),))
    )[0].values
    assert (nd["IPv6"], nd["State"], nd["next_state"]) == ("2001:db8::10", "reachable", "0:00:25s")
    # The record keeps a number, so a reader can compare rather than parse.
    assert as_dict(cache)["entries"][0]["expires_in"] == 14332


def test_the_irb_table_composes_the_address_flags_and_the_neighbour_settings():
    irb = IrbInterface(
        "irb1.101",
        nis=("ipvrf-1", "subnet-1"),
        ipv4=(IrbAddress("10.0.1.254/24", primary=True, anycast_gw=True), IrbAddress("10.0.1.253/24")),
        anycast_gw=True,
        anycast_gw_mac="00:00:5E:00:01:01",
        arp=IrbArp(learn_unsolicited=True, host_routes=(HostRouteRule("dynamic"),), evpn_advertise=("dynamic",)),
        nd=IrbNd(proxy=True, learn_unsolicited="global", interface_less_routing=True),
    )
    row = IRB_TABLE.rows(irb)[0].values
    assert row["NI"] == "ipvrf-1, subnet-1"
    assert row["ipv4"] == "10.0.1.254/24 (P,AGW), 10.0.1.253/24" and row["ipv6"] == ""
    assert (row["AGW?"], row["IFL?"]) == ("Y", "Y")
    assert row["arp"] == "learn-unsol, host-rt:dynamic/no-dp"
    assert row["nd"] == "proxy, learn-unsol:global"
    assert (row["arp-evpn"], row["nd-evpn"]) == ("dynamic", "-")
    bare = IRB_TABLE.rows(IrbInterface("irb0.0"))[0].values
    assert (bare["AGW?"], bare["arp"], bare["nd"], bare["arp-evpn"], bare["IFL?"]) == ("N", "-", "-", "-", "N")
    # The record keeps the parts the cells were composed from.
    assert as_dict(irb)["ipv4"][0] == {"prefix": "10.0.1.254/24", "primary": True, "anycast_gw": True}


def test_the_lag_table_shortens_member_names_and_the_es_dest_table_joins_vteps():
    lag = Lag("lag1", "up", mtu=9232, min_links=1, type="lacp", members=(LagMember("ethernet-1/20", "up", "ACTIVE"),))
    rows = LAG_TABLE.rows(lag)
    assert (rows[0].values["lag"], rows[0].values["min"], rows[0].values["stby-sig"]) == ("lag1", 1, "")
    assert (rows[0].values["member-itf"], rows[0].values["act"]) == ("et-1/20", "ACTIVE")
    dest = EsDestinations("vxlan1", (EsDestination("00:01:03:00:00:00:66:00:01:03", "vxlan1.101", ("192.168.255.3", "192.168.255.4")),))
    row = ES_DEST_TABLE.rows(dest)[0].values
    assert (row["tunnel"], row["esi"], row["vteps"]) == ("vxlan1", "00:01:03:00:00:00:66:00:01:03", "192.168.255.3 192.168.255.4")


def test_the_static_routes_table_marks_a_resolving_next_hop():
    table = StaticRouteTable(
        "default",
        (StaticRoute("10.9.0.0/16", "enable", installed=True, preference=5, next_hop_group="core",
                     next_hops=(StaticNextHop("10.0.0.1"), StaticNextHop("10.0.0.2", resolve=True))),),
    )
    row = STATIC_ROUTES_TABLE.rows(table)[0].values
    assert (row["NI"], row["route"], row["admin-state"], row["installed"], row["pref"]) == ("default", "10.9.0.0/16", "enable", True, 5)
    assert row["nhops"] == ["10.0.0.1", "10.0.0.2(R)"]
    unknown = STATIC_ROUTES_TABLE.rows(StaticRouteTable("default", (StaticRoute("10.9.0.0/16"),)))[0].values
    assert (unknown["installed"], unknown["nhops"]) == ("", "")


def test_the_sys_info_table_is_one_row_of_the_chassis():
    info = SystemInfo("7220 IXR-D2", "Sim Serial No.", "Sim Part No.", "1A:07:09:FF:00:00", "2026-08-28T16:02:09.262Z", "26.7.1")
    row = SYS_INFO_TABLE.rows(info)[0].values
    assert row == {
        "type": "7220 IXR-D2",
        "hw-mac-address": "1A:07:09:FF:00:00",
        "last-booted": "2026-08-28T16:02:09.262Z",
        "part-number": "Sim Part No.",
        "serial-number": "Sim Serial No.",
        "software-version": "26.7.1",
    }


def test_a_countdown_reads_as_the_cli_has_always_written_it():
    assert countdown(14332) == "3:58:52s"
    assert countdown(90061) == "1 day, 1:01:01s"
    assert countdown(-3) == "-1 day, 23:59:57s"
    assert countdown(None) == "-"


# --------------------------------------------------------------------------- #
# a field filter narrows records by their rows
# --------------------------------------------------------------------------- #


def test_a_filter_keeps_the_sub_records_whose_row_passed():
    kept = NI_TABLE.filter([LEAF_NI], {"Subitf": "^lag"})
    assert len(kept) == 1
    assert [i.name for i in kept[0].interfaces] == ["lag1.100"]
    # The record itself is untouched.
    assert len(LEAF_NI.interfaces) == 2


def test_a_filter_drops_a_record_none_of_whose_rows_passed():
    assert NI_TABLE.filter([LEAF_NI], {"Subitf": "ethernet"}) == []


def test_a_filter_on_the_record_s_own_columns_applies_to_every_row():
    assert NI_TABLE.filter([LEAF_NI], {"type": "mac-vrf"}) == [LEAF_NI]
    assert NI_TABLE.filter([LEAF_NI], {"type": "ip-vrf"}) == []


def test_a_filter_reads_a_record_without_sub_records_as_one_row():
    tables = [BridgeTable("subnet-1", ()), BridgeTable("subnet-2", ())]
    assert MAC_TABLE.filter(tables, {"NI": "2"}) == [tables[1]]


def test_no_filter_keeps_everything():
    assert NI_TABLE.filter([LEAF_NI], None) == [LEAF_NI]


# --------------------------------------------------------------------------- #
# the flatteners route through a table
# --------------------------------------------------------------------------- #


def test_extract_uses_the_table_s_columns_and_keeps_the_records():
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]})
    columns, per_node = extract("nwi_itfs", results, table=NI_TABLE)
    assert columns == NI_TABLE.column_names
    assert per_node[0].records == [LEAF_NI]
    assert [r.values["Subitf"] for r in per_node[0].rows] == ["irb1.101", "lag1.100"]


def test_extract_with_a_table_has_no_columns_when_nothing_answered():
    columns, per_node = extract("nwi_itfs", _aggregated("nwi_itfs", {"leaf1": []}), table=NI_TABLE)
    assert columns == [] and per_node == []


def test_extract_with_a_table_filters_the_records_it_keeps():
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]})
    _columns, per_node = extract("nwi_itfs", results, field_filter={"Subitf": "irb"}, table=NI_TABLE)
    assert [i.name for i in per_node[0].records[0].interfaces] == ["irb1.101"]
    assert len(per_node[0].rows) == 1


def test_extract_with_a_table_still_reports_a_failed_host():
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]}, failed=[("leaf2", RuntimeError("boom"))])
    _columns, per_node = extract(
        "nwi_itfs", results, table=NI_TABLE, on_error=lambda node, exc: {"_error": str(exc)}
    )
    failed = next(n for n in per_node if n.node == "leaf2")
    assert failed.rows[0].values == {"_error": "boom"} and failed.records == []


def test_flatten_uses_the_table_and_labels_the_node():
    columns, rows = flatten("leaf1", [LEAF_NI], NI_TABLE)
    assert columns == NI_TABLE.column_names
    assert rows[0]["Node"] == "leaf1" and rows[0]["Subitf"] == "irb1.101"
    assert flatten("leaf1", [], NI_TABLE) == ([], [])


# --------------------------------------------------------------------------- #
# records as objects
# --------------------------------------------------------------------------- #


def test_a_record_is_a_plain_object_with_its_lists_intact():
    obj = as_dict(LEAF_NI)
    assert obj["overlays"] == ["vxlan1.101"]
    assert obj["instances"] == [{"id": 1, "import_rts": ["100:101"], "export_rts": ["100:101"], "rd": ""}]
    assert obj["interfaces"][0] == {
        "name": "irb1.101", "oper": "up", "prefixes": [], "mtu": 9000, "vlan": None,
        "associated": ["ipvrf-1"],
    }
    assert "In-RT" not in obj and "Subitf" not in obj


def test_the_cli_emits_records_as_json_for_a_report_that_has_them(capsys):
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]})
    results.name = "nwi_itfs"
    cli.print_report(
        results, "Network Instances", [], output=cli.OutputFormat.JSON, table=NI_TABLE
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == [{"node": "leaf1", **as_dict(LEAF_NI)}]


def test_the_cli_emits_rows_as_csv_for_the_same_report(capsys):
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]})
    results.name = "nwi_itfs"
    cli.print_report(
        results, "Network Instances", [], output=cli.OutputFormat.CSV, table=NI_TABLE
    )
    header = capsys.readouterr().out.splitlines()[0]
    assert header == "Node," + ",".join(clean_structured_key(c) for c in NI_TABLE.column_names)


def test_the_mcp_server_emits_records_for_a_report_that_has_them(monkeypatch):
    results = _aggregated("nwi_itfs", {"leaf1": [LEAF_NI]}, failed=[("leaf2", RuntimeError("boom"))])
    monkeypatch.setattr(mcp_server, "_query", lambda spec, inv_filter, **params: results)
    emitted = json.loads(mcp_server._run_report("ni"))
    assert emitted[0] == {"node": "leaf1", **as_dict(LEAF_NI)}
    assert emitted[1] == {"node": "leaf2", "_error": "boom"}


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

CONVERTED = (
    "mac", "ni", "vxlan", "es", "bgp_peers", "ipv4_rib", "ipv6_rib", "bgp_rib",
    "subif", "ifstats", "lldp", "arp", "nd",
    "sys_info", "lag", "static_routes", "tunnel_table", "irb", "es_dest",
    "bfd", "isis", "ospf", "resources", "components", "transceivers",
    "bgp_received_routes", "bgp_advertised_routes",
) + tuple(r.name for r in REPORTS if r.name.startswith("bgp_rib_"))

#: What is left without a table: computed by the server's store rather than a
#: getter, nested too deep for a table, or collected fabric-wide as findings.
NOT_A_GETTER = ("overview", "topology", "services", "bridge_domains", "routers", "routing_pol", "checks")


@pytest.mark.parametrize("name", CONVERTED)
def test_a_converted_report_declares_a_table(name):
    assert isinstance(get_report(name).table_for({}), Table)


@pytest.mark.parametrize("name", CONVERTED)
def test_a_converted_report_s_key_columns_are_columns_it_has(name):
    spec = get_report(name)
    rendered = {"Node", *(clean_structured_key(c) for c in spec.table_for({}).column_names)}
    assert set(spec.key_columns) <= rendered, spec.key_columns


def test_every_getter_backed_report_declares_a_table():
    """Nothing is left flattening dicts by the keys they happen to carry."""
    assert {r.name for r in REPORTS if r.table is None} == set(NOT_A_GETTER)
    assert set(CONVERTED) | set(NOT_A_GETTER) == {r.name for r in REPORTS}


def test_the_bgp_rib_table_follows_the_family_route_type_and_detail():
    """One report, many tables: the columns are the family's, plus detail on request."""
    spec = get_report("bgp_rib")
    default = spec.table_for({}).column_names
    assert default == spec.table_for({"route_fam": "evpn", "route_type": "2"}).column_names
    assert "MAC" in default and "Prefix" not in default
    ipv4 = spec.table_for({"route_fam": "ipv4"}).column_names
    assert "Prefix" in ipv4 and "MAC" not in ipv4 and "soo" not in ipv4
    assert spec.table_for({"route_fam": "l3vpn-v4"}).column_names[1:4] == ["st", "Pfx", "RD"]
    detailed = spec.table_for({"route_fam": "ipv4", "detail": True}).column_names
    assert detailed[: len(ipv4)] == ipv4 and detailed[-1] == "neighbor-as"
    # A streaming variant has its family baked in and only detail to decide.
    assert get_report("bgp_rib_evpn_5").table_for({}).column_names == spec.table_for(
        {"route_fam": "evpn", "route_type": "5"}
    ).column_names
    assert "dpath" in get_report("bgp_rib_ipv4").table_for({"detail": True}).column_names
