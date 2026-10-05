"""The fabric topology, inferred from LLDP adjacencies and where services live.

Nothing tells fcli what a node is. The inventory may carry role labels or
nothing at all, and a label is a claim about intent rather than about what the
node runs. So the tier of a node is derived from the two things the fabric
itself reports: the services configured on it, and who its LLDP neighbours are.

* A node with mac-vrfs, and optionally ip-vrfs, is a **leaf**: the tier where a
  service meets a port.
* A node whose services carry two or more enabled ``bgp-vpn`` instances is a
  **DCGW**. The second instance is the WAN side of a stitched service, which
  only a gateway out of the DC has.
* A node with no mac-vrf and no ip-vrf that sees two or more leaves is a
  **spine**: it interconnects the leaves without terminating anything.
* Any other node without services is **core**: WAN P/PE routers and
  super-spines, which transit the fabric but attach to no leaf of it.

Below all of them sit the **clients**, which are not nodes of the inventory at
all but the far end of what a service is configured towards: a bridged
subinterface of a mac-vrf, or a routed port of an ip-vrf, on a port that faces
nothing else we know of. A client is identified by the name it advertises over
LLDP, or failing that by the ESI of the ethernet-segment its port is in, so that
a multi-homed one is a single box spanning its leaves.

Between the two sit the **ethernet-segments**. A multi-homed client does not
reach its leaves over a cable each but over one bundle, which is a configured
object in its own right: it has a name, an ESI that every leaf on it agrees on,
and it is where multi-homing goes wrong. So it is drawn as a tier of its own,
and the client hangs off the bundle rather than off each leaf.

The result is drawn one tier per layer, clients at the bottom and the WAN on top,
and is split into **fabrics**: nodes that share no cable with each other are not
one topology, however many clients happen to be plugged into both of them.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from ..checks import Finding
    from ..fabric import FabricState
    from ..incidents import Incident

from ..aliases import alias_index, resolve, tail
from ..connections.down_reason import STANDBY_STATE, is_intent, root_reason
from ..connections.layer2 import _df_candidates, _es_next_hops

#: The tiers of a fabric, bottom up, as ``(layer, role, label)``.
LAYERS: Tuple[Tuple[int, str, str], ...] = (
    (0, "client", "Clients"),
    (1, "segment", "Ethernet segments"),
    (2, "edge", "Edge / unclassified"),
    (3, "leaf", "Leaves"),
    (4, "spine", "Spines"),
    (5, "dcgw", "DC gateways"),
    (6, "core", "WAN / core"),
)

#: What an ethernet-segment node is named, so that it cannot collide with the
#: client on it - which, having nothing else to go by, is named after the ESI.
_SEGMENT_PREFIX = "es:"

#: Roles that share the layer holding whatever we could not place: nodes that
#: have told us nothing yet, and nodes we only know from someone's LLDP.
_EDGE_ROLES = ("unknown", "external")

#: Roles that are not a node of the fabric but something drawn from what a node
#: is configured towards, and that therefore cannot join two fabrics into one.
_ATTACHED_ROLES = ("client", "segment")

_LAYER_OF = {role: layer for layer, role, _ in LAYERS}
_LAYER_OF.update({role: _LAYER_OF["edge"] for role in _EDGE_ROLES})
_LAYER_LABEL = {layer: label for layer, _, label in LAYERS}

_UP_STATES = frozenset({"up", "enable", "enabled", "active"})
_DOWN_STATES = frozenset({"down", "disable", "disabled"})

#: Ports whose LLDP says nothing about the fabric. Every node of a lab hangs off
#: the same management bridge and sees all the others on it, which would draw a
#: full mesh over the topology and leave every node with the same neighbours.
_OUT_OF_BAND = ("mgmt",)

#: Interfaces a service binds that no client can be on. An IRB is the routed
#: side of a bridge domain, a loopback and ``system0`` are the node's own
#: addresses, and the management port is out of band.
_VIRTUAL_PORTS = ("irb", "lo", "system", "mgmt")


@dataclass
class Adjacency:
    """One LLDP neighbour, seen on one local port."""

    local_port: str
    #: The system-name the neighbour advertises, which is not the name the
    #: inventory knows it by.
    peer: str
    peer_port: str = ""
    oper_state: str = ""
    #: Not seen over LLDP now, but seen before: a cable that went down takes
    #: its LLDP adjacency with it, and a drawing that dropped it would hide
    #: the one link that matters.
    lost: bool = False


@dataclass
class Segment:
    """An ethernet-segment: one cable bundle, seen from each node it lands on.

    The ESI is the same on every node a multi-homed client is attached to, and
    for a client that runs no LLDP it is the only thing that says the two lags
    are one host rather than two.
    """

    name: str
    esi: str


@dataclass
class VirtualSegment:
    """A virtual ethernet-segment: no port, a next-hop tracked in a routed service.

    It is what L3 aliasing is built on. The leaves that can reach the
    next-hop advertise the segment, and a remote VTEP that learns a prefix
    behind it load-balances over all of them rather than only the one that
    advertised the prefix.
    """

    name: str
    esi: str
    mode: str = ""
    oper: str = ""
    #: Each tracked next-hop and the EVIs configured under it.
    next_hops: List[Tuple[str, List[str]]] = field(default_factory=list)
    #: Per network-instance it is associated with: the DF candidates (system
    #: addresses) and the one elected.
    associations: Dict[str, Tuple[List[str], str]] = field(default_factory=dict)
    #: Each next-hop, with the bridge domain whose IRB subnet it is in - how
    #: the next-hop is reached - where one is.
    via: Dict[str, str] = field(default_factory=dict)


@dataclass
class Attachment:
    """One client-facing subinterface: where a service meets a customer port."""

    subinterface: str
    #: The port the subinterface is on. LLDP reports a neighbour against the
    #: port, and it is what makes several vlans of one cable a single client.
    port: str
    #: The network-instance the subinterface is bound to.
    service: str
    #: ``bridged`` for a member of a mac-vrf, ``routed`` for one of an ip-vrf.
    kind: str
    vlan: str = ""
    ip: str = ""
    oper_state: str = ""


@dataclass
class NodeFacts:
    """What one node contributes to the topology."""

    name: str
    hostname: str = ""
    system_name: str = ""
    #: Chassis type from ``/platform/chassis``, the same field the sys-info
    #: report shows as ``type``.
    platform: str = ""
    site: str = ""
    mac_vrfs: int = 0
    ip_vrfs: int = 0
    #: Services with two or more enabled ``bgp-vpn`` instances: the DCGW mark.
    stitched: int = 0
    #: Whether any network-instance state arrived at all, which is what
    #: separates 'runs no services' from 'has not told us anything yet'.
    has_state: bool = False
    connected: bool = True
    error: Optional[str] = None
    adjacencies: List[Adjacency] = field(default_factory=list)
    #: The service-carrying subinterfaces, before the fabric-facing ones are
    #: taken out of them - which needs every node's LLDP, so it happens in
    #: :func:`build_topology` rather than here.
    attachments: List[Attachment] = field(default_factory=list)
    #: The ethernet-segment on each port that is in one.
    segments: Dict[str, Segment] = field(default_factory=dict)
    #: Egress of each interface, in bits per second, from streamed counters.
    egress: Dict[str, int] = field(default_factory=dict)
    #: The mac-vrfs and ip-vrfs it carries, by name: what a service overlay
    #: lights the node up for.
    services: List[str] = field(default_factory=list)
    #: Its virtual ethernet-segments, and the address it is a VTEP on - what
    #: a segment's DF candidates are named by.
    virtual_segments: List[VirtualSegment] = field(default_factory=list)
    system_address: str = ""

    @property
    def label(self) -> str:
        """The short name to draw the node under."""
        return self.system_name or self.name


def node_facts(
    name: str,
    *,
    hostname: str = "",
    labels: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    connected: bool = True,
    error: Optional[str] = None,
    egress: Optional[Dict[str, int]] = None,
    remembered: Optional[Dict[str, Tuple[str, str]]] = None,
) -> NodeFacts:
    """Read one node's contribution out of its streamed state.

    *snapshot* is the node's state as far as the topology report's paths
    reach, as :meth:`~nornir_srl.server.stream.HostStream.snapshot_paths`
    returns it.
    A node with nothing streamed yet yields facts that classify as ``unknown``
    rather than as a node without services.
    """
    labels = labels or {}
    snapshot = snapshot or {}
    system = _branch(snapshot, "system")
    facts = NodeFacts(
        name=name,
        hostname=hostname or name,
        system_name=str(_branch(system, "name").get("host-name") or ""),
        platform=_chassis_type(snapshot),
        site=str(labels.get("site") or ""),
        connected=connected,
        error=error,
        egress=dict(egress or {}),
    )

    instances = snapshot.get("network-instance")
    if isinstance(instances, list) and instances:
        facts.has_state = True
        details = _subinterface_details(snapshot)
        port_reasons = _port_reasons(snapshot)
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            ni_type = _norm(instance.get("type"))
            if ni_type == "mac-vrf":
                facts.mac_vrfs += 1
                kind = "bridged"
            elif ni_type in ("ip-vrf", "vrf") and str(instance.get("name", "")) != "mgmt":
                facts.ip_vrfs += 1
                kind = "routed"
            else:
                continue
            facts.services.append(str(instance.get("name", "")))
            facts.attachments.extend(
                _attachments(instance, kind, details, port_reasons)
            )
            if _is_stitched(instance):
                facts.stitched += 1

    details = _subinterface_details(snapshot)
    facts.system_address = _first_prefix(details.get("system0.0", {})).split("/", 1)[0]
    facts.virtual_segments = _virtual_segments(system, instances if isinstance(instances, list) else [], details)
    itf_states = _interface_states(snapshot)
    facts.adjacencies = _adjacencies(system, itf_states)
    # *remembered* is every cable seen on a port before, as (peer, peer port).
    # One LLDP no longer reports is drawn all the same, in the state its port
    # is in now - which, for a cable that stopped carrying LLDP, is usually
    # the reason it did.
    seen = {adj.local_port for adj in facts.adjacencies}
    for port, (peer, peer_port) in sorted((remembered or {}).items()):
        if port not in seen and not _out_of_band(port):
            facts.adjacencies.append(
                Adjacency(
                    local_port=port,
                    peer=peer,
                    peer_port=peer_port,
                    oper_state=itf_states.get(port, "down") or "down",
                    lost=True,
                )
            )
    facts.segments = _segments(system)
    return facts


def build_topology(facts: Iterable[NodeFacts]) -> Dict[str, Any]:
    """Turn per-node facts into the nodes, links and layers of the fabric."""
    nodes = list(facts)
    aliases = _alias_index(nodes)

    peers: Dict[str, Set[str]] = {f.name: set() for f in nodes}
    #: Neighbours that match no node of the inventory, by advertised name.
    outside: Dict[str, Set[str]] = {}
    links: Dict[Tuple[str, str], Dict[str, Any]] = {}
    #: The local ports that face a node of the inventory: the fabric itself.
    fabric_ports: Dict[str, Set[str]] = {f.name: set() for f in nodes}
    #: What an unmatched neighbour calls itself, per port it was seen on.
    outside_ports: Dict[Tuple[str, str], str] = {}

    for node in nodes:
        for adj in node.adjacencies:
            target = _resolve(adj.peer, aliases)
            if target == node.name:  # a neighbour on our own name is a loop
                continue
            if target is None:
                target = adj.peer
                outside.setdefault(target, set()).add(node.name)
                outside_ports[(node.name, adj.local_port)] = target
            else:
                fabric_ports[node.name].add(adj.local_port)
                peers[node.name].add(target)
                peers.setdefault(target, set()).add(node.name)
            _record_link(links, node.name, target, adj)

    # Roles are settled before the clients are added, so that hanging one off a
    # node can never be what decides which tier the node itself is on.
    roles = _classify(nodes, peers)
    clients = _clients(nodes, fabric_ports, outside_ports)
    segments = _segment_nodes(clients)
    for client in clients:
        #: The state of every port of a bundle, per ethernet-segment node.
        bundles: Dict[str, List[str]] = {}
        for attachment in client["attachments"]:
            # A port that is in an ethernet-segment is cabled to the segment
            # rather than to the client, and the segment down to the client.
            near = _segment_key(attachment["esi"]) if attachment["esi"] else client["name"]
            peers[attachment["node"]].add(near)
            _record_access_link(links, near, attachment)
            if attachment["esi"]:
                bundles.setdefault(near, []).append(attachment["state"])
        for name, states in sorted(bundles.items()):
            _record_bundle_link(links, name, client["name"], states)
        # A client that named itself over LLDP is no longer a stray neighbour.
        outside.pop(client["name"], None)

    virtual = _virtual_segment_nodes(nodes, clients, links)
    counts = _client_counts(clients)
    payload = [
        _node_payload(node, roles[node.name], sorted(peers[node.name]), counts.get(node.name, 0))
        for node in nodes
    ]
    payload.extend(
        _external_payload(name, sorted(seen_by)) for name, seen_by in sorted(outside.items())
    )
    payload.extend(segments)
    payload.extend(virtual)
    payload.extend(clients)
    payload.sort(key=lambda n: (-n["layer"], n["site"], _row_order(n), n["label"]))
    layer_of = {node["name"]: node["layer"] for node in payload}
    egress = {node.name: node.egress for node in nodes}
    cables = [_link_payload(link, layer_of, egress) for _key, link in sorted(links.items())]

    return {
        "nodes": payload,
        "links": cables,
        # Top of the drawing first, and only the tiers this fabric actually has.
        "layers": _layers(payload),
        "roles": _role_counts(payload),
        "fabrics": _assign_fabrics(payload, cables),
        "sites": sorted({n["site"] for n in payload if n["site"]}),
        "unresolved": [
            {"peer": name, "seen_by": sorted(seen_by)} for name, seen_by in sorted(outside.items())
        ],
    }


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def _classify(nodes: List[NodeFacts], peers: Dict[str, Set[str]]) -> Dict[str, str]:
    """Assign a role to every node, services first and adjacency second."""
    roles: Dict[str, str] = {}
    for node in nodes:
        if node.mac_vrfs or node.ip_vrfs:
            roles[node.name] = "dcgw" if node.stitched else "leaf"
        elif not node.has_state:
            roles[node.name] = "unknown"

    leaves = {name for name, role in roles.items() if role == "leaf"}
    gateways = {name for name, role in roles.items() if role == "dcgw"}
    for node in nodes:
        if node.name in roles:
            continue
        attached = peers.get(node.name, set())
        leaf_peers = len(attached & leaves)
        # Two leaves is what a spine is for: interconnecting them. A single leaf
        # counts as well, for a fabric small enough to have only one, but not
        # when the node also faces a gateway - that is a WAN router hanging off
        # the fabric rather than a spine inside it.
        if leaf_peers >= 2 or (leaf_peers == 1 and not attached & gateways):
            roles[node.name] = "spine"
        else:
            roles[node.name] = "core"
    return roles


def _node_payload(node: NodeFacts, role: str, peers: List[str], clients: int) -> Dict[str, Any]:
    return {
        "name": node.name,
        "label": node.label,
        "role": role,
        "layer": _LAYER_OF.get(role, _LAYER_OF["edge"]),
        "platform": node.platform,
        "site": node.site,
        "mac_vrfs": node.mac_vrfs,
        "ip_vrfs": node.ip_vrfs,
        "stitched": node.stitched,
        "clients": clients,
        "services": sorted(set(node.services)),
        "system_address": node.system_address,
        "peers": peers,
        "ports": len(node.adjacencies),
        "connected": node.connected,
        "error": node.error,
        "external": False,
        "attachments": [],
    }


def _external_payload(name: str, seen_by: List[str]) -> Dict[str, Any]:
    """A node we only know because a neighbour of ours advertises it."""
    return {
        "name": name,
        "label": name,
        "role": "external",
        "layer": _LAYER_OF["external"],
        "site": "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": 0,
        "peers": seen_by,
        "ports": len(seen_by),
        "connected": True,
        "error": None,
        "external": True,
        "attachments": [],
    }


def _row_order(node: Dict[str, Any]) -> str:
    """Clients and segments are drawn under a node they attach to, not by name."""
    if node["attachments"]:
        return min(attachment["node"] for attachment in node["attachments"])
    return ""


def _layers(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The occupied tiers, top of the drawing first."""
    result = []
    for layer in sorted({n["layer"] for n in nodes}, reverse=True):
        members = [n["name"] for n in nodes if n["layer"] == layer]
        result.append(
            {
                "index": layer,
                "label": _LAYER_LABEL.get(layer, f"Layer {layer}"),
                "nodes": members,
            }
        )
    return result


def _role_counts(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for node in nodes:
        counts[node["role"]] = counts.get(node["role"], 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# fabrics
# --------------------------------------------------------------------------- #


def _assign_fabrics(
    nodes: List[Dict[str, Any]], links: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Split the drawing into the fabrics that share no cable between nodes.

    Two nodes that meet only through a client are not one fabric. A server
    plugged into two pods, or into a DC and the one beside it, says nothing
    about the underlay of either, and drawing them as one topology claims a
    path across that does not exist. So the split is made on the cables between
    nodes alone, and a client is then drawn in every fabric it attaches to.

    A node with no such cable at all - one whose LLDP has not arrived yet, or
    one genuinely patched to nothing - is not a fabric of its own; those are
    gathered into a single group. Unless there are no cables anywhere, in which
    case they are the fabric rather than the leftovers of one, which is how a
    freshly started server draws one topology instead of one per node.

    Stamps ``fabrics`` on every node and returns the groups, largest first.
    """
    adjacency: Dict[str, Set[str]] = {
        node["name"]: set() for node in nodes if node["role"] not in _ATTACHED_ROLES
    }
    for link in links:
        if link["access"]:
            continue
        if link["a"] in adjacency and link["b"] in adjacency:
            adjacency[link["a"]].add(link["b"])
            adjacency[link["b"]].add(link["a"])

    components = _components(adjacency)
    cabled = sorted(
        (members for members in components if len(members) > 1),
        key=lambda members: (-len(members), members[0]),
    )
    loose = sorted(name for members in components if len(members) == 1 for name in members)
    groups: List[Tuple[List[str], bool]] = [(members, False) for members in cabled]
    if loose:
        groups.append((loose, bool(cabled)))

    fabric_of = {name: members[0] for members, _loose in groups for name in members}
    rank_of = {members[0]: rank for rank, (members, _loose) in enumerate(groups)}
    for node in nodes:
        if node["role"] in _ATTACHED_ROLES:
            found = {fabric_of[a["node"]] for a in node["attachments"] if a["node"] in fabric_of}
        else:
            found = {fabric_of[node["name"]]}
        node["fabrics"] = sorted(found, key=lambda fabric: rank_of[fabric])

    counts: Dict[str, int] = {}
    for node in nodes:
        for fabric in node["fabrics"]:
            counts[fabric] = counts.get(fabric, 0) + 1
    labels = _fabric_labels(groups, {node["name"]: node for node in nodes})
    return [
        {
            "id": members[0],
            "label": label,
            "nodes": counts.get(members[0], 0),
            "devices": len(members),
        }
        for (members, _loose), label in zip(groups, labels)
    ]


def _components(adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    """The connected components of *adjacency*, each sorted by name."""
    seen: Set[str] = set()
    result = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        members: Set[str] = set()
        stack = [start]
        while stack:
            name = stack.pop()
            if name in members:
                continue
            members.add(name)
            seen.add(name)
            stack.extend(adjacency[name] - members)
        result.append(sorted(members))
    return result


def _fabric_labels(
    groups: List[Tuple[List[str], bool]], by_name: Dict[str, Dict[str, Any]]
) -> List[str]:
    """Name every fabric the same way, or none of them that way.

    A fabric is best named after itself: the site its nodes are labelled with,
    or the name they share, says what ``Fabric 2`` cannot. But a scheme that
    fits one fabric and not the next reads as though the two were different
    kinds of thing - ``frontend`` beside ``Fabric 1`` looks like a name beside a
    placeholder - so a scheme is only used when it names every fabric of the
    drawing, and distinctly. Failing that they are numbered, largest first.
    """
    cabled = [members for members, loose in groups if not loose]
    for naming in (_fabric_site, _fabric_shared_name):
        names = [naming(members, by_name) for members in cabled]
        if all(names) and len(set(names)) == len(names):
            found = iter(names)
            return ["Unattached" if loose else next(found) for _members, loose in groups]
    return [
        "Unattached" if loose else f"Fabric {rank}"
        for rank, (_members, loose) in enumerate(groups, start=1)
    ]


def _fabric_site(members: List[str], by_name: Dict[str, Dict[str, Any]]) -> str:
    """The site every node of the fabric is labelled with, if they agree on one."""
    sites = {by_name[name]["site"] for name in members}
    return next(iter(sites)) if len(sites) == 1 else ""


def _fabric_shared_name(members: List[str], by_name: Dict[str, Dict[str, Any]]) -> str:
    """The dash-separated head every node shares: ``frontend-leaf1`` is ``frontend``.

    Only a head shorter than the shortest name counts, so nodes that agree on
    their whole name do not name the fabric after themselves.
    """
    parts = [by_name[name]["label"].split("-") for name in members]
    shared: List[str] = []
    for index in range(min(len(name) for name in parts) - 1):
        segment = parts[0][index]
        if any(name[index] != segment for name in parts):
            break
        shared.append(segment)
    return "-".join(shared)


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #


def _clients(
    nodes: List[NodeFacts],
    fabric_ports: Dict[str, Set[str]],
    outside_ports: Dict[Tuple[str, str], str],
) -> List[Dict[str, Any]]:
    """The clients hanging off the fabric, as nodes of their own.

    An attachment on a port that faces a node of the inventory is not a client
    but a fabric link that happens to carry a service - the WAN subinterface of
    a stitched ip-vrf on a DCGW is the obvious one - so those are left out.

    What remains is grouped per client rather than per subinterface, and several
    vlans of one cable are one client. Which ports belong to the same client is
    answered by whichever of these the fabric can tell us, in that order:

    * the name an unmatched LLDP neighbour advertises, because a client that
      says who it is says the same thing to every leaf it is attached to;
    * the ESI of the ethernet-segment the port is in, which is how a multi-homed
      client that runs no LLDP is still drawn as one box rather than one per
      leaf;
    * the port itself, when there is nothing else to go on.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        for attachment in node.attachments:
            if attachment.port in fabric_ports.get(node.name, ()):
                continue
            advertised = outside_ports.get((node.name, attachment.port))
            segment = node.segments.get(attachment.port)
            name = advertised or (segment.esi if segment else "") or f"{node.name}:{attachment.port}"
            client = grouped.setdefault(
                name,
                {
                    "name": name,
                    "advertised": advertised or "",
                    "sites": set(),
                    "attachments": [],
                },
            )
            client["sites"].add(node.site)
            client["attachments"].append(
                {
                    "node": node.name,
                    "site": node.site,
                    "port": attachment.port,
                    "subinterface": attachment.subinterface,
                    "service": attachment.service,
                    "kind": attachment.kind,
                    "vlan": attachment.vlan,
                    "ip": attachment.ip,
                    "state": attachment.oper_state,
                    "esi": segment.esi if segment else "",
                    "segment": segment.name if segment else "",
                }
            )
    return [_client_payload(client) for _name, client in sorted(grouped.items())]


def _client_payload(client: Dict[str, Any]) -> Dict[str, Any]:
    attachments = sorted(
        client["attachments"], key=lambda a: (a["node"], a["port"], a["subinterface"])
    )
    sites = {site for site in client["sites"] if site}
    esis = {a["esi"] for a in attachments if a["esi"]}
    return {
        "name": client["name"],
        # Every client box reads the same. What a client is called is inferred
        # from an ESI or a port name, which reads like an identity it does not
        # have; where it is plugged in and what it carries is in the panel.
        "label": "client",
        #: The name it advertised over LLDP, on the rare occasion it runs any.
        "advertised": client["advertised"],
        "role": "client",
        "layer": _LAYER_OF["client"],
        # A client of one site is drawn with it; one spanning two belongs to
        # neither, and grouping it under either would be a claim we cannot make.
        "site": sites.pop() if len(sites) == 1 else "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": 0,
        # A port in an ethernet-segment reaches the client through it.
        "peers": sorted(
            {_segment_key(a["esi"]) if a["esi"] else a["node"] for a in attachments}
        ),
        "ports": len({(a["node"], a["port"]) for a in attachments}),
        "connected": True,
        "error": None,
        "external": False,
        "attachments": attachments,
        "services": sorted({a["service"] for a in attachments if a["service"]}),
        "esi": esis.pop() if len(esis) == 1 else "",
    }


def _client_counts(clients: List[Dict[str, Any]]) -> Dict[str, int]:
    """How many clients hang off each node, segment in between or not."""
    counts: Dict[str, int] = {}
    for client in clients:
        for name in {a["node"] for a in client["attachments"]}:
            counts[name] = counts.get(name, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# ethernet-segments
# --------------------------------------------------------------------------- #


def _segment_key(esi: str) -> str:
    """The node name of the ethernet-segment with this ESI."""
    return f"{_SEGMENT_PREFIX}{esi}"


def _segment_nodes(clients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ethernet-segments the clients are attached over, as nodes of their own.

    Keyed on the ESI rather than on the name, because the ESI is the only thing
    the leaves of one segment are made to agree on: each configures a name of
    its own, and two leaves that disagree on it are still one segment.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for client in clients:
        for attachment in client["attachments"]:
            if not attachment["esi"]:
                continue
            segment = grouped.setdefault(
                attachment["esi"],
                {
                    "esi": attachment["esi"],
                    "names": set(),
                    "sites": set(),
                    "clients": set(),
                    "attachments": [],
                },
            )
            segment["names"].add(attachment["segment"])
            segment["sites"].add(attachment["site"])
            segment["clients"].add(client["name"])
            segment["attachments"].append(attachment)
    return [_segment_payload(segment) for _esi, segment in sorted(grouped.items())]


def _segment_payload(segment: Dict[str, Any]) -> Dict[str, Any]:
    attachments = sorted(
        segment["attachments"], key=lambda a: (a["node"], a["port"], a["subinterface"])
    )
    names = {name for name in segment["names"] if name}
    sites = {site for site in segment["sites"] if site}
    clients = sorted(segment["clients"])
    return {
        "name": _segment_key(segment["esi"]),
        "label": _segment_label(segment["esi"]),
        #: The name each leaf configured it under, which they may disagree on.
        "names": sorted(names),
        "role": "segment",
        "layer": _LAYER_OF["segment"],
        "site": sites.pop() if len(sites) == 1 else "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": len(clients),
        "peers": sorted({a["node"] for a in attachments} | set(clients)),
        "ports": len({(a["node"], a["port"]) for a in attachments}),
        "connected": True,
        "error": None,
        "external": False,
        "attachments": attachments,
        "services": sorted({a["service"] for a in attachments if a["service"]}),
        "esi": segment["esi"],
    }


def _segment_label(esi: str) -> str:
    """What an ethernet-segment box reads: ``ES`` and the tail of its ESI.

    The tail rather than the name, because the name is configured per leaf and
    nothing makes the two leaves of one segment agree on it, while the ESI is
    the one thing they are made to agree on. The last two bytes are what tells
    the segments of a fabric apart; the rest of an ESI is shared boilerplate.
    """
    return f"ES {':'.join(esi.split(':')[-2:])}"


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def _record_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    node: str,
    target: str,
    adj: Adjacency,
) -> None:
    """Add one adjacency to the link it belongs to.

    Both ends of a cable report it, so links and the ports of a link are keyed
    on the pair rather than appended, and a fabric of n cables stays n links
    however many nodes are streaming.
    """
    key = (node, target) if node <= target else (target, node)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    if node == key[0]:
        pair = (adj.local_port, adj.peer_port)
    else:
        pair = (adj.peer_port, adj.local_port)
    fresh = pair not in link["ports"]
    port = link["ports"].setdefault(pair, {"a_port": pair[0], "b_port": pair[1]})
    # Both ends have to have lost it: a cable one end still hears LLDP on is
    # one-sided, which is a finding of its own, not a lost cable.
    if adj.lost and fresh:
        port["lost"] = True
    elif not adj.lost:
        port.pop("lost", None)
    if adj.oper_state:
        link["states"].add(adj.oper_state)


def _record_access_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    client: str,
    attachment: Dict[str, Any],
) -> None:
    """Add the cable from a node to a client hanging off one of its ports.

    A client that advertises itself over LLDP already has this cable, reported
    from the fabric side with both port names on it, so the port is only added
    when the service is all we have to go on.
    """
    node = attachment["node"]
    key = (node, client) if node <= client else (client, node)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    link["access"] = True
    side = 0 if key[0] == node else 1
    if not any(ports[side] == attachment["port"] for ports in link["ports"]):
        pair = (attachment["port"], "") if side == 0 else ("", attachment["port"])
        link["ports"][pair] = {"a_port": pair[0], "b_port": pair[1]}
    if attachment["state"]:
        link["states"].add(attachment["state"])


def _record_bundle_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    segment: str,
    client: str,
    states: Iterable[str],
) -> None:
    """The cable from an ethernet-segment down to the client on it.

    It stands for the bundle as a whole rather than for any one port of it, so
    it carries no port names: those are on the links from the leaves above.
    """
    key = (segment, client) if segment <= client else (client, segment)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    link["access"] = True
    link["states"].update(state for state in states if state)


def _link_payload(
    link: Dict[str, Any],
    layer_of: Dict[str, int],
    egress: Dict[str, Dict[str, int]],
) -> Dict[str, Any]:
    # A port an ethernet-segment holds in standby says nothing about the cable
    # it is on: the bundle down to a multi-homed client is carried by whichever
    # leaf is forwarding, so letting the standby leaf's port vote would draw
    # that client as unreachable. A cable with nothing but standby on it is
    # standby itself.
    states = {state for state in link["states"] if state != STANDBY_STATE}
    if any(state in _DOWN_STATES for state in states):
        state = "down"
    elif states and all(state in _UP_STATES for state in states):
        state = "up"
    elif not states and link["states"]:
        state = STANDBY_STATE
    else:
        state = "unknown"
    a_rates: List[int] = []
    b_rates: List[int] = []
    ports = []
    for pair in sorted(link["ports"]):
        port = dict(link["ports"][pair])
        a_bps = _port_rate(egress, link["a"], port.get("a_port") or "")
        b_bps = _port_rate(egress, link["b"], port.get("b_port") or "")
        if a_bps is not None:
            port["a_out_bps"] = a_bps
            a_rates.append(a_bps)
        if b_bps is not None:
            port["b_out_bps"] = b_bps
            b_rates.append(b_bps)
        ports.append(port)
    payload = {
        "a": link["a"],
        "b": link["b"],
        "count": len(ports),
        "ports": ports,
        "state": state,
        # Set for the links inside one tier: a DCGW mesh, a pair of spines.
        "intra_layer": layer_of.get(link["a"]) == layer_of.get(link["b"]),
        # Set for a cable to a client rather than to another node of the fabric.
        "access": bool(link.get("access")),
        # Set when LLDP no longer reports any cable of it, and it is drawn
        # from what was seen before.
        "lost": bool(ports) and all(p.get("lost") for p in ports),
    }
    # A virtual segment's links are not cables: what they are, what to say
    # about them, and the service overlays they are drawn in.
    for key in ("kind", "note", "overlay_only", "df"):
        if link.get(key):
            payload[key] = link[key]
    # Each end is coloured from the hottest interface on that side of the cable.
    if a_rates:
        payload["a_out_bps"] = max(a_rates)
    if b_rates:
        payload["b_out_bps"] = max(b_rates)
    return payload


def _port_rate(egress: Dict[str, Dict[str, int]], node: str, port: str) -> Optional[int]:
    """The egress of *port* on *node*, if that interface has a derived rate."""
    if not port:
        return None
    rates = egress.get(node) or {}
    return rates[port] if port in rates else None


# --------------------------------------------------------------------------- #
# name resolution
# --------------------------------------------------------------------------- #


def _alias_index(nodes: List[NodeFacts]) -> Dict[str, str]:
    """Map every name a node may be known by onto its inventory name."""
    return alias_index(
        [(node.name, node.hostname, node.system_name) for node in nodes]
    )


_resolve = resolve
_tail = tail


# --------------------------------------------------------------------------- #
# state parsing
# --------------------------------------------------------------------------- #


def _adjacencies(system: Dict[str, Any], itf_states: Dict[str, str]) -> List[Adjacency]:
    result = []
    for itf in _as_list(_branch(system, "lldp").get("interface")):
        local = str(itf.get("name", ""))
        if _out_of_band(local):
            continue
        for neighbor in _as_list(itf.get("neighbor")):
            peer = str(neighbor.get("system-name") or "").strip()
            peer_port = str(neighbor.get("port-id") or "")
            if not peer or _out_of_band(peer_port):
                continue
            result.append(
                Adjacency(
                    local_port=local,
                    peer=peer,
                    peer_port=peer_port,
                    oper_state=itf_states.get(local, ""),
                )
            )
    return result


def _out_of_band(port: str) -> bool:
    return port.lower().startswith(_OUT_OF_BAND)


def _segments(system: Dict[str, Any]) -> Dict[str, Segment]:
    """The ethernet-segments of a node, by the port each one is on."""
    evpn = _branch(system, "network-instance", "protocols", "evpn", "ethernet-segments")
    result: Dict[str, Segment] = {}
    for instance in _as_list(evpn.get("bgp-instance")):
        for segment in _as_list(instance.get("ethernet-segment")):
            esi = str(segment.get("esi") or "")
            if not esi:
                continue
            for itf in _as_list(segment.get("interface")):
                port = str(itf.get("ethernet-interface") or "")
                if port:
                    result[port] = Segment(name=str(segment.get("name") or ""), esi=esi)
    return result


def _attachments(
    instance: Dict[str, Any],
    kind: str,
    details: Dict[str, Dict[str, Any]],
    port_reasons: Optional[Dict[str, str]] = None,
) -> List[Attachment]:
    """The subinterfaces a service binds, minus the ones no client can be on."""
    service = str(instance.get("name", ""))
    result = []
    for member in _as_list(instance.get("interface")):
        name = str(member.get("name", ""))
        port = name.rsplit(".", 1)[0]
        if not port or port.lower().startswith(_VIRTUAL_PORTS):
            continue
        detail = details.get(name, {})
        result.append(
            Attachment(
                subinterface=name,
                port=port,
                service=service,
                kind=kind,
                vlan=_vlan(detail),
                ip=_first_prefix(detail),
                # The service's own view of the subinterface first: a member of
                # a disabled mac-vrf reads up under /interface and down here.
                # A member whose port is only in standby is called that, so the
                # cable to a multi-homed client is not drawn from the leaf that
                # is not forwarding.
                oper_state=_member_state(member, detail, port_reasons, port),
            )
        )
    return result


def _member_state(
    member: Dict[str, Any],
    detail: Dict[str, Any],
    port_reasons: Optional[Dict[str, str]],
    port: str,
) -> str:
    """The state of a service member, with an intentional down called ``down/standby``."""
    state = _norm(member.get("oper-state")) or _norm(detail.get("oper-state"))
    if state != "down":
        return state
    reason = root_reason(
        member.get("oper-down-reason"),
        detail.get("oper-down-reason"),
        (port_reasons or {}).get(port, ""),
    )
    return STANDBY_STATE if is_intent(reason) else state


def _subinterface_details(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map ``<port>.<index>`` to what ``/interface`` knows about it.

    The network-instance names the subinterfaces bound to it and nothing more,
    so the vlan and the address a client attaches on come from the other tree.
    """
    details: Dict[str, Dict[str, Any]] = {}
    for itf in _as_list(snapshot.get("interface")):
        if not itf.get("name"):
            continue
        for subinterface in _as_list(itf.get("subinterface")):
            index = subinterface.get("index", subinterface.get("name"))
            if index is None:
                continue
            details[f"{itf['name']}.{index}"] = subinterface
    return details


def _vlan(detail: Dict[str, Any]) -> str:
    """The vlan a subinterface is tagged with, as it would be written down."""
    encap = _branch(detail, "vlan", "encap")
    tagged = encap.get("single-tagged")
    if isinstance(tagged, dict) and tagged.get("vlan-id") not in (None, ""):
        return str(tagged["vlan-id"])
    if "untagged" in encap:
        return "untagged"
    return ""


def _first_prefix(detail: Dict[str, Any]) -> str:
    """The first address of a routed subinterface, v4 before v6."""
    for family in ("ipv4", "ipv6"):
        for address in _as_list(_branch(detail, family).get("address")):
            prefix = address.get("ip-prefix")
            if prefix:
                return str(prefix)
    return ""


def _interface_states(snapshot: Dict[str, Any]) -> Dict[str, str]:
    states = {}
    for itf in _as_list(snapshot.get("interface")):
        if not itf.get("name"):
            continue
        state = _norm(itf.get("oper-state"))
        if state == "down" and is_intent(itf.get("oper-down-reason")):
            state = STANDBY_STATE
        if state:
            states[str(itf["name"])] = state
    return states


def _port_reasons(snapshot: Dict[str, Any]) -> Dict[str, str]:
    """Map port name -> its own ``oper-down-reason``, for the ports that have one.

    A subinterface only ever says ``port-down`` about its port; this is what
    that resolves against.
    """
    reasons: Dict[str, str] = {}
    for itf in _as_list(snapshot.get("interface")):
        name = itf.get("name")
        reason = _norm(itf.get("oper-down-reason"))
        if name and reason:
            reasons[str(name)] = reason
    return reasons


def _is_stitched(instance: Dict[str, Any]) -> bool:
    """Whether a service is stitched: two enabled bgp-vpn instances with RTs.

    The same rule the service reports use to mark a Gateway, so the topology and
    the Bridge Domains and Routers tables cannot disagree on what a DCGW is.
    """
    enabled = 0
    for bgp_instance in _as_list(_branch(instance, "protocols", "bgp-vpn").get("bgp-instance")):
        if _norm(bgp_instance.get("admin-state")) in _DOWN_STATES:
            continue
        if _has_route_target(bgp_instance):
            enabled += 1
    return enabled >= 2


def _has_route_target(bgp_instance: Dict[str, Any]) -> bool:
    config = bgp_instance.get("route-target")
    if not isinstance(config, dict):
        return False
    for key in ("import-rt", "export-rt"):
        raw = config.get(key)
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            target = item.get("target") if isinstance(item, dict) else item
            if target:
                return True
    return False


def _as_list(value: Any) -> List[Dict[str, Any]]:
    """A yang list as a list of its entries.

    gNMI returns a list of one as the entry itself, and a container that is not
    there at all as nothing, so every list has to be read through this.
    """
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _branch(node: Any, *names: str) -> Dict[str, Any]:
    """Descend through nested containers, yielding ``{}`` at the first miss."""
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _chassis_type(snapshot: Dict[str, Any]) -> str:
    """The chassis type the sys-info report shows, or empty if none streamed."""
    entries = _as_list(_branch(snapshot, "platform").get("chassis"))
    return str(entries[0].get("type") or "") if entries else ""


def _norm(value: Any) -> str:
    """Normalize a YANG enum leaf to its bare, lower-case value."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]


# --------------------------------------------------------------------------- #
# health: what the checks found, drawn onto the fabric
# --------------------------------------------------------------------------- #

_SEVERITY_RANK = {"error": 2, "warning": 1}

#: Findings listed per node in the drawing; the count on its badge is all of them.
_MAX_ISSUES = 50


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def _finding_payload(finding: Any, acked: bool = False) -> Dict[str, Any]:
    return {
        "severity": finding.severity,
        "check": finding.check,
        "node": finding.node,
        "subject": finding.subject,
        "detail": finding.detail,
        "acknowledged": acked,
    }


def annotate_health(
    graph: Dict[str, Any],
    located: Iterable[Tuple[Any, Optional[str]]],
    incidents: Iterable[Any],
    acked: Optional[Set[Tuple[str, str, str]]] = None,
) -> Dict[str, Any]:
    """Put each finding on the node and the cable it is about.

    *located* is every finding with the port it is on, where it is on one
    (:func:`nornir_srl.incidents.locate`). A node gets the count of its
    findings by severity and the worst of them as its ``health``; a cable
    gets the findings on either of its ends. The incidents are listed as
    they are, for the drawing to say what the colours add up to.

    A finding in *acked* - one someone acknowledged - is still listed, marked
    so, but no longer counts towards a badge or a colour: those are there to
    say *look here*, and it has been looked at.
    """
    acked = acked or set()
    by_name = {node["name"]: node for node in graph.get("nodes", [])}
    for node in by_name.values():
        node["findings"] = {"error": 0, "warning": 0}
        node["issues"] = []
        node["health"] = "ok" if not node.get("external") and node.get("role") not in ("client", "segment") else ""
    ends: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for link in graph.get("links", []):
        link["findings"] = []
        link["health"] = "ok"
        for pair in link.get("ports", []):
            for side in ("a", "b"):
                port = pair.get(f"{side}_port")
                if port:
                    ends.setdefault((link[side], port), []).append(link)
    # A segment's findings are about the segment as much as about the node
    # that reported them, and a virtual one is drawn as a node of its own.
    segments = {
        name: node for node in by_name.values() if node.get("virtual") for name in [node.get("esi")] + node.get("names", [])
    }
    for finding, port in located:
        is_acked = (finding.check, finding.node, finding.subject) in acked
        segment = segments.get(finding.subject.split("/", 1)[0]) if finding.check == "es_df" else None
        if segment is not None:
            if len(segment["issues"]) < _MAX_ISSUES:
                segment["issues"].append(_finding_payload(finding, is_acked))
            if not is_acked:
                segment["findings"][finding.severity] = segment["findings"].get(finding.severity, 0) + 1
                segment["health"] = _worse(finding.severity, segment["health"] or "ok")
        node = by_name.get(finding.node)
        if node is not None:
            if len(node["issues"]) < _MAX_ISSUES:
                node["issues"].append(_finding_payload(finding, is_acked))
            if not is_acked:
                counts = node["findings"]
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
                node["health"] = _worse(finding.severity, node["health"])
        if port is None:
            continue
        for link in ends.get((finding.node, port), []):
            link["findings"].append(_finding_payload(finding, is_acked))
            if not is_acked:
                link["health"] = _worse(finding.severity, link["health"])
    graph["incidents"] = [
        {
            "id": incident.id,
            "severity": incident.severity,
            "kind": incident.kind,
            "title": incident.title,
            "explanation": incident.explanation,
            "node": incident.node,
            "nodes": list(incident.nodes),
            "findings": len(incident.findings),
            "acknowledged": bool(getattr(incident, "acknowledged", False)),
        }
        for incident in incidents
    ]
    graph["summary"] = summarize(graph)
    return graph


_ROLE_NOUNS = {
    "spine": ("spine", "spines"),
    "leaf": ("leaf", "leaves"),
    "dcgw": ("DCGW", "DCGWs"),
    "core": ("WAN/core router", "WAN/core routers"),
    "unknown": ("unclassified node", "unclassified nodes"),
}


def _counted(count: int, nouns: Tuple[str, str]) -> str:
    return f"{count} {nouns[0] if count == 1 else nouns[1]}"


def summarize(graph: Dict[str, Any]) -> List[str]:
    """The fabric in a few lines, the way someone would brief it.

    What it is built of, what it carries, and what is wrong with it - the
    three things to know before looking at any single table.
    """
    nodes = graph.get("nodes", [])
    devices = [n for n in nodes if n.get("role") in _ROLE_NOUNS]
    roles = graph.get("roles", {})
    built = ", ".join(
        _counted(roles[role], _ROLE_NOUNS[role]) for role in ("spine", "leaf", "dcgw", "core", "unknown") if roles.get(role)
    )
    lines = [f"{len(devices)} nodes: {built}" if built else "No nodes have reported yet"]
    fabrics = graph.get("fabrics") or []
    if len(fabrics) > 1:
        lines[0] += f", in {len(fabrics)} fabrics ({', '.join(f['label'] for f in fabrics)})"
    elif graph.get("sites") and len(graph["sites"]) > 1:
        lines[0] += f", across sites {', '.join(graph['sites'])}"

    clients = [n for n in nodes if n.get("role") == "client"]
    services = {name for n in devices for name in n.get("services", [])}
    stitched = sum(n.get("stitched", 0) for n in devices)
    carried = []
    if services:
        carried.append(_counted(len(services), ("service", "services")))
    if stitched:
        carried.append(f"{stitched} stitched on the gateways")
    if clients:
        multihomed = sum(1 for n in clients if len({a["node"] for a in n.get("attachments", [])}) > 1)
        carried.append(
            _counted(len(clients), ("client", "clients"))
            + (f", {multihomed} multi-homed" if multihomed else "")
        )
    if carried:
        lines.append("Carries " + "; ".join(carried))

    down = [n["label"] for n in devices if not n.get("connected")]
    everything = graph.get("incidents", [])
    incidents = [i for i in everything if not i.get("acknowledged")]
    acknowledged = len(everything) - len(incidents)
    errors = sum(1 for i in incidents if i["severity"] == "error")
    warnings = sum(1 for i in incidents if i["severity"] == "warning")
    if down:
        lines.append(f"Not answering: {', '.join(sorted(down))}")
    if incidents:
        findings = sum(i["findings"] for i in incidents)
        worst = incidents[0]
        lines.append(
            f"{_counted(errors, ('error incident', 'error incidents'))}, "
            f"{_counted(warnings, ('warning', 'warnings'))} ({findings} findings); "
            f"worst: {worst['title']}"
            + (f"; {acknowledged} acknowledged" if acknowledged else "")
        )
    elif acknowledged:
        lines.append(f"Nothing open: {_counted(acknowledged, ('incident', 'incidents'))} acknowledged")
    elif "incidents" in graph:
        lines.append("No findings: every check passes")
    return lines


def facts_from_fabric_state(state: FabricState) -> List[NodeFacts]:
    """Build NodeFacts for every node from a collected FabricState."""
    facts = []
    all_nodes = dict.fromkeys(
        list(state.hostnames)
        + [n for rep in state.reports.values() for n in rep]
        + [n for (_, n) in state.errors]
    )

    for name in all_nodes:
        hostname = state.hostnames.get(name, name)
        sys_list = state.reports.get("sys_info", {}).get(name, [])
        sys_info = sys_list[0] if sys_list else None
        system_name = (
            getattr(sys_info, "host_name", None)
            or (sys_info.get("host_name") if isinstance(sys_info, dict) else "")
            or ""
        )
        platform = (
            getattr(sys_info, "type", None)
            or getattr(sys_info, "chassis_type", None)
            or (sys_info.get("type") or sys_info.get("chassis_type") or sys_info.get("chassis-type") if isinstance(sys_info, dict) else "")
            or ""
        )

        mac_vrfs, ip_vrfs, stitched = 0, 0, 0
        services: List[str] = []
        attachments: List[Attachment] = []
        nis = state.reports.get("ni", {}).get(name, [])
        has_state = bool(nis)
        for ni in nis:
            ntype = getattr(ni, "type", None) or (ni.get("type") if isinstance(ni, dict) else "")
            nname = getattr(ni, "name", None) or (ni.get("name") if isinstance(ni, dict) else "")
            if ntype == "mac-vrf":
                mac_vrfs += 1
                kind = "bridged"
            elif ntype in ("ip-vrf", "vrf") and nname != "mgmt":
                ip_vrfs += 1
                kind = "routed"
            else:
                continue
            services.append(str(nname))
            itfs = (
                getattr(ni, "interfaces", None)
                or (ni.get("interfaces") if isinstance(ni, dict) else ())
                or ()
            )
            for itf in itfs:
                itf_name = getattr(itf, "name", None) or (itf.get("name") if isinstance(itf, dict) else str(itf))
                if not any(itf_name.startswith(vp) for vp in _VIRTUAL_PORTS):
                    port = itf_name.rsplit(".", 1)[0]
                    oper = getattr(itf, "oper", None) or (itf.get("oper") if isinstance(itf, dict) else "")
                    attachments.append(
                        Attachment(
                            subinterface=itf_name,
                            port=port,
                            service=str(nname),
                            kind=kind,
                            oper_state=oper,
                        )
                    )
            instances = (
                getattr(ni, "instances", None)
                or (ni.get("instances") if isinstance(ni, dict) else ())
                or ()
            )
            if len(instances) >= 2:
                stitched += 1

        adjacencies: List[Adjacency] = []
        for itf in state.reports.get("lldp", {}).get(name, []):
            port = getattr(itf, "name", None) or getattr(itf, "interface", None) or (itf.get("name") or itf.get("interface") if isinstance(itf, dict) else "")
            if not any(port.startswith(oob) for oob in _OUT_OF_BAND):
                neighbors = getattr(itf, "neighbors", None) or (itf.get("neighbors") if isinstance(itf, dict) else None)
                if neighbors is not None:
                    for nbr in neighbors:
                        peer = getattr(nbr, "system_name", None) or getattr(nbr, "neighbor", None) or (nbr.get("system_name") or nbr.get("neighbor") if isinstance(nbr, dict) else "")
                        peer_port = getattr(nbr, "port_id", None) or getattr(nbr, "neighbor_interface", None) or (nbr.get("port_id") or nbr.get("neighbor_interface") if isinstance(nbr, dict) else "")
                        if peer:
                            adjacencies.append(
                                Adjacency(
                                    local_port=port,
                                    peer=str(peer),
                                    peer_port=str(peer_port),
                                    oper_state="up",
                                )
                            )
                else:
                    peer = getattr(itf, "neighbor", None) or (itf.get("neighbor") if isinstance(itf, dict) else "")
                    peer_port = getattr(itf, "neighbor_interface", None) or (itf.get("neighbor_interface") if isinstance(itf, dict) else "")
                    if peer:
                        adjacencies.append(
                            Adjacency(
                                local_port=port,
                                peer=str(peer),
                                peer_port=str(peer_port),
                                oper_state="up",
                            )
                        )

        segments: Dict[str, Segment] = {}
        for es in state.reports.get("es", {}).get(name, []):
            es_itf = getattr(es, "interface", None) or (es.get("interface") if isinstance(es, dict) else "")
            es_name = getattr(es, "name", None) or (es.get("name") if isinstance(es, dict) else "")
            es_esi = getattr(es, "esi", None) or (es.get("esi") if isinstance(es, dict) else "")
            if es_itf:
                segments[str(es_itf)] = Segment(name=str(es_name), esi=str(es_esi))

        connected = not any(n == name for (_, n) in state.errors)
        error = next((err for (rep, n), err in state.errors.items() if n == name), None)

        facts.append(
            NodeFacts(
                name=name,
                hostname=hostname,
                system_name=system_name,
                platform=platform,
                mac_vrfs=mac_vrfs,
                ip_vrfs=ip_vrfs,
                stitched=stitched,
                has_state=has_state or bool(adjacencies),
                connected=connected,
                error=error,
                adjacencies=adjacencies,
                attachments=attachments,
                segments=segments,
                services=services,
            )
        )
    return facts


def summarize_fabric(
    state: FabricState,
    *,
    findings: Optional[List[Finding]] = None,
    incidents: Optional[List[Incident]] = None,
) -> Dict[str, Any]:
    """Compute topology and executive briefing summary from a collected FabricState."""
    facts = facts_from_fabric_state(state)
    graph = build_topology(facts)
    if findings is None:
        from ..checks import run_checks  # noqa: PLC0415
        findings = run_checks(state)
    if incidents is None:
        from ..acks import mark as mark_acknowledged  # noqa: PLC0415
        from ..incidents import correlate  # noqa: PLC0415
        incidents = mark_acknowledged(correlate(findings, state), state.acknowledged)

    from ..incidents import locate  # noqa: PLC0415
    annotate_health(
        graph,
        locate(findings, state),
        incidents,
        acked=state.acknowledged,
    )
    lines = summarize(graph)
    graph["summary"] = lines

    devices = [n for n in graph.get("nodes", []) if n.get("role") in _ROLE_NOUNS]
    roles = graph.get("roles", {})
    services = {name for n in devices for name in n.get("services", [])}
    open_incidents = [i for i in incidents if not getattr(i, "acknowledged", False)]
    errors = sum(1 for i in open_incidents if getattr(i, "severity", "") == "error")
    warnings = sum(1 for i in open_incidents if getattr(i, "severity", "") == "warning")
    worst = open_incidents[0].title if open_incidents else ""

    return {
        "summary": lines,
        "nodes": len(devices),
        "roles": roles,
        "services": len(services),
        "incidents": {
            "open": len(open_incidents),
            "errors": errors,
            "warnings": warnings,
            "findings": len(findings),
            "worst": worst,
            "acknowledged": len(incidents) - len(open_incidents),
        },
        "graph": graph,
    }


# --------------------------------------------------------------------------- #
# virtual ethernet-segments: L3 aliasing in a routed service
# --------------------------------------------------------------------------- #


def _virtual_segments(
    system: Dict[str, Any], instances: List[Any], details: Dict[str, Dict[str, Any]]
) -> List[VirtualSegment]:
    """The virtual ethernet-segments configured on one node."""
    evpn = _branch(system, "network-instance", "protocols", "evpn", "ethernet-segments")
    irbs = _irb_domains(instances, details)
    found = []
    for bgp_instance in _as_list(evpn.get("bgp-instance")):
        for segment in _as_list(bgp_instance.get("ethernet-segment")):
            if _norm(segment.get("type")) != "virtual" or not segment.get("esi"):
                continue
            associations: Dict[str, Tuple[List[str], str]] = {}
            for vrf in _as_list(_branch(segment, "association").get("network-instance")):
                candidates = _df_candidates(vrf)
                associations[str(vrf.get("name", ""))] = (
                    [c.address for c in candidates],
                    next((c.address for c in candidates if c.designated), ""),
                )
            next_hops = _es_next_hops(segment)
            found.append(
                VirtualSegment(
                    name=str(segment.get("name") or ""),
                    esi=str(segment["esi"]),
                    mode=_norm(segment.get("oper-multi-homing-mode") or segment.get("multi-homing-mode")),
                    oper=_norm(segment.get("oper-state")),
                    next_hops=next_hops,
                    associations=associations,
                    via={address: domain for address, _evis in next_hops if (domain := _domain_of(address, irbs))},
                )
            )
    return found


def _irb_domains(instances: List[Any], details: Dict[str, Dict[str, Any]]) -> List[Tuple[Any, str]]:
    """Each IRB subnet on the node, with the bridge domain the IRB is in."""
    domain_of: Dict[str, str] = {}
    for instance in instances:
        if isinstance(instance, dict) and _norm(instance.get("type")) == "mac-vrf":
            for itf in _as_list(instance.get("interface")):
                name = str(itf.get("name", ""))
                if name.startswith("irb"):
                    domain_of[name] = str(instance.get("name", ""))
    subnets = []
    for name, detail in details.items():
        if name not in domain_of:
            continue
        for family in ("ipv4", "ipv6"):
            for address in _as_list(_branch(detail, family).get("address")):
                try:
                    subnets.append((ipaddress.ip_network(str(address.get("ip-prefix")), strict=False), domain_of[name]))
                except ValueError:
                    continue
    return subnets


def _domain_of(address: str, subnets: List[Tuple[Any, str]]) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    return next((domain for network, domain in subnets if ip in network and network.prefixlen < network.max_prefixlen), "")


def _virtual_key(esi: str) -> str:
    return f"ves:{esi}"


def _virtual_segment_nodes(
    nodes: List[NodeFacts],
    clients: List[Dict[str, Any]],
    links: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One node per virtual segment, cabled to the leaves it is attached to.

    *Attached* is what the segment's DF election says: the leaves among its
    candidates, which are the ones that can reach its next-hop. A leaf that
    has it configured but is not a candidate is listed, not drawn. The
    next-hop itself is drawn as a link to the client that owns it: the one
    attached, on an attached leaf, to the bridge domain whose IRB subnet
    the next-hop is in.

    None of this is a cable, so it is drawn only in the service overlay of
    the routed services the segment serves.
    """
    by_address = {n.system_address: n.name for n in nodes if n.system_address}
    grouped: Dict[str, List[Tuple[NodeFacts, VirtualSegment]]] = {}
    for node in nodes:
        for segment in node.virtual_segments:
            grouped.setdefault(segment.esi, []).append((node, segment))
    payload = []
    for esi, members in sorted(grouped.items()):
        name = _virtual_key(esi)
        services = sorted({ni for _node, seg in members for ni in seg.associations})
        candidates = {addr for _node, seg in members for cands, _df in seg.associations.values() for addr in cands}
        attached = sorted({by_address[a] for a in candidates if a in by_address})
        label = {n.name: n.label for n in nodes}
        # Each node elects a DF for itself; they should agree, and on a
        # single-active segment two of them forwarding is the fault.
        views: Dict[str, Dict[str, str]] = {}
        for node, seg in members:
            for ni, (_cands, elected) in seg.associations.items():
                if elected:
                    views.setdefault(ni, {})[label[node.name]] = label.get(by_address.get(elected, ""), elected)
        df = {ni: sorted(set(v.values())) for ni, v in views.items()}
        conflict = sorted(ni for ni, elected in df.items() if len(elected) > 1)
        next_hops: Dict[str, Dict[str, Any]] = {}
        for node, seg in members:
            for address, evis in seg.next_hops:
                entry = next_hops.setdefault(address, {"address": address, "evis": sorted(set(evis)), "via": ""})
                if node.name in attached and seg.via.get(address):
                    entry["via"] = seg.via[address]
        oper = "up" if any(seg.oper == "up" for node, seg in members if node.name in attached) else "down"
        for node_name in attached:
            key = (node_name, name) if node_name <= name else (name, node_name)
            link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
            link.update(kind="ves", overlay_only=services, access=True)
            link["states"].add(oper)
            if any(label[node_name] in elected for elected in df.values()):
                link["df"] = True
                link["note"] = "designated forwarder" + (" - but not the only one" if conflict else "")
        owners = set()
        for hop in next_hops.values():
            for client in clients:
                if any(a["node"] in attached and a["service"] == hop["via"] for a in client["attachments"]):
                    owners.add(client["name"])
                    key = (client["name"], name) if client["name"] <= name else (name, client["name"])
                    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
                    link.update(kind="ves-nh", overlay_only=services, access=True, note=f"next-hop {hop['address']} via {hop['via']}")
                    link["states"].add(oper)
        segments = {seg.name for _node, seg in members if seg.name}
        payload.append(
            {
                "name": name,
                "label": "vES",
                "names": sorted(segments),
                "role": "segment",
                "layer": _LAYER_OF["segment"],
                "site": "",
                "mac_vrfs": 0,
                "ip_vrfs": 0,
                "stitched": 0,
                "clients": len(owners),
                "peers": attached + sorted(owners),
                "ports": len(attached),
                "connected": True,
                "error": None,
                "external": False,
                "attachments": [
                    {
                        "node": node_name,
                        "port": "",
                        "subinterface": "",
                        "service": ni,
                        "vlan": "",
                        "ip": ", ".join(next_hops),
                        "state": oper,
                        "esi": esi,
                        "kind": "virtual",
                    }
                    for node_name in attached
                    for ni in services
                ],
                "services": services,
                "esi": esi,
                "virtual": True,
                "overlay_only": services,
                "ves": {
                    "mode": next((seg.mode for _n, seg in members if seg.mode), ""),
                    "oper": oper,
                    "next_hops": sorted(next_hops.values(), key=lambda h: h["address"]),
                    "df": df,
                    "df_views": views,
                    "df_conflict": conflict,
                    "attached": attached,
                    "configured": sorted({node.name for node, _seg in members}),
                    "owners": sorted(owners),
                    "aliasing": [],
                },
            }
        )
    return payload


def annotate_aliasing(graph: Dict[str, Any], state: Any) -> Dict[str, Any]:
    """Which remote VTEPs actually load-balance over each virtual segment.

    The evidence is the remote node's own route table in the routed service:
    the next-hop's host route installed over two or more of the segment's
    attached VTEPs, and the prefixes that resolve through that next-hop. Each
    such node gets an ``alias`` link to the segment, drawn in the service's
    overlay, saying over how many VTEPs it spreads the traffic.
    """
    names = {n["name"]: n for n in graph.get("nodes", [])}
    for segment in [n for n in graph.get("nodes", []) if n.get("virtual")]:
        ves = segment["ves"]
        vteps = {names[a]["system_address"]: a for a in ves["attached"] if names.get(a, {}).get("system_address")}
        hosts = {h["address"] for h in ves["next_hops"]}
        for report in ("ipv4_rib", "ipv6_rib"):
            for node, table in state.items(report):
                if node in ves["attached"] or table.ni not in segment["services"] or node not in names:
                    continue
                spread: Set[str] = set()
                behind: List[str] = []
                for route in table.routes:
                    host = route.prefix.split("/", 1)[0]
                    if host in hosts and route.prefix.endswith(("/32", "/128")):
                        spread |= {vteps[h.address] for h in route.next_hops if h.address in vteps}
                    elif any(
                        h.resolving_route.split("/", 1)[0] in hosts or h.address in hosts for h in route.next_hops
                    ):
                        behind.append(route.prefix)
                if len(spread) < 2:
                    continue
                ves["aliasing"].append(
                    {"node": node, "ni": table.ni, "vteps": sorted(spread), "prefixes": sorted(behind)}
                )
                graph["links"].append(
                    {
                        "a": node,
                        "b": segment["name"],
                        "count": 1,
                        "ports": [],
                        "state": "up",
                        "intra_layer": False,
                        "access": True,
                        "lost": False,
                        "kind": "alias",
                        "overlay_only": [table.ni],
                        "note": f"L3 aliasing: ECMP over {', '.join(sorted(names[v].get('label', v) for v in spread))}"
                        + (f" for {', '.join(sorted(behind)[:4])}" if behind else ""),
                    }
                )
    return graph

