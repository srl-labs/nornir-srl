"""What changed in a fabric between two readings of it.

The first question of almost every troubleshooting session is not *what is
wrong* but *what changed*: the session that went down two minutes ago, the
MAC that has moved between two leaves five times since, the route count that
halved on one peer. A table of the fabric as it is now cannot answer that; two
readings of it can.

Everything here is a pure function of two :class:`~nornir_srl.fabric.FabricState`
objects, so the same comparison serves three purposes:

* the live server diffs each reading against the one before it, and keeps the
  result as a timeline;
* it also diffs the current reading against a *baseline* - the fabric as it
  was when the server started, or when someone said "this is what good looks
  like" - which is drift rather than events;
* the MCP server does the same between two readings an agent asks for.

A reading is reduced to *observations* first - one value per thing that can
change, keyed by what that thing is - and it is those that are compared. That
keeps each report's knowledge in one small function, and lets a flap be
counted the same way whether it is a BGP session, a port, or a MAC address.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .checks import REQUIRED_REPORTS
from .fabric import FabricState, out_of_band, text

#: What a reading to diff is made of: what the checks read, plus the bridge
#: tables and the neighbour caches, whose entries moving is what a loop, a
#: duplicate address or a moved host looks like.
WATCH_REPORTS: Tuple[str, ...] = tuple(dict.fromkeys(REQUIRED_REPORTS + ("mac", "arp", "nd")))

#: A change that means something stopped working.
ERROR = "error"
#: One that is worth reading: legitimate at times, a fault at others.
WARNING = "warning"
#: One that is only news: something new, or gone that was not working anyway.
INFO = "info"
#: Something that was not working and now is.
OK = "ok"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, OK: 2, INFO: 3}

#: What a thing reads as when it is not there on one side of the comparison.
ABSENT = ""

#: The values that mean working. A change towards one of them is a recovery,
#: away from one of them a failure.
_GOOD = frozenset({"established", "up", "full", "two-way", "present"})

#: Kinds whose disappearance is not a fault in itself. A bridge-table entry
#: ages out, a VXLAN destination goes when its last MAC does.
_TRANSIENT = frozenset({"mac", "vxlan", "arp", "nd"})

#: How a transient entry moving reads. A MAC moving between ports is the
#: fabric following a host; an address answering from another MAC is a
#: duplicate address, a spoof or a host replaced, and worth a look.
_MOVE_SEVERITY = {"arp": "warning", "nd": "warning"}


@dataclass(frozen=True)
class Change:
    """One thing that is different now from what it was."""

    #: When it was noticed, as a Unix timestamp. Between two readings it
    #: happened some time after the first; this is the second.
    at: float
    node: str
    #: What kind of thing changed: ``bgp``, ``bgp-routes``, ``interface``,
    #: ``lldp``, ``bfd``, ``isis``, ``ospf``, ``es``, ``es-df``, ``mac``,
    #: ``vxlan``, ``routes``, ``hardware``, ``optic``, ``node`` or ``finding``.
    kind: str
    #: Which one, on that node: a peer, a port, a MAC in a network-instance.
    subject: str
    #: The value it had and the one it has now; :data:`ABSENT` where it did
    #: not exist on that side.
    before: str
    after: str
    severity: str
    detail: str = ""

    @property
    def time(self) -> str:
        """When, as the local time of day it reads as on a timeline."""
        return time.strftime("%H:%M:%S", time.localtime(self.at))

    @property
    def summary(self) -> str:
        """``established -> active``, ``new: spine1 e1/1``, ``gone: ...``."""
        if self.before == ABSENT:
            return f"new: {self.after}"
        if self.after == ABSENT:
            return f"gone: {self.before}"
        return f"{self.before} -> {self.after}"


def change_order(change: Change) -> Tuple[Any, ...]:
    """Newest first, and within one reading the worst first."""
    return (-change.at, _SEVERITY_ORDER.get(change.severity, 9), change.node, change.kind, change.subject)


# --------------------------------------------------------------------------- #
# observations: one value per thing that can change
# --------------------------------------------------------------------------- #

#: (kind, node, subject) -> the value it has.
Observations = Dict[Tuple[str, str, str], str]


def _bgp(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, entry, peer in state.sub_items("bgp_peers", "neighbors"):
        if not node_ok(node):
            continue
        subject = f"{entry.ni}/{peer.peer or '?'}"
        yield ("bgp", node, subject), text(peer.state) or "unknown"
        for family in peer.families:
            if family.enabled and text(peer.state) == "established":
                yield ("bgp-routes", node, f"{subject} {family.name}"), str(family.received)


def _interfaces(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, itf, subif in state.sub_items("subif", "subinterfaces"):
        if node_ok(node) and not out_of_band(itf.name) and subif.name:
            yield ("interface", node, subif.name), text(subif.oper) or "unknown"


def _lldp(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, itf, neighbor in state.sub_items("lldp", "neighbors"):
        if node_ok(node) and not out_of_band(itf.name) and neighbor.system_name:
            yield ("lldp", node, itf.name), f"{neighbor.system_name} {neighbor.port_id}".strip()


def _bfd(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, instance, session in state.sub_items("bfd", "sessions"):
        if node_ok(node):
            where = f"%{session.interface}" if session.interface else ""
            yield ("bfd", node, f"{instance.ni}/{session.remote_address}{where}"), session.state or "unknown"


def _isis(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, itf, adjacency in state.sub_items("isis", "adjacencies"):
        if node_ok(node):
            neighbor = adjacency.hostname or adjacency.system_id
            yield ("isis", node, f"{itf.name} {neighbor} {adjacency.level}".strip()), adjacency.state or "unknown"


def _ospf(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, itf, neighbor in state.sub_items("ospf", "neighbors"):
        if node_ok(node):
            yield ("ospf", node, f"{itf.name} {neighbor.router_id}"), neighbor.state or "unknown"


def _segments(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, segment in state.items("es"):
        if not node_ok(node):
            continue
        yield ("es", node, segment.name), text(segment.oper) or "unknown"
        for association in segment.associations:
            yield ("es-df", node, f"{segment.name}/{association.ni}"), association.designated or "none"


def _macs(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, table, entry in state.sub_items("mac", "entries"):
        if node_ok(node):
            yield ("mac", node, f"{table.ni} {entry.address}"), _mac_destination(entry)


def _mac_destination(entry: Any) -> str:
    """Where a MAC is, the way a move from one place to another reads."""
    if entry.vtep:
        return f"vtep {entry.vtep}"
    if entry.esi:
        return f"esi {entry.esi}"
    if entry.far_end:
        return f"far-end {entry.far_end}"
    return entry.interface or entry.destination


def _vxlan(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, itf, destination in state.sub_items("vxlan", "destinations"):
        if node_ok(node):
            yield ("vxlan", node, f"{itf.name} {destination.vtep}"), "present"


def _neighbors(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    """ARP and ND entries, by network-instance and address: the MAC it resolves to, and where."""
    for report, kind in (("arp", "arp"), ("nd", "nd")):
        for node, cache, entry in state.sub_items(report, "entries"):
            if node_ok(node) and entry.address and entry.mac:
                ni = "/".join(cache.nis) or "-"
                yield (kind, node, f"{ni} {entry.address}"), f"{entry.mac.lower()} on {cache.interface}"


#: Separates the parts of a RIB observation's subject: instance, family, prefix.
_RIB_SEP = "|"


def _routes(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    """Every route, by instance, family and prefix: the next-hops it has.

    Not recorded as it is - a link flapping moves the next-hops of thousands
    of prefixes at once - but summarized per route table, with only the
    prefixes that matter reported one by one; see :func:`_diff_routes`.
    """
    for report, family in (("ipv4_rib", "ipv4"), ("ipv6_rib", "ipv6")):
        for node, table, route in state.sub_items(report, "routes"):
            if node_ok(node) and route.active:
                yield ("rib", node, _RIB_SEP.join((table.ni, family, route.prefix))), _next_hops(route)


#: Where a reading keeps the watched prefixes it looked up one by one, by the
#: family they are in: report, family, the ``afi`` they are asked for in.
WATCHED_ROUTE_REPORTS: Tuple[Tuple[str, str, str], ...] = (
    ("ipv4_watched", "ipv4", "ipv4-unicast"),
    ("ipv6_watched", "ipv6", "ipv6-unicast"),
)


def _held(state: FabricState, family: str) -> Set[Tuple[str, str]]:
    """The (node, instance) tables *state* holds prefix by prefix in *family*."""
    return {(node, table.ni) for node, table in state.items(f"{family}_rib")}


def _watched_routes(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    """Watched prefixes, where a reading looked them up rather than holding their table.

    A table the reading holds in full reports them already, so only the
    others are taken from here.
    """
    for report, family, _afi in WATCHED_ROUTE_REPORTS:
        held = _held(state, family)
        for node, table, route in state.sub_items(report, "routes"):
            if node_ok(node) and route.active and (node, table.ni) not in held:
                yield ("rib-watched", node, _RIB_SEP.join((table.ni, family, route.prefix))), _next_hops(route)


def _table_sizes(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    """How many active routes each table has, where the reading does not hold its prefixes."""
    held = {family: _held(state, family) for family in ("ipv4", "ipv6")}
    for node, summary in state.items("rib_summary"):
        if node_ok(node) and (node, summary.ni) not in held.get(summary.family, ()):
            yield ("routes", node, f"{summary.ni} {summary.family}"), str(summary.active)


def _next_hops(route: Any) -> str:
    """A route's next-hops as one comparable value: sorted, one per ECMP member."""
    hops = sorted(
        {h.address or h.resolving_route or ",".join(e.label for e in h.egress) or h.type for h in route.next_hops}
    )
    return " ".join(hop for hop in hops if hop) or route.type


def _hardware(state: FabricState, node_ok: Callable[[str], bool]) -> Iterator[Tuple[Tuple[str, str, str], str]]:
    for node, component in state.items("components"):
        if node_ok(node):
            yield ("hardware", node, f"{component.kind} {component.id}"), component.oper or "unknown"
    for node, optic in state.items("transceivers"):
        if node_ok(node):
            yield ("optic", node, optic.interface), optic.oper or "unknown"


#: Report -> the observations it yields. A report is compared only on the
#: nodes it was collected from on both sides: one that failed to answer is
#: not a node whose sessions all went away.
_OBSERVERS: Tuple[Tuple[Tuple[str, ...], Callable[..., Iterator[Tuple[Tuple[str, str, str], str]]]], ...] = (
    (("bgp_peers",), _bgp),
    (("subif",), _interfaces),
    (("lldp",), _lldp),
    (("bfd",), _bfd),
    (("isis",), _isis),
    (("ospf",), _ospf),
    (("es",), _segments),
    (("mac",), _macs),
    (("vxlan",), _vxlan),
    (("ipv4_rib", "ipv6_rib"), _routes),
    (tuple(report for report, _family, _afi in WATCHED_ROUTE_REPORTS), _watched_routes),
    (("rib_summary",), _table_sizes),
    (("arp", "nd"), _neighbors),
    (("components", "transceivers"), _hardware),
)

#: Kinds whose value is a count, compared by how much it moved rather than
#: by whether it did.
_COUNTS = frozenset({"bgp-routes", "routes"})


def observe(
    state: FabricState, nodes: Optional[Callable[[str, Sequence[str]], bool]] = None
) -> Observations:
    """Every observation *state* holds, optionally only on the nodes *nodes* allows.

    *nodes* is called with a node and the reports an observation comes from.
    """
    found: Observations = {}
    for reports, observer in _OBSERVERS:
        if not any(state.reports.get(report) for report in reports):
            continue

        def node_ok(node: str, reports: Sequence[str] = reports) -> bool:
            return nodes is None or nodes(node, reports)

        for key, value in observer(state, node_ok):
            found[key] = value
    return found


# --------------------------------------------------------------------------- #
# comparing two readings
# --------------------------------------------------------------------------- #

#: A count has to move by at least this much, and by this fraction of what it
#: was, to be a change rather than the churn of a living fabric.
COUNT_MIN_DELTA = 5
COUNT_MIN_FRACTION = 0.1
#: A drop of this fraction or more is a warning rather than news.
COUNT_DROP_WARNING = 0.5


def diff_fabric(
    before: FabricState,
    after: FabricState,
    at: Optional[float] = None,
    watched: Iterable[str] = (),
) -> List[Change]:
    """What is different in *after* from *before*, worst first.

    Only what both readings could see is compared: a report a node did not
    answer on one side says nothing about that node, rather than everything
    on it having appeared or gone.

    The route tables are summarized per table rather than diffed prefix by
    prefix; the prefixes that are reported one by one are the defaults, the
    host routes to every node's system address, and *watched* ones.
    """
    at = time.time() if at is None else at

    def both(node: str, reports: Sequence[str]) -> bool:
        relevant = [r for r in reports if r in before.reports or r in after.reports]
        return bool(relevant) and all(
            node in before.reports.get(r, {}) and node in after.reports.get(r, {})
            for r in relevant
        )

    old, new = observe(before, both), observe(after, both)
    old_rib = {k: old.pop(k) for k in [k for k in old if k[0] == "rib"]}
    new_rib = {k: new.pop(k) for k in [k for k in new if k[0] == "rib"]}
    old_watched = {k: old.pop(k) for k in [k for k in old if k[0] == "rib-watched"]}
    new_watched = {k: new.pop(k) for k in [k for k in new if k[0] == "rib-watched"]}
    changes_learned = _diff_neighbors(
        {k: v for k, v in old.items() if k[0] in _NEIGHBOR_KINDS},
        {k: v for k, v in new.items() if k[0] in _NEIGHBOR_KINDS},
        at,
    )
    changes = [
        change
        for key in sorted(set(old) | set(new))
        for change in _compare(key, old.get(key, ABSENT), new.get(key, ABSENT), at)
    ]
    important = _important_prefixes(before, after, watched)
    changes += _diff_routes(old_rib, new_rib, important, at)
    changes += _diff_watched(old_watched, new_watched, at)
    changes += changes_learned
    changes.sort(key=change_order)
    return changes


# --------------------------------------------------------------------------- #
# neighbour caches: moves one by one, learning and ageing summarized
# --------------------------------------------------------------------------- #

_NEIGHBOR_KINDS = ("arp", "nd")


def _diff_neighbors(old: Observations, new: Observations, at: float) -> List[Change]:
    """ARP/ND entries learned and aged out, one change per node, cache and interface.

    An entry moving is reported by :func:`_compare` on its own; this is the
    rest of what happens to a neighbour cache - hosts appearing after a clear
    or a reboot, entries timing out - which is news in aggregate and noise
    one by one.
    """
    grouped: Dict[Tuple[str, str, str], Dict[str, List[str]]] = {}
    for key in set(old) ^ set(new):
        kind, node, subject = key
        value = new.get(key) or old.get(key, "")
        interface = value.rsplit(" on ", 1)[-1] if " on " in value else "-"
        address = subject.rsplit(" ", 1)[-1]
        what = "learned" if key in new else "aged out"
        grouped.setdefault((node, kind, interface), {}).setdefault(what, []).append(address)
    changes = []
    for (node, kind, interface), moved in sorted(grouped.items()):
        parts = []
        for what in ("learned", "aged out"):
            addresses = sorted(moved.get(what, []))
            if addresses:
                sample = ", ".join(addresses[:_SAMPLE]) + (
                    f" and {len(addresses) - _SAMPLE} more" if len(addresses) > _SAMPLE else ""
                )
                parts.append(f"{len(addresses)} {what} ({sample})")
        learned, aged = len(moved.get("learned", [])), len(moved.get("aged out", []))
        counts = ", ".join(c for c in (f"+{learned} learned" if learned else "", f"-{aged} aged out" if aged else "") if c)
        changes.append(Change(at, node, kind, interface, ABSENT, counts, INFO, "; ".join(parts)))
    return changes


# --------------------------------------------------------------------------- #
# route tables: summarized, with the prefixes that matter one by one
# --------------------------------------------------------------------------- #

#: Prefixes named in a summary before it says "and N more".
_SAMPLE = 4
#: A table that lost this fraction of its routes in one go is a warning.
ROUTES_WITHDRAWN_WARNING = 0.5
_DEFAULTS = ("0.0.0.0/0", "::/0")


def normalize_prefix(value: str) -> str:
    """A prefix as a route table spells it: ``10.1.4.16`` is ``10.1.4.16/32``.

    Raises :class:`ValueError` for something that is not an address or prefix.
    """
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=False))
    except ValueError:
        raise ValueError(f"'{value}' is not an IP prefix or address") from None


def _important_prefixes(before: FabricState, after: FabricState, watched: Iterable[str]) -> Callable[[str, str], str]:
    """What makes a prefix worth reporting on its own, if anything: ``why(ni, prefix)``."""
    from .checks import system_addresses  # noqa: PLC0415 - checks imports nothing of ours back

    loopbacks = set()
    for state in (before, after):
        for node, addresses in system_addresses(state).items():
            for address in addresses:
                loopbacks.add(normalize_prefix(address))
    pinned = {normalize_prefix(p) for p in watched}

    def why(ni: str, prefix: str) -> str:
        if prefix in pinned:
            return "watched"
        if prefix in _DEFAULTS:
            return "default route"
        if ni == "default" and prefix in loopbacks:
            return "system address"
        return ""

    return why


def _diff_routes(
    old: Observations, new: Observations, why: Callable[[str, str], str], at: float
) -> List[Change]:
    tables: Dict[Tuple[str, str, str], Dict[str, List[str]]] = {}
    sizes: Dict[Tuple[str, str, str], List[int]] = {}
    changes: List[Change] = []
    for key in set(old) | set(new):
        _kind, node, subject = key
        ni, family, prefix = subject.split(_RIB_SEP, 2)
        table = (node, ni, family)
        size = sizes.setdefault(table, [0, 0])
        size[0] += key in old
        size[1] += key in new
        before, after = old.get(key, ABSENT), new.get(key, ABSENT)
        if before == after:
            continue
        what = "new" if before == ABSENT else "withdrawn" if after == ABSENT else "changed"
        tables.setdefault(table, {}).setdefault(what, []).append(prefix)
        reason = why(ni, prefix)
        if reason:
            changes.append(_prefix_change(node, ni, prefix, before, after, reason, at))
    for (node, ni, family), moved in sorted(tables.items()):
        total_before, total_after = sizes[(node, ni, family)]
        withdrawn = moved.get("withdrawn", [])
        lost_default = any(p in _DEFAULTS for p in withdrawn)
        big_loss = total_before and len(withdrawn) / total_before >= ROUTES_WITHDRAWN_WARNING
        parts = []
        for what, label in (("changed", "changed next-hops"), ("withdrawn", "withdrawn"), ("new", "new")):
            prefixes = sorted(moved.get(what, []))
            if prefixes:
                sample = ", ".join(prefixes[:_SAMPLE]) + (f" and {len(prefixes) - _SAMPLE} more" if len(prefixes) > _SAMPLE else "")
                parts.append(f"{len(prefixes)} {label} ({sample})")
        changes.append(
            Change(
                at,
                node,
                "routes",
                f"{ni} {family}",
                f"{total_before} routes",
                f"{total_after} routes",
                WARNING if lost_default or big_loss else INFO,
                "; ".join(parts),
            )
        )
    return changes


def _diff_watched(old: Observations, new: Observations, at: float) -> List[Change]:
    """Watched prefixes in tables the reading only knows the size of: each one on its own."""
    changes = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key, ABSENT), new.get(key, ABSENT)
        if before != after:
            ni, _family, prefix = key[2].split(_RIB_SEP, 2)
            changes.append(_prefix_change(key[1], ni, prefix, before, after, "watched", at))
    return changes


def _prefix_change(node: str, ni: str, prefix: str, before: str, after: str, reason: str, at: float) -> Change:
    """One prefix that matters: gone, back, or its next-hops moved."""
    subject = f"{ni} {prefix}"
    if after == ABSENT:
        return Change(at, node, "route", subject, before, after, ERROR, f"{reason} withdrawn, was via {before}")
    if before == ABSENT:
        return Change(at, node, "route", subject, before, after, OK, f"{reason} installed via {after}")
    width_before, width_after = len(before.split()), len(after.split())
    if width_after < width_before:
        severity, detail = WARNING, f"{reason}: ECMP narrowed from {width_before} to {width_after} next-hops ({after})"
    elif width_after > width_before:
        severity, detail = OK, f"{reason}: ECMP widened from {width_before} to {width_after} next-hops ({after})"
    else:
        severity, detail = INFO, f"{reason}: next-hops moved from {before} to {after}"
    return Change(at, node, "route", subject, before, after, severity, detail)


def _compare(key: Tuple[str, str, str], before: str, after: str, at: float) -> Iterator[Change]:
    kind, node, subject = key
    if before == after:
        return
    if kind in _COUNTS:
        yield from _compare_count(key, before, after, at)
        return
    if kind in _TRANSIENT:
        # A MAC learned or aged out, a destination added or withdrawn: the
        # fabric doing its job. Only a MAC moving is news.
        if before == ABSENT or after == ABSENT:
            return
        yield Change(at, node, kind, subject, before, after, _MOVE_SEVERITY.get(kind, INFO), f"moved from {before} to {after}")
        return
    yield Change(at, node, kind, subject, before, after, _severity(kind, before, after), _describe(kind, before, after))


def _compare_count(key: Tuple[str, str, str], before: str, after: str, at: float) -> Iterator[Change]:
    kind, node, subject = key
    if before == ABSENT or after == ABSENT:
        return  # the session or table itself changed, which is reported as that
    old, new = int(before), int(after)
    delta = new - old
    if abs(delta) < max(COUNT_MIN_DELTA, COUNT_MIN_FRACTION * max(old, 1)):
        return
    # Every session in the fabric re-counts when one link goes: a count that
    # moved a little is the echo of a change reported elsewhere. A count that
    # halved, or that went to or from nothing, is news of its own.
    dropped = delta < 0 and old and -delta / old >= COUNT_DROP_WARNING
    if not dropped and old and new:
        return
    what = "received routes" if kind == "bgp-routes" else "routes"
    yield Change(
        at,
        node,
        kind,
        subject,
        before,
        after,
        WARNING if dropped else INFO,
        f"{what} {'fell' if delta < 0 else 'rose'} from {old} to {new}",
    )


def _severity(kind: str, before: str, after: str) -> str:
    was_good, is_good = before in _GOOD, after in _GOOD
    if kind == "es-df":
        return WARNING
    if kind == "lldp":
        return WARNING if after == ABSENT or before != ABSENT else INFO
    if is_good and not was_good:
        return OK
    if was_good and not is_good:
        # A port or session that was removed from the configuration is gone
        # rather than failed, but from here the two look alike: a failure is
        # the reading that gets it looked at.
        return ERROR
    if before == ABSENT or after == ABSENT:
        return INFO
    return WARNING


def _describe(kind: str, before: str, after: str) -> str:
    if kind == "lldp":
        if before == ABSENT:
            return f"now sees {after}"
        if after == ABSENT:
            return f"no longer sees {before}"
        return f"neighbour changed from {before} to {after}"
    if kind == "es-df":
        return f"designated forwarder changed from {before} to {after}"
    if before == ABSENT:
        return f"appeared, {after}"
    if after == ABSENT:
        return f"disappeared, was {before}"
    return f"{before} -> {after}"


def diff_findings(
    before: Iterable[Any], after: Iterable[Any], at: Optional[float] = None
) -> List[Change]:
    """Findings raised and cleared between two runs of the checks.

    A finding is the same one while its check, node and subject are: its
    detail carries counts that move from one sample to the next.
    """
    at = time.time() if at is None else at

    def keyed(findings: Iterable[Any]) -> Dict[Tuple[str, str, str], Any]:
        return {(f.check, f.node, f.subject): f for f in findings}

    old, new = keyed(before), keyed(after)
    changes = []
    for key in sorted(set(new) - set(old)):
        finding = new[key]
        changes.append(
            Change(at, finding.node, "finding", f"{finding.check} {finding.subject}", ABSENT, finding.severity, finding.severity, finding.detail)
        )
    for key in sorted(set(old) - set(new)):
        finding = old[key]
        changes.append(
            Change(at, finding.node, "finding", f"{finding.check} {finding.subject}", finding.severity, ABSENT, OK, f"cleared: {finding.detail}")
        )
    changes.sort(key=change_order)
    return changes


def settled_findings(
    raised: Mapping[Tuple[str, str, str], Any],
    previous: Iterable[Any],
    current: Iterable[Any],
    at: Optional[float] = None,
) -> Tuple[List[Change], Dict[Tuple[str, str, str], Any]]:
    """Findings raised and cleared on a timeline, ignoring the ones that blink.

    Some findings are about one sample - a port that discarded a packet in
    the last five seconds - and come and go from one reading to the next. A
    timeline that logged each of those would bury everything else, so a
    finding is only *raised* once two readings in a row have it, and only
    *cleared* if it had been raised.

    *raised* is what has been reported raised so far; returns the changes and
    what is raised after them.
    """
    at = time.time() if at is None else at
    key = lambda f: (f.check, f.node, f.subject)  # noqa: E731
    before = {key(f) for f in previous}
    now = {key(f): f for f in current}
    changes: List[Change] = []
    still = dict(raised)
    for k, finding in sorted(now.items()):
        if k in before and k not in still:
            still[k] = finding
            changes.append(
                Change(at, finding.node, "finding", f"{finding.check} {finding.subject}", ABSENT, finding.severity, finding.severity, finding.detail)
            )
    for k in sorted(set(still) - set(now)):
        finding = still.pop(k)
        changes.append(
            Change(at, finding.node, "finding", f"{finding.check} {finding.subject}", finding.severity, ABSENT, OK, f"cleared: {finding.detail}")
        )
    changes.sort(key=change_order)
    return changes, still


def node_change(node: str, connected: bool, at: Optional[float] = None, error: str = "") -> Change:
    """A node that stopped or started answering."""
    at = time.time() if at is None else at
    if connected:
        return Change(at, node, "node", "gnmi", "unreachable", "up", OK, "answering again")
    return Change(at, node, "node", "gnmi", "up", "unreachable", ERROR, error or "stopped answering")


# --------------------------------------------------------------------------- #
# reading a timeline
# --------------------------------------------------------------------------- #

#: Kinds whose transitions count towards a flap. A route count moving is
#: churn rather than flapping, and a finding is counted by what it is about.
FLAP_KINDS = frozenset(
    {"bgp", "interface", "lldp", "bfd", "isis", "ospf", "es", "es-df", "mac", "arp", "nd", "route", "optic", "node"}
)
#: Transitions within :data:`FLAP_WINDOW` seconds that make a flap.
FLAP_THRESHOLD = 3
FLAP_WINDOW = 600.0


@dataclass(frozen=True)
class Flap:
    """One thing that changed state too often, lately."""

    node: str
    kind: str
    subject: str
    count: int
    #: The values it went through, oldest first, without repeats.
    values: Tuple[str, ...]
    first: float
    last: float


def flaps(
    changes: Iterable[Change],
    now: Optional[float] = None,
    window: float = FLAP_WINDOW,
    threshold: int = FLAP_THRESHOLD,
) -> List[Flap]:
    """The things that changed at least *threshold* times in the last *window* seconds."""
    now = time.time() if now is None else now
    grouped: Dict[Tuple[str, str, str], List[Change]] = {}
    for change in changes:
        if change.kind in FLAP_KINDS and now - change.at <= window:
            grouped.setdefault((change.node, change.kind, change.subject), []).append(change)
    found = []
    for (node, kind, subject), seen in sorted(grouped.items()):
        if len(seen) < threshold:
            continue
        seen.sort(key=lambda c: c.at)
        values = [v for c in seen for v in (c.before, c.after) if v != ABSENT]
        found.append(
            Flap(node, kind, subject, len(seen), tuple(dict.fromkeys(values)), seen[0].at, seen[-1].at)
        )
    return found


def parse_since(value: Any, now: Optional[float] = None) -> Optional[float]:
    """``15m``, ``2h``, ``90s``, ``1d`` or a bare number of minutes, as a timestamp.

    ``None`` for an empty value, which means everything kept. Raises
    :class:`ValueError` for one that is none of these.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    now = time.time() if now is None else now
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    number, unit = (raw[:-1], raw[-1]) if raw[-1] in units else (raw, "m")
    try:
        amount = float(number)
    except ValueError:
        raise ValueError(f"'{value}' is not a time span: use e.g. 15m, 2h or 1d") from None
    return now - amount * units[unit]


def as_row(change: Change) -> Mapping[str, Any]:
    """A change as the plain object the JSON surfaces emit."""
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(change.at)),
        "node": change.node,
        "kind": change.kind,
        "subject": change.subject,
        "before": change.before,
        "after": change.after,
        "severity": change.severity,
        "detail": change.detail,
    }


__all__ = [
    "ABSENT",
    "Change",
    "ERROR",
    "FLAP_KINDS",
    "Flap",
    "INFO",
    "OK",
    "WARNING",
    "WATCH_REPORTS",
    "WATCHED_ROUTE_REPORTS",
    "as_row",
    "change_order",
    "diff_fabric",
    "diff_findings",
    "normalize_prefix",
    "flaps",
    "node_change",
    "settled_findings",
    "observe",
    "parse_since",
]
