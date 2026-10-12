"""Tests for the topology inferred from LLDP and service placement.

The fabric built here is the shape the inference has to get right: two leaves per
site under a pair of spines, DCGWs stitching the sites, and a WAN router that has
no services either but is not a spine.
"""

from typing import Any, Dict, List, Optional, Tuple

from nornir_srl.server.topology import build_topology, node_facts


def _rt_instance(instance_id: int, route_target: str) -> Dict[str, Any]:
    return {
        "id": instance_id,
        "route-target": {"import-rt": route_target, "export-rt": route_target},
    }


def _ni(
    name: str,
    ni_type: str,
    *instances: Dict[str, Any],
    members: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    instance: Dict[str, Any] = {"name": name, "type": ni_type}
    if instances:
        instance["protocols"] = {"bgp-vpn": {"bgp-instance": list(instances)}}
    if members:
        instance["interface"] = [{"name": member} for member in members]
    return instance


def _subinterfaces(port: str, *indices: Dict[str, Any]) -> Dict[str, Any]:
    """An ``/interface`` entry carrying the detail of its subinterfaces."""
    return {"name": port, "subinterface": list(indices)}


def _segments(*segments: Tuple[str, str, str]) -> Dict[str, Any]:
    """An EVPN tree from ``(segment name, esi, interface)`` triples."""
    return {
        "network-instance": {
            "protocols": {
                "evpn": {
                    "ethernet-segments": {
                        "bgp-instance": [
                            {
                                "id": 1,
                                "ethernet-segment": [
                                    {
                                        "name": name,
                                        "esi": esi,
                                        "interface": [{"ethernet-interface": itf}],
                                    }
                                    for name, esi, itf in segments
                                ],
                            }
                        ]
                    }
                }
            }
        }
    }


def _lldp(*cables: Tuple[str, str, str]) -> Dict[str, Any]:
    """An LLDP tree from ``(local port, neighbour, neighbour port)`` triples."""
    return {
        "lldp": {
            "interface": [
                {
                    "name": local,
                    "neighbor": [{"id": "1", "system-name": peer, "port-id": peer_port}],
                }
                for local, peer, peer_port in cables
            ]
        }
    }


def _facts(
    name: str,
    *,
    host_name: Optional[str] = None,
    site: str = "",
    cables: Tuple[Tuple[str, str, str], ...] = (),
    instances: Tuple[Dict[str, Any], ...] = (),
    interfaces: Tuple[Dict[str, Any], ...] = (),
    segments: Tuple[Tuple[str, str, str], ...] = (),
    platform: str = "",
    connected: bool = True,
    egress: Optional[Dict[str, int]] = None,
):
    system = _lldp(*cables)
    if host_name is not None:
        system["name"] = {"host-name": host_name}
    if segments:
        system.update(_segments(*segments))
    snapshot: Dict[str, Any] = {"system": system}
    if instances:
        snapshot["network-instance"] = list(instances)
    if interfaces:
        snapshot["interface"] = list(interfaces)
    if platform:
        snapshot["platform"] = {"chassis": {"type": platform}}
    return node_facts(
        f"clab-dc-{name}",
        hostname=f"clab-dc-{name}",
        labels={"site": site} if site else {},
        snapshot=snapshot,
        connected=connected,
        egress=egress,
    )


#: One mac-vrf and one ip-vrf, each with a single bgp-vpn instance: a leaf.
LEAF_SERVICES = (
    _ni("default", "default"),
    _ni("mac-vrf-100", "mac-vrf", _rt_instance(1, "100:100")),
    _ni("ip-vrf-1", "ip-vrf", _rt_instance(1, "100:1")),
)

#: The same services with a second, WAN-side instance: a DCGW.
GATEWAY_SERVICES = (
    _ni("default", "default"),
    _ni(
        "mac-vrf-100",
        "mac-vrf",
        _rt_instance(1, "100:100"),
        _rt_instance(2, "64500:100"),
    ),
    _ni("ip-vrf-1", "ip-vrf", _rt_instance(1, "100:1"), _rt_instance(2, "64500:1")),
)

#: A node that runs the fabric but terminates nothing: spine, or WAN router.
NO_SERVICES = (_ni("default", "default"), _ni("mgmt", "ip-vrf"))


def _fabric() -> List[Any]:
    return [
        _facts(
            "leaf1",
            host_name="leaf1",
            site="dc1",
            cables=(
                ("ethernet-1/1", "spine1", "ethernet-1/1"),
                ("ethernet-1/2", "spine2", "ethernet-1/1"),
            ),
            instances=LEAF_SERVICES,
        ),
        _facts(
            "leaf2",
            host_name="leaf2",
            site="dc1",
            cables=(
                ("ethernet-1/1", "spine1", "ethernet-1/2"),
                ("ethernet-1/2", "spine2", "ethernet-1/2"),
            ),
            instances=LEAF_SERVICES,
        ),
        _facts(
            "spine1",
            host_name="spine1",
            site="dc1",
            cables=(
                ("ethernet-1/1", "leaf1", "ethernet-1/1"),
                ("ethernet-1/2", "leaf2", "ethernet-1/1"),
                ("ethernet-1/5", "dcgw1", "ethernet-1/1"),
            ),
            instances=NO_SERVICES,
        ),
        _facts(
            "spine2",
            host_name="spine2",
            site="dc1",
            cables=(
                ("ethernet-1/1", "leaf1", "ethernet-1/2"),
                ("ethernet-1/2", "leaf2", "ethernet-1/2"),
                ("ethernet-1/5", "dcgw1", "ethernet-1/2"),
            ),
            instances=NO_SERVICES,
        ),
        _facts(
            "dcgw1",
            host_name="dcgw1",
            site="dc1",
            cables=(
                ("ethernet-1/1", "spine1", "ethernet-1/5"),
                ("ethernet-1/2", "spine2", "ethernet-1/5"),
                ("ethernet-1/5", "dcgw2", "ethernet-1/5"),
                ("ethernet-1/6", "p1", "ethernet-1/1"),
            ),
            instances=GATEWAY_SERVICES,
        ),
        _facts(
            "dcgw2",
            host_name="dcgw2",
            site="dc2",
            cables=(
                ("ethernet-1/5", "dcgw1", "ethernet-1/5"),
                ("ethernet-1/6", "p1", "ethernet-1/2"),
            ),
            instances=GATEWAY_SERVICES,
        ),
        _facts(
            "p1",
            host_name="p1",
            site="wan",
            cables=(
                ("ethernet-1/1", "dcgw1", "ethernet-1/6"),
                ("ethernet-1/2", "dcgw2", "ethernet-1/6"),
                ("ethernet-1/5", "p2", "ethernet-1/5"),
            ),
            instances=NO_SERVICES,
        ),
    ]


def _roles(graph: Dict[str, Any]) -> Dict[str, str]:
    return {node["label"]: node["role"] for node in graph["nodes"]}


# --------------------------------------------------------------------------- #
# reading the streamed state
# --------------------------------------------------------------------------- #


def test_node_facts_counts_the_services_of_a_leaf():
    facts = _facts("leaf1", host_name="leaf1", instances=LEAF_SERVICES)
    assert facts.mac_vrfs == 1
    assert facts.ip_vrfs == 1
    assert facts.stitched == 0
    assert facts.has_state


def test_node_facts_marks_two_bgp_vpn_instances_as_stitched():
    facts = _facts("dcgw1", host_name="dcgw1", instances=GATEWAY_SERVICES)
    assert facts.stitched == 2


def test_node_facts_ignores_the_mgmt_ip_vrf():
    facts = _facts("spine1", host_name="spine1", instances=NO_SERVICES)
    assert (facts.mac_vrfs, facts.ip_vrfs) == (0, 0)
    assert facts.has_state


def test_node_facts_without_state_is_not_a_node_without_services():
    facts = _facts("leaf9")
    assert not facts.has_state
    assert facts.adjacencies == []


def test_node_facts_reads_the_chassis_type():
    facts = _facts("leaf1", host_name="leaf1", platform="7220 IXR-D2L")
    assert facts.platform == "7220 IXR-D2L"


def test_node_facts_reads_chassis_type_from_a_list_entry():
    facts = node_facts(
        "leaf1",
        snapshot={"platform": {"chassis": [{"type": "7220 IXR-D3"}]}},
    )
    assert facts.platform == "7220 IXR-D3"


def test_topology_carries_the_chassis_type_on_each_node():
    graph = build_topology(
        [_facts("leaf1", host_name="leaf1", platform="7220 IXR-D2L", instances=LEAF_SERVICES)]
    )
    node = next(n for n in graph["nodes"] if n["label"] == "leaf1")
    assert node["platform"] == "7220 IXR-D2L"


def test_node_facts_leaves_out_the_management_network():
    """Every node of a lab sees every other one on the management bridge."""
    facts = _facts(
        "leaf1",
        host_name="leaf1",
        cables=(
            ("ethernet-1/1", "spine1", "ethernet-1/1"),
            ("mgmt0", "spine1", "mgmt0"),
            ("mgmt0", "p1", "mgmt0"),
        ),
    )
    assert [adjacency.local_port for adjacency in facts.adjacencies] == ["ethernet-1/1"]


def test_node_facts_leaves_out_a_neighbour_reached_on_its_management_port():
    """The other end may be a device whose management port is not named mgmt0."""
    facts = _facts(
        "leaf1",
        host_name="leaf1",
        cables=(("ethernet-1/1", "server1", "mgmt-eth0"),),
    )
    assert facts.adjacencies == []


def test_the_management_mesh_does_not_flatten_the_roles():
    """The mesh gives every node the same neighbours, which classifies nothing."""
    fabric = _fabric()
    graph = build_topology(fabric)
    meshed = [
        _facts(
            node.name.removeprefix("clab-dc-"),
            host_name=node.system_name,
            site=node.site,
            cables=tuple(
                (adjacency.local_port, adjacency.peer, adjacency.peer_port)
                for adjacency in node.adjacencies
            )
            + tuple(
                ("mgmt0", other.system_name, "mgmt0")
                for other in fabric
                if other.name != node.name
            ),
            instances=tuple(_instances_of(node)),
        )
        for node in fabric
    ]
    assert _roles(build_topology(meshed)) == _roles(graph)


def _instances_of(node):
    """The service set a node was built with, recovered from its counters."""
    if node.stitched:
        return GATEWAY_SERVICES
    if node.mac_vrfs:
        return LEAF_SERVICES
    return NO_SERVICES


def test_node_facts_reads_lldp_neighbours_and_the_local_port_state():
    facts = _facts(
        "leaf1",
        host_name="leaf1",
        cables=(("ethernet-1/1", "spine1", "ethernet-1/49"),),
        interfaces=({"name": "ethernet-1/1", "oper-state": "up"},),
    )
    adjacency = facts.adjacencies[0]
    assert (adjacency.local_port, adjacency.peer, adjacency.peer_port) == (
        "ethernet-1/1",
        "spine1",
        "ethernet-1/49",
    )
    assert adjacency.oper_state == "up"


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #


def test_services_make_a_leaf_and_stitched_services_make_a_gateway():
    roles = _roles(build_topology(_fabric()))
    assert roles["leaf1"] == "leaf"
    assert roles["leaf2"] == "leaf"
    assert roles["dcgw1"] == "dcgw"
    assert roles["dcgw2"] == "dcgw"


def test_a_node_without_services_that_sees_leaves_is_a_spine():
    roles = _roles(build_topology(_fabric()))
    assert roles["spine1"] == "spine"
    assert roles["spine2"] == "spine"


def test_a_node_without_services_that_only_sees_gateways_is_core():
    """A WAN router has no mac-vrfs either, but it is not a spine."""
    roles = _roles(build_topology(_fabric()))
    assert roles["p1"] == "core"


def test_a_single_leaf_fabric_still_has_a_spine():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
            ),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(("ethernet-1/1", "leaf1", "ethernet-1/1"),),
                instances=NO_SERVICES,
            ),
        ]
    )
    assert _roles(graph) == {"leaf1": "leaf", "spine1": "spine"}


def test_a_node_that_streamed_nothing_yet_is_unclassified():
    graph = build_topology([*_fabric(), _facts("leaf9", connected=False)])
    node = next(n for n in graph["nodes"] if n["label"].endswith("leaf9"))
    assert node["role"] == "unknown"
    assert node["layer"] == 2
    assert node["connected"] is False


def test_roles_are_counted_per_fabric():
    assert build_topology(_fabric())["roles"] == {
        "core": 1,
        "dcgw": 2,
        "spine": 2,
        "leaf": 2,
        "external": 1,
    }


# --------------------------------------------------------------------------- #
# layers
# --------------------------------------------------------------------------- #


def test_layers_run_from_the_wan_down_to_the_leaves():
    graph = build_topology(_fabric())
    assert [layer["index"] for layer in graph["layers"]] == [6, 5, 4, 3, 2]
    assert [layer["label"] for layer in graph["layers"]] == [
        "WAN / core",
        "DC gateways",
        "Spines",
        "Leaves",
        "Edge / unclassified",
    ]
    tiers = {layer["index"]: layer["nodes"] for layer in graph["layers"]}
    assert tiers[3] == ["clab-dc-leaf1", "clab-dc-leaf2"]
    assert tiers[6] == ["clab-dc-p1"]


def test_empty_tiers_are_left_out():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
            ),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(("ethernet-1/1", "leaf1", "ethernet-1/1"),),
                instances=NO_SERVICES,
            ),
        ]
    )
    assert [layer["index"] for layer in graph["layers"]] == [4, 3]


def test_sites_come_from_the_inventory_labels():
    assert build_topology(_fabric())["sites"] == ["dc1", "dc2", "wan"]


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def _link(graph: Dict[str, Any], a: str, b: str) -> Dict[str, Any]:
    pair = {f"clab-dc-{a}", f"clab-dc-{b}"}
    return next(link for link in graph["links"] if {link["a"], link["b"]} == pair)


def test_a_cable_reported_by_both_ends_is_one_link():
    graph = build_topology(_fabric())
    # 4 leaf-spine, 2 spine-dcgw, 1 dcgw mesh, 2 dcgw-p1, 1 p1 to the outside.
    assert len(graph["links"]) == 10
    link = _link(graph, "leaf1", "spine1")
    assert link["count"] == 1
    assert link["ports"] == [{"a_port": "ethernet-1/1", "b_port": "ethernet-1/1"}]


def test_parallel_cables_between_two_nodes_are_one_link():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(
                    ("ethernet-1/1", "spine1", "ethernet-1/1"),
                    ("ethernet-1/3", "spine1", "ethernet-1/3"),
                ),
                instances=LEAF_SERVICES,
            ),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(
                    ("ethernet-1/1", "leaf1", "ethernet-1/1"),
                    ("ethernet-1/3", "leaf1", "ethernet-1/3"),
                ),
                instances=NO_SERVICES,
            ),
        ]
    )
    assert len(graph["links"]) == 1
    assert graph["links"][0]["count"] == 2


def test_links_inside_one_tier_are_marked():
    graph = build_topology(_fabric())
    assert _link(graph, "dcgw1", "dcgw2")["intra_layer"] is True
    assert _link(graph, "leaf1", "spine1")["intra_layer"] is False


def test_link_state_follows_the_port_state_of_either_end():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(
                    ("ethernet-1/1", "spine1", "ethernet-1/1"),
                    ("ethernet-1/2", "spine2", "ethernet-1/1"),
                ),
                instances=LEAF_SERVICES,
                interfaces=(
                    {"name": "ethernet-1/1", "oper-state": "up"},
                    {"name": "ethernet-1/2", "oper-state": "down"},
                ),
            ),
            _facts("spine1", host_name="spine1", instances=NO_SERVICES),
            _facts("spine2", host_name="spine2", instances=NO_SERVICES),
        ]
    )
    assert _link(graph, "leaf1", "spine1")["state"] == "up"
    assert _link(graph, "leaf1", "spine2")["state"] == "down"


def test_a_link_whose_port_state_is_unknown_is_not_called_up():
    graph = build_topology(_fabric())
    assert _link(graph, "leaf1", "spine1")["state"] == "unknown"


def test_a_cable_held_down_by_an_ethernet_segment_is_standby_not_down():
    """Red on a standby port reads as a broken fabric; it is intent."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(
                    ("ethernet-1/1", "spine1", "ethernet-1/1"),
                    ("ethernet-1/2", "spine2", "ethernet-1/1"),
                ),
                instances=LEAF_SERVICES,
                interfaces=(
                    {"name": "ethernet-1/1", "oper-state": "up"},
                    {
                        "name": "ethernet-1/2",
                        "oper-state": "down",
                        "oper-down-reason": "standby-signaling",
                    },
                ),
            ),
            _facts("spine1", host_name="spine1", instances=NO_SERVICES),
            _facts("spine2", host_name="spine2", instances=NO_SERVICES),
        ]
    )
    assert _link(graph, "leaf1", "spine1")["state"] == "up"
    assert _link(graph, "leaf1", "spine2")["state"] == "down/standby"


def test_a_link_carries_the_egress_of_each_end():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
                egress={"ethernet-1/1": 8_000_000},
            ),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(("ethernet-1/1", "leaf1", "ethernet-1/1"),),
                instances=NO_SERVICES,
                egress={"ethernet-1/1": 1_000_000},
            ),
        ]
    )
    link = graph["links"][0]
    assert link["a"].endswith("leaf1")
    assert (link["a_out_bps"], link["b_out_bps"]) == (8_000_000, 1_000_000)
    assert link["ports"][0]["a_out_bps"] == 8_000_000
    assert link["ports"][0]["b_out_bps"] == 1_000_000


def test_parallel_cables_take_the_hottest_egress_of_each_end():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(
                    ("ethernet-1/1", "spine1", "ethernet-1/1"),
                    ("ethernet-1/3", "spine1", "ethernet-1/3"),
                ),
                instances=LEAF_SERVICES,
                egress={"ethernet-1/1": 100, "ethernet-1/3": 900},
            ),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(
                    ("ethernet-1/1", "leaf1", "ethernet-1/1"),
                    ("ethernet-1/3", "leaf1", "ethernet-1/3"),
                ),
                instances=NO_SERVICES,
                egress={"ethernet-1/1": 50, "ethernet-1/3": 20},
            ),
        ]
    )
    link = graph["links"][0]
    assert (link["a_out_bps"], link["b_out_bps"]) == (900, 50)


def test_a_link_without_rates_omits_them():
    link = _link(build_topology(_fabric()), "leaf1", "spine1")
    assert "a_out_bps" not in link
    assert "b_out_bps" not in link


def test_an_access_link_has_egress_only_on_the_fabric_end():
    graph = build_topology([_leaf_with_a_client(egress={"ethernet-1/10": 5000})])
    link = next(cable for cable in graph["links"] if cable["access"])
    assert link["a_out_bps"] == 5000
    assert "b_out_bps" not in link


# --------------------------------------------------------------------------- #
# resolving neighbours to inventory nodes
# --------------------------------------------------------------------------- #


def test_a_neighbour_is_matched_through_the_containerlab_prefix():
    """LLDP advertises 'leaf1'; the inventory calls the same node 'clab-dc-leaf1'."""
    graph = build_topology(_fabric())
    leaf1 = next(n for n in graph["nodes"] if n["label"] == "leaf1")
    assert leaf1["name"] == "clab-dc-leaf1"
    assert leaf1["peers"] == ["clab-dc-spine1", "clab-dc-spine2"]


def test_a_neighbour_is_matched_without_a_host_name_of_its_own():
    """A node that has not streamed its host-name is still resolvable by name."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
            ),
            _facts("spine1", instances=NO_SERVICES),
        ]
    )
    assert len(graph["links"]) == 1
    assert graph["unresolved"] == []


def test_a_neighbour_with_a_domain_suffix_is_matched():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1.dc1.example.net", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
            ),
            _facts("spine1", host_name="spine1", instances=NO_SERVICES),
        ]
    )
    assert graph["unresolved"] == []
    assert _link(graph, "leaf1", "spine1")["count"] == 1


def test_a_neighbour_outside_the_inventory_is_kept_as_an_external_node():
    graph = build_topology(_fabric())
    assert graph["unresolved"] == [{"peer": "p2", "seen_by": ["clab-dc-p1"]}]
    external = next(n for n in graph["nodes"] if n["external"])
    assert (external["name"], external["role"], external["layer"]) == ("p2", "external", 2)
    assert any({link["a"], link["b"]} == {"clab-dc-p1", "p2"} for link in graph["links"])


def test_a_neighbour_advertising_our_own_name_is_not_a_link():
    """Two nodes of a lab sharing a host-name would otherwise draw a self-loop."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "leaf1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
            )
        ]
    )
    assert graph["links"] == []


def test_an_ambiguous_name_is_not_resolved_to_either_node():
    """Two nodes answering to one name is a lab mistake, not a link to guess at."""
    graph = build_topology(
        [
            _facts("leaf1", host_name="dup", instances=LEAF_SERVICES),
            _facts("leaf2", host_name="dup", instances=LEAF_SERVICES),
            _facts(
                "spine1",
                host_name="spine1",
                cables=(("ethernet-1/1", "dup", "ethernet-1/1"),),
                instances=NO_SERVICES,
            ),
        ]
    )
    assert [entry["peer"] for entry in graph["unresolved"]] == ["dup"]


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #

#: A leaf with a customer on ethernet-1/10: the mac-vrf holds the access
#: subinterface next to its IRB, and the ip-vrf holds only the IRB.
CLIENT_SERVICES = (
    _ni("default", "default"),
    _ni(
        "mac-vrf-100",
        "mac-vrf",
        _rt_instance(1, "100:100"),
        members=("irb0.100", "ethernet-1/10.100"),
    ),
    _ni("ip-vrf-1", "ip-vrf", _rt_instance(1, "100:1"), members=("irb0.100",)),
)


def _clients(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [node for node in graph["nodes"] if node["role"] == "client"]


def _leaf_with_a_client(name: str = "leaf1", **kwargs: Any):
    return _facts(
        name,
        host_name=name,
        site="dc1",
        cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
        instances=CLIENT_SERVICES,
        **kwargs,
    )


def test_a_bridged_subinterface_of_a_mac_vrf_is_a_client():
    graph = build_topology([_leaf_with_a_client()])
    client = _clients(graph)[0]
    assert client["name"] == "clab-dc-leaf1:ethernet-1/10"
    assert client["peers"] == ["clab-dc-leaf1"]
    assert client["services"] == ["mac-vrf-100"]
    attachment = client["attachments"][0]
    assert attachment["kind"] == "bridged"
    assert attachment["subinterface"] == "ethernet-1/10.100"
    assert attachment["service"] == "mac-vrf-100"


def test_a_routed_port_of_an_ip_vrf_is_a_client():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(
                    _ni("default", "default"),
                    _ni("ip-vrf-1", "ip-vrf", members=("irb0.100", "ethernet-1/20.0")),
                ),
            )
        ]
    )
    attachment = _clients(graph)[0]["attachments"][0]
    assert (attachment["kind"], attachment["subinterface"]) == ("routed", "ethernet-1/20.0")


def test_the_interfaces_a_service_binds_to_itself_are_not_clients():
    """An IRB, a loopback and system0 are the node's own, not a customer's."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(
                    _ni(
                        "ip-vrf-1",
                        "ip-vrf",
                        members=("irb0.100", "lo0.1", "system0.0", "mgmt0.0"),
                    ),
                ),
            )
        ]
    )
    assert _clients(graph) == []


def test_a_service_on_a_fabric_port_is_not_a_client():
    """The WAN subinterface of a stitched ip-vrf lands on the DCGW's neighbour."""
    graph = build_topology(
        [
            _facts(
                "dcgw1",
                host_name="dcgw1",
                cables=(("ethernet-1/6", "p1", "ethernet-1/1"),),
                instances=(
                    _ni("default", "default"),
                    _ni(
                        "ip-vrf-1",
                        "ip-vrf",
                        _rt_instance(1, "100:1"),
                        _rt_instance(2, "64500:1"),
                        members=("irb0.100", "ethernet-1/6.0"),
                    ),
                ),
            ),
            _facts("p1", host_name="p1", instances=NO_SERVICES),
        ]
    )
    assert _clients(graph) == []


def test_two_vlans_of_one_cable_are_one_client():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(
                    _ni("mac-vrf-100", "mac-vrf", members=("ethernet-1/10.100",)),
                    _ni("mac-vrf-200", "mac-vrf", members=("ethernet-1/10.200",)),
                ),
            )
        ]
    )
    clients = _clients(graph)
    assert len(clients) == 1
    assert clients[0]["services"] == ["mac-vrf-100", "mac-vrf-200"]
    assert clients[0]["ports"] == 1


def test_a_client_that_advertises_itself_is_named_after_it_on_every_leaf():
    """A dual-homed server is one box, which only LLDP can tell us."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("lag1", "server1", "eth0"),),
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
            ),
            _facts(
                "leaf2",
                host_name="leaf2",
                cables=(("lag1", "server1", "eth1"),),
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
            ),
        ]
    )
    clients = _clients(graph)
    assert [client["name"] for client in clients] == ["server1"]
    assert clients[0]["peers"] == ["clab-dc-leaf1", "clab-dc-leaf2"]
    # It is a client of the fabric now, not a neighbour we failed to place.
    assert graph["unresolved"] == []
    assert not any(node["external"] for node in graph["nodes"])


def test_a_client_without_lldp_or_a_segment_is_drawn_once_per_port():
    """Two servers or one dual-homed server look the same with neither."""
    graph = build_topology([_leaf_with_a_client("leaf1"), _leaf_with_a_client("leaf2")])
    assert [client["name"] for client in _clients(graph)] == [
        "clab-dc-leaf1:ethernet-1/10",
        "clab-dc-leaf2:ethernet-1/10",
    ]


#: The same segment as both leaves of a multi-homing pair report it.
ESI = "01:24:24:24:24:24:00:00:00:01"


def _multi_homed_leaf(name: str):
    return _facts(
        name,
        host_name=name,
        site="dc1",
        instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
        segments=(("mh-server1", ESI, "lag1"),),
    )


def test_one_ethernet_segment_on_two_leaves_is_one_client():
    """Without LLDP the ESI is the only thing that says this is a single host."""
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    clients = _clients(graph)
    assert len(clients) == 1
    assert clients[0]["name"] == ESI
    assert clients[0]["esi"] == ESI
    assert clients[0]["ports"] == 2
    # It reaches its two leaves over the bundle rather than over a cable each.
    assert clients[0]["peers"] == [f"es:{ESI}"]


def test_a_client_box_says_client_and_nothing_it_was_named_after():
    """A name inferred from an ESI or a port reads like an identity it lacks."""
    graph = build_topology([_multi_homed_leaf("leaf1"), _leaf_with_a_client("leaf2")])
    assert [client["label"] for client in _clients(graph)] == ["client", "client"]


def test_a_multi_homed_client_is_cabled_to_its_leaves_through_the_segment():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    access = [link for link in graph["links"] if link["access"]]
    # A link is keyed on its endpoints sorted, so which of the two is 'a'
    # follows from the names rather than from one of them being the client.
    assert [{link["a"], link["b"]} for link in access] == [
        {ESI, f"es:{ESI}"},
        {"clab-dc-leaf1", f"es:{ESI}"},
        {"clab-dc-leaf2", f"es:{ESI}"},
    ]


def test_the_cable_from_a_segment_to_its_client_carries_no_port():
    """It stands for the bundle, not for either of the ports that make it up."""
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    bundle = next(
        link for link in graph["links"] if {link["a"], link["b"]} == {ESI, f"es:{ESI}"}
    )
    assert (bundle["ports"], bundle["count"]) == ([], 0)


def test_a_host_seen_on_the_members_of_a_segment_lag_hangs_under_the_segment():
    """LLDP runs per member port, while the service and the segment are on the lag."""

    def leaf(name: str):
        return _facts(
            name,
            host_name=name,
            cables=(("ethernet-1/1", "host1", "eth1"),),
            interfaces=({"name": "ethernet-1/1", "ethernet": {"aggregate-id": "lag1"}},),
            instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
            segments=(("mh-host1", ESI, "lag1"),),
        )

    graph = build_topology([leaf("leaf1"), leaf("leaf2")])
    assert [client["name"] for client in _clients(graph)] == ["host1"]
    assert not [node for node in graph["nodes"] if node["role"] == "external"]
    assert graph["unresolved"] == []
    assert sorted(sorted((link["a"], link["b"])) for link in graph["links"]) == [
        ["clab-dc-leaf1", f"es:{ESI}"],
        ["clab-dc-leaf2", f"es:{ESI}"],
        [f"es:{ESI}", "host1"],
    ]


def test_a_segment_on_one_leaf_only_is_still_that_one_client():
    graph = build_topology([_multi_homed_leaf("leaf1")])
    client = _clients(graph)[0]
    assert (client["name"], client["ports"]) == (ESI, 1)


def test_the_name_a_client_advertises_wins_over_its_segment():
    """A client that says who it is says the same to every leaf under it."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("lag1", "server1", "eth0"),),
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
                segments=(("mh-server1", ESI, "lag1"),),
            )
        ]
    )
    client = _clients(graph)[0]
    assert (client["name"], client["advertised"]) == ("server1", "server1")
    # The segment is still reported, it just is not what identifies the client.
    assert client["esi"] == ESI


def test_a_segment_on_a_fabric_port_hangs_nothing_off_it():
    """An ESI is no reason to draw a client where there is no service."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/1", "spine1", "ethernet-1/1"),),
                instances=LEAF_SERVICES,
                segments=(("stray", ESI, "ethernet-1/1"),),
            ),
            _facts("spine1", host_name="spine1", instances=NO_SERVICES),
        ]
    )
    assert _clients(graph) == []


def test_a_segment_without_an_esi_is_not_an_identity():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
                segments=(("half-configured", "", "lag1"),),
            )
        ]
    )
    assert _clients(graph)[0]["name"] == "clab-dc-leaf1:lag1"


def test_the_vlan_and_the_address_of_an_attachment_come_from_the_interface_tree():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(_ni("ip-vrf-1", "ip-vrf", members=("ethernet-1/20.0",)),),
                interfaces=(
                    _subinterfaces(
                        "ethernet-1/20",
                        {
                            "index": 0,
                            "oper-state": "up",
                            "vlan": {"encap": {"single-tagged": {"vlan-id": 100}}},
                            "ipv4": {"address": [{"ip-prefix": "10.0.0.1/30"}]},
                        },
                    ),
                ),
            )
        ]
    )
    attachment = _clients(graph)[0]["attachments"][0]
    assert (attachment["vlan"], attachment["ip"]) == ("100", "10.0.0.1/30")
    assert attachment["state"] == "up"


def test_an_attachment_without_the_interface_tree_still_places_the_client():
    """The vlan is an enrichment; the service alone is enough to draw the client."""
    client = _clients(build_topology([_leaf_with_a_client()]))[0]
    assert client["attachments"][0]["vlan"] == ""


# --------------------------------------------------------------------------- #
# where the clients are drawn
# --------------------------------------------------------------------------- #


def test_the_clients_are_the_bottom_tier():
    graph = build_topology(
        [
            _leaf_with_a_client(),
            _facts(
                "spine1",
                host_name="spine1",
                site="dc1",
                cables=(("ethernet-1/1", "leaf1", "ethernet-1/1"),),
                instances=NO_SERVICES,
            ),
        ]
    )
    assert [layer["label"] for layer in graph["layers"]] == ["Spines", "Leaves", "Clients"]
    bottom = graph["layers"][-1]
    assert (bottom["index"], bottom["nodes"]) == (0, ["clab-dc-leaf1:ethernet-1/10"])


def test_a_client_takes_the_site_of_the_leaf_it_hangs_off():
    client = _clients(build_topology([_leaf_with_a_client()]))[0]
    assert client["site"] == "dc1"


def test_the_clients_of_a_row_are_ordered_by_the_node_they_attach_to():
    """Drawn in port-name order they would cross over each other's leaf."""

    def leaf(name: str, port: str):
        return _facts(
            name,
            host_name=name,
            site="dc1",
            instances=(_ni("mac-vrf-100", "mac-vrf", members=(f"{port}.100",)),),
        )

    graph = build_topology([leaf("leaf2", "ethernet-1/10"), leaf("leaf1", "ethernet-1/20")])
    assert graph["layers"][-1]["nodes"] == [
        "clab-dc-leaf1:ethernet-1/20",
        "clab-dc-leaf2:ethernet-1/10",
    ]


def test_a_client_is_cabled_to_its_leaf_and_the_cable_is_marked_access():
    graph = build_topology([_leaf_with_a_client()])
    link = next(link for link in graph["links"] if link["b"].endswith("ethernet-1/10"))
    assert link["a"] == "clab-dc-leaf1"
    assert link["access"] is True
    assert link["ports"] == [{"a_port": "ethernet-1/10", "b_port": ""}]


def test_a_fabric_link_is_not_marked_access():
    assert _link(build_topology(_fabric()), "leaf1", "spine1")["access"] is False


def test_the_cable_of_a_client_that_runs_lldp_keeps_both_port_names():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                cables=(("ethernet-1/10", "server1", "eth0"),),
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("ethernet-1/10.100",)),),
            )
        ]
    )
    link = next(link for link in graph["links"] if "server1" in (link["a"], link["b"]))
    assert link["count"] == 1
    assert link["ports"] == [{"a_port": "ethernet-1/10", "b_port": "eth0"}]
    assert link["access"] is True


def test_a_node_counts_the_clients_that_hang_off_it():
    graph = build_topology([_leaf_with_a_client()])
    leaf = next(node for node in graph["nodes"] if node["name"] == "clab-dc-leaf1")
    assert leaf["clients"] == 1


def test_a_client_does_not_change_the_tier_of_the_fabric():
    """A leaf with customers on it is still a leaf, and its spine still a spine."""
    plain = build_topology(_fabric())

    def with_a_client(node):
        # Only the nodes that terminate services can have a customer on them.
        access = (
            (_ni("mac-vrf-900", "mac-vrf", members=("ethernet-1/30.900",)),)
            if node.mac_vrfs
            else ()
        )
        return _facts(
            node.name.removeprefix("clab-dc-"),
            host_name=node.system_name,
            site=node.site,
            cables=tuple(
                (adjacency.local_port, adjacency.peer, adjacency.peer_port)
                for adjacency in node.adjacencies
            ),
            instances=tuple(_instances_of(node)) + access,
        )

    graph = build_topology([with_a_client(node) for node in _fabric()])
    assert len(_clients(graph)) == 4  # two leaves and two gateways
    fabric_roles = {
        label: role for label, role in _roles(graph).items() if role != "client"
    }
    assert fabric_roles == _roles(plain)


# --------------------------------------------------------------------------- #
# the ethernet-segment tier
# --------------------------------------------------------------------------- #


def _segment_nodes(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [node for node in graph["nodes"] if node["role"] == "segment"]


def test_a_segment_is_a_tier_between_the_leaves_and_the_clients():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    assert [layer["label"] for layer in graph["layers"]] == [
        "Leaves",
        "Ethernet segments",
        "Clients",
    ]
    assert graph["layers"][1]["nodes"] == [f"es:{ESI}"]


def test_a_segment_is_drawn_under_the_tail_of_its_esi():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    segment = _segment_nodes(graph)[0]
    assert (segment["label"], segment["esi"]) == ("ES 00:01", ESI)
    assert segment["names"] == ["mh-server1"]
    assert segment["peers"] == [ESI, "clab-dc-leaf1", "clab-dc-leaf2"]
    assert segment["ports"] == 2
    assert segment["clients"] == 1


def test_two_leaves_that_disagree_on_the_name_keep_both_of_them():
    """Nothing makes the leaves of one segment configure the same name for it."""
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
                segments=(("to-server1", ESI, "lag1"),),
            ),
            _facts(
                "leaf2",
                host_name="leaf2",
                instances=(_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
                segments=(("es-1", ESI, "lag1"),),
            ),
        ]
    )
    segment = _segment_nodes(graph)[0]
    # The ESI is what they do agree on, so it is what the box is drawn under.
    assert segment["label"] == "ES 00:01"
    assert segment["names"] == ["es-1", "to-server1"]


def test_a_single_homed_client_gets_no_segment_of_its_own():
    graph = build_topology([_leaf_with_a_client()])
    assert _segment_nodes(graph) == []
    assert _clients(graph)[0]["peers"] == ["clab-dc-leaf1"]


def test_a_leaf_is_cabled_to_the_segment_rather_than_to_the_client_behind_it():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    leaf = next(node for node in graph["nodes"] if node["name"] == "clab-dc-leaf1")
    assert leaf["peers"] == [f"es:{ESI}"]
    # The client behind the segment is still one of the leaf's own.
    assert leaf["clients"] == 1


def test_a_segment_takes_the_site_of_the_leaves_it_lands_on():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    assert _segment_nodes(graph)[0]["site"] == "dc1"


def test_a_segment_is_counted_as_its_own_role():
    graph = build_topology([_multi_homed_leaf("leaf1"), _multi_homed_leaf("leaf2")])
    assert graph["roles"] == {"leaf": 2, "segment": 1, "client": 1}


# --------------------------------------------------------------------------- #
# fabrics
# --------------------------------------------------------------------------- #


def _pod(prefix: str, *, site: str = "", client: bool = False) -> List[Any]:
    """A leaf and a spine cabled to each other and to nothing outside the pod.

    With *client*, the leaf has its port in the ethernet-segment ``ESI`` instead
    of running plain services, so that two pods can be given the one multi-homed
    client between them.
    """
    services: Dict[str, Any] = {"instances": LEAF_SERVICES}
    if client:
        services = {
            "instances": (_ni("mac-vrf-100", "mac-vrf", members=("lag1.100",)),),
            "segments": (("mh-server1", ESI, "lag1"),),
        }
    return [
        _facts(
            f"{prefix}-leaf1",
            host_name=f"{prefix}-leaf1",
            site=site,
            cables=(("ethernet-1/1", f"{prefix}-spine1", "ethernet-1/1"),),
            **services,
        ),
        _facts(
            f"{prefix}-spine1",
            host_name=f"{prefix}-spine1",
            site=site,
            cables=(("ethernet-1/1", f"{prefix}-leaf1", "ethernet-1/1"),),
            instances=NO_SERVICES,
        ),
    ]


def _fabrics_of(graph: Dict[str, Any], name: str) -> List[str]:
    return next(node for node in graph["nodes"] if node["name"] == name)["fabrics"]


def test_a_fabric_that_is_all_one_piece_is_a_single_group():
    graph = build_topology(_fabric())
    assert [fabric["id"] for fabric in graph["fabrics"]] == ["clab-dc-dcgw1"]
    assert all(node["fabrics"] == ["clab-dc-dcgw1"] for node in graph["nodes"])


def test_nodes_that_share_no_cable_are_separate_fabrics():
    graph = build_topology(_pod("frontend") + _pod("backend"))
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["backend", "frontend"]
    assert _fabrics_of(graph, "clab-dc-backend-leaf1") == ["clab-dc-backend-leaf1"]
    assert _fabrics_of(graph, "clab-dc-frontend-spine1") == ["clab-dc-frontend-leaf1"]


def test_a_client_plugged_into_two_fabrics_does_not_join_them():
    """A server in both pods says nothing about a path between them."""
    graph = build_topology(_pod("frontend", client=True) + _pod("backend", client=True))
    assert len(graph["fabrics"]) == 2
    both = ["clab-dc-backend-leaf1", "clab-dc-frontend-leaf1"]
    # The one client, and the bundle it reaches its two leaves over, are drawn
    # on either tab rather than only on the one its first leaf happens to be on.
    assert _fabrics_of(graph, ESI) == both
    assert _fabrics_of(graph, f"es:{ESI}") == both


def test_the_largest_fabric_comes_first():
    graph = build_topology(_fabric() + _pod("frontend"))
    assert [fabric["devices"] for fabric in graph["fabrics"]] == [8, 2]


def test_the_fabrics_are_named_after_the_sites_their_nodes_share():
    graph = build_topology(_pod("pod1", site="dc1") + _pod("pod2", site="dc2"))
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["dc1", "dc2"]


def test_the_fabrics_are_named_after_the_names_their_nodes_share():
    graph = build_topology(_pod("frontend") + _pod("backend"))
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["backend", "frontend"]


def test_a_naming_that_does_not_fit_every_fabric_is_used_for_none_of_them():
    """'frontend' beside 'Fabric 1' reads as a name beside a placeholder."""
    graph = build_topology(_fabric() + _pod("frontend"))
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["Fabric 1", "Fabric 2"]


def test_a_site_two_fabrics_share_names_neither_of_them():
    """Two tabs reading 'dc1' say nothing about which is which."""
    graph = build_topology(_pod("pod1", site="dc1") + _pod("pod2", site="dc1"))
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["pod1", "pod2"]


def test_a_node_cabled_to_nothing_is_not_a_fabric_of_its_own():
    """One tab per unreachable node would be a tab bar of nothing but noise."""
    graph = build_topology(
        _pod("frontend")
        + [_facts("leaf9", host_name="leaf9"), _facts("leaf8", host_name="leaf8")]
    )
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["frontend", "Unattached"]
    assert graph["fabrics"][1]["devices"] == 2


def test_a_fabric_that_has_streamed_no_lldp_yet_is_one_fabric():
    """Every node is cabled to nothing at startup; that is not a tab each."""
    graph = build_topology([_facts("leaf1", site="dc1"), _facts("leaf2", site="dc1")])
    assert [fabric["label"] for fabric in graph["fabrics"]] == ["dc1"]


def test_a_fabric_counts_the_clients_hanging_off_it():
    graph = build_topology([_leaf_with_a_client()])
    fabric = graph["fabrics"][0]
    assert (fabric["devices"], fabric["nodes"]) == (2, 3)


def test_a_node_carries_its_system_addresses_per_family():
    graph = build_topology(
        [
            _facts(
                "leaf1",
                host_name="leaf1",
                interfaces=(
                    {
                        "name": "system0",
                        "subinterface": [
                            {
                                "index": 0,
                                "ipv4": {"address": [{"ip-prefix": "10.0.0.1/32"}]},
                                "ipv6": {"address": [{"ip-prefix": "fd00::1/128"}]},
                            }
                        ],
                    },
                ),
            )
        ]
    )
    node = graph["nodes"][0]
    assert (node["system_ipv4"], node["system_ipv6"]) == ("10.0.0.1", "fd00::1")
    assert node["system_address"] == "10.0.0.1"
