"""Tests for the fabric sanity checks.

Each check is a pure function over the payloads the report getters return, so a
test is a fabric written out as those payloads and the findings it should
produce. Where a getter returns records (:mod:`nornir_srl.records`) the fabric
is written as those records, and a check that reads a field by the wrong name
fails to import; where it still returns items, the shapes here mirror the
getter exactly, because a check that reads a key by the wrong name would
otherwise pass here and find nothing on a real fabric.
"""

from dataclasses import replace
from typing import Any, Dict, List, Tuple

from nornir_srl import checks as checks_module
from nornir_srl.checks import (
    CHECKS,
    CHECKS_BY_NAME,
    CHECKS_COLUMNS,
    ERROR,
    REQUIRED_REPORTS,
    WARNING,
    Check,
    FabricState,
    run_checks,
    underlay_domains,
)
from nornir_srl.records import (
    Association,
    BgpPeers,
    BgpVpnInstance,
    Candidate,
    EthernetSegment,
    Family,
    Interface,
    InterfaceStats,
    LldpInterface,
    LldpNeighbor,
    Neighbor,
    NetworkInstance,
    Route,
    RouteTable,
    Subinterface,
    SubinterfaceState,
    VxlanInterface,
)
from nornir_srl.reports import REPORTS_BY_NAME


def fabric(**reports: Dict[str, Any]) -> FabricState:
    """A fabric whose nodes are the ones named in the reports given."""
    state = FabricState(reports=dict(reports))
    state.hostnames = {
        node: node for report in reports.values() for node in report
    }
    return state


def run(check: str, state: FabricState) -> List[Dict[str, Any]]:
    return [f.as_row() for f in CHECKS_BY_NAME[check].run(state)]


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def test_check_names_are_unique():
    assert len({c.name for c in CHECKS}) == len(CHECKS)


#: Checks that read the server's timeline rather than a report.
TIMELINE_CHECKS = {"flapping"}


def test_every_check_reads_reports_that_exist():
    for check in CHECKS:
        if check.name in TIMELINE_CHECKS:
            assert not check.requires
            continue
        assert check.requires, f"{check.name} reads no report"
        for report in check.requires:
            assert report in REPORTS_BY_NAME, (
                f"{check.name} reads '{report}', which is not a report"
            )


def test_required_reports_is_what_the_checks_ask_for():
    assert set(REQUIRED_REPORTS) == {r for c in CHECKS for r in c.requires}


def test_a_finding_fills_every_column():
    findings = run("bgp_down", fabric(bgp_peers=_peers(state="active")))
    assert list(findings[0]) == list(CHECKS_COLUMNS)


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #


def _peers(
    state: str = "established",
    evpn: Family = Family("evpn", received=12, active=12, sent=6),
    **overrides: Any,
) -> Dict[str, Any]:
    """One leaf with one session carrying ipv4-unicast and *evpn*."""
    peer = Neighbor(
        peer="10.0.0.2",
        state=state,
        peer_as=65002,
        group="spines",
        families=(Family("ipv4-unicast", received=5, active=5, sent=3), evpn),
    )
    peer = replace(peer, **overrides)
    return {"leaf1": [BgpPeers("default", (peer,))]}


def test_bgp_down_finds_a_session_that_is_not_established():
    findings = run("bgp_down", fabric(bgp_peers=_peers(state="active")))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Node"] == "leaf1"
    assert findings[0]["Subject"] == "default/10.0.0.2"
    assert "session is active" in findings[0]["Detail"]
    assert "peer-group spines" in findings[0]["Detail"]


def test_bgp_down_leaves_an_established_session_alone():
    assert run("bgp_down", fabric(bgp_peers=_peers())) == []


def test_bgp_down_reads_the_state_whatever_its_case():
    assert run("bgp_down", fabric(bgp_peers=_peers(state="Established"))) == []


def test_bgp_down_does_not_report_a_session_that_is_not_meant_to_be_up():
    """The peers report carries no admin-state to tell a disabled peer by."""
    assert run("bgp_down", fabric(bgp_peers=_peers(state=""))) == []


def test_bgp_af_down_finds_the_family_under_an_established_session():
    peers = _peers(evpn=Family("evpn", oper="down"))
    findings = run("bgp_af_down", fabric(bgp_peers=peers))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert "evpn is down" in findings[0]["Detail"]


def test_bgp_af_down_says_nothing_about_a_session_already_reported_down():
    """One fault is one finding: bgp_down has it."""
    peers = _peers(state="idle", evpn=Family("evpn", oper="down"))
    assert run("bgp_af_down", fabric(bgp_peers=peers)) == []


def test_bgp_af_down_ignores_a_family_that_was_never_enabled():
    peers = _peers(evpn=Family("evpn", enabled=False, oper="down"))
    assert run("bgp_af_down", fabric(bgp_peers=peers)) == []


def test_bgp_no_routes_finds_a_family_that_has_learned_nothing():
    peers = _peers(evpn=Family("evpn", received=0, active=0, sent=6))
    findings = run("bgp_no_routes", fabric(bgp_peers=peers))
    assert len(findings) == 1
    assert findings[0]["Severity"] == WARNING
    assert "evpn is up but has received no routes" in findings[0]["Detail"]


def test_bgp_no_routes_counts_received_rather_than_active():
    """A route received and not selected is a policy question, not a fault."""
    peers = _peers(evpn=Family("evpn", received=12, active=0, sent=6))
    assert run("bgp_no_routes", fabric(bgp_peers=peers)) == []


def test_bgp_no_routes_ignores_a_family_that_is_not_carrying():
    peers = _peers(evpn=Family("evpn", enabled=False))
    assert run("bgp_no_routes", fabric(bgp_peers=peers)) == []


def test_bgp_no_routes_ignores_a_family_that_is_down():
    """A family that is down is bgp_af_down's finding, not an empty one."""
    peers = _peers(evpn=Family("evpn", oper="down"))
    assert run("bgp_no_routes", fabric(bgp_peers=peers)) == []


def test_bgp_checks_survive_a_network_instance_with_no_neighbors():
    state = fabric(bgp_peers={"leaf1": [BgpPeers("default", ())]})
    assert run("bgp_down", state) == []


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #


def _subif(node: str = "leaf1", **overrides: Any) -> Dict[str, Any]:
    subif = SubinterfaceState(
        "ethernet-1/1.0", type="routed", admin="enable", oper="up", ip_mtu=9000
    )
    return {node: [Interface("ethernet-1/1", (replace(subif, **overrides),))]}


def test_itf_down_finds_a_subinterface_enabled_but_not_up():
    state = fabric(subif=_subif(oper="down", down_reason="port-down"))
    findings = run("itf_down", state)
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1.0"
    assert "port-down" in findings[0]["Detail"]


def test_itf_down_leaves_a_port_held_down_on_purpose_alone():
    """The standby side of a single-active segment reads as down/standby."""
    state = fabric(subif=_subif(oper="down/standby", down_reason="standby-signaling"))
    assert run("itf_down", state) == []


def test_itf_down_leaves_an_administratively_disabled_port_alone():
    state = fabric(subif=_subif(oper="down", admin="disable"))
    assert run("itf_down", state) == []


def test_itf_down_says_so_when_the_node_gave_no_reason():
    findings = run("itf_down", fabric(subif=_subif(oper="down")))
    assert "no reason reported" in findings[0]["Detail"]


def test_itf_down_ignores_the_management_interface():
    state = fabric(
        subif={
            "leaf1": [
                Interface("mgmt0", (SubinterfaceState("mgmt0.0", admin="enable", oper="down"),))
            ]
        }
    )
    assert run("itf_down", state) == []


def _ifstats(**overrides: Any) -> Dict[str, Any]:
    stats = InterfaceStats("ethernet-1/1", in_kbps=12.0, out_kbps=8.0)
    return {"leaf1": [replace(stats, **overrides)]}


def test_itf_errors_finds_error_packets():
    findings = run("itf_errors", fabric(ifstats=_ifstats(in_errors=4)))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1"
    assert "4 in / 0 out error packets" in findings[0]["Detail"]


def test_itf_errors_reports_discards_less_urgently_than_errors():
    findings = run("itf_errors", fabric(ifstats=_ifstats(out_discards=7)))
    assert [f["Severity"] for f in findings] == [WARNING]
    assert "0 in / 7 out discarded packets" in findings[0]["Detail"]


def test_itf_errors_reports_errors_and_discards_separately():
    state = fabric(ifstats=_ifstats(in_errors=1, in_discards=2))
    assert sorted(f["Severity"] for f in run("itf_errors", state)) == [ERROR, WARNING]


def test_itf_errors_says_nothing_about_a_clean_interface():
    assert run("itf_errors", fabric(ifstats=_ifstats())) == []


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #


def _lldp(**adjacencies: List[Tuple[str, str, str]]) -> Dict[str, Any]:
    """``_lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")])`` per node."""
    return {
        node: [
            LldpInterface(local, (LldpNeighbor(peer, peer_port),))
            for local, peer, peer_port in links
        ]
        for node, links in adjacencies.items()
    }


def test_lldp_one_sided_says_nothing_about_a_link_both_ends_see():
    state = fabric(
        lldp=_lldp(
            leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
            spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")],
        )
    )
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_finds_the_end_that_is_not_seen_back():
    state = fabric(
        lldp=_lldp(
            leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
            spine1=[],
        )
    )
    findings = run("lldp_one_sided", state)
    assert len(findings) == 1
    assert findings[0]["Node"] == "leaf1"
    assert findings[0]["Subject"] == "ethernet-1/1"
    assert "does not see it back" in findings[0]["Detail"]


def test_lldp_one_sided_ignores_a_neighbour_that_is_not_in_the_inventory():
    """A node we do not poll cannot be expected to report anything back."""
    state = fabric(lldp=_lldp(leaf1=[("ethernet-1/1", "some-router", "xe-0/0/0")]))
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_matches_a_short_system_name_to_a_prefixed_inventory():
    """containerlab names a host clab-dc1-leaf1; the node advertises leaf1."""
    state = FabricState(
        reports={
            "lldp": {
                "clab-dc1-leaf1": [
                    LldpInterface("ethernet-1/1", (LldpNeighbor("spine1", "ethernet-1/1"),))
                ],
                "clab-dc1-spine1": [
                    LldpInterface("ethernet-1/1", (LldpNeighbor("leaf1", "ethernet-1/1"),))
                ],
            }
        }
    )
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_ignores_management_links():
    state = fabric(lldp=_lldp(leaf1=[("mgmt0", "spine1", "mgmt0")], spine1=[]))
    assert run("lldp_one_sided", state) == []


def _link_pair() -> Dict[str, Any]:
    return _lldp(
        leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
        spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")],
    )


def _mtu(node: str, mtu: int, index: str = "0") -> Dict[str, Any]:
    subif = SubinterfaceState(f"ethernet-1/1.{index}", admin="enable", oper="up", ip_mtu=mtu)
    return {node: [Interface("ethernet-1/1", (subif,))]}


def test_mtu_mismatch_finds_two_ends_that_disagree():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 1500)},
    )
    findings = run("mtu_mismatch", state)
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1.0"
    assert "9000" in findings[0]["Detail"] and "1500" in findings[0]["Detail"]


def test_mtu_mismatch_reports_a_link_once_though_both_ends_see_it():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 1500)},
    )
    assert len(run("mtu_mismatch", state)) == 1


def test_mtu_mismatch_says_nothing_when_the_ends_agree():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 9000)},
    )
    assert run("mtu_mismatch", state) == []


def test_mtu_mismatch_compares_subinterfaces_of_the_same_index():
    """Two services on one trunk are two MTUs, and neither is the other's."""
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000, index="0"), **_mtu("spine1", 1500, index="10")},
    )
    assert run("mtu_mismatch", state) == []


def test_mtu_mismatch_needs_both_ends_to_report_one():
    state = fabric(lldp=_link_pair(), subif=_mtu("leaf1", 9000))
    assert run("mtu_mismatch", state) == []


# --------------------------------------------------------------------------- #
# EVPN services
# --------------------------------------------------------------------------- #


def _ni(node: str, *, vxlan: str = "vxlan1.100", in_rt: str = "65000:100", out_rt: str = "65000:100"):
    return {
        node: [
            NetworkInstance(
                name="mac-vrf-100",
                type="mac-vrf",
                oper="up",
                overlays=(vxlan,),
                instances=(BgpVpnInstance(1, (in_rt,), (out_rt,)),),
            )
        ]
    }


def _vxlan(node: str, *, itf: str = "vxlan1.100", vni: int = 100):
    return {node: [VxlanInterface(name=itf, ni="mac-vrf-100", vni=vni)]}


def test_evpn_service_mismatch_says_nothing_when_two_nodes_agree():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_finds_a_vni_that_differs():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1", vni=100), **_vxlan("leaf2", vni=200)},
    )
    findings = run("evpn_service_mismatch", state)
    # Both ends are wrong until someone decides which one is right.
    assert {f["Node"] for f in findings} == {"leaf1", "leaf2"}
    assert all(f["Subject"] == "mac-vrf-100" for f in findings)
    assert all("VNI" in f["Detail"] for f in findings)


def test_evpn_service_mismatch_does_not_hold_an_unread_vni_against_every_node():
    """A node read without its vxlan-interfaces has an unknown VNI, not a different one."""
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2"), **_ni("leaf3")},
        vxlan={"leaf1": [], **_vxlan("leaf2"), **_vxlan("leaf3")},
    )
    findings = run("evpn_service_mismatch", state)
    # One warning about the gap, on the node that has it - not an error per
    # service on every node carrying it.
    assert [(f["Node"], f["Severity"], f["Subject"]) for f in findings] == [("leaf1", "warning", "VNI")]
    assert "vxlan1.100" in findings[0]["Detail"]


def test_evpn_service_mismatch_leaves_an_unreadable_vxlan_report_to_collection():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan=_vxlan("leaf2"),
    )
    state.errors[("vxlan", "leaf1")] = "timed out"
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_still_compares_route_targets_without_a_vni():
    state = fabric(
        ni={**_ni("leaf1", out_rt="65000:999"), **_ni("leaf2")},
        vxlan={"leaf1": [], **_vxlan("leaf2")},
    )
    findings = run("evpn_service_mismatch", state)
    assert any(f["Severity"] == "error" and "export route-target" in f["Detail"] for f in findings)
    assert not any(f["Detail"].startswith("VNI") for f in findings)


def test_evpn_service_mismatch_finds_route_targets_that_differ():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2", out_rt="65000:999")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    findings = run("evpn_service_mismatch", state)
    assert findings and all("export route-target" in f["Detail"] for f in findings)


def test_evpn_service_mismatch_reads_a_route_target_however_it_is_written():
    """One report strips the 'target:' prefix and another keeps it."""
    state = fabric(
        ni={**_ni("leaf1", in_rt="65000:100"), **_ni("leaf2", in_rt="target:65000:100")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    assert run("evpn_service_mismatch", state) == []


def _gateway(node: str, *, wan_rt: str = "65000:100"):
    """A DCI gateway: the leaves' instance, plus a WAN-side instance of its own."""
    return {
        node: [
            NetworkInstance(
                name="mac-vrf-100",
                type="mac-vrf",
                oper="up",
                overlays=("vxlan1.100",),
                instances=(
                    BgpVpnInstance(1, ("65000:100",), ("65000:100",)),
                    BgpVpnInstance(2, (wan_rt,), (wan_rt,), rd="192.0.2.8:100"),
                ),
            )
        ]
    }


def test_evpn_service_mismatch_lets_a_gateway_carry_a_second_instance():
    """A gateway's WAN-side route-target is not a disagreement with the leaves."""
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2"), **_gateway("dcgw1"), **_gateway("dcgw2")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2"), **_vxlan("dcgw1"), **_vxlan("dcgw2")},
    )
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_compares_a_second_instance_between_the_gateways():
    state = fabric(
        ni={**_ni("leaf1"), **_gateway("dcgw1"), **_gateway("dcgw2", wan_rt="65000:999")},
        vxlan={**_vxlan("leaf1"), **_vxlan("dcgw1"), **_vxlan("dcgw2")},
    )
    findings = run("evpn_service_mismatch", state)
    # Only the gateways have a second instance, so only they are held to it.
    assert {f["Node"] for f in findings} == {"dcgw1", "dcgw2"}
    assert all("bgp-instance 2" in f["Detail"] for f in findings)
    assert not any("leaf1" in f["Detail"] for f in findings)


def test_evpn_service_mismatch_takes_a_name_split_by_route_target_in_one_underlay_as_a_warning():
    """Two services under one name in one fabric, or a mistyped target: a
    warning either way, and no error about the VNI between them."""
    state = fabric(
        ni={
            **_ni("leaf1", in_rt="65000:201", out_rt="65000:201"),
            **_ni("leaf2", in_rt="65000:201", out_rt="65000:201"),
            **_ni("leaf3", in_rt="65000:202", out_rt="65000:202"),
        },
        vxlan={**_vxlan("leaf1", vni=201), **_vxlan("leaf2", vni=201), **_vxlan("leaf3", vni=202)},
    )
    findings = run("evpn_service_mismatch", state)
    assert {f["Severity"] for f in findings} == {WARNING}
    assert {f["Node"] for f in findings} == {"leaf1", "leaf2", "leaf3"}
    leaf1 = next(f for f in findings if f["Node"] == "leaf1")
    assert "65000:201, while leaf3 65000:202 in the same underlay" in leaf1["Detail"]
    assert "two services under one name" in leaf1["Detail"]


def _underlay(node: str, address: str, *reachable: str) -> Dict[str, Any]:
    """A node's own loopback, and the loopbacks it can reach in ``default``."""
    return {
        "ni": NetworkInstance(
            "default", "default", "up",
            interfaces=(Subinterface("system0.0", "up", prefixes=(f"{address}/32",)),),
        ),
        "rib": RouteTable("default", tuple(Route(f"{other}/32", "bgp") for other in reachable)),
    }


def _two_datacenters(*, dc2_gateway_wan_rt: str = "65000:100") -> FabricState:
    """Two fabrics whose underlays do not see each other, joined by gateways over a WAN.

    Each datacenter has a bridge domain of the same name with its own VNI and
    route-target; the gateways carry the leaves' instance and a WAN-side one
    of their own, and reach each other's loopbacks over the WAN.
    """
    dc1 = {"leaf1": "192.0.2.1", "leaf2": "192.0.2.2", "dcgw1": "192.0.2.8"}
    dc2 = {"leaf5": "192.0.2.5", "leaf6": "192.0.2.6", "dcgw3": "192.0.2.18"}
    ni: Dict[str, Any] = {}
    rib: Dict[str, Any] = {}
    vxlan: Dict[str, Any] = {}
    for site, members, rt, vni in ((1, dc1, "65000:201", 201), (2, dc2, "65000:202", 202)):
        for node, address in members.items():
            reachable = [a for n, a in members.items() if n != node]
            if node.startswith("dcgw"):
                # Gateways learn every gateway's loopback over the WAN.
                reachable += [a for n, a in {**dc1, **dc2}.items() if n.startswith("dcgw") and n != node]
                service = _gateway(node, wan_rt=dc2_gateway_wan_rt if site == 2 else "65000:100")[node][0]
                service = replace(service, instances=(BgpVpnInstance(1, (rt,), (rt,)), service.instances[1]))
            else:
                service = _ni(node, in_rt=rt, out_rt=rt)[node][0]
            underlay = _underlay(node, address, *reachable)
            ni[node] = [service, underlay["ni"]]
            rib[node] = [underlay["rib"]]
            vxlan.update(_vxlan(node, vni=vni))
    return fabric(ni=ni, vxlan=vxlan, ipv4_rib=rib)


def test_evpn_service_mismatch_says_nothing_about_a_name_shared_across_underlays():
    """Leaves whose underlays do not see each other are never one service."""
    assert run("evpn_service_mismatch", _two_datacenters()) == []


def test_evpn_service_mismatch_holds_the_gateways_wan_side_together_across_underlays():
    """The WAN side is one service among the gateways, reached over the WAN."""
    findings = run("evpn_service_mismatch", _two_datacenters(dc2_gateway_wan_rt="65000:999"))
    assert {(f["Node"], f["Severity"]) for f in findings} == {("dcgw1", ERROR), ("dcgw3", ERROR)}
    assert all("bgp-instance 2" in f["Detail"] for f in findings)


def test_underlay_domains_fall_back_to_one_without_route_tables():
    assert underlay_domains(["b", "a"], {}, {}) == [["a", "b"]]
    assert underlay_domains([], {}, {}) == []


def test_evpn_service_mismatch_still_errs_on_a_vni_within_one_route_target_group():
    """The silent failure: the same route-target, so routes are imported, but a
    different VNI, so the data plane never joins up."""
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2"), **_ni("leaf5", in_rt="65000:202", out_rt="65000:202")},
        vxlan={**_vxlan("leaf1", vni=100), **_vxlan("leaf2", vni=999), **_vxlan("leaf5", vni=202)},
    )
    findings = run("evpn_service_mismatch", state)
    errors = [f for f in findings if f["Severity"] == ERROR]
    assert {f["Node"] for f in errors} == {"leaf1", "leaf2"}
    assert all("VNI" in f["Detail"] and "leaf5" not in f["Detail"] for f in errors)


def test_evpn_service_mismatch_ignores_a_service_only_one_node_has():
    """A service on one leaf is a service, not a disagreement."""
    state = fabric(ni=_ni("leaf1"), vxlan=_vxlan("leaf1"))
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_ignores_the_default_network_instance():
    state = fabric(
        ni={
            "leaf1": [NetworkInstance("default", "default", "up")],
            "leaf2": [NetworkInstance("default", "default", "up", instances=(BgpVpnInstance(1, ("x",), ("y",)),))],
        },
        vxlan={},
    )
    assert run("evpn_service_mismatch", state) == []


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #


def _candidates(*addresses: str) -> Tuple[Candidate, ...]:
    """DF candidates written the way the ES report shows them: ``(DF)`` marks the elected one."""
    return tuple(
        Candidate(address.removesuffix("(DF)"), designated=address.endswith("(DF)"))
        for address in addresses
    )


def _es(
    node: str,
    *,
    associations: Tuple[Association, ...] = (
        Association("mac-vrf-100", _candidates("10.0.0.1", "10.0.0.2(DF)")),
    ),
    **overrides: Any,
) -> Dict[str, Any]:
    segment = EthernetSegment(
        name="es-1",
        esi="01:00:00:00:00:01:00:00:00:01",
        type="virtual",
        mh_mode="all-active",
        oper="up",
        interfaces=("lag1",),
        next_hops=(),
        associations=associations,
    )
    return {node: [replace(segment, **overrides)]}


def test_es_df_says_nothing_about_a_healthy_segment():
    assert run("es_df", fabric(es={**_es("leaf1"), **_es("leaf2")})) == []


def test_es_df_finds_a_network_instance_with_no_designated_forwarder():
    state = fabric(
        es=_es("leaf1", associations=(Association("mac-vrf-100", _candidates("10.0.0.1", "10.0.0.2")),))
    )
    findings = run("es_df", state)
    assert len(findings) == 1
    assert findings[0]["Subject"] == "es-1/mac-vrf-100"
    assert "no designated forwarder" in findings[0]["Detail"]


def test_es_df_finds_a_network_instance_with_no_candidates_at_all():
    state = fabric(es=_es("leaf1", associations=(Association("mac-vrf-100"),)))
    findings = run("es_df", state)
    assert len(findings) == 1
    assert "no candidates" in findings[0]["Detail"]


def test_es_df_checks_every_network_instance_on_a_segment():
    state = fabric(
        es=_es(
            "leaf1",
            associations=(
                Association("mac-vrf-100", _candidates("10.0.0.1(DF)")),
                Association("mac-vrf-200", _candidates("10.0.0.1", "10.0.0.2")),
            ),
        )
    )
    findings = run("es_df", state)
    assert [f["Subject"] for f in findings] == ["es-1/mac-vrf-200"]


def test_es_df_finds_a_segment_that_is_down():
    findings = run("es_df", fabric(es=_es("leaf1", oper="down")))
    assert any("segment is down" in f["Detail"] for f in findings)


def test_es_df_finds_two_nodes_disagreeing_about_the_multi_homing_mode():
    state = fabric(es={**_es("leaf1"), **_es("leaf2", mh_mode="single-active")})
    findings = run("es_df", state)
    assert {f["Node"] for f in findings} == {"leaf1", "leaf2"}
    assert all("multi-homing mode" in f["Detail"] for f in findings)


def test_es_df_does_not_compare_two_different_segments():
    state = fabric(
        es={
            **_es("leaf1"),
            **_es("leaf2", esi="01:00:00:00:00:02:00:00:00:02", mh_mode="single-active"),
        }
    )
    assert run("es_df", state) == []


# --------------------------------------------------------------------------- #
# running them together
# --------------------------------------------------------------------------- #


def test_a_healthy_fabric_produces_nothing():
    state = fabric(
        bgp_peers=_peers(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 9000)},
        ifstats=_ifstats(),
        lldp=_link_pair(),
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
        es={**_es("leaf1"), **_es("leaf2")},
    )
    assert run_checks(state) == []


def test_findings_come_back_worst_first():
    state = fabric(
        bgp_peers=_peers(state="idle"),
        ifstats=_ifstats(in_discards=3),
    )
    severities = [f.severity for f in run_checks(state)]
    assert severities == [ERROR, WARNING]


def test_only_runs_the_checks_asked_for():
    state = fabric(bgp_peers=_peers(state="idle"), ifstats=_ifstats(in_errors=1))
    findings = run_checks(state, only=["bgp_down"])
    assert {f.check for f in findings} == {"bgp_down"}


def test_a_check_whose_reports_were_not_collected_is_skipped():
    """Not asking a question is not the same as getting a clean answer."""
    findings = run_checks(fabric(bgp_peers=_peers(state="idle")))
    assert {f.check for f in findings} == {"bgp_down"}


def test_a_node_that_could_not_be_read_is_reported_rather_than_passed_over():
    state = fabric(bgp_peers=_peers())
    state.errors[("bgp_peers", "leaf9")] = "not connected"
    findings = run_checks(state)
    assert len(findings) == 1
    assert findings[0].check == "collection"
    assert findings[0].node == "leaf9"
    assert findings[0].severity == WARNING
    assert "not connected" in findings[0].detail


def test_a_check_that_raises_is_a_finding_rather_than_a_crash(monkeypatch):
    """One bad check must not take the whole report down with it."""

    def explode(_state):
        raise RuntimeError("boom")

    broken = Check(
        name="broken", title="Broken", requires=("bgp_peers",), run=explode
    )
    monkeypatch.setattr(checks_module, "CHECKS", (broken,))
    findings = run_checks(fabric(bgp_peers=_peers()))
    assert len(findings) == 1
    assert findings[0].check == "broken"
    assert findings[0].severity == ERROR
    assert "boom" in findings[0].detail


def test_checks_tolerate_a_report_that_came_back_empty():
    state = fabric(bgp_peers={"leaf1": []}, subif={"leaf1": []}, ifstats={"leaf1": []})
    assert run_checks(state) == []


def test_a_containerlab_node_s_dropped_packets_are_not_a_finding():
    """A veth discards IPv6 multicast and the like that a real port forwards."""
    stats = InterfaceStats("ethernet-1/1", in_discards=3, in_errors=1)
    state = fabric(ifstats={"leaf1": [stats], "leaf2": [stats]})
    state.containerlab = {"leaf1"}
    assert [row["Node"] for row in run("itf_errors", state)] == ["leaf2", "leaf2"]


def test_nodes_that_elect_different_designated_forwarders_are_an_error():
    """Two leaves each electing themselves both forward on a single-active segment."""
    def segment(df):
        candidates = tuple(Candidate(a, designated=a == df) for a in ("192.0.2.15", "192.0.2.16"))
        return EthernetSegment("vES", "00:01:ff", "virtual", "single-active", "up", associations=(Association("ipvrf-1", candidates),))

    state = fabric(es={"leaf5": [segment("192.0.2.15")], "leaf6": [segment("192.0.2.16")]})
    rows = run("es_df", state)
    assert [(r["Node"], r["Subject"]) for r in rows] == [("leaf5", "vES/ipvrf-1"), ("leaf6", "vES/ipvrf-1")]
    assert rows[0]["Detail"] == (
        "nodes disagree on the designated forwarder: "
        "192.0.2.15 according to leaf5; 192.0.2.16 according to leaf6"
    )

    agree = fabric(es={"leaf5": [segment("192.0.2.15")], "leaf6": [segment("192.0.2.15")]})
    assert run("es_df", agree) == []


# --------------------------------------------------------------------------- #
# configuration mismatches between the two ends of a session or a link
# --------------------------------------------------------------------------- #

from nornir_srl.records import IsisInterface, OspfInterface  # noqa: E402


def _addressed(**nodes: str) -> Dict[str, Any]:
    """``_addressed(leaf1="10.0.0.0/31")``: one routed subinterface each."""
    return {
        node: [Interface("ethernet-1/1", (SubinterfaceState("ethernet-1/1.0", ipv4=(prefix,)),))]
        for node, prefix in nodes.items()
    }


def _session(peer: str, local_as: int, peer_as: int, families=("ipv4-unicast",), state="established", **kw: Any) -> List[BgpPeers]:
    return [
        BgpPeers(
            ni="default",
            neighbors=(
                Neighbor(
                    peer=peer,
                    state=state,
                    local_as=local_as,
                    peer_as=peer_as,
                    families=tuple(Family(name) for name in families),
                    **kw,
                ),
            ),
        )
    ]


def _bgp_fabric(leaf: List[BgpPeers], spine: List[BgpPeers]) -> FabricState:
    return fabric(
        subif=_addressed(leaf1="10.0.0.1/31", spine1="10.0.0.0/31"),
        ni={},
        bgp_peers={"leaf1": leaf, "spine1": spine},
    )


def test_bgp_peer_mismatch_is_silent_on_two_ends_that_match():
    state = _bgp_fabric(_session("10.0.0.0", 65001, 65100), _session("10.0.0.1", 65100, 65001))
    assert run("bgp_peer_mismatch", state) == []


def test_bgp_peer_mismatch_says_which_as_one_end_expects_and_the_other_runs():
    state = _bgp_fabric(
        _session("10.0.0.0", 65001, 65101, state="active"), _session("10.0.0.1", 65100, 65001, state="active")
    )
    [finding] = run("bgp_peer_mismatch", state)
    assert finding["Node"] == "leaf1" and finding["Severity"] == ERROR
    assert finding["Subject"] == "default/10.0.0.0"
    assert finding["Detail"] == "expects AS 65101 from spine1, which runs AS 65100"


def test_bgp_peer_mismatch_finds_a_session_the_far_end_never_configured():
    state = _bgp_fabric(_session("10.0.0.0", 65001, 65100, state="active"), [BgpPeers(ni="default", neighbors=())])
    [finding] = run("bgp_peer_mismatch", state)
    assert finding["Detail"] == "spine1 owns 10.0.0.0 but has no session back to leaf1 in default"


def test_bgp_peer_mismatch_trusts_a_far_end_that_takes_dynamic_neighbours():
    dynamic = [BgpPeers(ni="default", neighbors=(Neighbor(peer="10.9.9.9", state="established", dynamic=True),))]
    state = _bgp_fabric(_session("10.0.0.0", 65001, 65100, state="active"), dynamic)
    assert run("bgp_peer_mismatch", state) == []


def test_bgp_peer_mismatch_finds_a_family_or_bfd_on_one_end_only_once():
    state = _bgp_fabric(
        _session("10.0.0.0", 65001, 65100, families=("ipv4-unicast", "evpn"), bfd=True),
        _session("10.0.0.1", 65100, 65001),
    )
    details = [f["Detail"] for f in run("bgp_peer_mismatch", state)]
    assert details == [
        "evpn is enabled on leaf1 only: spine1 does not exchange it on this session",
        "BFD protects it on leaf1 only, so a failure is detected fast on one end",
    ]


def test_bgp_peer_mismatch_reads_a_link_local_peer_by_its_address():
    state = fabric(
        subif={
            "leaf1": [Interface("ethernet-1/1", (SubinterfaceState("ethernet-1/1.0", ipv6=("fe80::1/64",)),))],
            "spine1": [Interface("ethernet-1/1", (SubinterfaceState("ethernet-1/1.0", ipv6=("fe80::2/64",)),))],
        },
        ni={},
        bgp_peers={
            "leaf1": _session("fe80::2%ethernet-1/1.0", 65001, 65100),
            "spine1": _session("fe80::1%ethernet-1/1.0", 65100, 65002),
        },
    )
    [finding] = run("bgp_peer_mismatch", state)
    assert finding["Node"] == "spine1" and "expects AS 65002 from leaf1" in finding["Detail"]


def _igp_fabric(**reports: Any) -> FabricState:
    return fabric(lldp=_lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")], spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")]), **reports)


def test_igp_peer_mismatch_is_silent_on_two_ends_that_match():
    state = _igp_fabric(
        ospf={
            "leaf1": [OspfInterface("default", "main", "0.0.0.0", "ethernet-1/1.0", interface_type="point-to-point")],
            "spine1": [OspfInterface("default", "main", "0.0.0.0", "ethernet-1/1.0", interface_type="point-to-point")],
        },
        isis={},
    )
    assert run("igp_peer_mismatch", state) == []


def test_igp_peer_mismatch_finds_an_ospf_area_and_network_type_that_differ():
    state = _igp_fabric(
        ospf={
            "leaf1": [OspfInterface("default", "main", "0.0.0.0", "ethernet-1/1.0", interface_type="point-to-point")],
            "spine1": [OspfInterface("default", "main", "0.0.0.1", "ethernet-1/1.0", interface_type="broadcast")],
        },
        isis={},
    )
    assert [(f["Severity"], f["Detail"]) for f in run("igp_peer_mismatch", state)] == [
        (ERROR, "OSPF area 0.0.0.0 here, 0.0.0.1 on spine1 ethernet-1/1.0: no adjacency forms"),
        (WARNING, "OSPF network type point-to-point here, broadcast on spine1 ethernet-1/1.0"),
    ]


def test_igp_peer_mismatch_finds_is_is_passive_or_typed_differently():
    state = _igp_fabric(
        isis={
            "leaf1": [IsisInterface("default", "i1", "ethernet-1/1.0", passive=True, circuit_type="point-to-point")],
            "spine1": [IsisInterface("default", "i1", "ethernet-1/1.0", circuit_type="broadcast")],
        },
        ospf={},
    )
    assert [(f["Severity"], f["Subject"], f["Detail"]) for f in run("igp_peer_mismatch", state)] == [
        (ERROR, "ethernet-1/1.0", "IS-IS network type point-to-point here, broadcast on spine1 ethernet-1/1.0"),
        (WARNING, "ethernet-1/1.0", "IS-IS is passive on leaf1 only: no adjacency forms over this link"),
    ]


def test_igp_peer_mismatch_finds_the_igp_on_one_end_of_a_link_only():
    state = _igp_fabric(
        isis={
            "leaf1": [IsisInterface("default", "i1", "ethernet-1/1.0")],
            "spine1": [IsisInterface("default", "i1", "system0.0")],
        },
        ospf={},
    )
    [finding] = run("igp_peer_mismatch", state)
    assert (finding["Node"], finding["Detail"]) == ("leaf1", "IS-IS runs on this end only: spine1 ethernet-1/1 does not run it")
    # A far end that runs no IS-IS at all is not one configured wrong on this link.
    state.reports["isis"] = {"leaf1": state.reports["isis"]["leaf1"]}
    assert run("igp_peer_mismatch", state) == []


def test_bgp_peer_mismatch_says_nothing_about_a_session_still_opening():
    """Families and BFD are negotiated: a session that has not come up - a
    dynamic one refused and retrying - has none yet on either end."""
    state = _bgp_fabric(
        _session("10.0.0.0", 65001, 65100, families=("ipv4-unicast", "evpn"), bfd=True, state="opensent"),
        _session("10.0.0.1", 65100, 65001, families=(), state="active"),
    )
    assert run("bgp_peer_mismatch", state) == []


def test_bgp_peer_mismatch_compares_no_as_a_dynamic_neighbour_only_learned():
    """A dynamic neighbour's peer AS is what the far end announced."""
    state = _bgp_fabric(
        _session("10.0.0.0", 65001, 65999, state="active", dynamic=True),
        _session("10.0.0.1", 65100, 65001, state="active"),
    )
    assert run("bgp_peer_mismatch", state) == []


def test_check_results_list_every_check_passing_failing_or_skipped():
    from nornir_srl.checks import CHECKS, Finding, check_results
    from nornir_srl.fabric import FabricState

    state = FabricState()
    first, second = CHECKS[0], CHECKS[1]
    for report in first.requires:
        state.reports[report] = {"leaf1": [], "leaf2": []}
    for report in second.requires:
        state.reports[report] = {"leaf1": [], "leaf2": []}
    state.errors[("lldp", "spine1")] = "unreachable"
    findings = [
        Finding(first.name, "error", "leaf1", "peer 10.0.0.1", "down"),
        Finding(first.name, "warning", "leaf2", "peer 10.0.0.2", "flapping"),
        Finding("collection", "warning", "spine1", "lldp", "not checked: unreachable"),
    ]
    results = {r["name"]: r for r in check_results(state, findings)}
    assert set(results) == {c.name for c in CHECKS} | {"collection"}
    one = results[first.name]
    assert (one["status"], one["errors"], one["warnings"]) == ("error", 1, 1)
    assert one["nodes"] == {"leaf1": "error", "leaf2": "warning"}
    assert results[second.name]["status"] == "pass"
    assert results[second.name]["nodes"] == {"leaf1": "pass", "leaf2": "pass"}
    unread = [c for c in CHECKS if c.requires and not set(c.requires) & set(state.reports)]
    assert unread and all(results[c.name]["status"] == "skipped" for c in unread)
    assert results["collection"]["status"] == "warning"
    ordered = check_results(state, findings)
    assert ordered[0]["status"] == "error", "worst first"
