"""The single registry of the reports fcli can produce.

A report is one table: a getter from :mod:`nornir_srl.connections` plus the
metadata needed to present it. All three surfaces are driven from this list -
the ``fcli`` commands, the ``fcli-mcp`` tools and the live tables of
``fcli server`` - so a report is defined once and cannot drift between them.

Which surfaces a report appears on is part of its spec (:attr:`ReportSpec.surfaces`),
because not every report suits every surface. The executive ``overview`` only
means something in the browser; ``bgp_rib`` takes an address family the streaming
server has no way to supply, so the server gets one pre-bound report per family
instead; ``routing_pol`` returns nested JSON that no table can represent.

Getters are called as ``spec.getter(device, **params)``, where *params* are the
arguments the surface collected (a CLI option, an MCP tool argument). Every
parameter has a default, so a surface that has nothing to pass - the server -
can always call ``spec.getter(device)``.

An argument a *user* supplies, rather than one the surface chooses, is declared
in :attr:`ReportSpec.params` as well: the CLI and MCP surfaces name their own
options and tool arguments, but the browser has nothing to go on but the report
registry, so a parameter it is meant to collect has to describe and validate
itself.

A report whose getter returns records (:mod:`nornir_srl.records`) declares the
:class:`~nornir_srl.rows.Table` that renders them in :attr:`ReportSpec.table`,
so the getter says what it found and the table says what that is called on a
screen. The tables live here next to the specs, and are the only place a
column name is written.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from .connections.routing import BGP_RIB_ROUTE_FAM_ALIASES
from .records import (
    BgpRib,
    BgpRoute,
    EthernetSegment,
    IrbAddress,
    IrbInterface,
    Neighbor,
    Route,
    StaticRoute,
    VxlanInterface,
)
from .rows import Column, Table, countdown

CLI = "cli"
MCP = "mcp"
SERVER = "server"
ALL_SURFACES: FrozenSet[str] = frozenset({CLI, MCP, SERVER})
INTERACTIVE: FrozenSet[str] = frozenset({CLI, MCP})
STREAMING: FrozenSet[str] = frozenset({SERVER})


@dataclass(frozen=True)
class SubscriptionSpec:
    """One gNMI subscription entry."""

    path: str
    #: ``state``, ``config`` or ``all``.
    #:
    #: A subscription on a single leaf that SR Linux defines as *config* -
    #: ``network-instance/type``, ``interface/admin-state``,
    #: ``system/name/host-name`` - has to ask for ``all``. A path is
    #: bootstrapped with a gNMI ``Get`` before it can be streamed, and a ``Get``
    #: with datatype ``state`` answers nothing at all for a config leaf: not an
    #: error, an empty response. The path then stays pending forever and
    #: whatever reads it renders as if the node had no such data. Subtrees are
    #: not affected - ``/network-instance[name=*]`` returns the config leaves
    #: inside it either way.
    datatype: str = "state"
    mode: str = "sample"  # sample | on_change | target_defined
    sample_interval: int = 10  # seconds

    def as_gnmi(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"path": self.path, "mode": self.mode}
        if self.mode == "sample":
            entry["sample_interval"] = int(self.sample_interval * 1_000_000_000)
        return entry


#: Paths streamed ON_CHANGE rather than sampled. SAMPLE re-sends the whole
#: subtree every interval whether anything changed or not, which for the
#: state of a few hundred services on every node is most of what a large
#: fabric's server does. These change rarely and carry no counters, so a
#: change is all they send; measured on SR Linux, they are silent at steady
#: state. What does carry counters - interface and subinterface statistics,
#: BFD, the control plane's CPU - stays sampled: ON_CHANGE would send every
#: tick of every counter. So do the ARP and ND caches, which live under the
#: sampled subinterfaces.
#:
#: A path is streamed one way or the other on every report that reads it,
#: and never overlaps a sampled one (tests/test_registry.py).
ON_CHANGE_PATHS: FrozenSet[str] = frozenset(
    {
        # What a service is: its type, state, interfaces and overlay.
        "/network-instance[name=*]/type",
        "/network-instance[name=*]/oper-state",
        "/network-instance[name=*]/interface",
        "/network-instance[name=*]/vxlan-interface",
        "/network-instance[name=*]/protocols/bgp/router-id",
        "/network-instance[name=*]/protocols/bgp-vpn",
        "/network-instance[name=*]/protocols/bgp-evpn",
        "/network-instance[name=*]/static-routes",
        # Port state, apart from its counters.
        "/interface[name=*]/admin-state",
        "/interface[name=*]/oper-state",
        "/interface[name=*]/oper-down-reason",
        "/interface[name=*]/description",
        "/system/network-instance/protocols/evpn/ethernet-segments",
        "/system/lldp/interface[name=*]/neighbor",
        # Sessions carry message counters, so they are not silent - but a few
        # updates per keepalive are still a fraction of re-sending every
        # session every interval, and a peer that goes is deleted.
        "/network-instance[name=*]/protocols/bgp/neighbor",
        "/system/name/host-name",
        "/platform/chassis",
        # Route tables: a route is re-sent when it changes, and only then.
        "/network-instance[name=*]/route-table/ipv4-unicast",
        "/network-instance[name=*]/route-table/ipv6-unicast",
        "/network-instance[name=*]/route-table/next-hop-group[index=*]",
        "/network-instance[name=*]/route-table/next-hop[index=*]",
        "/network-instance[name=*]/route-table/ipv4-unicast/statistics/active-routes",
        "/network-instance[name=*]/route-table/ipv6-unicast/statistics/active-routes",
        "/network-instance[name=default]/route-table/ipv4-unicast",
        "/network-instance[name=default]/route-table/ipv6-unicast",
        "/network-instance[name=default]/route-table/next-hop-group[index=*]",
        "/network-instance[name=default]/route-table/next-hop[index=*]",
        "/network-instance[name=default]/route-table/ipv4-unicast/route/ipv4-prefix",
        "/network-instance[name=default]/route-table/ipv6-unicast/route/ipv6-prefix",
    }
)


def subscription_mode(path: str) -> str:
    """How *path* is streamed: ``on_change`` or ``sample``; see :data:`ON_CHANGE_PATHS`."""
    return "on_change" if path in ON_CHANGE_PATHS else "sample"


def _streamed(report: "ReportSpec") -> "ReportSpec":
    """*report*, with each of its paths streamed the way :data:`ON_CHANGE_PATHS` says."""
    return replace(
        report,
        subscribe=tuple(replace(spec, mode=subscription_mode(spec.path)) for spec in report.subscribe),
    )


@dataclass(frozen=True)
class ParamSpec:
    """One argument a report takes from whoever is looking at it.

    Enough for a surface to ask for it without knowing which report it belongs
    to: what to call it, what a plausible value looks like, and what counts as
    one.
    """

    #: The keyword the getter takes, and the query argument it arrives in.
    name: str
    label: str
    placeholder: str = ""
    help: str = ""
    #: ``text``; ``address`` for one that has to parse as an IP address; ``ni``
    #: for the name of a network-instance, which a surface offers from the
    #: ones the fabric has - its placeholder being the one taken when none is
    #: chosen.
    kind: str = "text"
    #: True where nothing can be answered without it: a report renders in full
    #: without any of its parameters, but a lens is a question about something.
    required: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "placeholder": self.placeholder,
            "help": self.help,
            "kind": self.kind,
            "required": self.required,
        }

    def coerce(self, value: Any) -> Optional[str]:
        """*value* as the getter wants it, or ``None`` when it is not set.

        Raises :class:`ValueError` with something worth showing to whoever
        typed it.
        """
        text = str(value or "").strip()
        if not text:
            return None
        if self.kind == "address":
            try:
                return str(ipaddress.ip_address(text))
            except ValueError:
                raise ValueError(
                    f"{self.label}: '{text}' is not an IP address"
                ) from None
        return text


@dataclass(frozen=True)
class ReportSpec:
    """One report, on every surface that offers it."""

    #: Canonical id, and the CLI command name (Typer renders ``_`` as ``-``).
    name: str
    #: Key under which the getter returns its rows.
    resource: str
    title: str
    description: str
    #: Called as ``getter(device, **params)``.
    getter: Callable[..., Dict[str, Any]]
    category: str = "General"
    #: MCP tool name, where a more explicit one reads better for an agent.
    mcp_name: Optional[str] = None
    surfaces: FrozenSet[str] = ALL_SURFACES
    #: gNMI SAMPLE interval (seconds) for the paths this report subscribes to.
    sample_interval: int = 15
    #: Explicit subscriptions; when empty the server discovers the paths by
    #: running the getter once against a RecordingDevice.
    subscribe: Tuple[SubscriptionSpec, ...] = ()
    #: False when the payload nests too deeply for a table to represent.
    tabular: bool = True
    #: Arguments a user supplies, for the surfaces that can collect them.
    #: Every one is optional, and a report renders in full without them.
    params: Tuple[ParamSpec, ...] = ()
    #: The columns that identify a row rather than describe it, which is what
    #: comparing two renderings of this report keys on. Spelled as the rendered
    #: table spells them: the ``<n>_`` that only orders a column is gone by
    #: then, and so are the newlines that wrap a header.
    #:
    #: A report that declares none can still be compared - every difference
    #: then reads as one row gone and another arrived, rather than as a row
    #: that changed.
    key_columns: Tuple[str, ...] = ()
    #: How the getter's records read as rows, or - where that depends on the
    #: parameters the report was run with, as a BGP RIB's columns depend on
    #: the family - a function of those parameters. A report without one
    #: returns items that :mod:`nornir_srl.rows` flattens by the fields they
    #: carry.
    table: Union[None, Table, Callable[[Mapping[str, Any]], Table]] = None
    #: The report whose records this one collects a narrower cut of, and
    #: whose place it takes in a fabric reading; see :func:`reading_reports`.
    stands_in_for: Optional[str] = None

    @property
    def tool_name(self) -> str:
        """The name this report is exposed under on the MCP surface."""
        return self.mcp_name or self.name

    def on(self, surface: str) -> bool:
        return surface in self.surfaces

    def table_for(self, params: Optional[Mapping[str, Any]] = None) -> Optional[Table]:
        """The table that renders this report's records, run with *params*."""
        if self.table is None or isinstance(self.table, Table):
            return self.table
        return self.table(params or {})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "sample_interval": self.sample_interval,
            "params": [p.as_dict() for p in self.params],
            # The browser offers a comparison either way, and says which kind
            # it can be: without keys, a change reads as an add and a remove.
            "key_columns": list(self.key_columns),
        }


def coerce_params(report: ReportSpec, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """The parameters *report* declares, out of a surface's raw input.

    Anything it does not declare is ignored rather than handed on: the query
    string of a live table also carries the refresh interval and the inventory
    filter, which are the server's business and not the getter's.
    """
    params: Dict[str, Any] = {}
    for spec in report.params:
        value = spec.coerce(raw.get(spec.name, ""))
        if value is not None:
            params[spec.name] = value
    if report.resource == "bgp_rib":
        detail = str(raw.get("detail", "")).strip().lower()
        if detail in ("1", "true", "yes"):
            params["detail"] = True
    return params


def _bound_bgp_rib(
    route_fam: str, route_type: Optional[str] = None
) -> Callable[..., Dict[str, Any]]:
    """A ``bgp_rib`` getter with its address family already chosen."""

    def getter(device: Any, detail: bool = False) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"route_fam": route_fam, "detail": detail}
        if route_type is not None:
            kwargs["route_type"] = route_type
        return device.get_bgp_rib(**kwargs)

    return getter


def _bgp_rib(
    device: Any,
    route_fam: str = "evpn",
    route_type: Optional[str] = None,
    detail: bool = False,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"route_fam": route_fam, "detail": detail}
    if route_type is not None:
        kwargs["route_type"] = route_type
    return device.get_bgp_rib(**kwargs)


def _lpm_param(example: str) -> ParamSpec:
    """The address the RIB reports look up, as the CLI's ``-a`` does.

    Left empty the report is the whole route table, which is what it is for.
    Filled in, each route table keeps only the one prefix of it that the
    address falls into - the route the node would actually forward on.
    """
    return ParamSpec(
        name="address",
        label="LPM",
        placeholder=f"LPM lookup, e.g. {example}",
        help="Longest prefix matching this address, per node and route table",
        kind="address",
    )


# --------------------------------------------------------------------------- #
# the tables of the reports that return records
# --------------------------------------------------------------------------- #
#
# Columns are in the order they read. For a converted report that order was
# inherited from the old flattening, which sorted a sub-record's fields
# alphabetically, so it is not always the order one would choose: moving a
# column is a matter of moving a line, and reordering the ``columns`` list of
# the ``bgp_peers`` entry in each release recording that pins the table
# (``tests/fixtures/releases``), as was done to put ``state`` after ``peer``.


def _joined(values: Any, sep: str = ", ") -> str:
    return sep.join(str(v) for v in values)


def _listed(values: Any) -> Any:
    """A list cell, or nothing at all rather than an empty list."""
    return list(values) or ""


def _ni_associated(record: Any) -> str:
    return " ".join(record.associated)


NI_TABLE = Table(
    columns=(
        Column("NI", "name"),
        Column("oper", "oper"),
        Column("type", "type"),
        Column("router-id", "router_id"),
        Column("vxlan-itf", lambda ni: _joined(ni.overlays)),
        Column("evi", lambda ni: _joined(ni.evis)),
        Column("In-RT", lambda ni: _joined(ni.import_rts)),
        Column("Out-RT", lambda ni: _joined(ni.export_rts)),
    ),
    each="interfaces",
    each_columns=(
        Column("Subitf", "name"),
        Column("assoc-ni", _ni_associated),
        Column("if-oper", "oper"),
        Column("ip-prefix", lambda itf: _listed(itf.prefixes)),
        Column("mtu", "mtu"),
        Column("vlan", "vlan"),
    ),
)

IFSTATS_TABLE = Table(
    columns=(
        Column("interface", "name"),
        Column("oper-state", "oper"),
        Column("down-reason", "down_reason"),
        Column("in-Kbps", "in_kbps"),
        Column("out-Kbps", "out_kbps"),
        Column("in-pps", "in_pps"),
        Column("out-pps", "out_pps"),
        Column("in-err", "in_errors"),
        Column("out-err", "out_errors"),
        Column("in-disc", "in_discards"),
        Column("out-disc", "out_discards"),
        Column("in-pkts", "in_packets"),
        Column("out-pkts", "out_packets"),
        Column("in-octets", "in_octets"),
        Column("out-octets", "out_octets"),
    ),
)

SUBIF_TABLE = Table(
    columns=(Column("Itf", "name"),),
    each="subinterfaces",
    each_columns=(
        Column("Subitf", "name"),
        Column("admin", "admin"),
        Column("down-reason", "down_reason"),
        Column("ip-mtu", "ip_mtu"),
        Column("ipv4", lambda s: _listed(s.ipv4)),
        Column("ipv6", lambda s: _listed(s.ipv6)),
        Column("oper", "oper"),
        Column("type", "type"),
        Column("vlan", "vlan"),
    ),
)

LLDP_TABLE = Table(
    columns=(Column("interface", "name"),),
    each="neighbors",
    each_columns=(
        Column("Nbr-System", "system_name"),
        Column("Nbr-port", "port_id"),
        Column("Nbr-port-desc", "port_description"),
    ),
)

#: The ARP and ND tables share their shape; the address family names the
#: address column, and only ND has a reachability state to show.
_NEIGHBOR_CACHE_COLUMNS = (
    Column("interface", "interface"),
    Column("NI", lambda cache: _joined(cache.nis)),
)

ARP_TABLE = Table(
    columns=_NEIGHBOR_CACHE_COLUMNS,
    each="entries",
    each_columns=(
        Column("IPv4", "address"),
        Column("MAC", "mac"),
        Column("Type", "origin"),
        Column("expiry", lambda e: countdown(e.expires_in)),
    ),
)

ND_TABLE = Table(
    columns=_NEIGHBOR_CACHE_COLUMNS,
    each="entries",
    each_columns=(
        Column("IPv6", "address"),
        Column("MAC", "mac"),
        Column("State", "state"),
        Column("Type", "origin"),
        Column("next_state", lambda e: countdown(e.expires_in)),
    ),
)


SYS_INFO_TABLE = Table(
    columns=(
        Column("type", "type"),
        Column("hw-mac-address", "hw_mac_address"),
        Column("last-booted", "last_booted"),
        Column("part-number", "part_number"),
        Column("serial-number", "serial_number"),
        Column("software-version", "software_version"),
    ),
)

LAG_TABLE = Table(
    columns=(
        Column("lag", "name"),
        Column("oper", "oper"),
        Column("mtu", "mtu"),
        Column("min", "min_links"),
        Column("desc", "description"),
        Column("type", "type"),
        Column("speed", "speed"),
        Column("stby-sig", "standby_signaling"),
        Column("lacp-key", "lacp_key"),
        Column("lacp-itvl", "lacp_interval"),
        Column("lacp-mode", "lacp_mode"),
        Column("lacp-sysid", "lacp_system_id"),
        Column("lacp-prio", "lacp_priority"),
    ),
    each="members",
    each_columns=(
        Column("act", "activity"),
        # Abbreviated so the column stays narrow enough to read; the record
        # keeps the port's full name.
        Column("member-itf", lambda m: m.name.replace("ethernet", "et")),
        Column("member-oper", "oper"),
    ),
)


def _static_next_hops(route: StaticRoute) -> Any:
    """Each next-hop, marked ``(R)`` where it resolves through the route table."""
    return _listed(f"{nh.address}(R)" if nh.resolve else nh.address for nh in route.next_hops)


STATIC_ROUTES_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="routes",
    each_columns=(
        Column("route", "prefix"),
        Column("admin-state", "admin"),
        Column("installed", "installed"),
        Column("metric", "metric"),
        Column("pref", "preference"),
        Column("nhops", _static_next_hops),
    ),
)

TUNNEL_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="tunnels",
    each_columns=(
        Column("Prefix", "prefix"),
        Column("type", "type"),
        Column("owner", "owner"),
        Column("pref", "preference"),
        Column("metric", "metric"),
        Column("next-hop", lambda t: _listed(nh.address for nh in t.next_hops if nh.address)),
        Column("egress-itf", lambda t: _listed(nh.subinterface for nh in t.next_hops if nh.subinterface)),
        Column("label", lambda t: _listed(label for nh in t.next_hops for label in nh.labels)),
    ),
)


def _irb_addresses(addresses: Tuple[IrbAddress, ...]) -> str:
    """Each address with what it is there as: ``10.0.1.254/24 (P,AGW)``."""
    cells = []
    for address in addresses:
        flags = [flag for flag, on in (("P", address.primary), ("AGW", address.anycast_gw)) if on]
        cells.append(f"{address.prefix} ({','.join(flags)})" if flags else address.prefix)
    return ", ".join(cells)


def _host_route_cells(rules: Any) -> List[str]:
    return [
        f"host-rt:{rule.route_type or '?'}/{'dp' if rule.datapath_programming else 'no-dp'}"
        for rule in rules
    ]


def _irb_arp(irb: IrbInterface) -> str:
    parts = (["proxy"] if irb.arp.proxy else []) + (["learn-unsol"] if irb.arp.learn_unsolicited else [])
    return ", ".join(parts + _host_route_cells(irb.arp.host_routes)) or "-"


def _irb_nd(irb: IrbInterface) -> str:
    parts = ["proxy"] if irb.nd.proxy else []
    if irb.nd.learn_unsolicited and irb.nd.learn_unsolicited != "none":
        parts.append(f"learn-unsol:{irb.nd.learn_unsolicited}")
    return ", ".join(parts + _host_route_cells(irb.nd.host_routes)) or "-"


def _evpn_advertise(route_types: Tuple[str, ...]) -> str:
    return ", ".join(rt or "?" for rt in route_types) or "-"


def _yes_no(value: bool) -> str:
    return "Y" if value else "N"


IRB_TABLE = Table(
    columns=(
        Column("name", "name"),
        Column("NI", lambda irb: _joined(irb.nis)),
        Column("ipv4", lambda irb: _irb_addresses(irb.ipv4)),
        Column("ipv6", lambda irb: _irb_addresses(irb.ipv6)),
        Column("AGW?", lambda irb: _yes_no(irb.anycast_gw)),
        Column("arp", _irb_arp),
        Column("nd", _irb_nd),
        Column("arp-evpn", lambda irb: _evpn_advertise(irb.arp.evpn_advertise)),
        Column("nd-evpn", lambda irb: _evpn_advertise(irb.nd.evpn_advertise)),
        Column("IFL?", lambda irb: _yes_no(irb.arp.interface_less_routing or irb.nd.interface_less_routing)),
    ),
)

ES_DEST_TABLE = Table(
    columns=(Column("tunnel", "tunnel"),),
    each="destinations",
    each_columns=(
        Column("esi", "esi"),
        Column("vteps", lambda d: " ".join(d.vteps)),
    ),
)



def _family_cell(name: str) -> Callable[[Neighbor], str]:
    """``received/active/sent`` for a family, or the one word that says why not."""

    def cell(neighbor: Neighbor) -> str:
        family = neighbor.family(name)
        if family is None:
            return "-"
        if not family.enabled:
            return "disabled"
        if family.oper == "down":
            return "down"
        return f"{family.received}/{family.active}/{family.sent}"

    return cell


def _session(neighbor: Neighbor) -> str:
    """The session state, with the one that matters read as ``up``.

    The record keeps what the device says - ``established`` - so a check and
    the JSON output see BGP's own vocabulary; a table wants the word that the
    eye finds among ``idle``, ``active`` and ``connect``.
    """
    return "up" if neighbor.state == "established" else neighbor.state


def _flags(neighbor: Neighbor) -> str:
    return "".join(
        letter if enabled else "-"
        for letter, enabled in (
            ("D", neighbor.dynamic),
            ("B", neighbor.bfd),
            ("F", neighbor.fast_failover),
        )
    )


BGP_PEERS_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="neighbors",
    each_columns=(
        Column("peer", "peer"),
        Column("state", _session),
        Column("local-address", "local_address"),
        Column("local-port", "local_port"),
        Column("evpn\nRx/Act/Tx", _family_cell("evpn")),
        Column("ipv4-unicast\nRx/Act/Tx", _family_cell("ipv4-unicast")),
        Column("ipv6-unicast\nRx/Act/Tx", _family_cell("ipv6-unicast")),
        Column("l3vpn-ipv4-unicast\nRx/Act/Tx", _family_cell("l3vpn-ipv4-unicast")),
        Column("l3vpn-ipv6-unicast\nRx/Act/Tx", _family_cell("l3vpn-ipv6-unicast")),
        Column("export-policy", lambda n: _listed(n.export_policies)),
        Column("flags", _flags),
        Column("group", "group"),
        Column("import-policy", lambda n: _listed(n.import_policies)),
        Column("local-as", lambda n: n.local_as if n.local_as is not None else "-"),
        Column("peer-as", "peer_as"),
    ),
)

MAC_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="entries",
    each_columns=(
        Column("mac", "address"),
        Column("Dest", "destination"),
        Column("Type", "type"),
    ),
)


def _vxlan_destinations(vxlan: VxlanInterface) -> str:
    return (
        _joined(f"({d.vtep}, {d.vni if d.vni is not None else ''})" for d in vxlan.destinations)
        or "-"
    )


VXLAN_TABLE = Table(
    columns=(
        Column("vxlan-itf", "name"),
        Column("NI", "ni"),
        Column("ing-vni", lambda v: v.vni if v.vni is not None else "-"),
        Column("destinations", _vxlan_destinations),
    ),
)


def _es_attachment(es: EthernetSegment) -> str:
    """The ports a segment hangs off, or the next-hops a virtual one tracks."""
    return " ".join(es.interfaces) or " ".join(nh.address for nh in es.next_hops)


def _es_evis(es: EthernetSegment) -> str:
    """The EVIs of a virtual segment, paired with a next-hop only where that matters.

    One next-hop, or several that are tied to the same EVIs, is just the EVI
    list. A segment that tracks a different next-hop per EVI has to say which
    of them is which, or the column cannot be matched to a router at all.
    """
    with_evis = [nh for nh in es.next_hops if nh.evis]
    if not with_evis:
        return ""
    if len({nh.evis for nh in with_evis}) == 1:
        return " ".join(with_evis[0].evis)
    return " ".join(f"{nh.address}:{','.join(nh.evis)}" for nh in with_evis)


def _es_associations(es: EthernetSegment) -> str:
    """Each network-instance with its DF candidates, the elected one marked."""
    return _joined(
        f"{a.ni}:[{' '.join(c.address + '(DF)' if c.designated else c.address for c in a.candidates)}]"
        for a in es.associations
    )


ES_TABLE = Table(
    columns=(
        Column("name", "name"),
        Column("esi", "esi"),
        Column("type", "type"),
        Column("mh-mode", "mh_mode"),
        Column("oper", "oper"),
        Column("itf/nh", _es_attachment),
        Column("evi", _es_evis),
        Column("ni-peers", _es_associations),
    ),
)


def _usecs(value: Optional[int]) -> str:
    """A BFD interval, in the milliseconds it is configured in."""
    return f"{value // 1000}ms" if value else ""


BFD_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="sessions",
    each_columns=(
        Column("remote", "remote_address"),
        Column("state", "state"),
        Column("remote-state", "remote_state"),
        Column("interface", "interface"),
        Column("local", "local_address"),
        Column("clients", lambda s: _joined(s.protocols)),
        Column("last-change", "last_transition"),
        Column("failures", "failures"),
        Column("diag", lambda s: s.local_diagnostic if s.local_diagnostic != "no_diagnostic" else ""),
        Column("tx/rx", lambda s: f"{_usecs(s.tx_interval)}/{_usecs(s.rx_interval)}".strip("/")),
    ),
)

ISIS_TABLE = Table(
    columns=(
        Column("NI", "ni"),
        Column("instance", "instance"),
        Column("interface", "name"),
        Column("oper", "oper"),
        Column("circuit", lambda i: "passive" if i.passive else i.circuit_type),
    ),
    each="adjacencies",
    each_columns=(
        Column("neighbor", lambda a: a.hostname or a.system_id),
        Column("system-id", "system_id"),
        Column("level", "level"),
        Column("state", "state"),
        Column("nbr-ipv4", "ipv4"),
        Column("last-change", "last_transition"),
        Column("flaps", "transitions"),
        Column("down-reason", "down_reason"),
    ),
)

OSPF_TABLE = Table(
    columns=(
        Column("NI", "ni"),
        Column("instance", "instance"),
        Column("area", "area"),
        Column("interface", "name"),
        Column("oper", "oper"),
        Column("type", lambda i: "passive" if i.passive else i.interface_type),
    ),
    each="neighbors",
    each_columns=(
        Column("router-id", "router_id"),
        Column("address", "address"),
        Column("state", "state"),
        Column("last-established", "last_established"),
        Column("changes", "state_changes"),
    ),
)

RESOURCES_TABLE = Table(
    columns=(
        Column("component", "component"),
        Column("resource", "name"),
        Column("used-%", "used_percent"),
        Column("used", "used"),
        Column("free", "free"),
    ),
)

COMPONENTS_TABLE = Table(
    columns=(
        Column("kind", "kind"),
        Column("id", "id"),
        Column("oper", "oper"),
        Column("health", "health"),
        Column("type", "type"),
        Column("serial", "serial_number"),
    ),
)


def _dbm(value: Optional[float]) -> str:
    return f"{value:.2f}" if value is not None else ""


TRANSCEIVERS_TABLE = Table(
    columns=(
        Column("interface", "interface"),
        Column("oper", "oper"),
        Column("form-factor", "form_factor"),
        Column("pmd", "pmd"),
        Column("vendor", "vendor"),
        Column("part", "part_number"),
        Column("temp-C", lambda t: f"{t.temperature:.1f}" if t.temperature is not None else ""),
        Column("rx-dBm", lambda t: _joined(_dbm(c.input_power) for c in t.channels)),
        Column("tx-dBm", lambda t: _joined(_dbm(c.output_power) for c in t.channels)),
        Column("alarms", lambda t: _joined(t.alarms)),
        Column("warnings", lambda t: _joined(t.warnings)),
    ),
)


def _route_next_hop(nh: Any) -> str:
    """A next-hop as the table names it: its address, or what it resolves through."""
    if nh.type == "indirect" and nh.resolving_route:
        return f"{nh.resolving_route} (indirect)"
    return nh.address


def _route_egress(route: Route) -> Any:
    """Every port, tunnel or prefix the route leaves through, in next-hop order.

    One indirect next-hop can resolve onto several ports, so there can be
    more of these than next-hops.
    """
    return _listed(
        f"{hop.label}@vrf:{hop.ni}" if hop.ni else hop.label
        for nh in route.next_hops
        for hop in nh.egress
    )


IP_RIB_TABLE = Table(
    columns=(Column("NI", "ni"),),
    each="routes",
    each_columns=(
        Column("Act", lambda r: "yes" if r.active else "no"),
        Column("Prefix", "prefix"),
        Column("itf", _route_egress),
        Column("metric", "metric"),
        Column("next-hop", lambda r: _listed(nh for nh in map(_route_next_hop, r.next_hops) if nh)),
        Column("orig-vrf", "leaked_from"),
        Column("pref", "preference"),
        Column("type", "type"),
    ),
)


def _dash(value: Any) -> Any:
    return "-" if value is None else value


def _route_status(route: BgpRoute) -> str:
    """``u*>``: used, valid, best - the way the CLI marks a route."""
    return ("u" if route.used else "") + ("*" if route.valid else "") + (">" if route.best else "")


def _esi_labels(route: BgpRoute) -> str:
    return ",".join(
        label.replace("Single-Active", "S-A").replace("All-Active", "A-A")
        for label in route.esi_labels
    )


#: Every column a BGP RIB table can have, by name. Which of them a table shows
#: depends on the family and the EVPN route type.
_BGP_RIB_COLUMNS: Dict[str, Column] = {
    column.name: column
    for column in (
        Column("st", _route_status),
        Column("ESI", "esi"),
        Column("GW", "gateway"),
        Column("IP", "ip"),
        Column("IP-Pfx", "prefix"),
        Column("Pfx", "prefix"),
        Column("Prefix", "prefix"),
        Column("L1", lambda r: _dash(r.label1)),
        Column("L2", lambda r: _dash(r.label2)),
        Column("MAC", "mac"),
        Column("RD", "rd"),
        Column("RT", lambda r: _joined(r.route_targets)),
        Column("Tag", "tag"),
        Column("as-path", lambda r: _listed(r.as_path)),
        Column(
            "communities",
            lambda r: _joined([*r.communities, *r.large_communities, *r.ext_communities]),
        ),
        Column("esi-lbl", _esi_labels),
        Column("lpref", "local_pref"),
        Column("med", "med"),
        Column("neighbor", "neighbor"),
        Column("next-hop", "next_hop"),
        Column("NextHop", "next_hop"),
        Column("origin", "origin"),
        Column("peer", "neighbor"),
        Column("vni", lambda r: _dash(r.vni)),
        # The path attributes a table shows only in detail.
        Column("soo", lambda r: _joined(r.soo)),
        Column("tunnel-encap", lambda r: _joined(r.tunnel_encap)),
        Column("dpath", lambda r: " ".join(r.domain_path)),
        Column("valid", "valid"),
        Column("best", "best"),
        Column("used", "used"),
        Column("tie-break", "tie_break"),
        Column("internal-tags", lambda r: _listed(r.internal_tags)),
        Column("neighbor-as", "neighbor_as"),
    )
}

#: The columns of each BGP RIB table, keyed by family and - for EVPN - route
#: type, in the order the tables have always had them.
_BGP_RIB_LAYOUT: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("evpn", "1"): ("st", "ESI", "NextHop", "RD", "RT", "Tag", "as-path", "communities", "esi-lbl", "peer", "vni"),
    ("evpn", "2"): ("st", "ESI", "IP", "L1", "L2", "MAC", "RD", "RT", "as-path", "communities", "next-hop", "peer", "vni"),
    ("evpn", "3"): ("st", "RD", "RT", "Tag", "as-path", "communities", "next-hop", "origin", "peer"),
    ("evpn", "4"): ("st", "ESI", "RD", "RT", "as-path", "communities", "next-hop", "origin", "peer"),
    ("evpn", "5"): ("st", "ESI", "GW", "IP-Pfx", "RD", "RT", "as-path", "communities", "lpref", "med", "next-hop", "origin", "peer", "vni"),
    ("ipv4-unicast", ""): ("st", "Prefix", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
    ("ipv6-unicast", ""): ("st", "Prefix", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
    ("l3vpn-ipv4-unicast", ""): ("st", "Pfx", "RD", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
    ("l3vpn-ipv6-unicast", ""): ("st", "Pfx", "RD", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
}
_BGP_RIB_DETAIL: Tuple[str, ...] = (
    "soo", "tunnel-encap", "dpath", "valid", "best", "used", "tie-break", "internal-tags", "neighbor-as",
)

#: What the BGP RIB report calls a family, and what the model calls it.
_BGP_RIB_FAMILIES = {
    "evpn": "evpn",
    "ipv4": "ipv4-unicast",
    "ipv6": "ipv6-unicast",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}


def bgp_rib_table(route_fam: str = "evpn", route_type: Optional[str] = None, detail: bool = False) -> Table:
    """The table for one BGP RIB family, taking the getter's own arguments.

    An unknown family or route type gets the columns every family shares,
    which is what a table can still show of a getter that raised.
    """
    family = BGP_RIB_ROUTE_FAM_ALIASES.get(str(route_fam).lower(), str(route_fam))
    family = _BGP_RIB_FAMILIES.get(family, family)
    layout = _BGP_RIB_LAYOUT.get(
        (family, str(route_type or "2") if family == "evpn" else ""),
        ("st", "as-path", "communities", "neighbor", "next-hop"),
    )
    names = layout + (_BGP_RIB_DETAIL if detail else ())
    return Table(
        columns=(Column("NI", "ni"),),
        each="routes",
        each_columns=tuple(_BGP_RIB_COLUMNS[name] for name in names),
    )


def _bgp_rib_table_for(route_fam: Optional[str] = None, route_type: Optional[str] = None) -> Callable[[Mapping[str, Any]], Table]:
    """A report's table as a function of its parameters, with what is bound.

    The interactive report takes the family and route type as arguments; a
    streaming variant has them baked in and only ``detail`` left to decide.
    """

    def table(params: Mapping[str, Any]) -> Table:
        return bgp_rib_table(
            route_fam if route_fam is not None else str(params.get("route_fam") or "evpn"),
            route_type if route_fam is not None else params.get("route_type"),
            bool(params.get("detail")),
        )

    return table


def _network_instance_subscriptions(interval: int) -> Tuple[SubscriptionSpec, ...]:
    """What a network-instance is, as the instance and service reports read it.

    Enough to name and type it, see what is bound to it and what it
    advertises with. Not the whole subtree: that carries every route table
    and BGP RIB of the node, which on a spine is the bulk of its state and
    enough to put the node's entire stream behind - and then LLDP and routes
    go stale on it too. The getters still ask for ``/network-instance[name=*]``
    and are answered from what these subscriptions put under it.
    """
    return (
        SubscriptionSpec("/network-instance[name=*]/type", datatype="all", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/oper-state", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/interface", datatype="all", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/vxlan-interface", datatype="all", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/protocols/bgp/router-id", datatype="all", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/protocols/bgp-vpn", datatype="all", sample_interval=interval),
        SubscriptionSpec("/network-instance[name=*]/protocols/bgp-evpn", datatype="all", sample_interval=interval),
    )


#: Every service report reads the same two trees.
_SERVICE_SUBSCRIPTIONS: Tuple[SubscriptionSpec, ...] = (
    *_network_instance_subscriptions(20),
    # The BGP sessions a service tile lists.
    SubscriptionSpec("/network-instance[name=*]/protocols/bgp/neighbor", datatype="all", sample_interval=20),
    SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=20),
    # A member reported 'port-down' is explained by its parent port, and that
    # is what says whether a standby ethernet-segment or a fault put it there.
    SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=20),
    # The segment a multi-homed service's members hang off, whose mode and DF
    # say which leaf forwards for it.
    SubscriptionSpec(
        "/system/network-instance/protocols/evpn/ethernet-segments",
        datatype="all",
        sample_interval=20,
    ),
    SubscriptionSpec(
        "/network-instance[name=default]/route-table/ipv4-unicast/route/ipv4-prefix",
        datatype="state",
        sample_interval=20,
    ),
    SubscriptionSpec(
        "/network-instance[name=default]/route-table/ipv6-unicast/route/ipv6-prefix",
        datatype="state",
        sample_interval=20,
    ),
)

#: Resolving a route's next-hop needs the next-hop(-group) tables alongside it.
_NEXT_HOP_SUBSCRIPTIONS: Tuple[SubscriptionSpec, ...] = (
    SubscriptionSpec("/network-instance[name=*]/route-table/next-hop-group[index=*]", datatype="state"),
    SubscriptionSpec("/network-instance[name=*]/route-table/next-hop[index=*]", datatype="state"),
)


def _bgp_rib_variants() -> List[ReportSpec]:
    """One streaming report per BGP address family.

    A streamed report cannot be given arguments, so the server needs the address
    family baked in. EVPN is split further, by route type, because a fabric's
    EVPN RIB is far too large to read as one table.
    """
    evpn = [
        ("1", "type 1 (A-D)", "EVPN auto-discovery routes received from BGP peers."),
        ("2", "type 2 (MAC/IP)", "EVPN MAC/IP advertisement routes received from BGP peers."),
        ("3", "type 3 (IMET)", "EVPN inclusive multicast ethernet tag routes."),
        ("4", "type 4 (ES)", "EVPN ethernet segment routes."),
        ("5", "type 5 (IP prefix)", "EVPN IP prefix routes."),
    ]
    families = [
        ("ipv4", "ipv4", "IPv4 unicast", "IPv4 unicast"),
        ("ipv6", "ipv6", "IPv6 unicast", "IPv6 unicast"),
        ("l3vpn_v4", "l3vpn-ipv4-unicast", "L3VPN IPv4", "VPN-IPv4 unicast"),
        ("l3vpn_v6", "l3vpn-ipv6-unicast", "L3VPN IPv6", "VPN-IPv6 unicast"),
    ]
    variants = [
        ReportSpec(
            name=f"bgp_rib_evpn_{route_type}",
            resource="bgp_rib",
            title=f"BGP RIB - EVPN {label}",
            description=description,
            getter=_bound_bgp_rib("evpn", route_type),
            table=_bgp_rib_table_for("evpn", route_type),
            category="BGP RIB",
            surfaces=STREAMING,
        )
        for route_type, label, description in evpn
    ]
    variants.extend(
        ReportSpec(
            name=f"bgp_rib_{suffix}",
            resource="bgp_rib",
            title=f"BGP RIB - {label}",
            description=f"{noun} routes in the BGP RIB-in-post.",
            getter=_bound_bgp_rib(route_fam),
            table=_bgp_rib_table_for(route_fam),
            category="BGP RIB",
            surfaces=STREAMING,
        )
        for suffix, route_fam, label, noun in families
    )
    return variants


#: The families a peer's received routes can be asked for, by the name the
#: BGP peers table gives them, and what the RIB getter calls each.
_RECEIVED_FAMILIES: Dict[str, str] = {
    "evpn": "evpn",
    "ipv4-unicast": "ipv4",
    "ipv6-unicast": "ipv6",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}
_RECEIVED_ALIASES = {"ipv4": "ipv4-unicast", "ipv6": "ipv6-unicast"}


def _same_address(a: str, b: str) -> bool:
    """Whether two peer addresses are one, however each is written.

    A link-local peer carries the interface it is scoped to, which is part of
    which peer it is: compared as written when either does not parse.
    """
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


def _peer_rib(rib: str) -> Callable[..., Dict[str, Any]]:
    """A getter for the routes exchanged with one BGP peer, in one direction.

    *rib* ``in`` is what peers sent, read out of the same RIB the BGP RIB
    reports show; ``out`` is what was sent to them, out of the rib-out-post.
    Either keeps the routes whose neighbor is *peer*, or without one every
    peer's. Without a *family* every family is read - which is also what the
    server discovers the paths to stream from, as it calls a getter without
    arguments - and EVPN is every route type.
    """

    def getter(device: Any, peer: Optional[str] = None, family: Optional[str] = None) -> Dict[str, Any]:
        if family:
            wanted = _RECEIVED_ALIASES.get(family.lower(), family.lower())
            if wanted not in _RECEIVED_FAMILIES:
                raise ValueError(
                    f"family: '{family}' is not one of {', '.join(_RECEIVED_FAMILIES)}"
                )
            families = [wanted]
        else:
            families = list(_RECEIVED_FAMILIES)
        ribs: List[BgpRib] = []
        for name in families:
            route_fam = _RECEIVED_FAMILIES[name]
            for route_type in ("1", "2", "3", "4", "5") if name == "evpn" else (None,):
                kwargs: Dict[str, Any] = {"route_fam": route_fam, "rib": rib}
                if route_type is not None:
                    kwargs["route_type"] = route_type
                for table in device.get_bgp_rib(**kwargs).get("bgp_rib", []):
                    routes = tuple(
                        route
                        for route in table.routes
                        # A locally originated route names no peer.
                        if route.neighbor not in ("", "0.0.0.0", "::")
                        and (not peer or _same_address(route.neighbor, peer))
                    )
                    if routes:
                        ribs.append(replace(table, routes=routes))
        return {"bgp_rib": ribs}

    return getter


#: One layout for every family, so a peer's routes read as one table: the
#: NLRI fields a family or route type does not have stay empty.
BGP_RECEIVED_TABLE = Table(
    columns=(
        Column("NI", "ni"),
        Column("family", "family"),
        # The EVPN route type, 1 to 5; empty for the other families.
        Column("type", "route_type"),
    ),
    each="routes",
    each_columns=(
        Column("peer", "neighbor"),
        Column("st", _route_status),
        Column("RD", "rd"),
        Column("Prefix", "prefix"),
        Column("MAC", "mac"),
        Column("IP", "ip"),
        Column("ESI", "esi"),
        Column("Tag", "tag"),
        Column("next-hop", "next_hop"),
        Column("vni", "vni"),
        Column("RT", lambda r: _joined(r.route_targets)),
        Column("as-path", lambda r: _listed(r.as_path)),
        Column(
            "communities",
            lambda r: _joined([*r.communities, *r.large_communities, *r.ext_communities]),
        ),
        Column("lpref", "local_pref"),
        Column("med", "med"),
        Column("origin", "origin"),
    ),
)

#: What was sent carries no used/valid/best flags, and names the peer it went to.
BGP_ADVERTISED_TABLE = Table(
    columns=BGP_RECEIVED_TABLE.columns,
    each="routes",
    each_columns=tuple(c for c in BGP_RECEIVED_TABLE.each_columns if c.name != "st"),
)


REPORTS: List[ReportSpec] = [
    ReportSpec(
        name="overview",
        resource="overview",
        title="Overview",
        description="Executive Fabric KPI dashboard and health metrics.",
        # Computed by the store from the streamed trees below, not by a getter.
        getter=lambda d: {},
        category="Dashboard",
        surfaces=STREAMING,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=10),
            # 'admin-state', 'description' and 'type' are config leaves: see
            # SubscriptionSpec.
            SubscriptionSpec("/interface[name=*]/admin-state", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/oper-state", sample_interval=10),
            # A port an ethernet-segment holds in standby is down by design, and
            # this is what keeps it out of the 'oper down' count.
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/description", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/ethernet", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/type", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/oper-state", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp/neighbor", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp-vpn", datatype="all", sample_interval=10),
        ),
    ),
    ReportSpec(
        name="topology",
        resource="topology",
        title="Topology",
        description="Fabric graph from LLDP, with the tier of each node inferred from its services.",
        # Computed by the store from the streamed trees below, not by a getter.
        getter=lambda d: {},
        category="Dashboard",
        surfaces=STREAMING,
        # Paths that mix config leaves ('host-name', 'type') are read as 'all':
        # a 'state' Get answers nothing for those (see SubscriptionSpec). The
        # chassis type is state-only, the same path the sys-info report uses.
        subscribe=(
            # LLDP gives the cables; the host-name is what a neighbour is
            # advertised under, and the only reliable way back to the inventory.
            SubscriptionSpec(
                "/system/lldp/interface[name=*]/neighbor",
                datatype="all",
                sample_interval=30,
            ),
            SubscriptionSpec("/system/name/host-name", datatype="all", sample_interval=30),
            # Chassis type, drawn on each node. Sampled rarely: it does not
            # change without a hardware swap.
            SubscriptionSpec("/platform/chassis", datatype="state", sample_interval=60),
            SubscriptionSpec("/interface[name=*]/oper-state", datatype="all", sample_interval=30),
            # Which of the down ports are only standing by, so the cable to a
            # multi-homed client is not drawn from the leaf that is not
            # forwarding.
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=30),
            # Egress of each interface, so each end of a cable can be coloured
            # from the rate leaving that port. Sampled often enough that a lab
            # generating traffic will move the graph with it.
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=5),
            # A node's tier follows from the services on it: mac-vrfs and
            # ip-vrfs make it a leaf, two bgp-vpn instances make it a DCGW.
            SubscriptionSpec("/network-instance[name=*]/type", datatype="all", sample_interval=30),
            SubscriptionSpec(
                "/network-instance[name=*]/protocols/bgp-vpn",
                datatype="all",
                sample_interval=30,
            ),
            # The client tier: which subinterfaces a service is configured
            # towards, and the vlan and address each of them attaches on.
            SubscriptionSpec(
                "/network-instance[name=*]/interface",
                datatype="all",
                sample_interval=30,
            ),
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=30),
            # The ESI a port is in, which is what says the lags of a multi-homed
            # client on two leaves are one client rather than two.
            SubscriptionSpec(
                "/system/network-instance/protocols/evpn/ethernet-segments",
                datatype="all",
                sample_interval=30,
            ),
        ),
    ),
    ReportSpec(
        name="sys_info",
        table=SYS_INFO_TABLE,
        resource="sys_info",
        key_columns=("Node",),
        title="System Info",
        description="Chassis type, serial, software version and last boot time.",
        getter=lambda d: d.get_info(),
        category="System",
        sample_interval=60,
        subscribe=(
            SubscriptionSpec("/platform/chassis", datatype="state"),
            SubscriptionSpec("/platform/control[slot=A]", datatype="state"),
        ),
    ),
    ReportSpec(
        name="ifstats",
        table=IFSTATS_TABLE,
        resource="ifstats",
        key_columns=("Node", "interface"),
        title="Interface Stats",
        description="Per-interface rates and error/discard counters, derived from "
        "streamed gNMI counter samples.",
        getter=lambda d, interval=5: d.get_ifstats(interval=interval),
        category="Interfaces",
        sample_interval=5,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=5),
            SubscriptionSpec("/interface[name=*]/oper-state", sample_interval=30),
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="subif",
        table=SUBIF_TABLE,
        resource="subinterface",
        key_columns=("Node", "Subitf"),
        title="Sub-Interfaces",
        description="Sub-interfaces with their type, addresses and operational state.",
        getter=lambda d: d.get_sum_subitf(),
        category="Interfaces",
        mcp_name="subinterfaces",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=20),
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="lag",
        table=LAG_TABLE,
        resource="lag",
        key_columns=("Node", "lag", "member-itf"),
        title="LAGs",
        description="Link aggregation groups and their members.",
        getter=lambda d: d.get_lag(),
        category="Interfaces",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/interface[name=lag*]", datatype="all", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="ni",
        table=NI_TABLE,
        resource="nwi_itfs",
        key_columns=("Node", "NI", "Subitf"),
        title="Network Instances",
        description="Network instances and the interfaces bound to them.",
        getter=lambda d: d.get_nwi_itf(),
        category="Interfaces",
        mcp_name="network_instances",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=30),
            *_network_instance_subscriptions(30),
        ),
    ),
    ReportSpec(
        name="bgp_peers",
        table=BGP_PEERS_TABLE,
        resource="bgp_peers",
        key_columns=("Node", "NI", "peer"),
        title="BGP Peers",
        description="BGP neighbors, their session state and per-AF route counters.",
        getter=lambda d: d.get_sum_bgp(),
        category="BGP",
        sample_interval=10,
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp/neighbor", datatype="all", sample_interval=10),
        ),
    ),
    ReportSpec(
        name="bgp_rib",
        resource="bgp_rib",
        title="BGP RIB",
        description="Routes in the BGP RIB-in-post with their path attributes.",
        getter=_bgp_rib,
        table=_bgp_rib_table_for(),
        category="BGP RIB",
        surfaces=INTERACTIVE,
    ),
    *_bgp_rib_variants(),
    *(
        ReportSpec(
            name=f"bgp_{direction}_routes",
            resource="bgp_rib",
            title=f"BGP {direction.capitalize()} Routes",
            description=description,
            getter=_peer_rib(rib),
            table=table,
            category="BGP RIB",
            surfaces=STREAMING,
            params=(
                ParamSpec(
                    name="peer",
                    label="Peer",
                    placeholder="10.0.0.1",
                    help="The peer address whose routes to list; empty lists every peer's",
                    # Not 'address': an unnumbered peer is a link-local address
                    # scoped to its interface, fe80::1%ethernet-1/1.0, which does
                    # not parse as one.
                ),
                ParamSpec(
                    name="family",
                    label="Family",
                    placeholder="all",
                    help="evpn, ipv4-unicast, ipv6-unicast, l3vpn-ipv4-unicast or l3vpn-ipv6-unicast; empty is every family",
                ),
            ),
        )
        for direction, rib, table, description in (
            ("received", "in", BGP_RECEIVED_TABLE,
             "Routes a BGP peer sent, in every family or the one chosen, from the RIB-in-post."),
            ("advertised", "out", BGP_ADVERTISED_TABLE,
             "Routes sent to a BGP peer, in every family or the one chosen, from the RIB-out-post."),
        )
    ),
    ReportSpec(
        name="ipv4_rib",
        table=IP_RIB_TABLE,
        resource="ip_rib",
        # A prefix can be offered by more than one protocol at once, so the
        # route type is part of what names a route rather than of what it says.
        key_columns=("Node", "NI", "Prefix", "type"),
        title="IPv4 RIB",
        description="IPv4 route table with resolved next-hops.",
        getter=lambda d, address=None: d.get_rib(
            afi="ipv4-unicast", lpm_address=address
        ),
        category="Routing",
        params=(_lpm_param("10.0.0.1"),),
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv4-unicast", datatype="state"),
        )
        + _NEXT_HOP_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="ipv6_rib",
        table=IP_RIB_TABLE,
        resource="ip_rib",
        key_columns=("Node", "NI", "Prefix", "type"),
        title="IPv6 RIB",
        description="IPv6 route table with resolved next-hops.",
        getter=lambda d, address=None: d.get_rib(
            afi="ipv6-unicast", lpm_address=address
        ),
        category="Routing",
        params=(_lpm_param("2001:db8::1"),),
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv6-unicast", datatype="state"),
        )
        + _NEXT_HOP_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="static_routes",
        table=STATIC_ROUTES_TABLE,
        resource="static_routes",
        key_columns=("Node", "NI", "route"),
        title="Static Routes",
        description="Configured static routes and their operational state.",
        getter=lambda d: d.get_static_routes(),
        category="Routing",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/static-routes", datatype="all", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="tunnel_table",
        table=TUNNEL_TABLE,
        resource="tunnel_table",
        key_columns=("Node", "NI", "Prefix", "type"),
        title="Tunnel Table",
        description="IP tunnel table (VXLAN, LDP, SR-ISIS, RSVP, ...).",
        getter=lambda d: d.get_tunnel_table(),
        category="Routing",
    ),
    ReportSpec(
        name="routing_pol",
        resource="routing_pol",
        title="Routing Policies",
        description="Routing policies with their statements and actions.",
        getter=lambda d: d.get_routing_policies(),
        category="Routing",
        mcp_name="routing_policies",
        # Policies nest arbitrarily deep, so there are no columns to derive.
        surfaces=INTERACTIVE,
        tabular=False,
    ),
    ReportSpec(
        name="services",
        resource="services",
        key_columns=("Node", "Service Type", "MAC-VRF", "IP-VRF"),
        title="Services",
        description="EVPN Bridge Domains (MAC-VRF) and Routers (IP-VRF) grouped by "
        "Route-Target.",
        getter=lambda d: d.get_services(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="bridge_domains",
        resource="bridge_domains",
        key_columns=("Node", "MAC-VRF"),
        title="Bridge Domains",
        description="EVPN Bridge Domains (MAC-VRF) grouped by Route-Target with bound "
        "access sub-interfaces, their ethernet-segments and VXLAN overlays.",
        getter=lambda d: d.get_bridge_domains(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="routers",
        resource="routers",
        key_columns=("Node", "IP-VRF"),
        title="Routers",
        description="EVPN Routers (IP-VRF) grouped by Route-Target with bound MAC-VRFs, "
        "routed sub-interfaces, virtual ethernet-segments and VXLAN overlays.",
        getter=lambda d: d.get_routers(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="mac",
        table=MAC_TABLE,
        resource="mac_table",
        key_columns=("Node", "NI", "mac"),
        title="MAC Table",
        description="Bridge table MAC entries per network instance.",
        getter=lambda d: d.get_mac_table(),
        category="EVPN / L2",
        mcp_name="mac_table",
        sample_interval=10,
    ),
    ReportSpec(
        name="irb",
        table=IRB_TABLE,
        resource="irb",
        key_columns=("Node", "name"),
        title="IRB Interfaces",
        description="IRB sub-interfaces and their anycast gateway configuration.",
        getter=lambda d: d.get_irb(),
        category="EVPN / L2",
        mcp_name="irb_interfaces",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/interface[name=irb*]/subinterface", datatype="all", sample_interval=30),
            SubscriptionSpec("/network-instance[name=*]/interface", datatype="config", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="es",
        table=ES_TABLE,
        resource="es",
        key_columns=("Node", "name"),
        title="Ethernet Segments",
        description="EVPN ethernet segments, multi-homing mode and DF state.",
        getter=lambda d: d.get_es(),
        category="EVPN / L2",
        mcp_name="ethernet_segments",
        sample_interval=20,
    ),
    ReportSpec(
        name="es_dest",
        table=ES_DEST_TABLE,
        resource="es_dest",
        key_columns=("Node", "tunnel", "esi"),
        title="L2-ES Destinations",
        description="Ethernet segment destinations in the bridge table.",
        getter=lambda d: d.get_es_dest(),
        category="EVPN / L2",
        mcp_name="es_destinations",
        sample_interval=20,
    ),
    ReportSpec(
        name="vxlan",
        table=VXLAN_TABLE,
        resource="vxlan",
        key_columns=("Node", "vxlan-itf"),
        title="VXLAN Tunnels",
        description="VXLAN tunnel interfaces and their unicast destinations.",
        getter=lambda d: d.get_vxlan(),
        category="EVPN / L2",
        mcp_name="vxlan_tunnels",
        sample_interval=20,
    ),
    ReportSpec(
        name="lldp",
        table=LLDP_TABLE,
        resource="lldp_nbrs",
        key_columns=("Node", "interface", "Nbr-System", "Nbr-port"),
        title="LLDP Neighbors",
        description="LLDP neighbors seen on each interface.",
        getter=lambda d: d.get_lldp_sum(),
        category="Neighbors",
        mcp_name="lldp_neighbors",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/system/lldp/interface[name=*]/neighbor", datatype="state", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="arp",
        table=ARP_TABLE,
        resource="arp",
        key_columns=("Node", "interface", "IPv4"),
        title="ARP Table",
        description="IPv4 ARP / neighbor entries per sub-interface.",
        getter=lambda d: d.get_arp(),
        category="Neighbors",
        mcp_name="arp_table",
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor", datatype="all"),
            # Which instance a subinterface is in: the interface lists, not the
            # subtree, which would stream every BGP RIB along with them.
            SubscriptionSpec("/network-instance[name=*]/interface", datatype="config"),
        ),
    ),
    ReportSpec(
        name="nd",
        table=ND_TABLE,
        resource="nd",
        key_columns=("Node", "interface", "IPv6"),
        title="IPv6 Neighbors",
        description="IPv6 neighbor discovery entries per sub-interface.",
        getter=lambda d: d.get_nd(),
        category="Neighbors",
        mcp_name="ipv6_neighbors",
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor", datatype="all"),
            SubscriptionSpec("/network-instance[name=*]/interface", datatype="config"),
        ),
    ),
    ReportSpec(
        name="bfd",
        table=BFD_TABLE,
        resource="bfd",
        key_columns=("Node", "NI", "remote"),
        title="BFD Sessions",
        description="BFD sessions, the protocols they protect, and how often they failed.",
        getter=lambda d: d.get_bfd(),
        category="Routing",
        mcp_name="bfd_sessions",
        sample_interval=10,
        subscribe=(
            SubscriptionSpec("/bfd/network-instance[name=*]/peer", sample_interval=10),
        ),
    ),
    ReportSpec(
        name="isis",
        table=ISIS_TABLE,
        resource="isis",
        key_columns=("Node", "NI", "interface", "system-id", "level"),
        title="IS-IS Adjacencies",
        description="IS-IS interfaces and the adjacencies formed on them.",
        getter=lambda d: d.get_isis(),
        category="Routing",
        mcp_name="isis_adjacencies",
        sample_interval=15,
        subscribe=(
            SubscriptionSpec(
                "/network-instance[name=*]/protocols/isis/instance[name=*]/interface[interface-name=*]",
                datatype="all",
                sample_interval=15,
            ),
        ),
    ),
    ReportSpec(
        name="ospf",
        table=OSPF_TABLE,
        resource="ospf",
        key_columns=("Node", "NI", "interface", "router-id"),
        title="OSPF Neighbors",
        description="OSPF interfaces and the neighbours on them.",
        getter=lambda d: d.get_ospf(),
        category="Routing",
        mcp_name="ospf_neighbors",
        sample_interval=15,
        subscribe=(
            SubscriptionSpec(
                "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/interface[interface-name=*]",
                datatype="all",
                sample_interval=15,
            ),
        ),
    ),
    ReportSpec(
        name="resources",
        table=RESOURCES_TABLE,
        resource="resources",
        key_columns=("Node", "component", "resource"),
        title="Resources",
        description="CPU, memory and forwarding-table utilization per node.",
        getter=lambda d: d.get_resources(),
        category="System",
        mcp_name="platform_resources",
        sample_interval=30,
    ),
    ReportSpec(
        name="components",
        table=COMPONENTS_TABLE,
        resource="components",
        key_columns=("Node", "kind", "id"),
        title="Hardware",
        description="Control and line cards, fabric modules, fans and power supplies.",
        getter=lambda d: d.get_components(),
        category="System",
        mcp_name="hardware_components",
        sample_interval=60,
    ),
    ReportSpec(
        name="transceivers",
        table=TRANSCEIVERS_TABLE,
        resource="transceivers",
        key_columns=("Node", "interface"),
        title="Transceivers",
        description="Optics with their light levels, temperature and the DOM "
        "thresholds they report as crossed.",
        getter=lambda d: d.get_transceivers(),
        category="Interfaces",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/transceiver", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="checks",
        resource="checks",
        key_columns=("Node", "Check", "Subject"),
        title="Checks",
        description="Fabric sanity checks: BGP sessions, interfaces, LLDP adjacencies, "
        "MTU, EVPN service consistency and ethernet-segment DF election.",
        # A finding is about the fabric rather than about one node, so this is
        # not collected per device. Each surface runs nornir_srl.checks over the
        # reports the checks declare, which is where the gNMI work happens.
        getter=lambda d: {},
        category="Dashboard",
        mcp_name="fabric_checks",
        sample_interval=20,
    ),
]

REPORTS = [_streamed(r) for r in REPORTS]
REPORTS_BY_NAME: Dict[str, ReportSpec] = {r.name: r for r in REPORTS}


# --------------------------------------------------------------------------- #
# What the server's own readings collect
# --------------------------------------------------------------------------- #

#: Only the underlay's route table, with the next-hops it uses.
_UNDERLAY = "default"


def _underlay_rib(afi: str, family: str, title: str) -> ReportSpec:
    return ReportSpec(
        name=f"{family}_rib_underlay",
        table=IP_RIB_TABLE,
        resource="ip_rib",
        title=f"{title} RIB (underlay)",
        description=f"The {title} route table of the default network-instance.",
        getter=lambda d: d.get_rib(afi=afi, network_instance=_UNDERLAY),
        category="Routing",
        # Surfaces offer the whole table; this one only stands in for it.
        surfaces=frozenset(),
        subscribe=(
            SubscriptionSpec(f"/network-instance[name={_UNDERLAY}]/route-table/{afi}", datatype="state"),
            SubscriptionSpec(f"/network-instance[name={_UNDERLAY}]/route-table/next-hop-group[index=*]", datatype="state"),
            SubscriptionSpec(f"/network-instance[name={_UNDERLAY}]/route-table/next-hop[index=*]", datatype="state"),
        ),
        stands_in_for=f"{family}_rib",
    )


RIB_SUMMARY = "rib_summary"

#: Reports no surface offers, which the server's readings collect in place of,
#: or alongside, the ones the checks name.
_READING_ONLY: Tuple[ReportSpec, ...] = (
    _underlay_rib("ipv4-unicast", "ipv4", "IPv4"),
    _underlay_rib("ipv6-unicast", "ipv6", "IPv6"),
    ReportSpec(
        name=RIB_SUMMARY,
        resource="rib_summary",
        title="Route table sizes",
        description="Active routes per network-instance and address family.",
        getter=lambda d: d.get_rib_summary(),
        category="Routing",
        surfaces=frozenset(),
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv4-unicast/statistics/active-routes", sample_interval=20),
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv6-unicast/statistics/active-routes", sample_interval=20),
        ),
    ),
)
_READING_ONLY = tuple(_streamed(r) for r in _READING_ONLY)
_READING_ONLY_BY_NAME: Dict[str, ReportSpec] = {r.name: r for r in _READING_ONLY}


def reading_reports(reports: Sequence[str]) -> Tuple[str, ...]:
    """*reports*, as a server reading the whole fabric every few seconds collects them.

    The route tables are the one thing a fabric-wide reading cannot afford
    in full: every prefix of every VRF of every node, streamed and re-read on
    each reading, is what a large fabric's server spends all its time on. What
    the readings use them for is the underlay - which loopbacks each node
    reaches - so that is the table they hold prefix by prefix, and every
    other table is followed by its size. The full tables are still there
    for whoever opens them.
    """
    stand_ins = {r.stands_in_for: r.name for r in _READING_ONLY if r.stands_in_for}
    chosen = [stand_ins.get(name, name) for name in reports]
    if any(name in stand_ins for name in reports):
        chosen.append(RIB_SUMMARY)
    return tuple(dict.fromkeys(chosen))


def get_report(name: str) -> ReportSpec:
    """Look a report up by its canonical name."""
    try:
        return REPORTS_BY_NAME.get(name) or _READING_ONLY_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown report '{name}'") from None


def reports_for(surface: str) -> List[ReportSpec]:
    """Every report offered on *surface*, in registry order."""
    return [r for r in REPORTS if r.on(surface)]
