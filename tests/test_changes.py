"""What changed between two readings of a fabric, and what a timeline makes of it."""

from typing import Any, Dict

import pytest

from nornir_srl.changes import (
    ABSENT,
    ERROR,
    INFO,
    OK,
    WARNING,
    Change,
    diff_fabric,
    diff_findings,
    flaps,
    node_change,
    parse_since,
    settled_findings,
)
from nornir_srl.checks import CHECKS_BY_NAME, Finding
from nornir_srl.fabric import FabricState
from nornir_srl.records import (
    BgpPeers,
    BridgeTable,
    EthernetSegment,
    Association,
    Candidate,
    Family,
    Interface,
    LldpInterface,
    LldpNeighbor,
    MacEntry,
    Neighbor,
    SubinterfaceState,
)


def fabric(**reports: Dict[str, Any]) -> FabricState:
    return FabricState(reports=dict(reports))


def peers(*neighbors: Neighbor) -> list:
    return [BgpPeers("default", tuple(neighbors))]


def session(peer: str, state: str = "established", evpn: int = 100) -> Neighbor:
    return Neighbor(peer, state, families=(Family("evpn", oper="up", received=evpn),))


def summary(changes):
    return [(c.node, c.kind, c.subject, c.before, c.after, c.severity) for c in changes]


def test_a_session_going_down_is_an_error_and_coming_back_is_ok():
    up = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"))})
    down = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", "active"))})
    assert summary(diff_fabric(up, down, at=1)) == [
        ("leaf1", "bgp", "default/10.0.0.1", "established", "active", ERROR)
    ]
    assert summary(diff_fabric(down, up, at=2)) == [
        ("leaf1", "bgp", "default/10.0.0.1", "active", "established", OK)
    ]


def test_a_session_that_disappears_is_a_failure_one_that_appears_is_news():
    one = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"))})
    two = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"), session("10.0.0.2"))})
    appeared = diff_fabric(one, two, at=1)
    assert ("leaf1", "bgp", "default/10.0.0.2", ABSENT, "established", OK) in summary(appeared)
    gone = diff_fabric(two, one, at=1)
    assert ("leaf1", "bgp", "default/10.0.0.2", "established", ABSENT, ERROR) in summary(gone)


def test_a_node_that_did_not_answer_is_not_a_node_whose_sessions_all_went():
    before = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1")), "leaf2": peers(session("10.0.0.9"))})
    after = fabric(bgp_peers={"leaf2": peers(session("10.0.0.9"))})
    assert diff_fabric(before, after) == []


def test_a_route_count_is_news_only_when_it_halves_or_goes_to_or_from_nothing():
    base = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", evpn=100))})

    def to(count):
        return diff_fabric(base, fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", evpn=count))}), at=1)

    assert to(92) == []  # the fabric re-converging somewhere else
    assert to(70) == []  # a real move, but the echo of a change reported elsewhere
    (halved,) = to(40)
    assert (halved.kind, halved.severity, halved.detail) == ("bgp-routes", WARNING, "received routes fell from 100 to 40")
    (emptied,) = to(0)
    assert emptied.severity == WARNING


def test_a_port_going_down_and_an_lldp_neighbour_lost():
    def reading(oper: str, neighbors: tuple) -> FabricState:
        return fabric(
            subif={"leaf1": [Interface("ethernet-1/1", (SubinterfaceState("ethernet-1/1.0", oper=oper),))]},
            lldp={"leaf1": [LldpInterface("ethernet-1/1", neighbors)]},
        )

    before = reading("up", (LldpNeighbor("spine1", "ethernet-1/1"),))
    after = reading("down", ())
    assert summary(diff_fabric(before, after, at=1)) == [
        ("leaf1", "interface", "ethernet-1/1.0", "up", "down", ERROR),
        ("leaf1", "lldp", "ethernet-1/1", "spine1 ethernet-1/1", ABSENT, WARNING),
    ]


def test_a_mac_that_moves_is_news_one_that_ages_out_is_not():
    def table(*entries):
        return fabric(mac={"leaf1": [BridgeTable("macvrf-1", tuple(entries))]})

    here = MacEntry.read("00:00:00:00:01:11", "ethernet-1/3.100", "learnt")
    there = MacEntry.read("00:00:00:00:01:11", "vxlan-interface:vxlan0.100 vtep:192.0.2.13 vni:100", "evpn")
    (moved,) = diff_fabric(table(here), table(there), at=1)
    assert (moved.kind, moved.subject, moved.before, moved.after, moved.severity) == (
        "mac", "macvrf-1 00:00:00:00:01:11", "ethernet-1/3.100", "vtep 192.0.2.13", INFO
    )
    assert diff_fabric(table(here), table(), at=1) == []


def test_a_designated_forwarder_that_moves_is_a_warning():
    def segment(df: str) -> FabricState:
        candidates = tuple(Candidate(a, designated=a == df) for a in ("192.0.2.11", "192.0.2.12"))
        es = EthernetSegment("ES-01", "00:01", "local", "all-active", "up", associations=(Association("macvrf-1", candidates),))
        return fabric(es={"leaf1": [es]})

    (change,) = diff_fabric(segment("192.0.2.11"), segment("192.0.2.12"), at=1)
    assert (change.kind, change.severity) == ("es-df", WARNING)


def test_findings_raised_and_cleared():
    bgp = Finding("bgp_down", "error", "leaf1", "default/10.0.0.1", "session is active")
    mtu = Finding("mtu_mismatch", "error", "leaf1", "ethernet-1/1.0", "9000 vs 1500")
    changes = diff_findings([bgp], [mtu], at=1)
    assert summary(changes) == [
        ("leaf1", "finding", "mtu_mismatch ethernet-1/1.0", ABSENT, "error", "error"),
        ("leaf1", "finding", "bgp_down default/10.0.0.1", "error", ABSENT, OK),
    ]


def test_a_finding_that_blinks_for_one_reading_never_reaches_the_timeline():
    blink = Finding("itf_errors", "warning", "leaf1", "ethernet-1/3", "1 discarded packet")
    changes, raised = settled_findings({}, [], [blink], at=1)
    assert changes == [] and raised == {}
    changes, raised = settled_findings(raised, [blink], [], at=2)
    assert changes == [] and raised == {}


def test_a_finding_that_lasts_two_readings_is_raised_once_and_cleared_once():
    down = Finding("bgp_down", "error", "leaf1", "default/10.0.0.1", "session is active")
    _, raised = settled_findings({}, [], [down], at=1)
    changes, raised = settled_findings(raised, [down], [down], at=2)
    assert summary(changes) == [("leaf1", "finding", "bgp_down default/10.0.0.1", ABSENT, "error", "error")]
    changes, raised = settled_findings(raised, [down], [down], at=3)
    assert changes == []
    changes, raised = settled_findings(raised, [down], [], at=4)
    assert [c.severity for c in changes] == [OK] and raised == {}


def test_a_node_that_stops_answering():
    assert node_change("leaf1", False, at=1).severity == ERROR
    assert node_change("leaf1", True, at=2).severity == OK


# --------------------------------------------------------------------------- #
# flaps
# --------------------------------------------------------------------------- #


def _flip(at: float, before: str, after: str, kind: str = "bgp", subject: str = "default/10.0.0.1") -> Change:
    return Change(at, "leaf1", kind, subject, before, after, ERROR)


def test_three_transitions_in_the_window_are_a_flap():
    changes = [
        _flip(100, "established", "active"),
        _flip(200, "active", "established"),
        _flip(300, "established", "active"),
    ]
    (flap,) = flaps(changes, now=400)
    assert (flap.kind, flap.count, flap.values) == ("bgp", 3, ("established", "active"))
    assert flaps(changes, now=400, threshold=4) == []
    assert flaps(changes, now=100 + 601 + 200) == []  # the first two have left the window


def test_route_counts_and_findings_do_not_flap():
    changes = [_flip(t, "1", "2", kind="bgp-routes") for t in (1, 2, 3)]
    changes += [_flip(t, ABSENT, "error", kind="finding") for t in (1, 2, 3)]
    assert flaps(changes, now=10) == []


def test_the_flapping_check_reads_the_timeline():
    moves = [
        Change(t, "leaf1", "mac", "macvrf-1 00:00:00:00:01:11", a, b, INFO)
        for t, a, b in ((1, "ethernet-1/3.100", "ethernet-1/4.100"), (2, "ethernet-1/4.100", "ethernet-1/3.100"), (3, "ethernet-1/3.100", "ethernet-1/4.100"))
    ]
    import time

    now = time.time()
    state = FabricState(changes=[Change(now - 10 + c.at, c.node, c.kind, c.subject, c.before, c.after, c.severity) for c in moves])
    (finding,) = CHECKS_BY_NAME["flapping"].run(state)
    assert (finding.node, finding.subject) == ("leaf1", "macvrf-1/00:00:00:00:01:11")
    assert "moved 3 times between ethernet-1/3.100, ethernet-1/4.100" in finding.detail
    assert CHECKS_BY_NAME["flapping"].run(FabricState()) == []


# --------------------------------------------------------------------------- #
# since
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,seconds",
    [("15m", 900), ("2h", 7200), ("90s", 90), ("1d", 86400), ("10", 600), (" 5M ", 300)],
)
def test_since_takes_a_time_span(text, seconds):
    assert parse_since(text, now=10_000) == 10_000 - seconds


def test_since_empty_means_everything_and_nonsense_is_an_error():
    assert parse_since("") is None
    assert parse_since(None) is None
    with pytest.raises(ValueError):
        parse_since("yesterday")


# --------------------------------------------------------------------------- #
# ARP / ND
# --------------------------------------------------------------------------- #


def _cache(*entries, interface="irb0.104", nis=("macvrf-104", "ipvrf-1")):
    from nornir_srl.records import NeighborCache, NeighborEntry

    return [NeighborCache(interface, nis, tuple(NeighborEntry(ip, mac, "dynamic") for ip, mac in entries))]


def test_an_address_answering_from_another_mac_is_a_warning():
    before = fabric(arp={"leaf5": _cache(("10.1.4.16", "1A:00:00:00:00:01"))})
    after = fabric(arp={"leaf5": _cache(("10.1.4.16", "1A:00:00:00:00:02"))})
    (change,) = diff_fabric(before, after, at=1)
    assert (change.kind, change.subject, change.severity) == ("arp", "macvrf-104/ipvrf-1 10.1.4.16", WARNING)
    assert change.before == "1a:00:00:00:00:01 on irb0.104"


def test_neighbours_learned_and_aged_out_are_summarized_per_interface():
    empty = fabric(nd={"leaf5": _cache()})
    learned = fabric(nd={"leaf5": _cache(("2001:db8::16", "1A:00:00:00:00:01"), ("2001:db8::17", "1A:00:00:00:00:02"))})
    (change,) = diff_fabric(empty, learned, at=1)
    assert (change.kind, change.subject, change.severity) == ("nd", "irb0.104", INFO)
    assert change.detail == "2 learned (2001:db8::16, 2001:db8::17)"
    (gone,) = diff_fabric(learned, empty, at=1)
    assert gone.detail == "2 aged out (2001:db8::16, 2001:db8::17)"
    assert diff_fabric(learned, learned, at=1) == []


# --------------------------------------------------------------------------- #
# route tables
# --------------------------------------------------------------------------- #


def _rib(node_routes, ni="default"):
    from nornir_srl.records import Route, RouteNextHop, RouteTable

    return {
        node: [RouteTable(ni, tuple(Route(prefix, "bgp", next_hops=tuple(RouteNextHop(address=h) for h in hops)) for prefix, hops in routes))]
        for node, routes in node_routes.items()
    }


def _loopbacks(*pairs):
    from nornir_srl.records import NetworkInstance, Subinterface

    return {node: [NetworkInstance("default", "default", "up", interfaces=(Subinterface("system0.0", "up", (f"{ip}/32",)),))] for node, ip in pairs}


def test_a_route_table_is_summarized_in_one_change():
    ordinary = [(f"10.9.{i}.0/24", ["192.0.2.1", "192.0.2.2"]) for i in range(10)]
    moved = [(p, ["192.0.2.1"]) for p, _ in ordinary[:6]] + ordinary[6:9]
    before = fabric(ipv4_rib=_rib({"leaf1": ordinary}))
    after = fabric(ipv4_rib=_rib({"leaf1": moved + [("10.8.0.0/24", ["192.0.2.1"])]}))
    (change,) = diff_fabric(before, after, at=1)
    assert (change.kind, change.subject, change.before, change.after) == ("routes", "default ipv4", "10 routes", "10 routes")
    assert change.detail == (
        "6 changed next-hops (10.9.0.0/24, 10.9.1.0/24, 10.9.2.0/24, 10.9.3.0/24 and 2 more); "
        "1 withdrawn (10.9.9.0/24); 1 new (10.8.0.0/24)"
    )
    assert change.severity == INFO


def test_losing_most_of_a_table_or_its_default_is_a_warning():
    table = [("0.0.0.0/0", ["192.0.2.1"]), ("10.9.0.0/24", ["192.0.2.1"])]
    before = fabric(ipv4_rib=_rib({"leaf1": table}))
    after = fabric(ipv4_rib=_rib({"leaf1": table[1:]}))
    changes = {c.kind: c for c in diff_fabric(before, after, at=1)}
    assert changes["routes"].severity == WARNING
    # the default route is reported on its own too
    assert changes["route"].subject == "default 0.0.0.0/0" and changes["route"].severity == ERROR


def test_a_system_address_losing_an_ecmp_member_is_reported_on_its_own():
    rib = lambda hops: _rib({"leaf1": [("192.0.2.15/32", hops), ("10.9.0.0/24", hops)]})  # noqa: E731
    loop = _loopbacks(("leaf5", "192.0.2.15"))
    before = fabric(ipv4_rib=rib(["fe80::1", "fe80::2"]), ni=loop)
    after = fabric(ipv4_rib=rib(["fe80::1"]), ni=loop)
    route = next(c for c in diff_fabric(before, after, at=1) if c.kind == "route")
    assert route.subject == "default 192.0.2.15/32"
    assert route.severity == WARNING
    assert route.detail == "system address: ECMP narrowed from 2 to 1 next-hops (fe80::1)"


def test_a_watched_prefix_is_reported_on_its_own_in_any_instance():
    rib = lambda hops: _rib({"leaf1": [("6.6.6.1/32", hops)]}, ni="ipvrf-1")  # noqa: E731
    before, after = fabric(ipv4_rib=rib(["10.1.4.16"])), fabric(ipv4_rib=rib(["10.1.4.17"]))
    assert [c.kind for c in diff_fabric(before, after, at=1)] == ["routes"]
    watched = [c for c in diff_fabric(before, after, at=1, watched=["6.6.6.1"]) if c.kind == "route"]
    assert [(c.subject, c.detail) for c in watched] == [
        ("ipvrf-1 6.6.6.1/32", "watched: next-hops moved from 10.1.4.16 to 10.1.4.17")
    ]


def _sizes(node_tables):
    from nornir_srl.records import RouteTableSummary

    return {
        node: [RouteTableSummary(ni, family, active=active) for ni, family, active in tables]
        for node, tables in node_tables.items()
    }


def test_a_table_known_only_by_its_size_is_reported_when_it_halves():
    """What a server reading keeps of a VRF: the count, compared as a count."""
    before = fabric(rib_summary=_sizes({"leaf1": [("ipvrf-1", "ipv4", 400)]}))
    halved = fabric(rib_summary=_sizes({"leaf1": [("ipvrf-1", "ipv4", 180)]}))
    (change,) = diff_fabric(before, halved, at=1)
    assert (change.kind, change.subject, change.before, change.after, change.severity) == (
        "routes", "ipvrf-1 ipv4", "400", "180", WARNING
    )
    assert change.detail == "routes fell from 400 to 180"
    # the churn of a living table is not news
    churned = fabric(rib_summary=_sizes({"leaf1": [("ipvrf-1", "ipv4", 370)]}))
    assert diff_fabric(before, churned, at=1) == []


def test_a_table_held_prefix_by_prefix_is_not_counted_as_well():
    before = fabric(
        ipv4_rib=_rib({"leaf1": [(f"10.9.{i}.0/24", ["192.0.2.1"]) for i in range(10)]}),
        rib_summary=_sizes({"leaf1": [("default", "ipv4", 10)]}),
    )
    after = fabric(
        ipv4_rib=_rib({"leaf1": [(f"10.9.{i}.0/24", ["192.0.2.1"]) for i in range(4)]}),
        rib_summary=_sizes({"leaf1": [("default", "ipv4", 4)]}),
    )
    assert [(c.kind, c.before, c.after) for c in diff_fabric(before, after, at=1)] == [
        ("routes", "10 routes", "4 routes")
    ]


def test_a_watched_prefix_looked_up_on_its_own_is_reported_on_its_own():
    """A reading holds only the underlay; a watched prefix in a VRF is looked up."""
    watched = lambda hops: _rib({"leaf1": [("6.6.6.1/32", hops)]}, ni="ipvrf-1")  # noqa: E731
    underlay = _rib({"leaf1": [("192.0.2.1/32", ["fe80::1"])]})
    before = fabric(ipv4_rib=underlay, ipv4_watched=watched(["10.1.4.16", "10.1.4.17"]))
    after = fabric(ipv4_rib=underlay, ipv4_watched=watched(["10.1.4.16"]))
    assert [(c.kind, c.subject, c.severity, c.detail) for c in diff_fabric(before, after, at=1)] == [
        ("route", "ipvrf-1 6.6.6.1/32", WARNING, "watched: ECMP narrowed from 2 to 1 next-hops (10.1.4.16)")
    ]
    gone = fabric(ipv4_rib=underlay, ipv4_watched={"leaf1": []})
    (change,) = diff_fabric(before, gone, at=1)
    assert (change.kind, change.severity) == ("route", ERROR)


def test_a_watched_prefix_in_a_table_held_in_full_is_reported_once():
    table = lambda hops: _rib({"leaf1": [("192.0.2.9/32", hops)]})  # noqa: E731
    before = fabric(ipv4_rib=table(["fe80::1"]), ipv4_watched=table(["fe80::1"]))
    after = fabric(ipv4_rib=table(["fe80::2"]), ipv4_watched=table(["fe80::2"]))
    routes = [c for c in diff_fabric(before, after, at=1, watched=["192.0.2.9"]) if c.kind == "route"]
    assert len(routes) == 1


def test_a_prefix_is_normalized_the_way_route_tables_spell_it():
    from nornir_srl.changes import normalize_prefix

    assert normalize_prefix("10.1.4.16") == "10.1.4.16/32"
    assert normalize_prefix("2001:db8::1") == "2001:db8::1/128"
    assert normalize_prefix("10.1.4.1/24") == "10.1.4.0/24"
    with pytest.raises(ValueError):
        normalize_prefix("leaf1")
