"""Findings grouped into incidents: one root cause and what follows from it."""

from typing import Any, Dict, List

from nornir_srl.changes import ABSENT, WARNING, Change
from nornir_srl.checks import Finding
from nornir_srl.fabric import FabricState
from nornir_srl.incidents import correlate, incident_findings, locate
from nornir_srl.records import (
    BfdInstance,
    BfdSession,
    Interface,
    LldpInterface,
    LldpNeighbor,
    NetworkInstance,
    Route,
    RouteTable,
    Subinterface,
    SubinterfaceState,
)


def fabric(changes=(), errors=None, **reports: Dict[str, Any]) -> FabricState:
    state = FabricState(reports=dict(reports), changes=list(changes), errors=dict(errors or {}))
    # Every node of the inventory, as a real reading has it - including one
    # that answered nothing, which is only in the errors.
    nodes = {node for report in reports.values() for node in report}
    nodes |= {node for _report, node in state.errors}
    state.hostnames = {node: node for node in nodes}
    return state


def cabled(a: str, a_port: str, b: str, b_port: str) -> Dict[str, List[LldpInterface]]:
    return {
        a: [LldpInterface(a_port, (LldpNeighbor(b, b_port),))],
        b: [LldpInterface(b_port, (LldpNeighbor(a, a_port),))],
    }


def itf_down(node: str, subif: str) -> Finding:
    return Finding("itf_down", "error", node, subif, "admin enabled but oper down: port-down")


def test_a_link_down_takes_the_sessions_over_it_into_one_incident():
    findings = [
        itf_down("leaf1", "ethernet-1/1.0"),
        itf_down("spine1", "ethernet-1/1.0"),
        # a link-local session names the port it runs on
        Finding("bgp_down", "error", "spine1", "default/fe80::1%ethernet-1/1.0", "session is active"),
        # a BFD session carries it as a field
        Finding("bfd_down", "error", "leaf1", "default/fe80::2", "session is down"),
    ]
    state = fabric(
        lldp=cabled("leaf1", "ethernet-1/1", "spine1", "ethernet-1/1"),
        bfd={"leaf1": [BfdInstance("default", (BfdSession("fe80::1", "fe80::2", "down", interface="ethernet-1/1.0"),))]},
    )
    (incident,) = correlate(findings, state)
    assert incident.kind == "link"
    assert incident.root.check == "itf_down"
    assert incident.title == "leaf1 e1/1 <-> spine1 e1/1: interface down"
    assert len(incident.findings) == 4
    assert "1 BFD session down, 1 BGP session down" in incident.explanation


def test_a_link_lldp_has_lost_is_still_known_from_the_timeline():
    """A cable that goes down takes its LLDP adjacency with it."""
    lost = Change(1.0, "leaf1", "lldp", "ethernet-1/1", "spine1 ethernet-1/1", ABSENT, WARNING)
    state = fabric(changes=[lost], lldp={"leaf1": [], "spine1": []})
    (incident,) = correlate([itf_down("leaf1", "ethernet-1/1.0"), itf_down("spine1", "ethernet-1/1.0")], state)
    assert incident.kind == "link" and len(incident.findings) == 2


def test_two_ends_of_one_point_to_point_subnet_are_one_cable():
    subif = lambda name, ip: Interface(name.split(".")[0], (SubinterfaceState(name, oper="down", ipv4=(ip,)),))  # noqa: E731
    state = fabric(
        subif={"leaf1": [subif("ethernet-1/49.0", "10.0.0.0/31")], "spine1": [subif("ethernet-1/1.0", "10.0.0.1/31")]},
    )
    findings = [
        itf_down("leaf1", "ethernet-1/49.0"),
        itf_down("spine1", "ethernet-1/1.0"),
        # a numbered session is over the port whose subnet its peer is in
        Finding("bgp_down", "error", "leaf1", "default/10.0.0.1", "session is active"),
    ]
    (incident,) = correlate(findings, state)
    assert incident.kind == "link" and len(incident.findings) == 3


def test_a_node_that_answered_nothing_is_the_root_of_what_points_at_it():
    reports = ("bgp_peers", "subif", "lldp")
    state = fabric(
        errors={(report, "leaf3"): "failed to connect" for report in reports},
        bgp_peers={"leaf1": []},
        subif={"leaf1": []},
        # leaf3 cannot say what it is cabled to; leaf1 can.
        lldp={"leaf1": [LldpInterface("ethernet-1/3", (LldpNeighbor("leaf3", "ethernet-1/3"),))]},
    )
    findings = [
        Finding("collection", "warning", "leaf3", "bgp_peers", "not checked: failed to connect"),
        Finding("collection", "warning", "leaf3", "subif", "not checked: failed to connect"),
        Finding("bgp_down", "error", "leaf1", "default/fe80::3%ethernet-1/3.0", "session is active"),
    ]
    (incident,) = correlate(findings, state)
    assert incident.kind == "node" and incident.title == "leaf3 is unreachable"
    assert incident.root.check == "node_unreachable"
    assert len(incident.related) == 3


def _loopback(node: str, address: str) -> NetworkInstance:
    return NetworkInstance("default", "default", "up", interfaces=(Subinterface("system0.0", "up", (f"{address}/32",)),))


def test_an_overlay_session_to_a_loopback_with_no_route_is_an_underlay_problem():
    state = fabric(
        ni={"leaf1": [_loopback("leaf1", "192.0.2.11")], "leaf2": [_loopback("leaf2", "192.0.2.12")]},
        ipv4_rib={"leaf1": [RouteTable("default", (Route("192.0.2.11/32", "host"),))], "leaf2": [RouteTable("default", ())]},
    )
    finding = Finding("bgp_down", "error", "leaf1", "default/192.0.2.12", "session is active")
    (incident,) = correlate([finding], state)
    assert incident.kind == "underlay"
    assert incident.root.check == "underlay_unreachable"
    assert "192.0.2.12" in incident.root.detail


def test_both_ends_of_a_session_with_a_route_between_them_are_one_incident():
    routes = RouteTable("default", (Route("192.0.2.0/24", "bgp"),))
    state = fabric(
        ni={"leaf1": [_loopback("leaf1", "192.0.2.11")], "leaf2": [_loopback("leaf2", "192.0.2.12")]},
        ipv4_rib={"leaf1": [routes], "leaf2": [routes]},
    )
    findings = [
        Finding("bgp_down", "error", "leaf1", "default/192.0.2.12", "session is active"),
        Finding("bgp_down", "error", "leaf2", "default/192.0.2.11", "session is active"),
    ]
    (incident,) = correlate(findings, state)
    assert incident.kind == "session"
    assert incident.title == "leaf1 <-> leaf2 (default): BGP session down"


def test_the_same_cause_in_many_places_folds_into_one_pattern():
    lldp: Dict[str, List[LldpInterface]] = {}
    findings = []
    for n in range(1, 5):
        leaf = f"leaf{n}"
        lldp.update(cabled(leaf, "ethernet-1/1", f"spine{n}", "ethernet-1/1"))
        findings.append(
            Finding("bfd_down", "error", leaf, f"default/fe80::{n}%ethernet-1/1.0", "session is down on ethernet-1/1.0, protecting BGP")
        )
    incidents = correlate(findings, fabric(lldp=lldp))
    (pattern,) = incidents
    assert pattern.kind == "pattern"
    assert pattern.title == "BFD session down on 4 links"
    assert len(pattern.findings) == 4

    # Two are two places, not a pattern.
    assert [i.kind for i in correlate(findings[:2], fabric(lldp=lldp))] == ["link", "link"]


def test_bgp_sessions_down_fold_whatever_state_or_peer_details_a_reading_catches():
    """A dynamic session refused for a wrong AS keeps retrying: one reading
    catches it active with no peer-group or AS learned yet, the next in
    opensent with both. Those are one cause, folded into one card every time."""
    lldp: Dict[str, List[LldpInterface]] = {}
    details = (
        "session is active, peer-group -, AS ?",
        "session is opensent, peer-group fabric, AS 4200000002",
        "session is connect, peer-group fabric, AS ?",
        "session is openconfirm, peer-group fabric, AS 4200000004",
    )
    findings = []
    for n, detail in enumerate(details, start=1):
        leaf = f"leaf{n}"
        lldp.update(cabled("spine1", f"ethernet-1/{n}", leaf, "ethernet-1/49"))
        findings.append(Finding("bgp_down", "error", "spine1", f"default/fe80::{n}%ethernet-1/{n}.0", detail))
    (pattern,) = correlate(findings, fabric(lldp=lldp))
    assert pattern.kind == "pattern"
    assert pattern.title == "BGP session down on 4 links"
    # Each session's own detail is still what its finding says.
    assert {f.detail for f in pattern.findings} == set(details)


def test_every_flap_folds_into_one_incident_whatever_flaps():
    """Sessions, a designated forwarder and a port that keep changing are one
    thing to look at - the fabric is unsettled - however different each says
    it is. A flap that is not the root of its incident stays where it is."""
    lldp: Dict[str, List[LldpInterface]] = {}
    for n in range(1, 4):
        lldp.update(cabled("spine1", f"ethernet-1/{n}", f"leaf{n}", "ethernet-1/49"))
    lldp.update(cabled("spine2", "ethernet-1/9", "leaf9", "ethernet-1/50"))
    findings = [
        Finding("flapping", "warning", "spine1", f"default/fe80::{n}%ethernet-1/{n}.0", f"bgp changed {n + 2} times (established / active) in the last 10 minutes")
        for n in range(1, 3)
    ] + [
        Finding("flapping", "warning", "leaf3", "HOST1/macvrf-101", "es-df changed 3 times (192.0.2.12 / none / 192.0.2.14) in the last 10 minutes"),
        Finding("flapping", "warning", "spine1", "ethernet-1/3", "interface changed 4 times (up / down) in the last 10 minutes"),
        # Riding on a link that is down: that link is the incident.
        itf_down("spine2", "ethernet-1/9.0"),
        Finding("flapping", "warning", "spine2", "ethernet-1/9", "interface changed 5 times (up / down) in the last 10 minutes"),
    ]
    incidents = correlate(findings, fabric(lldp=lldp))
    flapping = [i for i in incidents if i.root.check == "flapping"]
    assert len(flapping) == 1
    (folded,) = flapping
    assert folded.kind == "pattern" and folded.title.startswith("Flapping in ")
    assert "2 bgp" in folded.explanation and "1 es-df" in folded.explanation and "1 interface" in folded.explanation
    assert len([f for f in folded.findings if f.check == "flapping"]) == 4
    down = next(i for i in incidents if i.root.check == "itf_down")
    assert any(f.check == "flapping" for f in down.findings)
    # Nothing is lost by folding.
    assert sorted(incident_findings(incidents), key=repr) == sorted(findings, key=repr)


def test_no_finding_is_lost_by_being_grouped():
    findings = [
        itf_down("leaf1", "ethernet-1/1.0"),
        Finding("mtu_outlier", "warning", "leaf2", "ip-mtu", "1500 where 9000 is the norm"),
        Finding("resource_high", "warning", "leaf2", "control A memory", "85% in use"),
    ]
    grouped = incident_findings(correlate(findings, fabric()))
    assert sorted(grouped, key=repr) == sorted(findings, key=repr)


def test_locate_puts_each_finding_on_its_port():
    findings = [
        itf_down("leaf1", "ethernet-1/1.0"),
        Finding("bgp_down", "error", "leaf1", "default/fe80::2%ethernet-1/2.0", "session is active"),
        Finding("mtu_outlier", "warning", "leaf1", "ip-mtu", "1500"),
    ]
    assert [port for _finding, port in locate(findings, fabric())] == ["ethernet-1/1", "ethernet-1/2", None]
