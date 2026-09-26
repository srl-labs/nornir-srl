"""Lenses: a question asked of the fabric, answered as one table.

A report renders one node's state. A check asks the fabric a fixed question and
answers yes or no. Neither is what someone troubleshooting actually types, which
is closer to *where is this MAC*, *how would this node reach that address*, and
*what does this service look like everywhere it exists*. Each of those is one
answer joined out of several reports across several nodes, so none of them fits
:class:`~nornir_srl.reports.ReportSpec` - a report's getter is handed one device
and cannot see the fabric it sits in.

A lens is the missing shape: it reads a :class:`~nornir_srl.fabric.FabricState`
the same way a check does, takes arguments the way a report does, and returns
what it found the way both do. :data:`LENSES` is the single registry, so a lens
is defined once and the CLI command and the MCP tool are generated from it.

A lens answers in layers. The function itself returns *records* - one
:class:`Sighting`, :class:`Hop` or :class:`Service` per thing found - holding
the answer as data: a list of VTEPs is a list, a count is a number, and what
kind of thing was found is a field rather than a word to match on. That is what
``-o json`` and the MCP tools emit, so whatever reads the answer by machine gets
fields rather than a sentence to parse. The table is built from those records by
the lens's :attr:`~LensSpec.columns`: each :class:`Column` names itself and says
how a record fills it, so a column name lives in exactly one place, and the
sentence in a ``Detail`` cell is composed for a reader rather than parsed by one.
The browser reads the same records as a hierarchy - :class:`Card` by thing
found, :class:`Entry` by node, :class:`Item` by record - built by the lens's
:attr:`~LensSpec.tree`, the way the services pages fold a fabric into cards.

Adding one means writing a record type, a function over a :class:`FabricState`
that returns a list of them, the columns and the tree that render one, and an
entry in :data:`LENSES`.

A lens reads the records of :mod:`nornir_srl.records` where a report returns
them, and the item dicts of a report that does not yet - the route tables,
LLDP, ARP and ND - as the getter shaped them. Where one of those arrives
pre-formatted it is read back apart here, marked with a comment, so that
converting the report has a list of callers to fix.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .aliases import resolve
from .fabric import FabricState, out_of_band, parent, text
from .checks import (
    service_disagreements,
    service_facts,
    service_groups,
    system_addresses,
    underlay_domains,
    underlay_hosts,
)
from .acks import mark as mark_acknowledged
from .changes import parse_since
from .checks import REQUIRED_REPORTS, run_checks
from .incidents import correlate
from .records import BgpVpnInstance, EthernetSegment, NeighborCache, NeighborEntry, Route, RouteNextHop, as_dict
from .reports import ALL_SURFACES, SERVER, ParamSpec
from .rows import Column, countdown

#: How far a path walk follows the fabric before deciding it is going in
#: circles. A datacenter fabric is three tiers; anything beyond this is a loop
#: the walk should report rather than keep following.
MAX_HOPS = 16


# --------------------------------------------------------------------------- #
# records: what a lens answers, as data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Sighting:
    """One place in the fabric that knows about an address.

    Which of the optional fields are filled depends on :attr:`kind`.
    """

    node: str
    ni: str
    #: ``configured``: the address is a subinterface's own - a loopback, a
    #: system address, an irb gateway. ``arp`` or ``neighbor``: an address
    #: binding that named the MAC. ``local``: a bridge-table entry learned on
    #: this node's own port. ``remote``: one learned over the overlay, from a
    #: VTEP or behind a segment. ``duplicate``: learned locally here and on
    #: :attr:`also_on` as well. ``multihomed``: learned locally here and on
    #: :attr:`also_on`, each on a port of the same ethernet-segment.
    #: ``bgp``: a host route to the IP that BGP installed in a route table.
    #: ``not-found``: no node has it.
    kind: str
    #: The IP a binding was found for, or the MAC.
    address: str
    #: What the entry is on: the subinterface an address is configured on or a
    #: binding or a local entry was learned on, the VTEP a remote one came
    #: from, or the segment it is behind. Exactly one of the three is set for
    #: anything that was found, except that a local entry learned on a port of
    #: an ethernet-segment also carries that segment's ESI.
    interface: str = ""
    vtep: str = ""
    esi: str = ""
    #: How the entry got there, as the table says it: ``learnt``, ``evpn``,
    #: ``static``, ``dynamic``.
    origin: str = ""
    #: ``configured``: the prefix as it is configured, ``192.0.2.3/32``;
    #: ``bgp``: the host route, ``6.6.6.1/32``.
    prefix: str = ""
    #: ``arp``/``neighbor``: the MAC the binding resolved to, and when it goes.
    mac: str = ""
    expiry: str = ""
    #: ``remote``: the overlay interface it was learned over, and the VNI.
    overlay: str = ""
    vni: Optional[int] = None
    #: ``remote`` behind a segment: what the segment is called on the nodes
    #: that have it configured. Empty when none of the collected nodes do.
    #: Learned locally: the segment the port it was learned on belongs to.
    segments: Tuple[str, ...] = ()
    #: ``duplicate`` or ``multihomed``: the other nodes that learned it locally.
    also_on: Tuple[str, ...] = ()
    #: ``not-found``: how many nodes' bridge tables were searched.
    searched: int = 0
    #: ``bgp``: the route's next-hops, each with what it resolves over:
    #: ``10.1.4.16 (vxlan 192.0.2.15, 192.0.2.16)``.
    next_hops: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Hop:
    """One step of a walk through the route tables.

    A hop is one lookup on one node and what the walk did with the result.
    Every ECMP branch and every tunnel is a hop of its own, so several hops
    can share a number. Which of the optional fields are filled depends on
    :attr:`outcome`.
    """

    hop: int
    node: str
    ni: str
    #: The address looked up here: the destination, or the tunnel endpoint
    #: the walk is chasing through the underlay on behalf of a VRF.
    address: str
    #: What the walk did with the lookup.
    #: ``forwarded``: out of :attr:`egress` to :attr:`peer`, where it goes on.
    #: ``dead-end``: out of an interface with no LLDP neighbour, so it cannot.
    #: ``tunnel``: resolved to a tunnel - VXLAN to a VTEP, LDP or SR to a
    #: far-end PE - and goes on in the underlay towards :attr:`endpoint`.
    #: ``endpoint-reached``: the underlay delivered the tunnel endpoint; the
    #: packet is decapsulated and looked up again in :attr:`resumes_in`.
    #: ``leaked``: the route matched was leaked from :attr:`resumes_in`, whose
    #: next-hops forward it; the walk goes on there.
    #: ``delivered``: the destination is attached here. ``local-ip``: it is
    #: this node's own address. ``neighbor``/``no-neighbor``: whether ARP or
    #: ND has the delivered address. ``no-route``, ``loop``, ``too-long``:
    #: where a walk gives up.
    outcome: str
    #: The route the lookup matched, as the route table has it.
    prefix: str = ""
    route_type: str = ""
    next_hops: Tuple[str, ...] = ()
    #: The subinterface the packet leaves on, or the tunnel it takes.
    egress: str = ""
    #: The node on the other end of that cable, and its port.
    peer: str = ""
    peer_port: str = ""
    #: ``tunnel``: its kind - ``vxlan``, ``ldp``, ``sr-isis`` - and the
    #: address it leads to.
    tunnel: str = ""
    endpoint: str = ""
    #: ``endpoint-reached``: the network-instance the walk picks up again in.
    #: Chosen by name where the far end has one, else by the route-target the
    #: origin's instance exports, since a gateway need not call it the same.
    #: ``leaked``: the instance the route was leaked from, on this node.
    resumes_in: str = ""
    #: ``neighbor``: the MAC the binding resolved to, and how it was learned.
    mac: str = ""
    origin: str = ""
    #: ``loop``: the steps already taken, as ``node/network-instance``.
    visited: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Interface:
    """A subinterface of a network-instance, and whether it is up."""

    name: str
    oper: str


@dataclass(frozen=True)
class Service:
    """One network-instance as one node carries it."""

    node: str
    ni: str
    type: str
    oper: str
    #: The EVI of each BGP-EVPN instance it advertises with; a gateway has two.
    evis: Tuple[str, ...]
    #: The ingress VNI of each overlay interface bound to it.
    vnis: Tuple[int, ...]
    import_rts: Tuple[str, ...]
    export_rts: Tuple[str, ...]
    interfaces: Tuple[Interface, ...]
    #: The instances attached to it: the ip-vrf an irb of a mac-vrf routes
    #: into, or the mac-vrfs an ip-vrf routes for.
    bound: Tuple[str, ...]
    #: The VTEPs its overlay sends to.
    vteps: Tuple[str, ...]
    local_macs: int
    remote_macs: int
    #: The ethernet-segments associated with it.
    segments: Tuple[str, ...]
    #: Its bgp-vpn instances, each with its own route-targets; a gateway has
    #: two. :attr:`import_rts` and :attr:`export_rts` are their union.
    instances: Tuple[BgpVpnInstance, ...] = ()
    #: Which underlay the node is in, numbered from ``1``, when the service is
    #: carried in more than one - the way the services page tells sites
    #: apart. Nodes in different underlays never have to agree.
    site: str = ""


# --------------------------------------------------------------------------- #
# the hierarchy: how records read as cards
# --------------------------------------------------------------------------- #

#: What a card, an entry or an item says about itself: ``up``, ``warn``,
#: ``down``, or nothing where there is no verdict to give.
_UP, _WARN, _DOWN = "up", "warn", "down"
_SEVERITY = {"": 0, _UP: 1, _WARN: 2, _DOWN: 3}


def _worst(states: Iterable[str]) -> str:
    return max(states, key=lambda s: _SEVERITY.get(s, 0), default="")


@dataclass(frozen=True)
class Detail:
    """One line of an item: a label and a value.

    A tuple value is shown as pills; each of those is a string, or a
    ``(text, state)`` pair where the pill has a state of its own.
    """

    label: str
    value: Any
    state: str = ""


@dataclass(frozen=True)
class Item:
    """One record, under the node that reports it."""

    title: str
    state: str = ""
    #: What the state badge says, where the state is only its colour - an
    #: incident's severity is drawn in the colour of down without being one.
    label: str = ""
    details: Tuple[Detail, ...] = ()


@dataclass(frozen=True)
class Entry:
    """One node's part of a card."""

    title: str
    state: str = ""
    #: What the state badge says, where the state is only its colour - an
    #: incident's severity is drawn in the colour of down without being one.
    label: str = ""
    badge: str = ""
    items: Tuple[Item, ...] = ()


@dataclass(frozen=True)
class Card:
    """One thing a lens found - an address, a hop, a service - across the fabric."""

    title: str
    subtitle: str = ""
    icon: str = ""
    state: str = ""
    #: What the state badge says, where the state is only its colour - an
    #: incident's severity is drawn in the colour of down without being one.
    label: str = ""
    badge: str = ""
    entries: Tuple[Entry, ...] = ()
    #: What identifies the thing on the card to the server, for an action
    #: taken on it: an incident's id.
    key: str = ""
    #: The action the card offers on it: ``ack`` or ``unack``.
    action: str = ""


def _entries(
    records: Iterable[Any],
    item: Callable[[Any], Item],
    noun: str,
    sort: bool = True,
    by_severity: bool = False,
) -> Tuple[Entry, ...]:
    """The records grouped by node, each node's state the worst of its items.

    *by_severity* labels a node by the severity its colour stands for, where
    the items are findings or changes rather than things that are up or down.
    """
    by_node: Dict[str, List[Any]] = {}
    for record in records:
        by_node.setdefault(record.node, []).append(record)
    nodes = sorted(by_node) if sort else list(by_node)
    entries = []
    for node in nodes:
        items = tuple(item(record) for record in by_node[node])
        entries.append(
            Entry(
                title=node or "-",
                state=_worst(i.state for i in items),
                label=_severity_label(_severity_of(_worst(i.state for i in items))) if by_severity else "",
                badge=_count(len(items), noun),
                items=items,
            )
        )
    return tuple(entries)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


# --------------------------------------------------------------------------- #
# rows: how a record reads in a table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LensSpec:
    """One question, on every surface that offers it."""

    #: Canonical id, and the CLI command name (Typer renders ``_`` as ``-``).
    name: str
    title: str
    description: str
    #: What the reports it reads are called in the report registry. These are
    #: what a surface has to collect before it can call :attr:`run`.
    requires: Tuple[str, ...]
    #: How a record reads as a row, in the order the columns read. ``Node``
    #: is every record's own and the table renders it itself, so no lens
    #: declares it.
    columns: Tuple[Column, ...]
    #: Called as ``run(state, **params)``; returns the records.
    run: Callable[..., List[Any]]
    #: How the records read as cards, for the browser.
    tree: Callable[[List[Any]], List[Card]]
    #: How they read as a graph, where the answer is one: a walk from a
    #: source to a destination, drawn hop by hop with its fan-out.
    graph: Optional[Callable[[List[Any]], Dict[str, Any]]] = None
    params: Tuple[ParamSpec, ...] = ()
    #: MCP tool name, where a more explicit one reads better for an agent.
    mcp_name: Optional[str] = None
    surfaces: FrozenSet[str] = ALL_SURFACES
    #: False when consecutive rows of one node are one answer rather than a
    #: list of them, and the table should keep them in the order produced.
    group_by_node: bool = True

    @property
    def tool_name(self) -> str:
        return self.mcp_name or self.name

    @property
    def column_names(self) -> List[str]:
        return [column.name for column in self.columns]

    def on(self, surface: str) -> bool:
        return surface in self.surfaces

    def row(self, record: Any) -> Dict[str, Any]:
        """*record* as one table row, keyed by column name."""
        return {
            "Node": record.node or "-",
            **{column.name: column.of(record) for column in self.columns},
        }

    def rows(self, records: Iterable[Any]) -> List[Dict[str, Any]]:
        return [self.row(record) for record in records]

    def as_dict(self) -> Dict[str, Any]:
        """What the browser needs to offer the lens: the same shape as a report."""
        return {
            "name": self.name,
            "kind": "lens",
            "title": self.title,
            "description": self.description,
            "category": "Lenses",
            "params": [p.as_dict() for p in self.params],
            "key_columns": [],
        }


def coerce_lens_params(lens: LensSpec, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """The parameters *lens* declares, out of a surface's raw input.

    Like :func:`~nornir_srl.reports.coerce_params`, but a lens is a question
    about something, so a parameter it needs is an error to leave out.
    """
    params: Dict[str, Any] = {}
    for spec in lens.params:
        value = spec.coerce(raw.get(spec.name, ""))
        if value is not None:
            params[spec.name] = value
        elif spec.required:
            raise ValueError(f"{lens.title} needs {spec.label.lower()}: {spec.help.lower()}")
    return params


# --------------------------------------------------------------------------- #
# small readings of an address
# --------------------------------------------------------------------------- #

#: Route types that terminate a walk because the destination is attached to the
#: node that holds them rather than reachable through it.
_ATTACHED = ("local", "host", "direct", "arp-nd", "static-local")


def _address(value: Any) -> Optional[Any]:
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _network(value: Any) -> Optional[Any]:
    try:
        return ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return None


def _mac(value: Any) -> str:
    """A MAC as the bridge table writes it, or ``""`` if it is not one."""
    raw = str(value or "").strip().upper().replace("-", ":")
    octets = raw.split(":")
    if len(octets) == 6 and all(re.fullmatch(r"[0-9A-F]{1,2}", o) for o in octets):
        return ":".join(o.zfill(2) for o in octets)
    return ""


def _joined(values: Iterable[Any], limit: int = 4) -> str:
    """A short list as one cell, saying how many it left out."""
    items = [str(v) for v in values if str(v)]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


# --------------------------------------------------------------------------- #
# where: locating an address in the fabric
# --------------------------------------------------------------------------- #


def _arp_bindings(state: FabricState) -> List[Tuple[str, NeighborCache, NeighborEntry, str]]:
    """Every ARP and ND entry in the fabric, as (node, cache, entry, report)."""
    return [
        (node, cache, entry, report)
        for report in ("arp", "nd")
        for node, cache, entry in state.sub_items(report, "entries")
    ]


def _es_names(state: FabricState) -> Dict[str, Tuple[str, ...]]:
    """ESI -> what the segment is called on the nodes that have it configured."""
    names: Dict[str, set] = {}
    for _node, segment in state.items("es"):
        if segment.esi:
            names.setdefault(segment.esi, set()).add(segment.name)
    return {esi: tuple(sorted(found)) for esi, found in names.items()}


def _port_segments(state: FabricState) -> Dict[Tuple[str, str], EthernetSegment]:
    """(node, port) -> the ethernet-segment that port belongs to on that node."""
    return {
        (node, port): segment
        for node, segment in state.items("es")
        if segment.esi
        for port in segment.interfaces
        if port
    }


def _next_hop_text(hop: RouteNextHop) -> str:
    """A route's next-hop, with the tunnels or interfaces it leaves by."""
    tunnels = [e.value.split("/", 1)[0] for e in hop.egress if e.kind == "tunnel"]
    interfaces = [e.value for e in hop.egress if e.kind != "tunnel" and e.value]
    via = ""
    if tunnels:
        kinds = sorted({e.tunnel for e in hop.egress if e.kind == "tunnel" and e.tunnel})
        via = f"{'/'.join(kinds) or 'tunnel'} {', '.join(dict.fromkeys(tunnels))}"
    elif interfaces:
        via = ", ".join(dict.fromkeys(interfaces))
    return f"{hop.address or hop.type or '?'}" + (f" ({via})" if via else "")


def _bgp_host_routes(state: FabricState, address: Any) -> List[Sighting]:
    """The active host routes to *address* that BGP installed, on every node."""
    found = []
    for report in ("ipv4_rib", "ipv6_rib"):
        for node, table in state.items(report):
            for route in table.routes:
                if not route.active or not route.type.startswith("bgp"):
                    continue
                network = _network(route.prefix)
                if (
                    network is None
                    or network.version != address.version
                    or network.prefixlen != network.max_prefixlen
                    or network.network_address != address
                ):
                    continue
                found.append(
                    Sighting(
                        node=node,
                        ni=table.ni,
                        kind="bgp",
                        address=str(address),
                        origin=route.type,
                        prefix=route.prefix,
                        next_hops=tuple(_next_hop_text(hop) for hop in route.next_hops),
                    )
                )
    return found


def lens_where(state: FabricState, target: str = "") -> List[Sighting]:
    """Every place in the fabric that knows about one MAC or IP address.

    Answers the question a bridge-table dump does not: not *what is in this
    node's table*, but *which node owns this address, which node learned it
    over the overlay, and do two of them disagree about that*.

    An IP is resolved through ARP or ND to a MAC first, so ``where 10.0.1.51``
    and ``where 00:C1:AB:00:01:21`` converge on the same answer. An IP no
    interface has and no binding names can still be a host route BGP
    learned - a loopback behind a CE, an EVPN RT-5 host - and each node that
    installed one says how it learned it and where it forwards.
    """
    wanted = str(target or "").strip()
    if not wanted:
        raise ValueError("where needs a MAC or IP address to look for")

    sightings: List[Sighting] = []
    mac = _mac(wanted)
    address = _address(wanted)
    if not mac and address is None:
        raise ValueError(f"'{wanted}' is neither a MAC nor an IP address")

    # An IP is first of all somebody's own: a loopback, a system address, the
    # gateway of an irb. Beyond that it is a way of naming a MAC: resolve it
    # through ARP or ND, report the bindings that did so, and carry on with
    # what they resolved to.
    if address is not None:
        for node, instance, itf in state.sub_items("ni", "interfaces"):
            for prefix in itf.prefixes:
                network = _network(prefix)
                if network is not None and _address(prefix.split("/", 1)[0]) == address:
                    sightings.append(
                        Sighting(
                            node=node,
                            ni=instance.name,
                            kind="configured",
                            address=str(address),
                            interface=itf.name,
                            prefix=prefix,
                        )
                    )
        for node, cache, entry, report in _arp_bindings(state):
            if _address(entry.address) != address:
                continue
            bound = _mac(entry.mac)
            mac = mac or bound
            # An ARP entry ages out; an ND entry's timer only moves it to its
            # next reachability state, which is not an expiry.
            ages_out = report == "arp" and entry.expires_in is not None
            sightings.append(
                Sighting(
                    node=node,
                    ni=", ".join(cache.nis),
                    kind="arp" if report == "arp" else "neighbor",
                    address=str(address),
                    interface=cache.interface,
                    origin=text(entry.origin),
                    mac=bound or entry.mac,
                    expiry=countdown(entry.expires_in) if ages_out else "",
                )
            )
        sightings.extend(_bgp_host_routes(state, address))
        if not mac:
            if not sightings:
                nodes = set(state.nodes("ni")) | set(state.nodes("arp")) | set(state.nodes("nd"))
                nodes |= set(state.nodes("ipv4_rib")) | set(state.nodes("ipv6_rib"))
                sightings.append(
                    Sighting(node="", ni="", kind="not-found", address=str(address), searched=len(nodes))
                )
            return sightings

    segments = _es_names(state)
    ports = _port_segments(state)
    # Network-instance -> the sightings learned locally in it, by position.
    local: Dict[str, List[int]] = {}
    for node, table, entry in state.sub_items("mac", "entries"):
        if _mac(entry.address) != mac:
            continue
        if entry.local:
            local.setdefault(table.ni, []).append(len(sightings))
            # A bridge table names the subinterface; a segment names its port.
            segment = ports.get((node, parent(str(entry.interface or ""))))
            sightings.append(
                Sighting(
                    node,
                    table.ni,
                    "local",
                    mac,
                    interface=entry.interface,
                    origin=text(entry.type),
                    esi=segment.esi if segment else "",
                    segments=(segment.name,) if segment else (),
                )
            )
            continue
        sightings.append(
            Sighting(
                node,
                table.ni,
                "remote",
                mac,
                # Over MPLS there is no VTEP: the far-end PE is what it sits behind.
                vtep=entry.vtep or entry.far_end,
                esi=entry.esi,
                origin=text(entry.type),
                overlay=entry.overlay or ("mpls" if entry.far_end else ""),
                vni=entry.vni if entry.vni is not None else entry.label,
                segments=segments.get(entry.esi, ()) if entry.esi else (),
            )
        )

    # Two nodes both owning one MAC locally is legitimate when they are the two
    # sides of a multihomed segment, and is a duplicate or a silent move
    # otherwise. When every node learned it on a port of one and the same
    # segment it is multihomed; when not, the bridge table cannot say which,
    # so say that it cannot: each local sighting becomes a duplicate naming
    # the others.
    for indexes in local.values():
        owners = sorted({sightings[i].node for i in indexes})
        if len(owners) < 2:
            continue
        esis = {sightings[i].esi for i in indexes}
        kind = "multihomed" if len(esis) == 1 and "" not in esis else "duplicate"
        for i in indexes:
            sighting = sightings[i]
            sightings[i] = replace(
                sighting,
                kind=kind,
                also_on=tuple(node for node in owners if node != sighting.node),
            )

    if not sightings:
        sightings.append(
            Sighting(
                node="",
                ni="",
                kind="not-found",
                address=mac or wanted,
                searched=len(state.nodes("mac")),
            )
        )
    return sightings


def _sighting_detail(sighting: Sighting) -> str:
    """What a reader wants to know about a sighting beyond where it is."""
    if sighting.kind in ("arp", "neighbor"):
        detail = f"{sighting.mac or '?'}, {sighting.origin}"
        return detail + (f", expires {sighting.expiry}" if sighting.expiry else "")
    if sighting.kind == "not-found":
        if _mac(sighting.address):
            return f"no node reports it in any bridge table ({sighting.searched} searched)"
        return (
            "no interface has it, no ARP or ND entry names it and no BGP host "
            f"route leads to it ({sighting.searched} nodes searched)"
        )
    if sighting.kind == "configured":
        return f"{sighting.prefix} configured on {sighting.interface}"
    if sighting.kind == "bgp":
        hops = "; ".join(sighting.next_hops) or "no next-hop"
        return f"{sighting.prefix} learned by {sighting.origin}, next-hop {hops}"
    detail = sighting.origin
    if sighting.vtep:
        detail += f", overlay {sighting.overlay}"
        detail += f", vni {sighting.vni}" if sighting.vni is not None else ""
    elif sighting.esi:
        detail += f", segment {', '.join(sighting.segments) or 'not local'}"
        detail += f" ({sighting.esi})" if sighting.kind != "remote" else ""
    if sighting.kind == "multihomed":
        return detail + (
            f"; also learned locally on {', '.join(sighting.also_on)}, "
            f"on the same ethernet-segment"
        )
    if sighting.also_on:
        detail += (
            f"; also learned locally on {', '.join(sighting.also_on)}: expected on "
            "an all-active segment, a move or a duplicate otherwise"
        )
    return detail


WHERE_COLUMNS: Tuple[Column, ...] = (
    Column("NI", "ni"),
    Column("Found", "kind"),
    Column("Address", "address"),
    Column(
        "Via",
        lambda s: s.interface or s.vtep or s.esi or ", ".join(h.split(" ", 1)[0] for h in s.next_hops),
    ),
    Column("Detail", _sighting_detail),
)

_SIGHTING_STATE = {
    "configured": _UP,
    "local": _UP,
    "arp": _UP,
    "neighbor": _UP,
    "bgp": _UP,
    "multihomed": _UP,
    "duplicate": _WARN,
    "not-found": _DOWN,
}


def _sighting_item(s: Sighting) -> Item:
    details: List[Detail] = []
    if s.prefix:
        details.append(Detail("Prefix", s.prefix))
    if s.interface:
        details.append(Detail("Interface", s.interface))
    if s.vtep:
        details.append(Detail("VTEP", s.vtep))
    if s.esi:
        details.append(Detail("ESI", s.esi))
        details.append(Detail("Segment", tuple(s.segments) or "not local", "" if s.segments else _WARN))
    if s.overlay:
        details.append(Detail("Overlay", f"{s.overlay} vni {s.vni}" if s.vni is not None else s.overlay))
    if s.mac:
        details.append(Detail("MAC", s.mac))
    if s.origin:
        details.append(Detail("Learned by" if s.kind == "bgp" else "Origin", s.origin))
    if s.next_hops:
        details.append(Detail("Next-hop", tuple(s.next_hops)))
    if s.expiry:
        details.append(Detail("Expires", s.expiry))
    if s.also_on and s.kind == "multihomed":
        details.append(Detail("Also learned locally on", tuple(s.also_on)))
        details.append(Detail("Note", "learned on the same ethernet-segment on each node"))
    elif s.also_on:
        details.append(
            Detail(
                "Also learned locally on",
                tuple(s.also_on),
                _WARN,
            )
        )
        details.append(
            Detail("Note", "expected on an all-active segment, a move or a duplicate otherwise")
        )
    return Item(
        title=f"{s.kind} in {s.ni}" if s.ni else s.kind,
        state=_SIGHTING_STATE.get(s.kind, ""),
        details=tuple(details),
    )


def tree_where(sightings: List[Sighting]) -> List[Card]:
    """One card per address the answer is about: the IP looked up, the MAC found."""
    cards = []
    for address in dict.fromkeys(s.address for s in sightings):
        found = [s for s in sightings if s.address == address]
        if all(s.kind == "not-found" for s in found):
            cards.append(
                Card(title=address, subtitle=_sighting_detail(found[0]), icon="🔍", state=_DOWN, badge="not found")
            )
            continue
        kinds: Dict[str, int] = {}
        for s in found:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        entries = _entries(found, _sighting_item, "sighting")
        cards.append(
            Card(
                title=address,
                subtitle=", ".join(f"{kind} on {_count(n, 'node')}" for kind, n in kinds.items()),
                icon="📍",
                state=_worst(e.state for e in entries),
                badge=_count(len(entries), "node"),
                entries=entries,
            )
        )
    return cards


# --------------------------------------------------------------------------- #
# path: how the fabric would forward towards an address
# --------------------------------------------------------------------------- #


def _rib_report(address: Any) -> str:
    return "ipv6_rib" if getattr(address, "version", 4) == 6 else "ipv4_rib"


def _routes(state: FabricState, report: str, node: str, ni: str) -> List[Route]:
    """The active routes of one network-instance on one node."""
    return [
        route
        for table_node, table in state.items(report)
        if table_node == node and table.ni == ni
        for route in table.routes
        if route.active
    ]


def _lpm(routes: Sequence[Route], address: Any) -> Optional[Route]:
    """The longest prefix among *routes* that contains *address*."""
    best, best_len = None, -1
    for route in routes:
        network = _network(route.prefix)
        if network is None or network.version != address.version:
            continue
        if address in network and network.prefixlen > best_len:
            best, best_len = route, network.prefixlen
    return best


def _lldp_peers(state: FabricState) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """(node, interface) -> the (node, port) on the other end of that cable."""
    index = state.alias_index()
    peers: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for node, itf, neighbor in state.sub_items("lldp", "neighbors"):
        if out_of_band(itf.name):
            continue
        resolved = resolve(neighbor.system_name, index) if neighbor.system_name else None
        if resolved:
            peers[(node, itf.name)] = (resolved, neighbor.port_id)
    return peers


def _neighbor_index(state: FabricState) -> Dict[Tuple[str, str], List[Tuple[str, str, str]]]:
    """(node, address) -> every (interface, MAC, origin) ARP or ND binds it to."""
    index: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = {}
    for node, cache, entry, _report in _arp_bindings(state):
        address = _address(entry.address)
        if address is not None:
            index.setdefault((node, str(address)), []).append(
                (cache.interface, entry.mac, text(entry.origin))
            )
    return index


def _starting_nodes(state: FabricState, source: str, ni: str) -> List[str]:
    """Where a walk begins: a named node, or whoever knows the source address."""
    known = state.nodes("ipv4_rib") or state.nodes("lldp")
    index = state.alias_index()
    resolved = resolve(source, index)
    if resolved:
        return [resolved]

    address = _address(source)
    if address is None:
        raise ValueError(f"'{source}' is neither a node in the inventory nor an address")

    # An address starts the walk wherever it is attached: on the node that
    # resolved it itself, through ARP or ND on a routed port or as the irb
    # gateway that answered. An anycast gateway spreads that binding over
    # EVPN to every leaf carrying the irb, so a binding learned that way only
    # counts where the host's MAC is on a port of the node's own - the sides
    # of its segment - rather than on every leaf in the fabric. Failing any
    # binding, a connected route covering it.
    own_macs = {
        (node, _mac(entry.address))
        for node, _table, entry in state.sub_items("mac", "entries")
        if entry.local
    }
    starts = []
    for node, _cache, entry, _report in _arp_bindings(state):
        if _address(entry.address) != address or node in starts:
            continue
        if text(entry.origin) != "evpn" or (node, _mac(entry.mac)) in own_macs:
            starts.append(node)
    if starts:
        return starts
    for node in known:
        route = _lpm(_routes(state, _rib_report(address), node, ni), address)
        if route and text(route.type) in ("local", "host", "direct"):
            starts.append(node)
    if not starts:
        raise ValueError(
            f"no node has {address} directly attached in network-instance '{ni}'; "
            "name the node to start from instead"
        )
    return starts


#: One lookup still to do: the node and network-instance to do it in, the
#: address to look up, the hop it is on, the steps taken to get there, and -
#: when it is an underlay leg chasing a tunnel endpoint on behalf of a VRF -
#: the node and network-instance the tunnel was taken from and the address
#: to resume with once the endpoint is reached.
_Pending = Tuple[str, str, Any, int, Tuple[str, ...], Optional[Tuple[str, str, Any]]]


def _resume_instance(state: FabricState, node: str, origin_node: str, origin_ni: str, address: Any) -> str:
    """The network-instance a tunnel's payload is looked up in at its far end.

    A leaf's VTEP and a gateway's WAN loopback both land the packet in a VRF
    of the node that owns them. Most fabrics call that VRF the same on every
    node, but a gateway stitching two datacenters need not: what ties the two
    together is the route-target the origin's instance exports and the far
    end's imports. Failing both, the VRF that has a route to the address.
    """
    instances = {inst.name: inst for n, inst in state.items("ni") if n == node}
    if origin_ni in instances or not instances:
        return origin_ni
    origin = next((inst for n, inst in state.items("ni") if n == origin_node and inst.name == origin_ni), None)
    exported = {rt for inst in (origin.instances if origin else ()) for rt in inst.export_rts}
    vrfs = [inst for inst in instances.values() if text(inst.type) == "ip-vrf"]
    by_target = [
        inst
        for inst in vrfs
        if exported & {rt for bgp in inst.instances for rt in bgp.import_rts}
    ]
    candidates = by_target or vrfs
    with_route = [
        inst
        for inst in candidates
        if _lpm(_routes(state, _rib_report(address), node, inst.name), address) is not None
    ]
    chosen = with_route or candidates
    return chosen[0].name if chosen else origin_ni


def lens_path(
    state: FabricState,
    source: str = "",
    destination: str = "",
    ni: str = "default",
) -> List[Hop]:
    """How the fabric would forward from one place to an address, hop by hop.

    Computed from the route tables rather than probed: at each node the
    destination is looked up, the egress interface that lookup selects is
    matched against LLDP to find the node on the other end, and the walk
    continues there. ECMP is followed on every branch, so the table shows the
    whole fan-out rather than one arbitrary path through it.

    A lookup in a VRF that resolves onto a tunnel hands the walk back to the
    underlay: the walk switches to the default instance and continues towards
    the tunnel's endpoint - a VTEP over VXLAN, a far-end gateway over LDP or
    SR-MPLS - which is how the two tables actually compose on the wire. When
    the endpoint is reached the packet is decapsulated and the walk resumes
    in the VRF there, so a DCI path traces end to end: VXLAN to the DC
    gateway, MPLS across to the far gateway, VXLAN again to the leaf.

    A lookup that matches a route leaked from another instance crosses into
    that instance the same way, since the route is forwarded by that
    instance's next-hops: out of one of its ports, or into a tunnel that
    lands in its counterpart at the far end rather than in this one's.

    The final hop of a delivered destination includes the ARP or ND entry for
    it, confirming the host is reachable, or noting when no binding exists.
    """
    target = _address(destination)
    if target is None:
        raise ValueError(f"'{destination}' is not an IP address")
    report = _rib_report(target)
    peers = _lldp_peers(state)
    neighbors = _neighbor_index(state)
    hops: List[Hop] = []

    pending: List[_Pending] = [
        (node, ni, target, 1, (), None) for node in _starting_nodes(state, source, ni)
    ]
    # ECMP branches fan out and converge again: every spine leads to the same
    # gateway, every gateway to the same far end. A lookup already made on
    # another branch is one answer, reported where it was first reached.
    made: set = set()
    while pending:
        node, instance, address, hop, seen, resume = pending.pop(0)
        # A walk is going in circles when it looks the same address up in the
        # same place twice on one branch; a gateway's underlay is walked once
        # towards its VTEP and again towards the far gateway, and that is no
        # loop.
        step = f"{node}/{instance}" if address == target else f"{node}/{instance}@{address}"
        here = dict(hop=hop, node=node, ni=instance, address=str(address))
        if hop > MAX_HOPS or step in seen:
            hops.append(
                Hop(**here, outcome="loop", visited=seen)
                if step in seen
                else Hop(**here, outcome="too-long")
            )
            continue
        # Another branch converging on a lookup already made is not a loop
        # but not news either.
        resumed = (resume[1], str(resume[2])) if resume else None
        lookup = (node, instance, str(address), resumed)
        if lookup in made:
            continue
        made.add(lookup)

        route = _lpm(_routes(state, report, node, instance), address)
        if route is None:
            hops.append(Hop(**here, outcome="no-route"))
            continue

        kind = text(route.type)
        # Where the route leaves the node: every port, tunnel or unresolved
        # prefix of every next-hop, in next-hop order.
        leaves = [hop for nh in route.next_hops for hop in nh.egress]
        matched = dict(
            prefix=route.prefix,
            route_type=kind,
            next_hops=tuple(nh.address or nh.resolving_route for nh in route.next_hops),
        )

        # A leaked route is another instance's route with that instance's
        # next-hops: its port, or its tunnel with its VNI. The walk crosses
        # into it before following them, so that what comes next is read
        # where it actually is - the neighbour on a port of that instance,
        # the VRF its tunnel lands in at the far end.
        if route.leaked_from and route.leaked_from != instance:
            hops.append(Hop(**here, **matched, outcome="leaked", resumes_in=route.leaked_from))
            seen, hop, instance = seen + (step,), hop + 1, route.leaked_from
            step = f"{node}/{instance}" if address == target else f"{node}/{instance}@{address}"
            here = dict(hop=hop, node=node, ni=instance, address=str(address))
            made.add((node, instance, str(address), resumed))

        # A route that resolved onto a tunnel names it rather than an
        # interface: VXLAN to a VTEP from a leaf, LDP or SR-MPLS to a far-end
        # gateway from a DCGW. The walk hands off to the underlay there: one
        # branch per endpoint, looked up again in the default instance, which
        # is how the two route tables actually compose on the wire.
        tunnels = [hop for hop in leaves if hop.kind == "tunnel" and _address(hop.value.split("/", 1)[0])]
        if tunnels:
            for tunnel in tunnels:
                endpoint = tunnel.value.split("/", 1)[0]
                hops.append(
                    Hop(
                        **here,
                        **matched,
                        outcome="tunnel",
                        egress=f"{tunnel.tunnel}:{endpoint}",
                        tunnel=tunnel.tunnel,
                        endpoint=endpoint,
                    )
                )
                # Carry the VRF and destination along, so the walk can resume
                # in the VRF once the endpoint is reached. A tunnel taken on
                # the way to another's endpoint keeps the outer one.
                pending.append(
                    (
                        node,
                        "default",
                        _address(endpoint),
                        hop + 1,
                        seen + (step,),
                        resume if resume is not None else (node, instance, address),
                    )
                )
            continue

        # Anything else the route leaves through is looked up against LLDP by
        # its port; a prefix the chain stopped at, or a tunnel of another
        # kind, has no neighbour and ends the walk saying so.
        egress = [hop.label for hop in leaves]
        if kind in _ATTACHED or not egress:
            # An attached route has one interface, or none at all when the
            # destination is the node itself.
            attached = egress or [""]
            if resume is not None:
                # The underlay delivered the tunnel endpoint: the packet is
                # decapsulated and looked up in the VRF on the node that owns
                # it - the one the origin's VRF sends to, whatever it is called.
                origin_node, origin_ni, resume_address = resume
                resume_ni = _resume_instance(state, node, origin_node, origin_ni, resume_address)
                for interface in attached:
                    hops.append(
                        Hop(**here, **matched, outcome="endpoint-reached", egress=interface, resumes_in=resume_ni)
                    )
                pending.append((node, resume_ni, resume_address, hop + 1, seen + (step,), None))
                continue
            for interface in attached:
                hops.append(Hop(**here, **matched, outcome="delivered", egress=interface))
            if address != target:
                continue
            # The destination itself is delivered: confirm the last mile.
            last = dict(hop=hop + 1, node=node, ni=instance, address=str(target))
            if kind == "host":
                # A host route is the node's own address (a loopback, system0):
                # there is no neighbour to resolve.
                for interface in attached:
                    hops.append(Hop(**last, outcome="local-ip", egress=interface))
                continue
            bound = neighbors.get((node, str(target)), [])
            for interface, mac, origin in bound:
                hops.append(
                    Hop(**last, outcome="neighbor", egress=interface, mac=mac, origin=origin)
                )
            if not bound:
                hops.append(Hop(**last, outcome="no-neighbor"))
            continue

        for subinterface in egress:
            peer = peers.get((node, parent(subinterface)))
            if peer is None:
                hops.append(Hop(**here, **matched, outcome="dead-end", egress=subinterface))
                continue
            hops.append(
                Hop(
                    **here,
                    **matched,
                    outcome="forwarded",
                    egress=subinterface,
                    peer=peer[0],
                    peer_port=peer[1],
                )
            )
            pending.append((peer[0], instance, address, hop + 1, seen + (step,), resume))

    hops.sort(key=lambda h: (h.hop, h.node, h.egress))
    return hops


#: The sentence the Detail column makes of each outcome. Every outcome has one,
#: even where it is empty, so that a new outcome cannot render as nothing by
#: accident.
_HOP_DETAIL: Dict[str, Callable[[Hop], str]] = {
    "forwarded": lambda h: "",
    "dead-end": lambda h: (
        f"no LLDP neighbour on {parent(h.egress)}, the path stops being traceable here"
    ),
    "tunnel": lambda h: f"over {h.tunnel} to {h.endpoint}, continuing in default",
    "endpoint-reached": lambda h: f"tunnel endpoint reached, continuing in {h.resumes_in}",
    "leaked": lambda h: f"leaked from {h.resumes_in}, continuing there",
    "delivered": lambda h: f"delivered here, {h.route_type} on {h.egress or 'this node'}",
    "local-ip": lambda h: f"locally configured on {h.egress or 'this node'}",
    "neighbor": lambda h: f"{h.mac} on {h.egress}, {h.origin}",
    "no-neighbor": lambda h: f"no ARP/ND entry for {h.address} on this node",
    "no-route": lambda h: f"nothing in {h.ni} matches {h.address}",
    "loop": lambda h: f"already visited on this path ({' -> '.join(h.visited)})",
    "too-long": lambda h: f"still not delivered after {MAX_HOPS} hops",
}

#: Outcomes that are about the delivered address rather than a route, so the
#: Prefix column shows the address the walk was confirming.
_LAST_MILE = ("local-ip", "neighbor", "no-neighbor")


PATH_COLUMNS: Tuple[Column, ...] = (
    Column("Hop", "hop"),
    Column("NI", "ni"),
    Column("Prefix", lambda h: h.prefix or (h.address if h.outcome in _LAST_MILE else "-")),
    Column("Type", lambda h: h.route_type or h.outcome),
    Column("Next-hop", lambda h: h.mac or _joined(h.next_hops)),
    Column("Egress", "egress"),
    Column("Peer", lambda h: f"{h.peer} {h.peer_port}".strip()),
    Column("Detail", lambda h: _HOP_DETAIL[h.outcome](h)),
)

#: What each outcome says about the walk: reaching or being delivered is
#: good, a hop that only goes on says nothing yet, and a stop is a fault.
_HOP_STATE = {
    "delivered": _UP,
    "local-ip": _UP,
    "neighbor": _UP,
    "endpoint-reached": _UP,
    "dead-end": _DOWN,
    "no-route": _DOWN,
    "no-neighbor": _DOWN,
    "loop": _DOWN,
    "too-long": _DOWN,
}


def _hop_item(h: Hop) -> Item:
    details = [Detail("Outcome", h.outcome, _HOP_STATE.get(h.outcome, ""))]
    if h.route_type:
        details.append(Detail("Route", f"{h.prefix} ({h.route_type})"))
    if h.next_hops:
        details.append(Detail("Next-hop", tuple(h.next_hops)))
    if h.mac:
        details.append(Detail("MAC", f"{h.mac} ({h.origin})" if h.origin else h.mac))
    if h.egress:
        details.append(Detail("Egress", h.egress))
    if h.peer:
        details.append(Detail("Peer", f"{h.peer} {h.peer_port}".strip()))
    if h.tunnel:
        details.append(Detail("Tunnel", f"{h.tunnel} to {h.endpoint}"))
    if h.resumes_in:
        details.append(Detail("Resumes in", h.resumes_in))
    sentence = _HOP_DETAIL[h.outcome](h)
    if sentence:
        details.append(Detail("Detail", sentence))
    return Item(
        title=f"{h.ni}: {h.prefix or h.address}",
        state=_HOP_STATE.get(h.outcome, ""),
        details=tuple(details),
    )


def graph_path(hops: List[Hop]) -> Dict[str, Any]:
    """The walk as a graph: one box per lookup, an edge to each lookup it leads to.

    Boxes sit in the column of their hop, so ECMP fans out across a column
    and converges again where branches meet: every leaf's underlay lookup
    leads to the same two spines, both spines to the same gateway. What a
    lookup did says where the packet goes next - out of a port to the LLDP
    peer, into a tunnel and so into the underlay on the same node, out of a
    tunnel and so into the VRF, along a leaked route and so into the instance
    it came from, off an attached interface and so to the host - and that is
    the edge. A lookup that stopped the walk has no edge out.
    """
    if not hops:
        return {"nodes": [], "edges": [], "destination": ""}
    # The first hop looked up the destination; the underlay legs look up
    # tunnel endpoints on its behalf.
    destination = hops[0].address
    boxes: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
    for h in hops:
        key = (h.hop, h.node, h.ni, h.address)
        box = boxes.get(key)
        if box is None:
            last_mile = h.outcome in _LAST_MILE
            box = boxes[key] = {
                "id": _box_id(*key),
                "hop": h.hop,
                "node": h.node,
                "ni": h.ni,
                "address": h.address,
                "title": h.address if last_mile else h.node,
                "subtitle": "" if last_mile else (h.ni if h.address == destination else f"{h.ni} · {h.address}"),
                "state": "",
                "outcomes": [],
                "details": [],
            }
        box["outcomes"].append(h.outcome)
        box["state"] = _worst([box["state"], _HOP_STATE.get(h.outcome, "")])
        line = _HOP_DETAIL[h.outcome](h)
        if h.outcome == "neighbor":
            box["subtitle"] = f"{h.mac} on {h.egress}"
        elif h.outcome == "local-ip":
            box["subtitle"] = f"own address on {h.egress}" if h.egress else "own address"
        elif h.outcome == "no-neighbor":
            box["subtitle"] = "no ARP/ND entry"
        if line and line not in box["details"]:
            box["details"].append(line)

    by_place: Dict[Tuple[str, str, str], List[Tuple[int, str, str, str]]] = {}
    for key in boxes:
        by_place.setdefault(key[1:], []).append(key)

    def successor(h: Hop, node: str, ni: str, address: str) -> Optional[str]:
        """The box a lookup leads to: at the next hop, or wherever another
        branch reached the same place first."""
        candidates = by_place.get((node, ni, address), [])
        exact = [key for key in candidates if key[0] == h.hop + 1]
        chosen = exact or sorted(candidates)
        return _box_id(*chosen[0]) if chosen else None

    edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    stops: List[Dict[str, Any]] = []
    for h in hops:
        source = _box_id(h.hop, h.node, h.ni, h.address)
        if h.outcome == "forwarded":
            to, label = successor(h, h.peer, h.ni, h.address), h.egress
        elif h.outcome == "tunnel":
            to, label = successor(h, h.node, "default", h.endpoint), h.egress
        elif h.outcome == "endpoint-reached":
            to, label = successor(h, h.node, h.resumes_in, destination), f"into {h.resumes_in}"
        elif h.outcome == "leaked":
            to, label = successor(h, h.node, h.resumes_in, h.address), f"leaked from {h.resumes_in}"
        elif h.outcome == "delivered":
            to, label = successor(h, h.node, h.ni, destination), h.egress
        elif h.outcome == "dead-end":
            # A branch that dies on a port is still a branch of the fan-out:
            # it goes out of the port to nowhere the walk can see.
            to, label = f"{h.hop + 1}:stop/{h.node}/{h.egress}", h.egress
            if not any(stop["id"] == to for stop in stops):
                stops.append(
                    {
                        "id": to,
                        "hop": h.hop + 1,
                        "node": "",
                        "ni": "",
                        "address": h.address,
                        "title": "no neighbour",
                        "subtitle": f"on {parent(h.egress)}",
                        "state": _DOWN,
                        "outcomes": ["dead-end"],
                        "details": [_HOP_DETAIL["dead-end"](h)],
                    }
                )
        else:
            continue
        if to is None:
            continue
        edges.setdefault((source, to, label), {"from": source, "to": to, "label": label, "state": _HOP_STATE.get(h.outcome, "")})
    return {
        "destination": destination,
        "nodes": [boxes[key] for key in sorted(boxes)] + sorted(stops, key=lambda stop: stop["id"]),
        "edges": list(edges.values()),
    }


def _box_id(hop: int, node: str, ni: str, address: str) -> str:
    return f"{hop}:{node}/{ni}@{address}"


def tree_path(hops: List[Hop]) -> List[Card]:
    """One card per hop of the walk, the nodes reached at it inside."""
    cards = []
    for number in sorted({h.hop for h in hops}):
        at = [h for h in hops if h.hop == number]
        entries = _entries(at, _hop_item, "lookup", sort=False)
        outcomes: Dict[str, int] = {}
        for h in at:
            outcomes[h.outcome] = outcomes.get(h.outcome, 0) + 1
        cards.append(
            Card(
                title=f"Hop {number}",
                subtitle=", ".join(f"{n} {outcome}" for outcome, n in outcomes.items()),
                icon="🧭",
                state=_worst(e.state for e in entries),
                badge=_count(len(entries), "node"),
                entries=entries,
            )
        )
    return cards


# --------------------------------------------------------------------------- #
# service: one service, everywhere it exists
# --------------------------------------------------------------------------- #


def lens_service(state: FabricState, name: str = "") -> List[Service]:
    """One network-instance as every node that carries it sees it.

    The services reports are a per-node projection - this leaf's view of every
    service on it. Troubleshooting a service wants the transpose: one service,
    one record per node, so that the node whose VNI or route-target does not
    match the others is a column to read down rather than four tables to
    compare.
    """
    wanted = str(name or "").strip()
    if not wanted:
        raise ValueError("service needs the name of a network-instance")
    pattern = re.compile(wanted, re.IGNORECASE)

    # vxlan-interface -> what that node sends on it.
    overlays = {(node, vxlan.name): vxlan for node, vxlan in state.items("vxlan")}
    macs: Dict[Tuple[str, str], List[Any]] = {}
    for node, table, entry in state.sub_items("mac", "entries"):
        macs.setdefault((node, table.ni), []).append(entry)
    segments: Dict[Tuple[str, str], List[str]] = {}
    for node, segment in state.items("es"):
        for association in segment.associations:
            segments.setdefault((node, association.ni), []).append(segment.name)

    services: List[Service] = []
    for node, instance in state.items("ni"):
        if not pattern.search(instance.name):
            continue
        vnis, vteps = [], []
        for overlay in instance.overlays:
            found = overlays.get((node, overlay))
            if found is None:
                continue
            if found.vni is not None:
                vnis.append(found.vni)
            vteps.extend(d.vtep for d in found.destinations)
        entries = macs.get((node, instance.name), [])
        local = sum(1 for e in entries if e.local)
        services.append(
            Service(
                node=node,
                ni=instance.name,
                type=instance.type,
                oper=instance.oper,
                evis=instance.evis,
                vnis=tuple(vnis),
                import_rts=instance.import_rts,
                export_rts=instance.export_rts,
                interfaces=tuple(
                    Interface(name=i.name, oper=text(i.oper)) for i in instance.interfaces
                ),
                bound=tuple(sorted({ni for i in instance.interfaces for ni in i.associated})),
                vteps=tuple(sorted(set(vteps))),
                local_macs=local,
                remote_macs=len(entries) - local,
                segments=tuple(sorted(set(segments.get((node, instance.name), [])))),
                instances=instance.instances,
            )
        )

    if not services:
        raise ValueError(
            f"no network-instance matching '{wanted}' on any of the "
            f"{len(state.nodes('ni'))} node(s) collected"
        )

    # Which underlay each node carries the service in: nodes whose VTEPs
    # cannot reach each other are never one service, however it is named.
    system, hosts = system_addresses(state), underlay_hosts(state)
    sited: List[Service] = []
    for name in dict.fromkeys(s.ni for s in services):
        mine = [s for s in services if s.ni == name]
        gateways = {s.node for s in mine if len(s.instances) > 1}
        domains = underlay_domains([s.node for s in mine], system, hosts, gateways)
        site = {node: str(index) for index, domain in enumerate(domains, start=1) for node in domain}
        sited.extend(
            replace(s, site=site[s.node] if len(domains) > 1 else "") for s in mine
        )
    return sited


SERVICE_COLUMNS: Tuple[Column, ...] = (
    Column("NI", "ni"),
    Column("Type", "type"),
    Column("Oper", "oper"),
    Column("EVI", lambda s: ", ".join(s.evis)),
    Column("VNI", lambda s: _joined(s.vnis)),
    Column("In-RT", lambda s: ", ".join(s.import_rts)),
    Column("Out-RT", lambda s: ", ".join(s.export_rts)),
    Column("Interfaces", lambda s: _joined(f"{i.name}({i.oper})" for i in s.interfaces)),
    Column("Bound", lambda s: _joined(s.bound)),
    Column("VTEPs", lambda s: _joined(s.vteps)),
    Column(
        "MACs",
        lambda s: (
            f"{s.local_macs} local / {s.remote_macs} remote"
            if s.local_macs or s.remote_macs
            else ""
        ),
    ),
    Column("ES", lambda s: _joined(s.segments)),
    Column("Site", "site"),
)


def _service_item(s: Service) -> Item:
    details = [
        Detail("Type", s.type),
        Detail("Oper", s.oper, _UP if text(s.oper) == "up" else _DOWN),
    ]
    if s.evis:
        details.append(Detail("EVI", tuple(s.evis)))
    if s.vnis:
        details.append(Detail("VNI", tuple(str(v) for v in s.vnis)))
    if s.import_rts:
        details.append(Detail("Import RT", tuple(s.import_rts)))
    if s.export_rts:
        details.append(Detail("Export RT", tuple(s.export_rts)))
    if s.interfaces:
        details.append(
            Detail(
                "Interfaces",
                tuple((i.name, _UP if i.oper == "up" else _DOWN) for i in s.interfaces),
            )
        )
    if s.bound:
        details.append(Detail("Bound", tuple(s.bound)))
    if s.vteps:
        details.append(Detail("VTEPs", tuple(s.vteps)))
    if s.local_macs or s.remote_macs:
        details.append(Detail("MACs", f"{s.local_macs} local / {s.remote_macs} remote"))
    if s.segments:
        details.append(Detail("Ethernet segments", tuple(s.segments)))
    if s.site:
        details.append(Detail("Underlay", s.site))
    return Item(title=s.ni, state=_UP if text(s.oper) == "up" else _DOWN, details=tuple(details))


def tree_service(services: List[Service]) -> List[Card]:
    """One card per network-instance, every node that carries it inside.

    A card is marked when the nodes disagree about what the service is: a
    VNI or a route-target that differs on one of them is the thing the
    transpose exists to show.
    """
    cards = []
    for name in dict.fromkeys(s.ni for s in services):
        mine = [s for s in services if s.ni == name]
        entries = _entries(mine, _service_item, "instance")
        # The same questions the evpn_service_mismatch check asks, within
        # each underlay the service is carried in: which nodes are one
        # service, by the route-target they share, and whether those agree on
        # it - fact by fact and instance by instance, so a gateway's WAN side
        # is not a disagreement with the leaves.
        facts = {s.node: service_facts(", ".join(map(str, s.vnis)), s.instances) for s in mine}
        sites = dict.fromkeys(s.site for s in mine)
        split: List[str] = []
        disputed: List[str] = []
        for site in sites:
            within = {s.node: facts[s.node] for s in mine if s.site == site}
            groups = service_groups(within)
            if len(groups) > 1:
                split.append(
                    "; ".join(
                        f"{_joined(within[group[0]].get('import route-target') or ()) or 'none'} on {_joined(group)}"
                        for group in groups
                    )
                )
            disputed.extend(
                fact
                for group in groups
                for fact, _values in service_disagreements({node: within[node] for node in group})
            )
        kind = mine[0].type
        subtitle = kind
        if mine[0].evis:
            subtitle += f", EVI {_joined(mine[0].evis)}"
        if len(sites) > 1:
            subtitle += f", in {len(sites)} underlays"
        if split:
            subtitle += " - two services under one name in one underlay, by route-target: " + " / ".join(split)
        if disputed:
            subtitle += " - nodes disagree on " + ", ".join(dict.fromkeys(disputed))
        cards.append(
            Card(
                title=name,
                subtitle=subtitle,
                icon="🌉" if kind == "mac-vrf" else "🔀" if kind == "ip-vrf" else "📦",
                state=_worst([*(e.state for e in entries), _WARN if disputed or split else ""]),
                badge=_count(len(entries), "node"),
                entries=entries,
            )
        )
    return cards


# --------------------------------------------------------------------------- #
# incidents: the findings, grouped by what they have in common
# --------------------------------------------------------------------------- #

#: Finding and change severities as the state a card or pill is drawn in.
_TONE = {"error": _DOWN, "warning": _WARN, "ok": _UP}


def _severity_of(state: str) -> str:
    """The severity a colour state was drawn for."""
    return next((severity for severity, tone in _TONE.items() if tone == state), "")


def _severity_label(severity: str) -> str:
    """What a severity's badge says: the severity, never the colour's state name."""
    return severity.upper() if severity in _TONE else ""

_INCIDENT_ICONS = {
    "link": "🔗",
    "port": "🔌",
    "node": "🖥",
    "session": "🤝",
    "underlay": "🛤",
    "platform": "🌡",
    "segment": "🧷",
    "finding": "⚠",
    "pattern": "🔁",
}


def lens_incidents(state: FabricState) -> List[Any]:
    """Every check's findings, grouped into incidents with a root cause each.

    The ones someone acknowledged come last, marked as such.
    """
    return mark_acknowledged(correlate(run_checks(state), state), state.acknowledged)


INCIDENT_COLUMNS: Tuple[Column, ...] = (
    Column("Severity", "severity"),
    Column("Incident", "title"),
    Column("Root cause", lambda i: i.root.check),
    Column(
        "Scope",
        lambda i: "fabric-wide"
        if i.kind == "pattern"
        else (f"{len(i.nodes)} nodes" if len(i.nodes) > 1 else "1 node"),
    ),
    Column("Findings", lambda i: len(i.findings)),
    Column("Ack", lambda i: "acknowledged" if i.acknowledged else ""),
    Column("Explanation", "explanation"),
)


def tree_incidents(incidents: List[Any]) -> List[Card]:
    cards = []
    for incident in incidents:
        by_node: Dict[str, List[Item]] = {}
        for finding in incident.findings:
            is_root = finding is incident.root
            by_node.setdefault(finding.node, []).append(
                Item(
                    title=f"{'root cause: ' if is_root else ''}{finding.check} {finding.subject}",
                    state=_TONE.get(finding.severity, ""),
                    label=_severity_label(finding.severity),
                    details=(Detail("detail", finding.detail),),
                )
            )
        entries = tuple(
            Entry(
                title=node,
                state=_worst(item.state for item in items),
                label=_severity_label(_severity_of(_worst(item.state for item in items))),
                badge=_count(len(items), "finding"),
                items=tuple(items),
            )
            for node, items in sorted(by_node.items(), key=lambda kv: (kv[0] != incident.root.node, kv[0]))
        )
        acked = incident.acknowledged
        cards.append(
            Card(
                title=f"✓ {incident.title}" if acked else incident.title,
                subtitle=incident.explanation,
                icon=_INCIDENT_ICONS.get(incident.kind, "⚠"),
                # Acknowledged is known: drawn without the colour that says
                # look here, but still listed until it is fixed.
                state="" if acked else _TONE.get(incident.severity, ""),
                label="ACKNOWLEDGED" if acked else _severity_label(incident.severity),
                badge=("acknowledged · " if acked else "") + _count(len(incident.findings), "finding"),
                entries=entries,
                key=incident.id,
                action="unack" if acked else "ack",
            )
        )
    return cards


# --------------------------------------------------------------------------- #
# changes: what is different from before
# --------------------------------------------------------------------------- #

#: What ``since`` is set to, to compare against the baseline rather than the
#: timeline.
BASELINE = "baseline"


def lens_changes(state: FabricState, since: str = "") -> List[Any]:
    """What changed lately, or how the fabric has drifted from its baseline.

    Answered from :attr:`FabricState.history`, which only the live server
    keeps: everywhere else there is no past to answer from.
    """
    history = state.history
    if history is None:
        return []
    if str(since).strip().lower() == BASELINE:
        return history.drift()
    return history.changes(since=parse_since(since))


CHANGE_COLUMNS: Tuple[Column, ...] = (
    Column("Time", "time"),
    Column("Severity", "severity"),
    Column("Kind", "kind"),
    Column("Subject", "subject"),
    Column("Change", "summary"),
    Column("Detail", "detail"),
)


def tree_changes(changes: List[Any]) -> List[Card]:
    """One card per minute, newest first; the nodes that changed in it inside."""
    buckets: Dict[str, List[Any]] = {}
    for change in changes:
        buckets.setdefault(change.time[:5], []).append(change)
    cards = []
    for minute, members in buckets.items():
        cards.append(
            Card(
                title=minute,
                subtitle=", ".join(sorted({c.kind for c in members})),
                icon="🕒",
                state=_worst(_TONE.get(c.severity, "") for c in members),
                label=_severity_label(_severity_of(_worst(_TONE.get(c.severity, "") for c in members))),
                badge=_count(len(members), "change"),
                entries=_entries(
                    members,
                    lambda c: Item(
                        title=f"{c.kind} {c.subject}",
                        state=_TONE.get(c.severity, ""),
                        label=_severity_label(c.severity),
                        details=(
                            Detail("at", c.time),
                            Detail("change", c.summary),
                            *((Detail("detail", c.detail),) if c.detail and c.detail != c.summary else ()),
                        ),
                    ),
                    "change",
                    by_severity=True,
                ),
            )
        )
    return cards


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

LENSES: Tuple[LensSpec, ...] = (
    LensSpec(
        name="incidents",
        title="Incidents",
        description=(
            "Every check's findings grouped by root cause: a link that is down "
            "together with the BGP, BFD and IGP sessions that went down over it, "
            "a node that stopped answering with everything that points at it. "
            "Start here to find out what is wrong."
        ),
        requires=REQUIRED_REPORTS,
        columns=INCIDENT_COLUMNS,
        run=lens_incidents,
        tree=tree_incidents,
        mcp_name="fabric_incidents",
        group_by_node=False,
    ),
    LensSpec(
        name="changes",
        title="Changes",
        description=(
            "What changed in the fabric and when: sessions, ports, LLDP "
            "neighbours, BFD and IGP adjacencies, designated forwarders, MAC "
            "moves, route counts and findings raised or cleared. 'since' takes "
            "a time span (15m, 2h) or 'baseline' for the drift from the fabric "
            "as it was when the baseline was taken."
        ),
        # The timeline is kept by the server as it streams, not read here.
        requires=(),
        columns=CHANGE_COLUMNS,
        run=lens_changes,
        tree=tree_changes,
        params=(
            ParamSpec(
                name="since",
                label="Since",
                placeholder="15m, 2h or baseline",
                help="How far back to look, or 'baseline' for the drift from the baseline",
            ),
        ),
        mcp_name="recent_changes",
        # Only the live server keeps a timeline to answer from.
        surfaces=frozenset({SERVER}),
        group_by_node=False,
    ),
    LensSpec(
        name="where",
        title="Where",
        description=(
            "Locates a MAC or IP address across the fabric: which node has it "
            "configured or owns it, which nodes learned it over the overlay, "
            "and whether more than one claims it locally."
        ),
        requires=("ni", "mac", "arp", "nd", "es", "ipv4_rib", "ipv6_rib"),
        columns=WHERE_COLUMNS,
        run=lens_where,
        tree=tree_where,
        params=(
            ParamSpec(
                name="target",
                label="Address",
                placeholder="00:C1:AB:00:01:21 or 10.0.1.51",
                help="The MAC or IP address to locate",
                required=True,
            ),
        ),
        mcp_name="locate_address",
    ),
    LensSpec(
        name="path",
        title="Path",
        description=(
            "Walks the route tables hop by hop from a node or address towards a "
            "destination, following every ECMP branch and every tunnel: VXLAN to "
            "the VTEP, MPLS to the far-end gateway, and back into the VRF there."
        ),
        # ``ni`` says which VRF a tunnel lands in at its far end; ``mac`` which
        # leaves a source address is attached to, rather than merely known on.
        requires=("ni", "ipv4_rib", "ipv6_rib", "lldp", "arp", "nd", "mac"),
        columns=PATH_COLUMNS,
        run=lens_path,
        tree=tree_path,
        graph=graph_path,
        params=(
            ParamSpec(
                name="source",
                label="From",
                placeholder="leaf1 or 10.0.1.51",
                help="The node or attached address the walk starts from",
                required=True,
            ),
            ParamSpec(
                name="destination",
                label="To",
                placeholder="10.0.2.51",
                help="The address being forwarded towards",
                kind="address",
                required=True,
            ),
            ParamSpec(
                name="ni",
                label="Network instance",
                placeholder="default",
                help="The instance to look the destination up in",
                kind="ni",
            ),
        ),
        mcp_name="trace_path",
        group_by_node=False,
    ),
    LensSpec(
        name="service",
        title="Service",
        description=(
            "One network-instance as every node that carries it sees it: type, "
            "EVI, VNI, route-targets, bound interfaces, VTEP peers, MAC counts "
            "and ethernet-segments, one row per node."
        ),
        # The RIBs say which nodes share an underlay, and so can disagree at all.
        requires=("ni", "vxlan", "mac", "es", "ipv4_rib", "ipv6_rib"),
        columns=SERVICE_COLUMNS,
        run=lens_service,
        tree=tree_service,
        params=(
            ParamSpec(
                name="name",
                label="Service",
                placeholder="subnet-1",
                help="Network-instance name, matched as a case-insensitive regex",
                required=True,
            ),
        ),
        mcp_name="service_detail",
    ),
)

LENSES_BY_NAME: Dict[str, LensSpec] = {lens.name: lens for lens in LENSES}

#: Every report the lenses read, which is what a surface may have to collect.
LENS_REPORTS: Tuple[str, ...] = tuple(
    dict.fromkeys(report for lens in LENSES for report in lens.requires)
)


def get_lens(name: str) -> LensSpec:
    """Look a lens up by its canonical name."""
    try:
        return LENSES_BY_NAME[name.replace("-", "_")]
    except KeyError:
        raise KeyError(f"unknown lens '{name}'") from None


def lenses_for(surface: str) -> List[LensSpec]:
    """Every lens offered on *surface*, in registry order."""
    return [lens for lens in LENSES if lens.on(surface)]


__all__ = [
    "LENSES",
    "LENSES_BY_NAME",
    "LENS_REPORTS",
    "PATH_COLUMNS",
    "SERVICE_COLUMNS",
    "WHERE_COLUMNS",
    "Card",
    "Detail",
    "Entry",
    "Item",
    "coerce_lens_params",
    "Hop",
    "Interface",
    "LensSpec",
    "Service",
    "Sighting",
    "as_dict",
    "get_lens",
    "lens_path",
    "lens_service",
    "lens_where",
    "lenses_for",
    "graph_path",
    "tree_path",
    "tree_service",
    "tree_where",
]
