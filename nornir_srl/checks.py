"""Fabric sanity checks: the first questions asked of a fabric, answered at once.

A check is a pure function over the payloads report getters already return. It
sees the whole fabric rather than one node, which is what lets it say things a
single table cannot - that a link is only seen from one end, that two leaves
disagree about the VNI of a service.

The payloads, not the rendered tables, are what a check reads. A table exists to
be looked at: its column names carry newlines and sort prefixes, and they are
free to change when the display does. ``spec.getter(device)`` returns the same
structure on every surface - the records of :mod:`nornir_srl.records` where a
report has them - so a check written against it holds on all three.

Adding one means writing a function that takes a :class:`FabricState` and yields
:class:`Finding` objects, then listing it in :data:`CHECKS` with the reports it
reads. Everything else - the CLI command, the MCP tool, the server table - is
driven from that list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .aliases import resolve
from .fabric import (
    FabricState,
    collect_fabric_state as _collect,
    index as _index,
    out_of_band as _out_of_band,
    parent as _parent,
    text as _text,
)

from .records import Neighbor, SubinterfaceState

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from nornir.core import Nornir

#: What the report holding the findings is called in the registry.
CHECKS_REPORT = "checks"
#: Its columns, in the order they read.
CHECKS_COLUMNS = ("Severity", "Check", "Node", "Subject", "Detail")

#: Findings worth waking someone for: the fabric is not doing what it was
#: built to do.
ERROR = "error"
#: Findings worth reading: legitimate in some fabrics, a fault in most.
WARNING = "warning"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1}


@dataclass(frozen=True)
class Finding:
    """One thing a check found wrong, on one node."""

    check: str
    severity: str
    node: str
    #: What on that node: an interface, a peer, a service.
    subject: str
    detail: str

    def as_row(self) -> Dict[str, Any]:
        return dict(
            zip(
                CHECKS_COLUMNS,
                (self.severity, self.check, self.node, self.subject, self.detail),
            )
        )


@dataclass(frozen=True)
class Check:
    """One question asked of the fabric."""

    name: str
    title: str
    #: What the reports it reads are called in the registry.
    requires: Tuple[str, ...]
    run: Callable[["FabricState"], List[Finding]]


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #

#: What a neighbour that is not meant to be up reads as. A peer configured and
#: then administratively disabled is not a fault, and the peers report carries
#: no admin-state of its own to tell it apart by.
_BGP_NOT_A_FAULT = {"", "disabled", "-"}


def _bgp_neighbors(state: FabricState) -> Iterator[Tuple[str, str, Neighbor]]:
    """Every BGP neighbour in the fabric, as (node, network-instance, peer)."""
    for node, entry, peer in state.sub_items("bgp_peers", "neighbors"):
        yield node, entry.ni, peer


def check_bgp_down(state: FabricState) -> List[Finding]:
    """A configured BGP session that is not established."""
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        session = _text(peer.state)
        if session == "established" or session in _BGP_NOT_A_FAULT:
            continue
        findings.append(
            Finding(
                check="bgp_down",
                severity=ERROR,
                node=node,
                subject=f"{ni}/{peer.peer or '?'}",
                detail=(
                    f"session is {session}, peer-group {peer.group or '-'}, "
                    f"AS {peer.peer_as if peer.peer_as is not None else '?'}"
                ),
            )
        )
    return findings


def check_bgp_af_down(state: FabricState) -> List[Finding]:
    """An established session carrying an address family that is down.

    The session hides it: the peers report shows ``established`` while the
    family that actually carries the overlay never came up.
    """
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        if _text(peer.state) != "established":
            continue
        for family in peer.families:
            if not family.enabled or _text(family.oper) != "down":
                continue
            findings.append(
                Finding(
                    check="bgp_af_down",
                    severity=ERROR,
                    node=node,
                    subject=f"{ni}/{peer.peer or '?'}",
                    detail=f"session established but {family.name} is down",
                )
            )
    return findings


def check_bgp_no_routes(state: FabricState) -> List[Finding]:
    """An established session that has received nothing on a family it negotiated.

    Normal on the day a fabric is built and on a peer that has nothing to
    advertise; on an EVPN session in a running fabric it is the symptom of a
    policy dropping everything, or of a peering that came up after the routes
    it should have learned.
    """
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        if _text(peer.state) != "established":
            continue
        for family in peer.families:
            if not family.enabled or _text(family.oper) == "down" or family.received:
                continue
            findings.append(
                Finding(
                    check="bgp_no_routes",
                    severity=WARNING,
                    node=node,
                    subject=f"{ni}/{peer.peer or '?'}",
                    detail=f"{family.name} is up but has received no routes",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #


def _subinterfaces(state: FabricState) -> Iterator[Tuple[str, str, SubinterfaceState]]:
    """Every subinterface in the fabric, as (node, parent interface, subif)."""
    for node, itf, subif in state.sub_items("subif", "subinterfaces"):
        if not _out_of_band(itf.name):
            yield node, itf.name, subif


def check_itf_down(state: FabricState) -> List[Finding]:
    """A subinterface that is administratively up but operationally down.

    A subinterface held down on purpose - the standby side of a single-active
    ethernet-segment - reads as ``down/standby`` rather than ``down``, and is
    not a fault.
    """
    findings = []
    for node, _parent_itf, subif in _subinterfaces(state):
        if _text(subif.oper) != "down":
            continue
        if _text(subif.admin) not in ("enable", "", "up"):
            continue
        reason = subif.down_reason or "no reason reported"
        findings.append(
            Finding(
                check="itf_down",
                severity=ERROR,
                node=node,
                subject=subif.name or "?",
                detail=f"admin enabled but oper down: {reason}",
            )
        )
    return findings


def check_itf_errors(state: FabricState) -> List[Finding]:
    """Packets an interface dropped or failed to receive during the sample.

    The counters are the change over the sampling interval, not the totals, so
    a finding means it is happening now rather than that it once did.

    Not on a containerlab node: the kernel discards on a veth what a real
    port forwards - IPv6 multicast the container's own stack does not want,
    for one - so the counters move on a healthy lab all the time and say
    nothing about the fabric.
    """
    findings = []
    for node, stats in state.items("ifstats"):
        if _out_of_band(stats.name) or node in state.containerlab:
            continue
        if stats.in_errors or stats.out_errors:
            findings.append(
                Finding(
                    check="itf_errors",
                    severity=ERROR,
                    node=node,
                    subject=stats.name,
                    detail=(
                        f"{stats.in_errors} in / {stats.out_errors} out "
                        "error packets during the sample"
                    ),
                )
            )
        if stats.in_discards or stats.out_discards:
            findings.append(
                Finding(
                    check="itf_errors",
                    severity=WARNING,
                    node=node,
                    subject=stats.name,
                    detail=(
                        f"{stats.in_discards} in / {stats.out_discards} out "
                        "discarded packets during the sample"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Link:
    """One end of an adjacency, as the node holding it sees it."""

    node: str
    port: str
    peer: str
    peer_port: str


def _adjacencies(state: FabricState) -> List[_Link]:
    """Every LLDP adjacency whose neighbour is a node we also have."""
    index = state.alias_index()
    links = []
    for node, itf, neighbor in state.sub_items("lldp", "neighbors"):
        if _out_of_band(itf.name):
            continue
        if not neighbor.system_name or _out_of_band(neighbor.port_id):
            continue
        peer = resolve(neighbor.system_name, index)
        if peer and peer != node:
            links.append(_Link(node, itf.name, peer, neighbor.port_id))
    return links


def check_lldp_one_sided(state: FabricState) -> List[Finding]:
    """A neighbour one node can see and the node on the other end cannot.

    Both ends run LLDP, so a link either end fails to report is a link that is
    not carrying in both directions - a receive fibre, or a port left down.
    """
    seen = {(link.node, link.port, link.peer, link.peer_port) for link in _adjacencies(state)}
    findings = []
    for link in sorted(seen):
        node, port, peer, peer_port = link
        if (peer, peer_port, node, port) in seen:
            continue
        findings.append(
            Finding(
                check="lldp_one_sided",
                severity=WARNING,
                node=node,
                subject=port,
                detail=f"sees {peer} {peer_port}, which does not see it back",
            )
        )
    return findings


def check_mtu_mismatch(state: FabricState) -> List[Finding]:
    """Two ends of one link that do not agree on the MTU.

    What this costs is a fabric that passes every ping and drops everything
    large, which is the failure everyone spends an afternoon on.
    """
    mtus: Dict[Tuple[str, str], Dict[str, int]] = {}
    for node, parent, subif in _subinterfaces(state):
        if subif.ip_mtu is not None:
            mtus.setdefault((node, parent), {})[_index(subif.name)] = subif.ip_mtu

    findings = []
    compared = set()
    for link in _adjacencies(state):
        # A link is seen from both ends, and is one finding either way.
        ends = tuple(sorted([(link.node, link.port), (link.peer, link.peer_port)]))
        if ends in compared:
            continue
        compared.add(ends)
        near, far = mtus.get(ends[0], {}), mtus.get(ends[1], {})
        for index in sorted(set(near) & set(far)):
            if near[index] == far[index]:
                continue
            findings.append(
                Finding(
                    check="mtu_mismatch",
                    severity=ERROR,
                    node=ends[0][0],
                    subject=f"{ends[0][1]}.{index}",
                    detail=(
                        f"ip-mtu {near[index]}, but {ends[1][0]} "
                        f"{ends[1][1]}.{index} on the other end has {far[index]}"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# fabric-wide consistency
# --------------------------------------------------------------------------- #


def outliers(values: Mapping[str, Any], floor: int = 3) -> Dict[str, Tuple[Any, Any]]:
    """The entries of *values* that disagree with what most of them say.

    The fault this catches is the one nothing reports as down: every leaf
    configured the same way except one, which works right up until traffic
    takes the path through it. Nothing in a per-node table shows that, because
    each node on its own looks fine - only the fabric read sideways does.

    Returns ``subject -> (its value, the majority value)`` for every subject
    that is in the minority. *floor* is how many subjects have to agree before
    a majority means anything: two nodes differing are two opinions, not an
    outlier and a norm.
    """
    counts: Dict[Any, int] = {}
    for value in values.values():
        counts[value] = counts.get(value, 0) + 1
    if len(values) < floor or len(counts) < 2:
        return {}
    majority, agreeing = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    # A plurality is not a norm: with 2/2/1 there is nothing to be an outlier
    # from, and saying so would be inventing a convention the fabric has not.
    if agreeing * 2 <= len(values):
        return {}
    return {
        subject: (value, majority)
        for subject, value in values.items()
        if value != majority
    }


def check_mtu_outlier(state: FabricState) -> List[Finding]:
    """A node whose fabric-facing MTU is not the one the rest of the fabric uses.

    ``mtu_mismatch`` compares the two ends of a cable, so it only sees a
    disagreement where LLDP sees a link. This reads the same values down the
    whole fabric instead, which catches the leaf configured at the default MTU
    on a link whose far end has not been brought up yet - before it is carrying
    anything, rather than after.
    """
    # The MTU a node uses on its fabric ports, when it uses just one. A node
    # with a deliberate mix is not making a claim this check can read.
    per_node: Dict[str, int] = {}
    for node, _parent_itf, subif in _subinterfaces(state):
        mtu = subif.ip_mtu
        if mtu is None or subif.name.startswith(("irb", "system", "lo")):
            continue
        seen = per_node.setdefault(node, mtu)
        if seen != mtu:
            per_node[node] = -1  # mixed, so not comparable
    comparable = {node: mtu for node, mtu in per_node.items() if mtu > 0}

    return [
        Finding(
            check="mtu_outlier",
            severity=WARNING,
            node=node,
            subject="ip-mtu",
            detail=(
                f"fabric interfaces use ip-mtu {mine}, where {majority} is what "
                f"{len(comparable) - len(outliers(comparable))} of "
                f"{len(comparable)} nodes use"
            ),
        )
        for node, (mine, majority) in sorted(outliers(comparable).items())
    ]


# --------------------------------------------------------------------------- #
# EVPN services
# --------------------------------------------------------------------------- #


def service_facts(vni: str, instances: Sequence[Any]) -> Dict[str, Any]:
    """What one node thinks a service looks like, by the name of each fact.

    The route-targets are one fact per bgp-vpn instance: a gateway carries a
    second instance for its WAN side, with route-targets the leaves never
    see, and that is not the two of them disagreeing.
    """
    facts: Dict[str, Any] = {"VNI": vni}
    for inst in instances:
        which = f" of bgp-instance {inst.id}" if inst.id != 1 else ""
        facts[f"import route-target{which}"] = _rt_set(inst.import_rts)
        facts[f"export route-target{which}"] = _rt_set(inst.export_rts)
    return facts


def service_disagreements(by_node: Mapping[str, Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """The facts the nodes carrying one service do not agree on.

    Each fact is compared among the nodes that have it: a fact only a
    gateway's second instance has is compared between the gateways.
    """
    disagreements = []
    for fact in dict.fromkeys(name for facts in by_node.values() for name in facts):
        values = {node: facts[fact] for node, facts in by_node.items() if fact in facts}
        if len(values) < 2 or len({_describe(v) for v in values.values()}) < 2:
            continue
        disagreements.append((fact, values))
    return disagreements


def system_addresses(state: FabricState) -> Dict[str, Set[str]]:
    """Each node's own loopback addresses: what the others see it as in the underlay."""
    found: Dict[str, Set[str]] = {}
    for node, instance, itf in state.sub_items("ni", "interfaces"):
        if instance.name != "default" or not itf.name.startswith(("system0", "lo")):
            continue
        for prefix in itf.prefixes:
            found.setdefault(node, set()).add(prefix.split("/", 1)[0])
    return found


def underlay_hosts(state: FabricState) -> Dict[str, Set[str]]:
    """The host routes in each node's default instance: the loopbacks it can reach."""
    hosts: Dict[str, Set[str]] = {}
    for report in ("ipv4_rib", "ipv6_rib"):
        for node, table, route in state.sub_items(report, "routes"):
            if table.ni != "default":
                continue
            address, _, length = route.prefix.partition("/")
            if length in ("32", "128"):
                hosts.setdefault(node, set()).add(address)
    return hosts


def underlay_domains(
    nodes: Sequence[str],
    system: Mapping[str, Set[str]],
    hosts: Mapping[str, Set[str]],
    gateways: Set[str] = frozenset(),
) -> List[List[str]]:
    """*nodes* grouped by the underlay they share, as the services page tells sites apart.

    Two nodes share an underlay when each has the other's system address as a
    host route in ``default``: that is what lets their VTEPs reach each other,
    and nodes that cannot are never one service however they are named.
    Gateways learn each other's loopbacks over the WAN, so between two of
    them that says nothing while the set has other nodes in it; a set of
    gateways alone is the WAN side itself and keeps those edges. Without any
    route table to go on - a state collected without the RIBs - every node is
    taken to be in one underlay, which is what the question used to assume.
    """
    names = sorted(nodes)
    if not any(hosts.get(name) for name in names):
        return [names] if names else []
    skip_gateway_pairs = any(name not in gateways for name in names)

    def sees(observer: str, other: str) -> bool:
        return any(ip in hosts.get(observer, ()) for ip in system.get(other, ()))

    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for index, a in enumerate(names):
        for b in names[index + 1 :]:
            if skip_gateway_pairs and a in gateways and b in gateways:
                continue
            if sees(a, b) and sees(b, a):
                parent[find(b)] = find(a)
    domains: Dict[str, List[str]] = {}
    for name in names:
        domains.setdefault(find(name), []).append(name)
    return sorted(domains.values())


def service_groups(by_node: Mapping[str, Dict[str, Any]]) -> List[List[str]]:
    """The nodes carrying one name, grouped into the services they actually are.

    Two nodes carry the same service when they import a common route-target
    on it: that is what lets their routes reach each other. A name that
    splits into several such groups is one service per site - a bridge
    domain each datacenter has its own of, stitched through an ip-vrf - or
    one node with a mistyped target; either way the groups are compared
    within themselves, not against each other. Nodes with no route-target
    at all are one group of their own.
    """
    targets = {node: set(facts.get("import route-target") or ()) for node, facts in by_node.items()}
    groups: List[List[str]] = []
    for node in sorted(targets):
        mine = targets[node]
        joined = [
            group
            for group in groups
            if any(mine & targets[other] or (not mine and not targets[other]) for other in group)
        ]
        members = [node] + [member for group in joined for member in group]
        groups = [group for group in groups if group not in joined] + [sorted(members)]
    return sorted(groups)


def check_evpn_service_mismatch(state: FabricState) -> List[Finding]:
    """Nodes that disagree about a service they both carry.

    A mac-vrf stretched across two leaves has to use the same VNI and the same
    route-targets on both, or the two halves quietly never join up. Compared
    among the nodes that share an underlay, because nodes that cannot reach
    each other's VTEPs are never one service whatever they are named; per
    bgp-vpn instance, so a gateway's WAN-side instance is held against the
    other gateways rather than against the leaves; and among the nodes that
    share a route-target, so two services under one name in one underlay is
    a warning about the split rather than an error about every node.
    """
    # vxlan-interface -> the VNI it sends on, per node.
    vnis: Dict[Tuple[str, str], Any] = {}
    for node, vxlan in state.items("vxlan"):
        vnis[(node, vxlan.name)] = vxlan.vni

    # Service name -> {node: what that node thinks the service looks like},
    # and which nodes carry it as a gateway, with a WAN-side instance too.
    services: Dict[str, Dict[str, Dict[str, Any]]] = {}
    gateways: Dict[str, Set[str]] = {}
    for node, instance in state.items("ni"):
        if _text(instance.type) not in ("mac-vrf", "ip-vrf"):
            continue
        services.setdefault(instance.name, {})[node] = service_facts(
            ", ".join(str(vnis.get((node, overlay), "?")) for overlay in instance.overlays),
            instance.instances,
        )
        if len(instance.instances) > 1:
            gateways.setdefault(instance.name, set()).add(node)
    system, hosts = system_addresses(state), underlay_hosts(state)

    def disagree(name: str, nodes: Sequence[str], facts_of: Callable[[str], Dict[str, Any]]) -> Iterator[Finding]:
        by_node = {node: facts_of(node) for node in nodes}
        for fact, values in service_disagreements(by_node):
            for node in sorted(values):
                others = sorted(set(values) - {node})
                yield Finding(
                    check="evpn_service_mismatch",
                    severity=ERROR,
                    node=node,
                    subject=name,
                    detail=(
                        f"{fact} {_describe(values[node])}, against "
                        + ", ".join(f"{other} {_describe(values[other])}" for other in others)
                    ),
                )

    findings: List[Finding] = []
    for name, by_node in sorted(services.items()):
        wan = gateways.get(name, set())
        for domain in underlay_domains(list(by_node), system, hosts, wan):
            local = {node: by_node[node] for node in domain}
            groups = service_groups(local)
            if len(groups) > 1:
                targets = {node: local[node].get("import route-target") for node in local}
                for group in groups:
                    elsewhere = sorted(set(local) - set(group))
                    for node in group:
                        findings.append(
                            Finding(
                                check="evpn_service_mismatch",
                                severity=WARNING,
                                node=node,
                                subject=name,
                                detail=(
                                    f"import route-target {_describe(targets[node])}, while "
                                    + ", ".join(f"{other} {_describe(targets[other])}" for other in elsewhere)
                                    + " in the same underlay: two services under one name, or a mistyped target"
                                ),
                            )
                        )
            for group in groups:
                # The DC side: everything but what a gateway's other instances carry.
                findings.extend(
                    disagree(
                        name,
                        group,
                        lambda node: {f: v for f, v in local[node].items() if "bgp-instance" not in f},
                    )
                )
        # The WAN side is a service among the gateways of every underlay,
        # reached over the WAN, so they are one domain of their own for it.
        if len(wan) > 1:
            for domain in underlay_domains(sorted(wan), system, hosts, wan):
                findings.extend(
                    disagree(
                        name,
                        domain,
                        lambda node: {f: v for f, v in by_node[node].items() if "bgp-instance" in f},
                    )
                )
    return findings


def _rt_set(targets: Sequence[str]) -> Sequence[str]:
    """Route-targets as a comparable set, however they were written."""
    return sorted({rt.strip().removeprefix("target:") for rt in targets if rt.strip()})


def _describe(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value) if str(value) else "none"


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #

def check_es_df(state: FabricState) -> List[Finding]:
    """Ethernet segments without a working designated-forwarder election.

    A segment with no DF in a network-instance forwards no broadcast traffic
    into it, and two nodes that disagree about the multi-homing mode of one
    segment either duplicate frames or black-hole them.
    """
    findings = []
    modes: Dict[str, Dict[str, str]] = {}

    for node, segment in state.items("es"):
        if segment.esi:
            modes.setdefault(segment.esi, {})[node] = _text(segment.mh_mode)

        if _text(segment.oper) not in ("up", ""):
            attached = " ".join(segment.interfaces) or " ".join(
                nh.address for nh in segment.next_hops
            )
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=segment.name or "?",
                    detail=f"segment is {_text(segment.oper)} on {attached or 'no interface'}",
                )
            )

        for association in segment.associations:
            if association.designated is not None:
                continue
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=f"{segment.name or '?'}/{association.ni}",
                    detail=(
                        "no designated forwarder elected among "
                        + (
                            " ".join(c.address for c in association.candidates)
                            or "no candidates"
                        )
                    ),
                )
            )

    # Every node computes the DF of a segment in a network-instance itself, and
    # they have to come to the same answer: two that each elect themselves both
    # forward, which on a single-active segment is what it exists to prevent.
    elected: Dict[Tuple[str, str], Dict[str, str]] = {}
    for node, segment in state.items("es"):
        for association in segment.associations:
            if segment.esi and association.designated is not None:
                elected.setdefault((segment.esi, association.ni), {})[node] = association.designated
    owner = {address: node for node, addresses in system_addresses(state).items() for address in addresses}
    names = {segment.esi: segment.name for _node, segment in state.items("es") if segment.name}
    for (esi, ni), by_node in sorted(elected.items()):
        if len(set(by_node.values())) < 2:
            continue
        views: Dict[str, List[str]] = {}
        for node, df in by_node.items():
            views.setdefault(df, []).append(node)
        said = "; ".join(
            f"{df}{f' ({owner[df]})' if df in owner else ''} according to {', '.join(sorted(nodes))}"
            for df, nodes in sorted(views.items())
        )
        for node in sorted(by_node):
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=f"{names.get(esi, esi)}/{ni}",
                    detail=f"nodes disagree on the designated forwarder: {said}",
                )
            )

    for esi, by_node in sorted(modes.items()):
        if len({mode for mode in by_node.values() if mode}) < 2:
            continue
        for node in sorted(by_node):
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=esi,
                    detail=(
                        f"multi-homing mode {by_node[node] or 'unset'}, against "
                        + ", ".join(
                            f"{other} {by_node[other] or 'unset'}"
                            for other in sorted(set(by_node) - {node})
                        )
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# liveness: BFD and the IGPs
# --------------------------------------------------------------------------- #

#: BFD states that are not a fault: up, or taken down on purpose.
_BFD_FINE = {"up", "admin-down", "admin_down"}

#: OSPF neighbour states that are a working adjacency. Two routers on a
#: broadcast segment that are neither of them DR stop at two-way, correctly.
_OSPF_FINE = {"full", "two-way", "2-way"}

#: Interfaces an IGP runs on without ever forming an adjacency, by name.
_IGP_LOOPBACKS = ("system", "lo")


def check_bfd_down(state: FabricState) -> List[Finding]:
    """A BFD session that is not up.

    BFD is what tells BGP or IS-IS that a link has stopped forwarding within
    a second rather than a hold time, so a session down leaves the protocol
    it protects blind - or, where the protocol waits for it, down as well.
    """
    findings = []
    for node, instance, session in state.sub_items("bfd", "sessions"):
        if session.state in _BFD_FINE:
            continue
        where = f" on {session.interface}" if session.interface else ""
        diagnostic = session.local_diagnostic if session.local_diagnostic not in ("", "no_diagnostic") else ""
        silent = session.remote_discriminator == 0 and not session.failures
        findings.append(
            Finding(
                check="bfd_down",
                severity=ERROR,
                node=node,
                subject=f"{instance.ni}/{session.remote_address}",
                detail=(
                    f"session is {session.state or 'unknown'}{where}, protecting "
                    f"{', '.join(session.protocols) or 'nothing'}"
                    + (f", diagnostic {diagnostic}" if diagnostic else "")
                    + (
                        "; the far end has never answered - is BFD enabled there?"
                        if silent
                        else ""
                    )
                ),
            )
        )
    return findings


def check_igp_adjacency_down(state: FabricState) -> List[Finding]:
    """An IS-IS adjacency or OSPF neighbour that is not up."""
    findings = []
    for node, itf, adjacency in state.sub_items("isis", "adjacencies"):
        if adjacency.state == "up":
            continue
        neighbor = adjacency.hostname or adjacency.system_id
        reason = f", {adjacency.down_reason}" if adjacency.down_reason else ""
        findings.append(
            Finding(
                check="igp_adjacency_down",
                severity=ERROR,
                node=node,
                subject=itf.name,
                detail=f"IS-IS {adjacency.level} adjacency to {neighbor} is {adjacency.state or 'unknown'}{reason}",
            )
        )
    for node, itf, neighbor in state.sub_items("ospf", "neighbors"):
        if neighbor.state in _OSPF_FINE:
            continue
        findings.append(
            Finding(
                check="igp_adjacency_down",
                severity=ERROR,
                node=node,
                subject=itf.name,
                detail=f"OSPF neighbour {neighbor.router_id} in area {itf.area} is {neighbor.state or 'unknown'}",
            )
        )
    return findings


def check_igp_no_adjacency(state: FabricState) -> List[Finding]:
    """An IGP interface that is up and meant to form an adjacency, but has none.

    Nothing reports this as down: the interface is up, the IGP runs on it,
    and there is simply nobody on the other end - a mismatched area, level or
    authentication, or an MTU the hellos do not fit through.
    """
    findings = []
    for report, noun, key in (("isis", "IS-IS", "adjacencies"), ("ospf", "OSPF", "neighbors")):
        for node, itf in state.items(report):
            if itf.passive or itf.oper != "up" or getattr(itf, key):
                continue
            if itf.name.startswith(_IGP_LOOPBACKS):
                continue
            findings.append(
                Finding(
                    check="igp_no_adjacency",
                    severity=WARNING,
                    node=node,
                    subject=itf.name,
                    detail=f"{noun} runs on it and it is up, but no adjacency formed",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# configuration: the two ends of one session or one link
# --------------------------------------------------------------------------- #


def _address_owners(state: FabricState) -> Dict[str, str]:
    """Every address a node has on a subinterface or loopback, and the node."""
    owners: Dict[str, str] = {}
    for node, addresses in system_addresses(state).items():
        for address in addresses:
            owners.setdefault(address, node)
    for node, _parent, subif in _subinterfaces(state):
        for prefix in tuple(subif.ipv4) + tuple(subif.ipv6):
            owners.setdefault(prefix.split("/", 1)[0], node)
    return owners


def _peer_host(peer: str) -> str:
    """``fe80::1%ethernet-1/1.0`` and ``10.0.0.1`` as the bare address."""
    return str(peer or "").split("/", 1)[0].split("%", 1)[0].strip()


def _enabled_families(peer: Neighbor) -> Set[str]:
    return {family.name for family in peer.families if family.enabled}


def check_bgp_peer_mismatch(state: FabricState) -> List[Finding]:
    """Two ends of one BGP session configured not to match.

    Each end is read as configured: the AS it expects from the other, the
    families it offers, whether BFD protects the session. A session between
    two nodes of the fabric can be checked from both ends, and what the ends
    disagree on is the reason it is down - or, for a family or BFD, what it
    is silently not doing while it is up.
    """
    owners = _address_owners(state)
    addresses: Dict[str, Set[str]] = {}
    for address, node in owners.items():
        addresses.setdefault(node, set()).add(address)
    sessions: Dict[Tuple[str, str], List[Neighbor]] = {}
    dynamic: Set[Tuple[str, str]] = set()
    for node, ni, peer in _bgp_neighbors(state):
        sessions.setdefault((node, ni), []).append(peer)
        if peer.dynamic:
            dynamic.add((node, ni))

    findings = []
    reported: Set[Tuple[str, ...]] = set()
    for (node, ni), peers in sorted(sessions.items()):
        for peer in peers:
            far = owners.get(_peer_host(peer.peer))
            if far is None or far == node:
                continue
            mine = addresses.get(node, set())
            back = next(
                (p for p in sessions.get((far, ni), []) if _peer_host(p.peer) in mine),
                None,
            )
            subject = f"{ni}/{peer.peer}"
            if back is None:
                # An end that accepts dynamic neighbours only lists a session
                # once it is up; nothing configured is missing there.
                # A far end whose sessions were not read says nothing either.
                if (far, ni) in dynamic or far not in state.nodes("bgp_peers"):
                    continue
                if _text(peer.state) == "established":
                    continue
                findings.append(
                    Finding(
                        check="bgp_peer_mismatch",
                        severity=ERROR,
                        node=node,
                        subject=subject,
                        detail=f"{far} owns {_peer_host(peer.peer)} but has no session back to {node} in {ni}",
                    )
                )
                continue
            if peer.peer_as is not None and back.local_as is not None and peer.peer_as != back.local_as:
                findings.append(
                    Finding(
                        check="bgp_peer_mismatch",
                        severity=ERROR,
                        node=node,
                        subject=subject,
                        detail=f"expects AS {peer.peer_as} from {far}, which runs AS {back.local_as}",
                    )
                )
            pair = (ni,) + tuple(sorted([(node, peer.peer), (far, back.peer)]))
            if pair in reported:
                continue
            reported.add(pair)
            here, there = _enabled_families(peer), _enabled_families(back)
            for family in sorted(here ^ there):
                which, other = (node, far) if family in here else (far, node)
                findings.append(
                    Finding(
                        check="bgp_peer_mismatch",
                        severity=WARNING,
                        node=node,
                        subject=subject,
                        detail=f"{family} is enabled on {which} only: {other} does not exchange it on this session",
                    )
                )
            if peer.bfd != back.bfd:
                which = node if peer.bfd else far
                findings.append(
                    Finding(
                        check="bgp_peer_mismatch",
                        severity=WARNING,
                        node=node,
                        subject=subject,
                        detail=f"BFD protects it on {which} only, so a failure is detected fast on one end",
                    )
                )
    return findings


def _igp_ends(state: FabricState) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(node, port) -> protocol -> the interface an IGP runs on it as."""
    ends: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for report in ("isis", "ospf"):
        for node, itf in state.items(report):
            if itf.name.startswith(_IGP_LOOPBACKS):
                continue
            ends.setdefault((node, _parent(itf.name)), {})[report] = itf
    return ends


def check_igp_peer_mismatch(state: FabricState) -> List[Finding]:
    """Two ends of one link configured not to form the IGP adjacency they should.

    Reads both ends of every cable LLDP sees: the IGP running on one end
    only, an OSPF area that differs, a network type that differs, one end
    passive. Each of them is an adjacency that does not form, or one that
    forms and carries less than it should, and none is visible from either
    end alone.
    """
    ends = _igp_ends(state)
    running = {report: set(state.nodes(report)) for report in ("isis", "ospf")}
    findings = []
    compared = set()
    for link in _adjacencies(state):
        pair = tuple(sorted([(link.node, link.port), (link.peer, link.peer_port)]))
        if pair in compared:
            continue
        compared.add(pair)
        (node, port), (far, far_port) = pair
        near, other = ends.get((node, port), {}), ends.get((far, far_port), {})
        for report, noun in (("isis", "IS-IS"), ("ospf", "OSPF")):
            a, b = near.get(report), other.get(report)
            if a is None and b is None:
                continue
            if a is None or b is None:
                # Only a mismatch where the other end runs the protocol at
                # all and simply not on this link.
                only, missing = ((node, port), (far, far_port)) if a is not None else ((far, far_port), (node, port))
                if missing[0] not in running[report]:
                    continue
                itf = a or b
                findings.append(
                    Finding(
                        check="igp_peer_mismatch",
                        severity=WARNING,
                        node=only[0],
                        subject=itf.name,
                        detail=f"{noun} runs on this end only: {missing[0]} {missing[1]} does not run it",
                    )
                )
                continue
            if report == "ospf" and a.area != b.area:
                findings.append(
                    Finding(
                        check="igp_peer_mismatch",
                        severity=ERROR,
                        node=node,
                        subject=a.name,
                        detail=f"OSPF area {a.area} here, {b.area} on {far} {b.name}: no adjacency forms",
                    )
                )
            kind_a = a.circuit_type if report == "isis" else a.interface_type
            kind_b = b.circuit_type if report == "isis" else b.interface_type
            if kind_a and kind_b and kind_a != kind_b:
                findings.append(
                    Finding(
                        check="igp_peer_mismatch",
                        severity=ERROR if report == "isis" else WARNING,
                        node=node,
                        subject=a.name,
                        detail=f"{noun} network type {kind_a} here, {kind_b} on {far} {b.name}",
                    )
                )
            if a.passive != b.passive:
                which = node if a.passive else far
                findings.append(
                    Finding(
                        check="igp_peer_mismatch",
                        severity=WARNING,
                        node=node,
                        subject=a.name,
                        detail=f"{noun} is passive on {which} only: no adjacency forms over this link",
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# the platform
# --------------------------------------------------------------------------- #

#: Utilization at which a resource is worth a look, and at which it is about
#: to refuse the next route, MAC or process.
RESOURCE_WARNING = 80
RESOURCE_ERROR = 95


def check_resource_high(state: FabricState) -> List[Finding]:
    """CPU, memory or a forwarding table close to full.

    A full forwarding table is the failure nothing else explains: the route
    is in the RIB, BGP is happy, and the packet is dropped anyway because
    the hardware had no room left to program it.
    """
    findings = []
    for node, resource in state.items("resources"):
        used = resource.used_percent
        if used is None or used < RESOURCE_WARNING:
            continue
        counts = f" ({resource.used} used, {resource.free} free)" if resource.used is not None else ""
        findings.append(
            Finding(
                check="resource_high",
                severity=ERROR if used >= RESOURCE_ERROR else WARNING,
                node=node,
                subject=f"{resource.component} {resource.name}",
                detail=f"{used}% in use{counts}",
            )
        )
    return findings


def check_hardware_fault(state: FabricState) -> List[Finding]:
    """A card, fan or power supply that is fitted and not working."""
    findings = []
    for node, component in state.items("components"):
        if component.oper in ("", "empty", "up", "booting"):
            if component.health != "unhealthy":
                continue
        findings.append(
            Finding(
                check="hardware_fault",
                severity=ERROR,
                node=node,
                subject=f"{component.kind} {component.id}",
                detail=f"oper-state {component.oper or 'unknown'}, health {component.health or 'unknown'}",
            )
        )
    return findings


def check_optic_dom(state: FabricState) -> List[Finding]:
    """An optic reporting one of its own alarm or warning thresholds as crossed.

    Light that is fading is a link that is going to start dropping frames -
    usually with CRC errors first, and then flapping - so the optic saying so
    is the earliest warning there is.
    """
    findings = []
    for node, optic in state.items("transceivers"):
        for severity, crossed in ((ERROR, optic.alarms), (WARNING, optic.warnings)):
            if not crossed:
                continue
            reading = []
            rx = optic.lowest_input_power
            if rx is not None:
                reading.append(f"lowest rx {rx:.2f} dBm")
            if optic.temperature is not None:
                reading.append(f"{optic.temperature:.1f} C")
            findings.append(
                Finding(
                    check="optic_dom",
                    severity=severity,
                    node=node,
                    subject=optic.interface,
                    detail=(
                        f"{'alarm' if severity == ERROR else 'warning'}: {', '.join(crossed)}"
                        + (f" ({', '.join(reading)})" if reading else "")
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


def check_flapping(state: FabricState) -> List[Finding]:
    """Something that keeps changing state: a session, a port, a MAC.

    Each reading on its own shows it up - or down - and nothing wrong; only
    the timeline shows it going back and forth. A MAC that moves between two
    ports over and over is a loop or two hosts sharing an address.

    Reads :attr:`FabricState.changes`, which only the live server keeps, so
    elsewhere this finds nothing.
    """
    from .changes import FLAP_WINDOW, flaps  # noqa: PLC0415 - changes imports fabric

    findings = []
    for flap in flaps(state.changes):
        minutes = round(FLAP_WINDOW / 60)
        if flap.kind == "mac":
            ni, _, mac = flap.subject.partition(" ")
            subject, what = f"{ni}/{mac}", f"moved {flap.count} times between {', '.join(flap.values)}"
        else:
            subject, what = flap.subject, f"changed {flap.count} times ({' / '.join(flap.values)})"
        findings.append(
            Finding(
                check="flapping",
                severity=WARNING,
                node=flap.node,
                subject=subject,
                detail=f"{flap.kind} {what} in the last {minutes} minutes",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

CHECKS: Tuple[Check, ...] = (
    Check(
        name="bgp_down",
        title="BGP sessions that are not established",
        requires=("bgp_peers",),
        run=check_bgp_down,
    ),
    Check(
        name="bgp_af_down",
        title="Established sessions with an address family down",
        requires=("bgp_peers",),
        run=check_bgp_af_down,
    ),
    Check(
        name="bgp_no_routes",
        title="Established sessions that have received no routes",
        requires=("bgp_peers",),
        run=check_bgp_no_routes,
    ),
    Check(
        name="itf_down",
        title="Interfaces enabled but not up",
        requires=("subif",),
        run=check_itf_down,
    ),
    Check(
        name="itf_errors",
        title="Interfaces dropping packets",
        requires=("ifstats",),
        run=check_itf_errors,
    ),
    Check(
        name="lldp_one_sided",
        title="Links only one end can see",
        requires=("lldp",),
        run=check_lldp_one_sided,
    ),
    Check(
        name="mtu_mismatch",
        title="Links whose ends disagree about the MTU",
        requires=("lldp", "subif"),
        run=check_mtu_mismatch,
    ),
    Check(
        name="mtu_outlier",
        title="Nodes whose fabric MTU differs from the rest of the fabric",
        requires=("subif",),
        run=check_mtu_outlier,
    ),
    Check(
        name="evpn_service_mismatch",
        title="Services whose nodes disagree about VNI or route-targets",
        # The RIBs say which nodes share an underlay, and so can disagree at all.
        requires=("ni", "vxlan", "ipv4_rib", "ipv6_rib"),
        run=check_evpn_service_mismatch,
    ),
    Check(
        name="es_df",
        title="Ethernet segments without a designated forwarder, or with two",
        # The system addresses say which node a DF address is.
        requires=("es", "ni"),
        run=check_es_df,
    ),
    Check(
        name="bfd_down",
        title="BFD sessions that are not up",
        requires=("bfd",),
        run=check_bfd_down,
    ),
    Check(
        name="igp_adjacency_down",
        title="IS-IS adjacencies and OSPF neighbours that are not up",
        requires=("isis", "ospf"),
        run=check_igp_adjacency_down,
    ),
    Check(
        name="igp_no_adjacency",
        title="IGP interfaces that are up but formed no adjacency",
        requires=("isis", "ospf"),
        run=check_igp_no_adjacency,
    ),
    Check(
        name="bgp_peer_mismatch",
        title="BGP sessions whose two ends are configured not to match",
        # The addresses say which node the far end of a session is.
        requires=("bgp_peers", "subif", "ni"),
        run=check_bgp_peer_mismatch,
    ),
    Check(
        name="igp_peer_mismatch",
        title="Links whose two ends are configured for different IGP adjacencies",
        requires=("lldp", "isis", "ospf"),
        run=check_igp_peer_mismatch,
    ),
    Check(
        name="resource_high",
        title="CPU, memory or forwarding tables close to full",
        requires=("resources",),
        run=check_resource_high,
    ),
    Check(
        name="hardware_fault",
        title="Cards, fans and power supplies that are fitted and not working",
        requires=("components",),
        run=check_hardware_fault,
    ),
    Check(
        name="optic_dom",
        title="Optics reporting a DOM alarm or warning",
        requires=("transceivers",),
        run=check_optic_dom,
    ),
    Check(
        name="flapping",
        title="Sessions, ports and MACs that keep changing state",
        # The timeline rather than a report: only the live server keeps one.
        requires=(),
        run=check_flapping,
    ),
)

CHECKS_BY_NAME: Mapping[str, Check] = {check.name: check for check in CHECKS}

#: Every report the checks read, which is what a surface has to collect.
REQUIRED_REPORTS: Tuple[str, ...] = tuple(
    dict.fromkeys(report for check in CHECKS for report in check.requires)
)


def collect_fabric_state(
    target: "Nornir", reports: Sequence[str] = REQUIRED_REPORTS
) -> FabricState:
    """Run the reports the checks read over a Nornir inventory.

    A thin default over :func:`nornir_srl.fabric.collect_fabric_state`: the
    checks read a fixed set of reports, so the caller does not have to name
    them. A lens names its own.
    """
    return _collect(target, reports)


def run_checks(
    state: FabricState, only: Optional[Sequence[str]] = None
) -> List[Finding]:
    """Every finding in the fabric, worst first.

    A check whose reports could not be collected from any node is skipped
    rather than reported clean, and one that raises is reported as a finding of
    its own: a check going wrong is not the same as a fabric being right.
    """
    selected = [c for c in CHECKS if not only or c.name in only]
    findings: List[Finding] = []
    for check in selected:
        if check.requires and not any(state.reports.get(report) for report in check.requires):
            continue
        try:
            findings.extend(check.run(state))
        except Exception as exc:  # noqa: BLE001 - one bad check is not the fabric
            findings.append(
                Finding(
                    check=check.name,
                    severity=ERROR,
                    node="-",
                    subject="check failed",
                    detail=str(exc),
                )
            )

    for (report, node), error in sorted(state.errors.items()):
        findings.append(
            Finding(
                check="collection",
                severity=WARNING,
                node=node,
                subject=report,
                detail=f"not checked: {error}",
            )
        )

    findings.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.check, f.node, f.subject)
    )
    return findings
