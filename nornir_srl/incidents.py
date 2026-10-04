"""Findings grouped into incidents: one root cause and what follows from it.

One broken cable does not produce one finding. It produces an interface down
on both ends, the BGP sessions over it down on both ends, the BFD sessions
protecting them, an IGP adjacency, maybe an ethernet-segment without a
forwarder - a dozen rows in the checks table, each of them true, none of them
saying that they are all the same thing. Reading them is where the time goes.

:func:`correlate` does that reading in code, before anyone - or any model - has
to. Every finding is *anchored* to what it is about: a link (both ends of one
cable), a node, a BGP session between two nodes, a node's underlay route to
another. Findings on the same anchor are one incident, and within it the one
that explains the others is the root: an interface down explains the session
over it, not the other way round.

The anchoring rules are deliberately few, and each one is something the
fabric's own state says rather than a guess:

* a finding about a port or subinterface is on the link that port is cabled
  to - as LLDP sees it now, as it saw it before the link went down (the
  timeline keeps that), or as two ends of one point-to-point subnet say;
* a session is over the port its peer address is reached on - a link-local
  peer names it, any other is on a connected subnet or not directly attached;
* a session whose peer is a node that stopped answering is about that node;
* an overlay session to a loopback the node has no route to is about the
  underlay between the two, which is where to look instead of at BGP.

What no rule anchors is an incident of its own, so nothing is ever lost by
being grouped: the incidents together hold exactly the findings.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .aliases import resolve
from .checks import CHECKS_BY_NAME, ERROR, WARNING, Finding
from .fabric import FabricState, out_of_band, parent

#: Which finding explains the others when several share an anchor, most
#: fundamental first. A node that is gone explains everything about it; a
#: port that is down explains the sessions over it; a session explains the
#: routes it did not deliver.
ROOT_PRIORITY: Tuple[str, ...] = (
    "node_unreachable",
    "hardware_fault",
    "optic_dom",
    "itf_down",
    "lldp_one_sided",
    "mtu_mismatch",
    "itf_errors",
    "underlay_unreachable",
    # A configuration that cannot work explains the adjacency or the session
    # it keeps down.
    "igp_peer_mismatch",
    "igp_adjacency_down",
    "igp_no_adjacency",
    "bgp_peer_mismatch",
    "bfd_down",
    "bgp_down",
    "bgp_af_down",
    "flapping",
    "es_df",
    "bgp_no_routes",
    "resource_high",
    "collection",
)

#: Checks whose subject is a port or a subinterface.
_PORT_CHECKS = frozenset(
    {
        "itf_down",
        "itf_errors",
        "lldp_one_sided",
        "mtu_mismatch",
        "optic_dom",
        "igp_adjacency_down",
        "igp_no_adjacency",
        "igp_peer_mismatch",
    }
)
#: Checks whose subject is ``<ni>/<peer address>``.
_SESSION_CHECKS = frozenset({"bgp_down", "bgp_af_down", "bgp_no_routes", "bfd_down", "bgp_peer_mismatch"})
#: Checks about the node itself rather than about anything on it.
_PLATFORM_CHECKS = frozenset({"hardware_fault", "resource_high"})
#: Flap kinds whose subject is a port or subinterface.
_PORT_FLAPS = ("interface", "lldp", "optic", "isis", "ospf")

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1}

#: Words a finding is summed up in, in the one line that says what else an
#: incident holds: "2 BGP sessions down, 1 BFD session down".
_NOUNS = {
    "itf_down": ("interface down", "interfaces down"),
    "itf_errors": ("port dropping packets", "ports dropping packets"),
    "lldp_one_sided": ("one-sided LLDP adjacency", "one-sided LLDP adjacencies"),
    "mtu_mismatch": ("MTU mismatch", "MTU mismatches"),
    "optic_dom": ("optic alarm", "optic alarms"),
    "igp_adjacency_down": ("IGP adjacency down", "IGP adjacencies down"),
    "igp_no_adjacency": ("IGP interface without adjacency", "IGP interfaces without adjacency"),
    "bfd_down": ("BFD session down", "BFD sessions down"),
    "bgp_down": ("BGP session down", "BGP sessions down"),
    "bgp_af_down": ("BGP family down", "BGP families down"),
    "bgp_peer_mismatch": ("BGP configuration mismatch", "BGP configuration mismatches"),
    "igp_peer_mismatch": ("IGP configuration mismatch", "IGP configuration mismatches"),
    "bgp_no_routes": ("BGP family without routes", "BGP families without routes"),
    "es_df": ("ethernet-segment problem", "ethernet-segment problems"),
    "flapping": ("flap", "flaps"),
    "collection": ("report not collected", "reports not collected"),
    "hardware_fault": ("hardware fault", "hardware faults"),
    "resource_high": ("resource running out", "resources running out"),
}


@dataclass(frozen=True)
class Incident:
    """One thing wrong with the fabric, with every finding it accounts for."""

    #: Stable while the incident lasts: what it is anchored to.
    id: str
    severity: str
    #: ``link``, ``port``, ``node``, ``session``, ``underlay``, ``platform``,
    #: ``segment`` or ``finding`` - what the incident is about.
    kind: str
    title: str
    #: The node the root finding is on, which is where to start looking.
    node: str
    nodes: Tuple[str, ...]
    #: The finding that explains the others. It is one of the fabric's own,
    #: except for a node that stopped answering or an overlay session with no
    #: underlay under it, which no single check says in so many words.
    root: Finding
    #: Everything else the incident accounts for, worst first.
    related: Tuple[Finding, ...] = ()
    explanation: str = ""
    #: Every finding it holds has been acknowledged by someone
    #: (:mod:`nornir_srl.acks`): it is known, and kept out of the counts and
    #: colours that exist to draw the eye.
    acknowledged: bool = False
    #: When the oldest of its findings was raised, and the newest: since when
    #: it has been wrong, and when it last got worse. ``None`` where nothing
    #: keeps a timeline - the one-shot surfaces.
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    #: The oldest finding was already there when watching began, so
    #: *first_seen* is only when that was: it has been wrong since before.
    since_before: bool = False

    @property
    def findings(self) -> Tuple[Finding, ...]:
        return (self.root,) + self.related


# --------------------------------------------------------------------------- #
# what the fabric says about where things are
# --------------------------------------------------------------------------- #

Port = Tuple[str, str]  # (node, port)


class _Fabric:
    """The readings of a fabric that anchoring needs, indexed once."""

    def __init__(self, state: FabricState) -> None:
        self.state = state
        self.index = state.alias_index()
        self.links: Dict[Port, Port] = {}
        self._cable_lldp(state)
        self._cable_lost(state)
        #: address -> (node, subinterface) that has it configured.
        self.owners: Dict[str, Tuple[str, str]] = {}
        #: node -> [(connected network, subinterface)].
        self.connected: Dict[str, List[Tuple[Any, str]]] = {}
        self._addresses(state)
        self._cable_subnets()
        self.loopbacks: Dict[str, str] = {}  # address -> node
        for node, instance, itf in state.sub_items("ni", "interfaces"):
            if instance.name == "default" and itf.name.startswith(("system0", "lo")):
                for prefix in itf.prefixes:
                    self.loopbacks[_host(prefix)] = node
        self.hosts: Dict[str, Set[str]] = {}
        for report in ("ipv4_rib", "ipv6_rib"):
            for node, table, route in state.sub_items(report, "routes"):
                if table.ni == "default":
                    self.hosts.setdefault(node, set()).add(route.prefix)
        #: (node, ni/remote) -> the subinterface a BFD session runs on.
        self.bfd_ports: Dict[Tuple[str, str], str] = {}
        for node, instance, session in state.sub_items("bfd", "sessions"):
            if session.interface:
                self.bfd_ports[(node, f"{instance.ni}/{session.remote_address}")] = session.interface
        #: (node, segment name) -> the ports it hangs off.
        self.segment_ports: Dict[Tuple[str, str], Tuple[str, ...]] = {
            (node, segment.name): tuple(segment.interfaces) for node, segment in state.items("es")
        }

    # -- cabling -------------------------------------------------------------

    def _cable(self, a: Port, b: Port) -> None:
        self.links.setdefault(a, b)
        self.links.setdefault(b, a)

    def _cable_lldp(self, state: FabricState) -> None:
        for node, itf, neighbor in state.sub_items("lldp", "neighbors"):
            if out_of_band(itf.name) or not neighbor.system_name:
                continue
            peer = resolve(neighbor.system_name, self.index)
            if peer and peer != node and neighbor.port_id:
                self._cable((node, itf.name), (peer, neighbor.port_id))

    def _cable_lost(self, state: FabricState) -> None:
        """Adjacencies LLDP had and lost: a cable that went down is still that cable.

        The timeline says so as a change; a server that keeps what LLDP ever
        showed - across its own restarts - says so as its cabling.
        """
        cabling = getattr(state.history, "cabling", None)
        for node, ports in (cabling() if callable(cabling) else {}).items():
            for port, (system, peer_port) in ports.items():
                peer = resolve(system, self.index)
                if peer and peer != node and not out_of_band(port):
                    self._cable((node, port), (peer, peer_port))
        for change in state.changes:
            if getattr(change, "kind", "") != "lldp" or not change.before:
                continue
            system, _, port = change.before.partition(" ")
            peer = resolve(system, self.index)
            if peer and peer != change.node and port:
                self._cable((change.node, change.subject), (peer, port))

    def _addresses(self, state: FabricState) -> None:
        for node, itf, subif in state.sub_items("subif", "subinterfaces"):
            if out_of_band(itf.name):
                continue
            for prefix in tuple(subif.ipv4) + tuple(subif.ipv6):
                self.owners.setdefault(_host(prefix), (node, subif.name))
                network = _network(prefix)
                if network is not None:
                    self.connected.setdefault(node, []).append((network, subif.name))

    def _cable_subnets(self) -> None:
        """Two ends of one point-to-point subnet are two ends of one cable."""
        ends: Dict[Any, List[Tuple[str, str]]] = {}
        for node, networks in self.connected.items():
            for network, subif in networks:
                if network.prefixlen >= network.max_prefixlen - 2 and not network.is_link_local:
                    ends.setdefault(network, []).append((node, subif))
        for members in ends.values():
            if len(members) == 2 and members[0][0] != members[1][0]:
                (a, sa), (b, sb) = members
                self._cable((a, parent(sa)), (b, parent(sb)))

    def link(self, node: str, port: str) -> FrozenSet[Port]:
        """The ends of the cable *port* of *node* is, or just that end."""
        end = (node, parent(port))
        far = self.links.get(end)
        return frozenset({end, far}) if far else frozenset({end})

    # -- sessions ------------------------------------------------------------

    def session_port(self, node: str, subject: str) -> Optional[str]:
        """The subinterface a session with *subject* ``<ni>/<peer>`` runs over, if one."""
        _ni, _, peer = subject.partition("/")
        address, _, scope = peer.partition("%")
        if scope:
            return scope
        if (node, subject) in self.bfd_ports:
            return self.bfd_ports[(node, subject)]
        ip = _ip(address)
        if ip is None:
            return None
        for network, subif in self.connected.get(node, ()):
            if ip in network and network.prefixlen < network.max_prefixlen:
                return subif
        return None

    def session_peer(self, subject: str) -> Optional[str]:
        """The node on the other end of a session, where its address is one of ours."""
        address = _host(subject.partition("/")[2].partition("%")[0])
        if address in self.loopbacks:
            return self.loopbacks[address]
        owner = self.owners.get(address)
        return owner[0] if owner else None

    def reaches(self, node: str, address: str) -> Optional[bool]:
        """Whether *node*'s default instance has a route covering *address*.

        ``None`` when there is no route table to go on.
        """
        routes = self.hosts.get(node)
        ip = _ip(address)
        if routes is None or ip is None:
            return None
        for prefix in routes:
            network = _network(prefix)
            if network is not None and ip in network and network.prefixlen > 0:
                return True
        return False


def _host(prefix: str) -> str:
    return str(prefix).split("/", 1)[0].split("%", 1)[0].strip()


def _ip(text: str) -> Optional[Any]:
    try:
        return ipaddress.ip_address(_host(text))
    except ValueError:
        return None


def _network(prefix: str) -> Optional[Any]:
    try:
        return ipaddress.ip_network(str(prefix).strip(), strict=False)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# anchoring
# --------------------------------------------------------------------------- #

Anchor = Tuple[Any, ...]


def _port_subject(finding: Finding) -> Optional[str]:
    if finding.check in _PORT_CHECKS:
        return finding.subject
    if finding.check == "flapping":
        kind = finding.detail.split(" ", 1)[0]
        if kind in _PORT_FLAPS:
            return finding.subject.split(" ", 1)[0]
    return None


def _anchor(finding: Finding, fabric: _Fabric, unreachable: Set[str], port_anchors: Set[Anchor]) -> Anchor:
    """What *finding* is about, as a key the findings about the same thing share."""
    if finding.check == "collection" and finding.node in unreachable:
        return ("node", finding.node)
    if finding.check in _PLATFORM_CHECKS:
        return ("platform", finding.node)

    port = _port_subject(finding)
    if port is not None:
        return ("link", fabric.link(finding.node, port))

    session = finding.check in _SESSION_CHECKS or (
        finding.check == "flapping" and finding.detail.split(" ", 1)[0] in ("bgp", "bfd")
    )
    if session:
        over = fabric.session_port(finding.node, finding.subject)
        if over is not None:
            link = ("link", fabric.link(finding.node, over))
            if link in port_anchors:
                return link
        peer = fabric.session_peer(finding.subject)
        if peer is None and over is not None:
            # A peer whose addresses are not known - it has answered nothing -
            # is still the node on the far end of the cable the session is on.
            far = fabric.links.get((finding.node, parent(over)))
            peer = far[0] if far else None
        if peer in unreachable or finding.node in unreachable:
            return ("node", peer if peer in unreachable else finding.node)
        address = _host(finding.subject.partition("/")[2])
        if finding.check == "bgp_down" and peer and address in fabric.loopbacks:
            if fabric.reaches(finding.node, address) is False:
                return ("underlay", finding.node, peer)
        if over is not None:
            return ("link", fabric.link(finding.node, over))
        ni = finding.subject.partition("/")[0]
        return ("session", ni, frozenset({finding.node, peer or address}))

    if finding.check == "es_df":
        name = finding.subject.split("/", 1)[0]
        for port in fabric.segment_ports.get((finding.node, name), ()):
            link = ("link", fabric.link(finding.node, port))
            if link in port_anchors:
                return link
        return ("segment", finding.subject.split("/", 1)[0])

    return ("finding", finding.check, finding.node, finding.subject)


def unreachable_nodes(state: FabricState) -> Set[str]:
    """Nodes no report could be collected from at all."""
    tried: Dict[str, int] = {}
    for (_report, node) in state.errors:
        tried[node] = tried.get(node, 0) + 1
    answered = {node for payloads in state.reports.values() for node in payloads}
    return {node for node in tried if node not in answered}


# --------------------------------------------------------------------------- #
# correlating
# --------------------------------------------------------------------------- #


def _rank(finding: Finding) -> Tuple[int, int, str, str]:
    check = finding.check
    return (
        ROOT_PRIORITY.index(check) if check in ROOT_PRIORITY else len(ROOT_PRIORITY),
        _SEVERITY_ORDER.get(finding.severity, 9),
        finding.node,
        finding.subject,
    )


def correlate(findings: Sequence[Finding], state: FabricState) -> List[Incident]:
    """*findings* grouped by root cause, worst incident first."""
    fabric = _Fabric(state)
    unreachable = unreachable_nodes(state)
    # Findings about ports first, so a session knows which links have
    # something wrong with them and joins those rather than standing alone.
    port_anchors: Set[Anchor] = set()
    for finding in findings:
        port = _port_subject(finding)
        if port is not None:
            port_anchors.add(("link", fabric.link(finding.node, port)))

    groups: Dict[Anchor, List[Finding]] = {}
    for finding in findings:
        groups.setdefault(_anchor(finding, fabric, unreachable, port_anchors), []).append(finding)
    # A node gone with no finding of its own - the checks only say what they
    # could not read - is still the root of what hangs off it.
    for node in unreachable:
        groups.setdefault(("node", node), [])

    incidents = _patterns([_incident(anchor, members, fabric) for anchor, members in groups.items()])
    incidents = _dated(incidents, state)
    incidents.sort(
        key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), -len(i.findings), i.node, i.title)
    )
    return incidents


def _dated(incidents: List[Incident], state: FabricState) -> List[Incident]:
    """*incidents* with when their findings were raised, where a timeline says.

    A finding the timeline has not raised yet - it has been there one
    reading, and is raised on the second - is from now.
    """
    history = state.history
    raised_at = getattr(history, "raised_at", None)
    if raised_at is None:
        return incidents
    known = raised_at()
    now = time.time()
    dated = []
    for incident in incidents:
        times = [known.get((f.check, f.node, f.subject), (now, False)) for f in incident.findings]
        if not times:
            dated.append(incident)
            continue
        first = min(times, key=lambda t: t[0])
        dated.append(
            replace(incident, first_seen=first[0], last_seen=max(t[0] for t in times), since_before=first[1])
        )
    return dated


def _incident(anchor: Anchor, members: List[Finding], fabric: _Fabric) -> Incident:
    kind = anchor[0]
    members = sorted(members, key=_rank)
    root = _root(anchor, members, fabric)
    related = tuple(f for f in members if f is not root)
    nodes = tuple(sorted({f.node for f in (root,) + related if f.node and f.node != "-"}))
    severity = min((f.severity for f in (root,) + related), key=lambda s: _SEVERITY_ORDER.get(s, 9))
    title = _title(anchor, root)
    if kind == "link" and len(anchor[1]) == 1:
        kind = "port"
    return Incident(
        id=_identity(anchor),
        severity=severity,
        kind=kind,
        title=title,
        node=root.node,
        nodes=nodes,
        root=root,
        related=related,
        explanation=_explain(root, related),
    )


def _root(anchor: Anchor, members: List[Finding], fabric: _Fabric) -> Finding:
    """The finding that explains the rest, synthesizing one where no check says it."""
    if anchor[0] == "node":
        node = anchor[1]
        return Finding(
            check="node_unreachable",
            severity=ERROR,
            node=node,
            subject="gnmi",
            detail="no report could be collected: the node is down or unreachable",
        )
    if anchor[0] == "underlay":
        _kind, node, peer = anchor
        address = next(
            (a for a, owner in fabric.loopbacks.items() if owner == peer), peer
        )
        return Finding(
            check="underlay_unreachable",
            severity=ERROR,
            node=node,
            subject=peer,
            detail=f"no route to {peer}'s loopback {address} in the default instance, so no overlay session to it can come up",
        )
    return members[0]


def _identity(anchor: Anchor) -> str:
    parts = []
    for part in anchor:
        if isinstance(part, frozenset):
            parts.append("+".join(sorted(":".join(p) if isinstance(p, tuple) else str(p) for p in part)))
        else:
            parts.append(str(part))
    return "|".join(parts)


def _short(port: str) -> str:
    return port.replace("ethernet-", "e")


def _title(anchor: Anchor, root: Finding) -> str:
    what = _NOUNS.get(root.check, (CHECKS_BY_NAME[root.check].title if root.check in CHECKS_BY_NAME else root.check,))[0]
    kind = anchor[0]
    if kind == "link":
        ends = sorted(anchor[1])
        cable = " <-> ".join(f"{node} {_short(port)}" for node, port in ends)
        return f"{cable}: {what}"
    if kind == "node":
        return f"{anchor[1]} is unreachable"
    if kind == "underlay":
        return f"{anchor[1]} has no underlay route to {anchor[2]}"
    if kind == "session":
        _kind, ni, ends = anchor
        return f"{' <-> '.join(sorted(str(e) for e in ends))} ({ni}): {what}"
    if kind == "platform":
        return f"{anchor[1]} platform: {what}"
    if kind == "segment":
        if "disagree on the designated forwarder" in root.detail:
            what = f"designated forwarder disagreement in {root.subject.split('/', 1)[-1]}"
        return f"ethernet-segment {anchor[1]}: {what}"
    return f"{root.node} {root.subject}: {what}"


def _explain(root: Finding, related: Tuple[Finding, ...]) -> str:
    text = root.detail.rstrip(".")
    if not related:
        return text
    stop = "" if text.endswith(("?", "!")) else "."
    counts: Dict[str, int] = {}
    for finding in related:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    parts = []
    for check in sorted(counts, key=lambda c: ROOT_PRIORITY.index(c) if c in ROOT_PRIORITY else 99):
        singular, plural = _NOUNS.get(check, (check, check))
        parts.append(f"{counts[check]} {singular if counts[check] == 1 else plural}")
    return f"{text}{stop} It accounts for {', '.join(parts)}."


# --------------------------------------------------------------------------- #
# patterns: one cause, many places
# --------------------------------------------------------------------------- #

#: How many incidents with the same cause make it a pattern rather than a
#: coincidence.
PATTERN_MIN = 3

_PLACES = {"link": "links", "port": "ports", "session": "sessions", "node": "nodes", "underlay": "node pairs"}


#: The states a BGP session passes through while it keeps trying and failing.
#: A session refused for a wrong AS cycles active, connect, opensent,
#: openconfirm and back every few seconds; which one a reading catches it in
#: is chance, not a different cause.
_BGP_TRYING = re.compile(r"\bsession is (?:idle|connect|active|opensent|openconfirm)\b")


#: Checks whose detail says more than what kind of wrong it is, folded on
#: the part that does. A BGP session down names its peer-group and the
#: peer's AS after that, which a dynamic neighbour only knows once its
#: handshake gets that far: the same down session reads 'peer-group fabric,
#: AS 4200000004' in one reading and 'peer-group -, AS ?' in the next.
_CAUSE_ONLY = {"bgp_down": lambda detail: detail.split(",", 1)[0]}


def _template(detail: str, check: str = "") -> str:
    """A finding's detail with its names and numbers taken out: what kind of wrong it is."""
    detail = _CAUSE_ONLY.get(check, lambda d: d)(detail)
    detail = _BGP_TRYING.sub("session is not established", detail)
    return re.sub(r"\S*\d\S*", "#", detail)


def _patterns(incidents: List[Incident]) -> List[Incident]:
    """Incidents with the same root cause in several places, folded into one.

    Sixteen links each with its BFD session down and the far end silent are
    not sixteen problems: they are one - BFD not enabled on the spines - and
    reading them as one card is what makes that visible. Every finding is
    kept; only the grouping changes.
    """
    by_cause: Dict[Tuple[str, str, str], List[Incident]] = {}
    for incident in incidents:
        if incident.root.check == FLAPPING:
            # Whatever keeps changing - a session, a port, a DF, a MAC - is
            # one thing to look at: the fabric is unsettled there.
            key = (FLAPPING, "", "")
        elif incident.kind in ("finding", "platform", "segment"):
            key = ("", incident.id, "")
        else:
            key = (incident.root.check, _template(incident.root.detail, incident.root.check), incident.root.severity)
        by_cause.setdefault(key, []).append(incident)
    folded: List[Incident] = []
    for (check, template, _severity), members in by_cause.items():
        if check == FLAPPING and len(members) >= 2:
            folded.append(_flaps(members))
            continue
        if not check or len(members) < PATTERN_MIN:
            folded.extend(members)
            continue
        folded.append(_pattern(check, template, members))
    return folded


#: The check whose incidents fold into one, whatever flaps.
FLAPPING = "flapping"


def _flaps(members: List[Incident]) -> Incident:
    """Every incident that is something flapping, as one: what flaps, and how much of each.

    A flapping finding says what kind of thing it is about first -
    ``bgp changed 4 times ...``, ``es-df changed 3 times ...`` - which is
    what the summary counts.
    """
    members = sorted(members, key=lambda i: i.title)
    flaps = [f for incident in members for f in incident.findings if f.check == FLAPPING]
    root = max(flaps, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9) * -1, f.detail))
    related = tuple(f for incident in members for f in incident.findings if f is not root)
    kinds: Dict[str, int] = {}
    for flap in flaps:
        kind = flap.detail.split(" ", 1)[0] or "?"
        kinds[kind] = kinds.get(kind, 0) + 1
    what = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    nodes = tuple(sorted({node for incident in members for node in incident.nodes}))
    places = "places" if len({i.kind for i in members}) != 1 else _PLACES.get(members[0].kind, "places")
    return Incident(
        id=f"pattern|{FLAPPING}",
        severity=min((i.severity for i in members), key=lambda s: _SEVERITY_ORDER.get(s, 9)),
        kind="pattern",
        title=f"Flapping in {len(members)} {places}",
        node=root.node,
        nodes=nodes,
        root=root,
        related=related,
        explanation=(
            f"Things that keep changing state, on {len(nodes)} node{'s' if len(nodes) != 1 else ''}: "
            f"{what}. {1 + len(related)} findings in all."
        ),
    )


def _pattern(check: str, template: str, members: List[Incident]) -> Incident:
    members = sorted(members, key=lambda i: i.title)
    root = members[0].root
    related = tuple(f for incident in members for f in incident.findings if f is not root)
    kinds = {incident.kind for incident in members}
    places = _PLACES.get(next(iter(kinds)), "places") if len(kinds) == 1 else "places"
    singular = _NOUNS.get(check, (check, check))[0]
    nodes = tuple(sorted({node for incident in members for node in incident.nodes}))
    where = "; ".join(incident.title.split(":", 1)[0] for incident in members[:4])
    more = f" and {len(members) - 4} more" if len(members) > 4 else ""
    return Incident(
        id=f"pattern|{check}|{template}",
        severity=min((i.severity for i in members), key=lambda s: _SEVERITY_ORDER.get(s, 9)),
        kind="pattern",
        title=f"{singular} on {len(members)} {places}",
        node=root.node,
        nodes=nodes,
        root=root,
        related=related,
        explanation=(
            f"The same cause in {len(members)} {places} ({where}{more}): "
            f"{root.detail.rstrip('.')}. {1 + len(related)} findings in all."
        ),
    )


def locate(findings: Sequence[Finding], state: FabricState) -> List[Tuple[Finding, Optional[str]]]:
    """Each finding with the port it is on, where it is on one.

    The port is the parent interface: a finding about ``ethernet-1/1.0`` or a
    session over it is on the cable ``ethernet-1/1`` is. That is what a
    drawing of the fabric colours.
    """
    fabric = _Fabric(state)
    located: List[Tuple[Finding, Optional[str]]] = []
    for finding in findings:
        port = _port_subject(finding)
        if port is None and finding.check in _SESSION_CHECKS:
            port = fabric.session_port(finding.node, finding.subject)
        if port is None and finding.check == "es_df":
            ports = fabric.segment_ports.get((finding.node, finding.subject.split("/", 1)[0]), ())
            port = ports[0] if len(ports) == 1 else None
        located.append((finding, parent(port) if port else None))
    return located


def incident_findings(incidents: Iterable[Incident]) -> List[Finding]:
    """Every finding the incidents hold, the synthesized roots included."""
    return [finding for incident in incidents for finding in incident.findings]


__all__ = [
    "Incident",
    "ROOT_PRIORITY",
    "correlate",
    "incident_findings",
    "locate",
    "unreachable_nodes",
]
