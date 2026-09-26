"""Unit tests for the gNMI state tree backing the fcli server."""

import copy

import pytest

from nornir_srl.server import tree
from nornir_srl.server.tree import (
    ListNode,
    delete,
    get_node,
    insert,
    join_path,
    key_matches,
    materialize,
    parse_elem,
    parse_path,
    prune,
    select_path,
    split_path,
    strip_module,
)

# --------------------------------------------------------------------------- #
# path parsing
# --------------------------------------------------------------------------- #


def test_split_path_ignores_slashes_inside_keys():
    assert split_path("/interface[name=ethernet-1/1]/statistics") == [
        "interface[name=ethernet-1/1]",
        "statistics",
    ]


def test_split_path_strips_empty_components():
    assert split_path("//system/lldp/") == ["system", "lldp"]


def test_parse_elem_without_keys():
    assert parse_elem("statistics") == ("statistics", {})


def test_parse_elem_with_multiple_keys():
    name, keys = parse_elem("route[prefix=10.0.0.0/24][owner=bgp]")
    assert name == "route"
    assert keys == {"prefix": "10.0.0.0/24", "owner": "bgp"}


def test_parse_elem_strips_module_prefix():
    assert parse_elem("srl_nokia-interfaces:interface[name=mgmt0]") == (
        "interface",
        {"name": "mgmt0"},
    )


def test_parse_path_full():
    assert parse_path("/network-instance[name=default]/protocols/bgp") == [
        ("network-instance", {"name": "default"}),
        ("protocols", {}),
        ("bgp", {}),
    ]


def test_join_path_drops_empty_parts():
    assert join_path("", "system/lldp", None, "interface") == "system/lldp/interface"


def test_strip_module():
    assert strip_module("srl_nokia-if:interface") == "interface"
    assert strip_module("interface") == "interface"


# --------------------------------------------------------------------------- #
# tree mutation
# --------------------------------------------------------------------------- #


def test_insert_scalar_leaf():
    tree = {}
    insert(tree, "system/information/version", "24.10.1")
    assert materialize(tree) == {"system": {"information": {"version": "24.10.1"}}}


def test_insert_keyed_element_renders_as_list():
    tree = {}
    insert(tree, "interface[name=ethernet-1/1]/admin-state", "enable")
    insert(tree, "interface[name=ethernet-1/2]/admin-state", "disable")
    assert materialize(tree) == {
        "interface": [
            {"name": "ethernet-1/1", "admin-state": "enable"},
            {"name": "ethernet-1/2", "admin-state": "disable"},
        ]
    }


def test_insert_merges_subtree_then_leaf():
    tree = {}
    insert(tree, "system/lldp", {"interface": [{"name": "e1", "enabled": True}]})
    insert(tree, "system/lldp/interface[name=e1]/enabled", False)
    assert materialize(tree) == {
        "system": {"lldp": {"interface": [{"name": "e1", "enabled": False}]}}
    }


def test_insert_keeps_entries_without_key_leaves():
    tree = {}
    insert(tree, "system/lldp", {"neighbor": [{"system-name": "leaf2"}]})
    insert(tree, "system/lldp/neighbor[id=1]/system-name", "spine1")
    neighbors = materialize(tree)["system"]["lldp"]["neighbor"]
    assert {"system-name": "leaf2"} in neighbors
    assert {"id": "1", "system-name": "spine1"} in neighbors


def test_insert_strips_module_prefixes_from_values():
    tree = {}
    insert(tree, "interface[name=e1]", {"srl_nokia-if:type": "srl_nokia-if:bridged"})
    assert materialize(tree) == {"interface": [{"name": "e1", "type": "bridged"}]}


def test_insert_at_root_merges():
    tree = {"a": 1}
    insert(tree, "", {"b": 2})
    assert materialize(tree) == {"a": 1, "b": 2}


def test_later_subtree_update_replaces_list_entry_content():
    tree = {}
    insert(tree, "interface[name=e1]/statistics", {"in-octets": 10, "out-octets": 20})
    insert(tree, "interface[name=e1]/statistics", {"in-octets": 30})
    stats = materialize(tree)["interface"][0]["statistics"]
    assert stats == {"in-octets": 30, "out-octets": 20}


def test_delete_removes_list_entry():
    tree = {}
    insert(tree, "interface[name=e1]/admin-state", "enable")
    insert(tree, "interface[name=e2]/admin-state", "enable")
    delete(tree, "interface[name=e1]")
    assert materialize(tree) == {"interface": [{"name": "e2", "admin-state": "enable"}]}


def test_delete_last_list_entry_drops_the_list():
    tree = {}
    insert(tree, "interface[name=e1]/admin-state", "enable")
    delete(tree, "interface[name=e1]")
    assert materialize(tree) == {}


def test_delete_container():
    tree = {}
    insert(tree, "system/lldp/enabled", True)
    delete(tree, "system/lldp")
    assert materialize(tree) == {"system": {}}


def test_delete_unknown_path_is_a_noop():
    tree = {}
    insert(tree, "system/lldp/enabled", True)
    delete(tree, "system/bgp/nope")
    assert materialize(tree) == {"system": {"lldp": {"enabled": True}}}


def test_get_node_returns_list_entry():
    tree = {}
    insert(tree, "interface[name=ethernet-1/1]/statistics/in-octets", 42)
    node = get_node(tree, "interface[name=ethernet-1/1]/statistics")
    assert materialize(node) == {"in-octets": 42}


def test_get_node_missing_returns_none():
    assert get_node({}, "interface[name=e1]") is None


def test_materialize_deep_copies_opaque_values():
    tree = {}
    insert(tree, "system", {"routes": [1, 2, 3]})
    snapshot = materialize(tree)
    snapshot["system"]["routes"].append(4)
    assert materialize(tree)["system"]["routes"] == [1, 2, 3]


def test_list_node_len_tracks_entries():
    node = ListNode()
    node.entry({"name": "e1"})
    node.entry({"name": "e2"})
    node.entry({"name": "e1"})
    assert len(node) == 2


# --------------------------------------------------------------------------- #
# ageing out entries that stopped being refreshed
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock(monkeypatch):
    """Drive the timestamps list entries are aged by."""
    current = {"t": 100.0}
    monkeypatch.setattr(tree, "_now", lambda: current["t"])
    return current


def test_prune_drops_entries_that_stopped_being_refreshed(clock):
    root = {}
    insert(root, "candidate[address=10.0.0.1]/designated-forwarder", True)
    insert(root, "candidate[address=10.0.0.2]/designated-forwarder", False)

    clock["t"] = 130.0
    insert(root, "candidate[address=10.0.0.2]/designated-forwarder", True)

    assert prune(root, 120.0) == 1
    assert materialize(root)["candidate"] == [
        {"address": "10.0.0.2", "designated-forwarder": True}
    ]


def test_prune_keeps_entries_still_being_refreshed(clock):
    root = {}
    insert(root, "candidate[address=10.0.0.1]/designated-forwarder", True)

    clock["t"] = 130.0
    insert(root, "candidate[address=10.0.0.1]/designated-forwarder", True)

    assert prune(root, 120.0) == 0
    assert len(materialize(root)["candidate"]) == 1


def test_refreshing_a_nested_leaf_keeps_its_keyed_ancestors(clock):
    """An update deep inside an entry is what proves the entry still exists."""
    root = {}
    insert(root, "segment[name=ES-01]/candidate[address=10.0.0.1]/df", True)

    clock["t"] = 130.0
    insert(root, "segment[name=ES-01]/candidate[address=10.0.0.1]/df", False)

    assert prune(root, 120.0) == 0
    assert materialize(root)["segment"][0]["name"] == "ES-01"


def test_prune_removes_a_stale_entry_from_within_a_fresh_one(clock):
    root = {}
    insert(root, "segment[name=ES-01]/candidate[address=10.0.0.1]/df", True)
    insert(root, "segment[name=ES-01]/candidate[address=10.0.0.2]/df", True)

    clock["t"] = 130.0
    insert(root, "segment[name=ES-01]/candidate[address=10.0.0.2]/df", True)

    assert prune(root, 120.0) == 1
    segment = materialize(root)["segment"][0]
    assert segment["candidate"] == [{"address": "10.0.0.2", "df": True}]


def test_prune_leaves_opaque_lists_alone(clock):
    """Unpromoted lists are replaced wholesale by each update, so never stale."""
    root = {}
    insert(root, "system", {"routes": [{"prefix": "10.0.0.0/8"}]})

    clock["t"] = 130.0
    assert prune(root, 120.0) == 0
    assert materialize(root)["system"]["routes"] == [{"prefix": "10.0.0.0/8"}]


_ROUTE = "network-instance[name=ipvrf-1]/route-table/ipv4-unicast/route[ipv4-prefix={}][id=0]"


def _routes_from_a_get(root, pin):
    """What a bootstrap Get leaves: the route list opaque, its keys unknown."""
    insert(
        root,
        "network-instance[name=ipvrf-1]/route-table/ipv4-unicast",
        {"route": [{"ipv4-prefix": p, "id": 0, "active": True} for p in ("10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24")]},
        pin=pin,
    )


def _prefixes(root):
    table = get_node(root, "network-instance[name=ipvrf-1]/route-table/ipv4-unicast")
    return sorted(r["ipv4-prefix"] for r in materialize(table)["route"])


def test_an_on_change_update_pins_the_entries_it_promotes(clock):
    """One route changing must not leave the rest of its table to age out.

    A Get cannot say which leaves key the route list, so it stays opaque until
    the first keyed update promotes it. Every entry of it was ON_CHANGE data,
    and none of them will be re-sent until it changes.
    """
    root = {}
    _routes_from_a_get(root, pin=True)
    insert(root, _ROUTE.format("10.0.1.0/24"), {"active": False}, pin=True)

    clock["t"] = 200.0
    assert prune(root, 150.0) == 0
    assert _prefixes(root) == ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"]


def test_an_on_change_delete_pins_the_entries_it_promotes(clock):
    root = {}
    _routes_from_a_get(root, pin=True)
    delete(root, _ROUTE.format("10.0.1.0/24"), ["/network-instance[name=*]/route-table/ipv4-unicast"], pin=True)

    clock["t"] = 200.0
    assert prune(root, 150.0) == 0
    assert _prefixes(root) == ["10.0.0.0/24", "10.0.2.0/24"]


def test_a_sample_update_leaves_the_entries_it_promotes_to_age(clock):
    root = {}
    _routes_from_a_get(root, pin=False)
    insert(root, _ROUTE.format("10.0.1.0/24"), {"active": True})
    clock["t"] = 200.0
    insert(root, _ROUTE.format("10.0.1.0/24"), {"active": True})

    assert prune(root, 150.0) == 2
    assert _prefixes(root) == ["10.0.1.0/24"]


def test_pruned_entry_can_come_back(clock):
    root = {}
    insert(root, "candidate[address=10.0.0.1]/df", True)

    clock["t"] = 130.0
    prune(root, 120.0)
    insert(root, "candidate[address=10.0.0.1]/df", False)

    assert prune(root, 120.0) == 0
    assert materialize(root)["candidate"] == [{"address": "10.0.0.1", "df": False}]


# --------------------------------------------------------------------------- #
# restricting a shared envelope to one path
# --------------------------------------------------------------------------- #


def test_key_matches_literal_wildcard_and_glob():
    assert key_matches("lag1", "lag1")
    assert not key_matches("lag1", "lag2")
    assert key_matches("*", "ethernet-1/1")
    assert key_matches("lag*", "lag1")
    assert not key_matches("lag*", "ethernet-1/1")


def test_select_path_applies_a_glob_key_predicate():
    """A report reading ``lag*`` must not see what other paths put there."""
    interfaces = [
        {"name": "lag1", "oper-state": "up"},
        {"name": "ethernet-1/1", "oper-state": "up"},
        {"name": "lag2", "oper-state": "down"},
    ]
    selected = select_path(interfaces, "/interface[name=lag*]", "interface")
    assert [i["name"] for i in selected] == ["lag1", "lag2"]


def test_select_path_keeps_every_entry_for_a_wildcard():
    interfaces = [
        {"name": "lag1", "statistics": {"in-octets": "1"}},
        {"name": "ethernet-1/1", "statistics": {"in-octets": "2"}},
    ]
    selected = select_path(interfaces, "/interface[name=*]/statistics", "interface")
    assert selected == interfaces


#: One network-instance as the shared tree ends up holding it: the branch the
#: BGP RIB report asked for, the sibling branch SR Linux streams along with it,
#: the attribute sets a second path contributes, and a table belonging to an
#: unrelated report.
SHARED_NI = {
    "name": "default",
    "router-id": "10.0.0.1",
    "bridge-table": {"mac-table": {"mac": [{"address": "00:11:22:33:44:55"}]}},
    "bgp-rib": {
        "attr-sets": {"attr-set": [{"index": 1, "origin": "igp"}]},
        "afi-safi": [
            {
                "afi-safi-name": "evpn",
                "evpn": {
                    "rib-in-out": {
                        "rib-in-post": {
                            "mac-ip-route": [{"attr-id": 1, "used-route": True}]
                        },
                        "rib-in-pre": {"mac-ip-route": [{"attr-id": 2}]},
                    }
                },
            }
        ],
    },
}

RIB_REQUEST = (
    "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/"
    "rib-in-out/rib-in-post/mac-ip-route"
)
ATTR_SETS_REQUEST = "/network-instance[name=*]/bgp-rib/attr-sets/attr-set"


def test_select_path_drops_the_branches_the_request_did_not_name():
    """The BGP RIB getters walk their response looking for dicts to augment, so
    a branch a ``Get`` would not have returned makes them fail on a field only
    the requested branch has."""
    selected = select_path([copy.deepcopy(SHARED_NI)], RIB_REQUEST, "network-instance")
    entry = selected[0]
    assert set(entry) == {"name", "router-id", "bgp-rib"}
    assert set(entry["bgp-rib"]) == {"afi-safi"}
    assert list(entry["bgp-rib"]["afi-safi"][0]["evpn"]["rib-in-out"]) == [
        "rib-in-post"
    ]


def test_select_path_keeps_the_leaves_along_the_way():
    """Leaves carry the list keys a report projects, so they are not branches."""
    selected = select_path([copy.deepcopy(SHARED_NI)], RIB_REQUEST, "network-instance")
    assert selected[0]["router-id"] == "10.0.0.1"
    assert selected[0]["bgp-rib"]["afi-safi"][0]["afi-safi-name"] == "evpn"


def test_select_path_drops_entries_without_the_requested_branch():
    """A Get never names a list entry that holds nothing under the path."""
    nis = [copy.deepcopy(SHARED_NI), {"name": "mac-vrf-1", "bridge-table": {}}]
    selected = select_path(nis, ATTR_SETS_REQUEST, "network-instance")
    assert [ni["name"] for ni in selected] == ["default"]


def test_select_path_returns_an_empty_envelope_when_nothing_matches():
    nis = [{"name": "mac-vrf-1", "bridge-table": {}}]
    assert select_path(nis, ATTR_SETS_REQUEST, "network-instance") == []
    assert select_path({"network-instance": nis}, ATTR_SETS_REQUEST, "") == {}


def test_select_path_keeps_a_leaf_list():
    value = [
        {
            "name": "default",
            "ip-addresses": ["10.0.0.1", "10.0.0.2"],
            "bgp-rib": {"attr-sets": {"attr-set": [{"index": 1}]}},
        }
    ]
    selected = select_path(value, ATTR_SETS_REQUEST, "network-instance")
    assert selected[0]["ip-addresses"] == ["10.0.0.1", "10.0.0.2"]


def test_select_path_filters_below_the_envelope():
    value = {
        "interface": [
            {"name": "lag1", "subinterface": [{"index": 0}]},
            {"name": "ethernet-1/1", "subinterface": [{"index": 0}]},
        ]
    }
    selected = select_path(value, "/interface[name=lag*]/subinterface", "")
    assert [i["name"] for i in selected["interface"]] == ["lag1"]


def test_select_path_leaves_a_keyed_envelope_alone():
    control = {"software-version": "v24.10.1"}
    selected = select_path(
        control, "/platform/control[slot=A]", "platform/control[slot=A]"
    )
    assert selected == control


def test_select_path_ignores_an_envelope_deeper_than_the_request():
    entry = {"name": "lag1"}
    assert select_path(entry, "/interface", "interface[name=lag1]") == entry


def test_a_list_key_from_a_get_response_keeps_its_type():
    """A Get types its key leaves; only a gNMI path forces them to be text."""
    tree: dict = {}
    insert(
        tree,
        "network-instance",
        {"route": [{"ethernet-tag-id": 0, "attr-id": 5}]},
        key_hints={"route": ["ethernet-tag-id"]},
    )
    route = materialize(tree)["network-instance"]["route"][0]
    assert route["ethernet-tag-id"] == 0
