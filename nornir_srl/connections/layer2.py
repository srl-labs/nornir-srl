from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Tuple

from .down_reason import STANDBY_STATE, ParentReasons, parent_interface
from .down_reason import clean_leaf as _clean_state
from ..records import (
    Association,
    BridgeTable,
    Candidate,
    EsDestination,
    EsDestinations,
    EthernetSegment,
    HostRouteRule,
    IrbAddress,
    IrbArp,
    IrbInterface,
    IrbNd,
    LldpInterface,
    LldpNeighbor,
    MacEntry,
    NextHop,
    VxlanDestination,
    VxlanInterface,
    as_int,
)
from .helpers import as_list, bgp_evpn_evis, first_payload, instances_by_interface
from .routing import _gnmi_path_missing, _suppress_pygnmi_client_logging


def _subinterface_down_reason(
    name: str,
    itf: Dict[str, Any],
    details: Dict[str, Any],
    parents: ParentReasons,
) -> str:
    """Why a subinterface in a service is down, followed to its root cause.

    The three views each defer to the next: the network-instance says
    ``subif-down``, the subinterface says ``port-down``, and the parent port is
    where the reason worth showing finally is.
    """
    return parents.resolve(
        name, itf.get("oper-down-reason"), details.get("oper-down-reason")
    )


def _subinterface_state(
    name: str,
    itf: Dict[str, Any],
    details: Dict[str, Any],
    parents: ParentReasons,
) -> str:
    """The oper-state of a subinterface as its network-instance sees it.

    SR Linux answers this differently depending on where it is asked. An IRB in a
    disabled mac-vrf reads ``up`` under ``/interface`` - the subinterface itself
    is fine - while the mac-vrf holding it reports it ``down`` with
    ``net-inst-down``. In a service listing the latter is the truthful one, so the
    network-instance's own view wins and ``/interface`` only fills in what the
    network-instance does not carry.

    A subinterface that is only down because an ethernet-segment holds its port
    in standby comes back as ``down/standby``: the port really is down, but it
    is doing what it was configured to do, and counting that as a fault is what
    left multi-homed services permanently degraded on whichever leaf was not
    forwarding.
    """
    state = _clean_state(itf.get("oper-state")) or _clean_state(details.get("oper-state"))
    if not state:
        admin = _clean_state(itf.get("admin-state")) or _clean_state(
            details.get("admin-state")
        )
        state = "down" if admin in ("disable", "disabled") else "up"
    return parents.state(
        state, name, itf.get("oper-down-reason"), details.get("oper-down-reason")
    )


def _subinterface_state_label(
    name: str,
    itf: Dict[str, Any],
    details: Dict[str, Any],
    parents: ParentReasons,
) -> str:
    """How to label a subinterface's state, with the reason when it is down.

    A bare ``[down]`` next to a service that is itself down invites the question
    this answers: ``[down: net-inst-down]`` says the subinterface is only down
    because the service is. ``down/standby`` needs no such reason - the word is
    the reason.
    """
    state = _subinterface_state(name, itf, details, parents)
    if state != "down":
        return state
    reason = _subinterface_down_reason(name, itf, details, parents)
    return f"{state}: {reason}" if reason else state


#: Member states that count towards a service being up, and towards it being
#: down. ``down/standby`` is deliberately in neither: see
#: :func:`_service_oper_state`.
_MEMBER_UP_STATES = frozenset({"up", "enable", "enabled", "active"})
_MEMBER_DOWN_STATES = frozenset({"down", "disable", "disabled"})


def _service_oper_state(ni_oper: str, member_states: List[str]) -> str:
    """The state of a service, refined by the subinterfaces placed in it.

    A network-instance reports itself up while some of its members are down,
    which is what ``degraded`` is for: the service exists on the node but is not
    carrying everything it was meant to.

    Members in standby are counted neither way. An ethernet-segment leaves the
    non-forwarding leaf's port in standby by design, so counting it as down
    would mark every multi-homed service degraded on exactly the node where
    nothing is wrong. A service whose members are all standby falls back to what
    the network-instance says about itself, which is still reachable over VXLAN.
    """
    if ni_oper == "down":
        return "down"
    counted = [state for state in member_states if state != STANDBY_STATE]
    if not counted:
        return ni_oper or "up"
    if all(state in _MEMBER_UP_STATES for state in counted):
        return "up"
    if all(state in _MEMBER_DOWN_STATES for state in counted):
        return "down"
    return "degraded"


def _peer_address_sort_key(address: str) -> Tuple[int, Any]:
    """Order peer addresses with IPv4 before IPv6, then numerically."""
    try:
        parsed = ipaddress.ip_address(address)
        return (1 if isinstance(parsed, ipaddress.IPv6Address) else 0, parsed)
    except ValueError:
        return (2, address)


def _bgp_peers_for_ni(ni: Dict[str, Any]) -> str:
    """BGP neighbors of one network-instance, as ``<local> -> <addr> UP|DOWN`` tokens."""
    protocols = ni.get("protocols")
    if not isinstance(protocols, dict):
        return "-"
    bgp = protocols.get("bgp")
    if not isinstance(bgp, dict):
        return "-"
    items: List[Tuple[str, str]] = []
    for neighbor in as_list(bgp.get("neighbor")):
        if not isinstance(neighbor, dict):
            continue
        addr = neighbor.get("peer-address")
        if not addr:
            continue
        transport = neighbor.get("transport") or {}
        local_addr = transport.get("local-address") or "-"
        up = _clean_state(neighbor.get("session-state")) == "established"
        items.append((addr, f"{local_addr} -> {addr} {'UP' if up else 'DOWN'}"))
    if not items:
        return "-"
    items.sort(key=lambda item: _peer_address_sort_key(item[0]))
    return ", ".join(label for _, label in items)


#: Where a node's ethernet-segments live. Read by the ES report, and by the
#: bridge-domain report for the segment a member's port sits on.
_ES_PATH = "/system/network-instance/protocols/evpn/ethernet-segments"
_ES_ENVELOPE = _ES_PATH.lstrip("/")


def _df_candidates(vrf: Dict[str, Any]) -> Tuple[Candidate, ...]:
    """The designated-forwarder candidates of one network-instance on a segment."""
    # Only the first bgp-instance elects a DF for the segment.
    instances = as_list(vrf.get("bgp-instance"))
    candidates = (instances[0] if instances else {}).get(
        "computed-designated-forwarder-candidates", {}
    )
    return tuple(
        Candidate(str(peer.get("address")), bool(peer.get("designated-forwarder")))
        for peer in as_list(candidates.get("designated-forwarder-candidate"))
    )


def _df_peers(vrf: Dict[str, Any]) -> str:
    """The DF candidates of one network-instance on a segment, as one string.

    The elected one is marked, because on a single-active segment it is the
    answer to why the other leaf is holding its port in standby.
    """
    return " ".join(
        f"{c.address}(DF)" if c.designated else c.address for c in _df_candidates(vrf)
    )


def _association_instances(vrf: Dict[str, Any]) -> List[str]:
    """The bgp-instance ids a segment is associated with in one network-instance.

    A gateway is shown as one tile per instance, and this is what keeps a
    segment on the side of it that actually carries it.
    """
    ids: List[str] = []
    for inst in as_list(vrf.get("bgp-instance")):
        if not isinstance(inst, dict):
            continue
        iid = inst.get("instance", inst.get("id"))
        if iid is None or str(iid) == "":
            continue
        if str(iid) not in ids:
            ids.append(str(iid))
    return ids


def _evi_values(container: Any) -> List[str]:
    """The ``evi`` list of a container, as strings.

    SR Linux models the EVI range of an ethernet-segment next-hop as a list
    keyed on ``start``, so the value is not under the list name itself.
    """
    if not isinstance(container, dict):
        return []
    values: List[str] = []
    for item in as_list(container.get("evi")):
        evi = item.get("start") if isinstance(item, dict) else item
        if evi is None or str(evi) == "":
            continue
        if str(evi) not in values:
            values.append(str(evi))
    return values


def _es_next_hops(es: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """The L3 next-hops of a virtual ethernet-segment, each with its EVIs.

    A virtual ES has no port to hang off: it tracks a next-hop address, and
    the EVI configured under that next-hop is the ip-vrf the segment serves.
    """
    pairs: List[Tuple[str, List[str]]] = []
    for nh in as_list(es.get("next-hop")):
        if not isinstance(nh, dict):
            continue
        address = str(nh.get("l3-next-hop") or "")
        if address:
            pairs.append((address, _evi_values(nh)))
    return pairs


def _compress_esi(esi: Any) -> str:
    """An ESI with its longest run of zero bytes written as ``..``.

    A 10-byte ESI is mostly padding: ``00:01:00:00:00:00:00:00:00:03`` says
    nothing that ``00:01:..:03`` does not, and at full width it crowds out
    everything else on the line.
    """
    text = str(esi or "")
    parts = text.split(":")
    runs: List[Tuple[int, int]] = []
    index = 0
    while index < len(parts):
        if parts[index] != "00":
            index += 1
            continue
        end = index
        while end < len(parts) and parts[end] == "00":
            end += 1
        runs.append((index, end - index))
        index = end
    if not runs:
        return text
    # The longest run, and the leftmost of them if several tie - the same rule
    # that decides where '::' goes in an IPv6 address.
    start, length = max(runs, key=lambda run: run[1])
    if length < 2:
        return text
    return ":".join(parts[:start] + [".."] + parts[start + length :])


class _EthernetSegments:
    """A node's ethernet-segments, indexed by port and by network-instance.

    A bridge domain finds its own by the port the segment is on. A virtual
    segment has no port, and its association state names the network-instance
    it serves outright, so a router finds its own by name.
    Read on first use. A node with no bridge domains and no EVPN router never
    asks, so a spine does not spend a Get on a tree it has nothing in.
    """

    def __init__(self, get: Any) -> None:
        self._get = get
        self._loaded = False
        self._by_port: Dict[str, Dict[str, Any]] = {}
        self._by_ni: Dict[str, List[Dict[str, Any]]] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        with _suppress_pygnmi_client_logging():
            try:
                resp = self._get(paths=[_ES_PATH], datatype="all")
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return
                raise
        segments = first_payload(resp).get(_ES_ENVELOPE) or {}
        for instance in as_list(segments.get("bgp-instance")):
            for es in as_list(instance.get("ethernet-segment")):
                if not isinstance(es, dict):
                    continue
                association = es.get("association") or {}
                segment = {
                    "name": str(es.get("name") or ""),
                    "esi": str(es.get("esi") or ""),
                    "mh-mode": _clean_state(es.get("multi-homing-mode")),
                    "oper": _clean_state(es.get("oper-state")),
                    # The DF election runs per network-instance, so a bridge
                    # domain only ever wants the candidates of its own.
                    "peers": {
                        str(vrf.get("name") or ""): _df_peers(vrf)
                        for vrf in as_list(association.get("network-instance"))
                        if isinstance(vrf, dict)
                    },
                }
                for itf in as_list(es.get("interface")):
                    port = itf.get("ethernet-interface") if isinstance(itf, dict) else None
                    if port:
                        self._by_port[str(port)] = segment
                next_hops = [nh for nh, _evis in _es_next_hops(es)]
                if not next_hops:
                    continue
                for vrf in as_list(association.get("network-instance")):
                    if not isinstance(vrf, dict):
                        continue
                    name = str(vrf.get("name") or "")
                    if name:
                        self._by_ni.setdefault(name, []).append(
                            {
                                "segment": segment,
                                "next-hop": " ".join(next_hops),
                                "instances": _association_instances(vrf),
                            }
                        )

    def of(self, port: str) -> Optional[Dict[str, Any]]:
        """The segment configured on *port*, if there is one."""
        self._load()
        return self._by_port.get(str(port))

    def for_ni(self, ni_name: str, instance: str = "") -> List[Dict[str, Any]]:
        """The virtual segments *ni_name* is associated with, each with its next-hop.

        *instance* narrows them to one bgp-instance of the network-instance,
        for a gateway that is shown as a tile per instance.
        """
        self._load()
        entries = self._by_ni.get(str(ni_name), [])
        if not instance:
            return entries
        return [
            e
            for e in entries
            if not e["instances"] or str(instance) in e["instances"]
        ]


def _es_label(segment: Dict[str, Any], ni_name: str, next_hop: str = "") -> str:
    """One ethernet-segment as the service it serves shows it.

    The peers are those of the service's own network-instance: the DF is
    elected per service, and the candidates of the other services on the same
    segment say nothing about this one.
    """
    fields = [f"ID: {_compress_esi(segment.get('esi'))}"]
    if segment.get("name"):
        fields.append(str(segment["name"]))
    if next_hop:
        fields.append(f"nh: {next_hop}")
    if segment.get("mh-mode"):
        fields.append(f"mode: {segment['mh-mode']}")
    if segment.get("oper"):
        fields.append(f"oper: {segment['oper']}")
    peers = (segment.get("peers") or {}).get(ni_name, "")
    if peers:
        fields.append(f"peers: {peers}")
    return ", ".join(fields)


def _host_address(pfx: str) -> str:
    """The address of an ``ip-prefix``, without its prefix length."""
    text = str(pfx).strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return text.split("/")[0]


def _family_addresses(ip_cfg: Any, version: int) -> List[str]:
    """Unicast host addresses of one IP family, skipping link-local and multicast."""
    if not isinstance(ip_cfg, dict):
        return []
    addrs: List[str] = []
    for addr in as_list(ip_cfg.get("address")):
        pfx = None
        if isinstance(addr, dict):
            pfx = addr.get("ip-prefix") or addr.get("prefix")
        elif isinstance(addr, str):
            pfx = addr
        if not pfx:
            continue
        host = _host_address(str(pfx))
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version != version or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            continue
        if host not in addrs:
            addrs.append(host)
    return addrs


def _system0_addresses(subitf_details: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """IPv4 and IPv6 addresses configured on system0, as display strings.

    system0 is the loopback the fabric uses as a node identifier; the services
    tree shows these next to the node name. Link-local IPv6 is omitted.
    """
    si: Dict[str, Any] = {}
    for key in ("system0.0", "system0"):
        if key in subitf_details:
            si = subitf_details[key]
            break
    else:
        for key, details in subitf_details.items():
            if str(key).startswith("system0"):
                si = details
                break
    ipv4 = ", ".join(_family_addresses(si.get("ipv4"), 4))
    ipv6 = ", ".join(_family_addresses(si.get("ipv6"), 6))
    return ipv4, ipv6


def _to_subnet(pfx: str) -> str:
    """The network prefix of an interface address, e.g. ``10.1.100.1/24`` → ``10.1.100.0/24``.

    Link-local and unspecified addresses are omitted; they are not service subnets.
    """
    text = str(pfx).strip()
    if not text:
        return ""
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return text
    if net.is_link_local or net.is_unspecified or net.is_multicast:
        return ""
    return str(net)


def _host_ip_if_host_route(pfx: Any) -> str:
    """The address of a host route (``/32`` or ``/128``), otherwise ``""``."""
    text = str(pfx or "").strip()
    if not text:
        return ""
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return ""
    if net.prefixlen != net.max_prefixlen:
        return ""
    if net.is_link_local or net.is_unspecified or net.is_multicast:
        return ""
    return str(net.network_address)


def _host_ips_from_payload(payload: Any) -> List[str]:
    """Host-route addresses nested anywhere in a gNMI route-table payload."""
    found: List[str] = []
    seen: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("ipv4-prefix", "ipv6-prefix"):
                if key in obj:
                    ip = _host_ip_if_host_route(obj[key])
                    if ip and ip not in seen:
                        seen.add(ip)
                        found.append(ip)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return found


def _split_host_ips(text: Any) -> List[str]:
    """Bare addresses from a comma- or space-separated cell."""
    if not text:
        return []
    ips: List[str] = []
    for part in str(text).replace(",", " ").split():
        host = part.split("/")[0].strip()
        if host:
            ips.append(host)
    return ips


def _underlay_hosts_from_instances(ni_list: List[Any]) -> str:
    """Host routes in the default instance's route-table, if that table is present."""
    for ni in as_list(ni_list):
        if isinstance(ni, dict) and str(ni.get("name", "")).lower() == "default":
            return ", ".join(_host_ips_from_payload(ni.get("route-table") or {}))
    return ""


def assign_underlay_sites(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Number underlay islands ``1``, ``2``, … from default-RIB reachability of system0.

    Two nodes share a site when each has the other's system0 address as a host
    route in network-instance ``default``. When the set also includes
    non-gateway nodes (leaves), edges between two Gateway nodes are ignored:
    DCGWs learn each other over the WAN and would otherwise glue every DC into
    one component. A service that is only Gateways (the WAN/DCI side) keeps
    those edges, so dcgw1..4 that all have each other's loopbacks stay one tile.

    Returns ``{}`` when there is only one island (or none), so a single fabric
    is not labelled ``(1)``.
    """
    by_node: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get("Node") or "unknown")
        by_node.setdefault(name, []).append(row)
    nodes = sorted(by_node)
    if len(nodes) < 2:
        return {}

    system: Dict[str, List[str]] = {}
    rib: Dict[str, set[str]] = {}
    gateway: set[str] = set()
    any_rib = False
    for name in nodes:
        sample = by_node[name][0]
        system[name] = _split_host_ips(sample.get("System IPv4")) + _split_host_ips(
            sample.get("System IPv6")
        )
        hosts = set(_split_host_ips(sample.get("Underlay Hosts")))
        rib[name] = hosts
        if hosts:
            any_rib = True
        if any(
            r.get("Gateway") in ("Y", True, "true")
            for r in by_node[name]
        ):
            gateway.add(name)

    if not any_rib:
        return {}

    skip_gateway_pairs = bool(nodes) and any(n not in gateway for n in nodes)

    def sees(observer: str, other: str) -> bool:
        other_ips = system[other]
        if not other_ips:
            return False
        table = rib[observer]
        return any(ip in table for ip in other_ips)

    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if skip_gateway_pairs and a in gateway and b in gateway:
                continue
            if sees(a, b) and sees(b, a):
                union(a, b)

    components: Dict[str, List[str]] = {}
    for name in nodes:
        components.setdefault(find(name), []).append(name)
    ordered = sorted(components.values(), key=lambda members: members[0])
    if len(ordered) <= 1:
        return {}
    return {
        name: str(index)
        for index, members in enumerate(ordered, start=1)
        for name in members
    }


def stamp_underlay_sites(rows: List[Dict[str, Any]]) -> bool:
    """Stamp ``Site`` on each row, clustered per Bridge Domain / Router.

    A DCGW appears in both the DC-side and WAN-side tiles; those tiles must be
    clustered on their own members so the WAN tile can stay one Router while
    the DC tile still splits by fabric.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("Bridge Domain") or row.get("Service Type") == "Bridge Domain":
            key = "bd:" + str(row.get("Bridge Domain") or row.get("Route Targets") or "")
        else:
            key = "rt:" + str(row.get("Router") or row.get("Route Targets") or "")
        groups.setdefault(key, []).append(row)
    any_site = False
    for group in groups.values():
        sites = assign_underlay_sites(group)
        for row in group:
            site = sites.get(str(row.get("Node") or ""), "")
            if site:
                any_site = True
            row["Site"] = site
    return any_site


def _format_route_target(target: Any) -> str:
    """A route-target as ``target:x:y``, or ``""`` if *target* is empty."""
    if not target:
        return ""
    text = str(target)
    return text if text.startswith("target:") else f"target:{text}"


def _instance_route_targets(inst: Dict[str, Any]) -> List[str]:
    """The import and export route-targets of one ``bgp-vpn`` instance."""
    if not isinstance(inst, dict):
        return []
    rt_cfg = inst.get("route-target", {})
    if not isinstance(rt_cfg, dict):
        return []
    rts: set[str] = set()
    for key in ("import-rt", "export-rt"):
        for item in as_list(rt_cfg.get(key)):
            target = item.get("target") if isinstance(item, dict) else item
            formatted = _format_route_target(target)
            if formatted:
                rts.add(formatted)
    return sorted(rts)


def _vpn_tile_groups(ni: Dict[str, Any], isolated_label: str) -> List[Dict[str, Any]]:
    """How a network-instance is shown as service tiles, one group per tile.

    A single enabled ``bgp-vpn`` instance (or none) is one tile, keyed by its
    route-target. Two or more enabled instances with route-targets are a
    Gateway: each instance becomes its own tile with that instance's RT so the
    DC and WAN sides of a DCGW stay distinct.
    """
    enabled: List[Dict[str, Any]] = []
    for i, inst in enumerate(
        as_list(ni.get("protocols", {}).get("bgp-vpn", {}).get("bgp-instance")),
        start=1,
    ):
        if not isinstance(inst, dict):
            continue
        if _clean_state(inst.get("admin-state")) in ("disable", "disabled"):
            continue
        iid = inst.get("id")
        if iid is None:
            iid = inst.get("index")
        enabled.append(
            {
                "id": str(iid) if iid is not None and str(iid) != "" else str(i),
                "rts": _instance_route_targets(inst),
            }
        )

    with_rts = [e for e in enabled if e["rts"]]
    if len(with_rts) < 2:
        rts = sorted({rt for e in enabled for rt in e["rts"]})
        return [
            {
                "primary": rts[0] if rts else isolated_label,
                "rts": rts,
                "id": with_rts[0]["id"] if len(with_rts) == 1 else "",
                "gateway": False,
            }
        ]

    primary_counts: Dict[str, int] = {}
    for e in with_rts:
        primary = e["rts"][0]
        primary_counts[primary] = primary_counts.get(primary, 0) + 1

    groups = []
    for e in with_rts:
        primary = e["rts"][0]
        if primary_counts[primary] > 1:
            primary = f"{primary} (bgp-instance {e['id']})"
        groups.append(
            {
                "primary": primary,
                "rts": e["rts"],
                "id": e["id"],
                "gateway": True,
            }
        )
    return groups


def _irb_addresses(family: Dict[str, Any]) -> Tuple[IrbAddress, ...]:
    """The addresses under an irb's ``ipv4`` or ``ipv6`` container."""
    return tuple(
        IrbAddress(
            prefix=str(addr.get("ip-prefix") or ""),
            # ``primary`` is a presence container: it comes back as ``[None]``.
            primary=addr.get("primary") is not None,
            anycast_gw=bool(addr.get("anycast-gw")),
        )
        for addr in as_list(family.get("address"))
        if isinstance(addr, dict)
    )


def _host_route_rules(neighbor_cfg: Dict[str, Any]) -> Tuple[HostRouteRule, ...]:
    """The ``host-route/populate`` entries of an irb's ARP or ND settings."""
    return tuple(
        HostRouteRule(
            route_type=str(rule.get("route-type") or ""),
            datapath_programming=bool(rule.get("datapath-programming")),
        )
        for rule in as_list((neighbor_cfg.get("host-route") or {}).get("populate"))
        if isinstance(rule, dict)
    )


def _evpn_advertise_entries(neighbor_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        entry
        for entry in as_list((neighbor_cfg.get("evpn") or {}).get("advertise"))
        if isinstance(entry, dict)
    ]


def _evpn_advertised(neighbor_cfg: Dict[str, Any]) -> Tuple[str, ...]:
    """The entry origins an irb advertises into EVPN."""
    return tuple(str(entry.get("route-type") or "") for entry in _evpn_advertise_entries(neighbor_cfg))


def _interface_less_routing(neighbor_cfg: Dict[str, Any]) -> bool:
    return any("interface-less-routing" in entry for entry in _evpn_advertise_entries(neighbor_cfg))


#: Each vxlan-interface's VNI.
VXLAN_VNI_PATH = "/tunnel-interface[name=*]/vxlan-interface[index=*]/ingress"
#: The VTEPs each vxlan-interface sends unicast to.
VXLAN_DEST_PATH = "/tunnel-interface[name=*]/vxlan-interface[index=*]/bridge-table/unicast-destinations/destination"
#: The VTEPs of each ethernet-segment destination, per vxlan-interface.
ES_DEST_PATH = "/tunnel-interface[name=*]/vxlan-interface[index=*]/bridge-table/unicast-destinations/es-destination[esi=*]/vtep"


class Layer2Mixin:
    """Mixin providing Layer2 related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def _has_feature(self, feature: str) -> bool:
        """Whether the device advertises *feature* under ``/system/features``.

        Reports gate on this so a node that does no bridging or EVPN renders an
        empty table instead of failing on a path it does not implement.
        """
        payload = first_payload(self.get(paths=["/system/features"], datatype="state"))
        return feature in (payload.get("system/features") or [])

    def _subinterface_details(self) -> Dict[str, Dict[str, Any]]:
        """Map ``<interface>.<index>`` to that subinterface's state.

        The network-instance tree names the subinterfaces placed in a service but
        carries none of their detail, so the addresses, anycast-gw flag and VLAN
        encapsulation that the service reports show come from this second Get.
        """
        details: Dict[str, Dict[str, Any]] = {}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=["/interface[name=*]/subinterface"], datatype="all"
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return details
                raise
        for itf in as_list(first_payload(resp).get("interface")):
            if not isinstance(itf, dict):
                continue
            itf_name = itf.get("name", "")
            for si in as_list(itf.get("subinterface")):
                if not isinstance(si, dict):
                    continue
                si_name = si.get("name", "")
                if not si_name:
                    si_name = f"{itf_name}.{si.get('index', '')}"
                elif str(si_name).isdigit():
                    si_name = f"{itf_name}.{si_name}"
                details[si_name] = si
        return details

    def _default_underlay_hosts(self, ni_list: Optional[List[Any]] = None) -> str:
        """Host routes (``/32``, ``/128``) in network-instance ``default``.

        These are the loopbacks this node can see in the underlay; services use
        them to split a Route-Target that spans more than one DC.
        """
        from_ni = _underlay_hosts_from_instances(ni_list or [])
        if from_ni:
            return from_ni
        hosts: List[str] = []
        seen: set[str] = set()
        for path in (
            "/network-instance[name=default]/route-table/ipv4-unicast/route/ipv4-prefix",
            "/network-instance[name=default]/route-table/ipv6-unicast/route/ipv6-prefix",
        ):
            with _suppress_pygnmi_client_logging():
                try:
                    resp = self.get(paths=[path], datatype="state")
                except BaseException as e:
                    if _gnmi_path_missing(e) or isinstance(e, KeyError):
                        continue
                    raise
            payload: Any = first_payload(resp)
            if not payload:
                payload = resp
            for ip in _host_ips_from_payload(payload):
                if ip not in seen:
                    seen.add(ip)
                    hosts.append(ip)
        return ", ".join(hosts)

    def get_lldp_sum(self, interface: Optional[str] = "*") -> Dict[str, Any]:
        path = f"/system/lldp/interface[name={interface}]/neighbor"
        resp = self.get(paths=[path], datatype="state")
        lldp = first_payload(resp).get("system/lldp") or {}
        interfaces = [
            LldpInterface(
                name=str(itf.get("name") or ""),
                neighbors=tuple(
                    LldpNeighbor(
                        system_name=str(nbr.get("system-name") or ""),
                        port_id=str(nbr.get("port-id") or ""),
                        port_description=str(nbr.get("port-description") or ""),
                    )
                    for nbr in as_list(itf.get("neighbor"))
                    if isinstance(nbr, dict)
                ),
            )
            for itf in as_list(lldp.get("interface"))
            if isinstance(itf, dict)
        ]
        return {"lldp_nbrs": interfaces}

    def get_mac_table(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        path = f"/network-instance[name={network_instance}]/bridge-table/mac-table/mac"
        if not self._has_feature("bridged"):
            return {"mac_table": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path], datatype="state")
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"mac_table": []}
                raise
        tables = [
            BridgeTable(
                ni=str(ni.get("name", "")),
                entries=tuple(
                    MacEntry.read(mac.get("address"), mac.get("destination"), mac.get("type"))
                    for mac in as_list(
                        ni.get("bridge-table", {}).get("mac-table", {}).get("mac")
                    )
                    if isinstance(mac, dict)
                ),
            )
            for ni in as_list(first_payload(resp).get("network-instance"))
            if isinstance(ni, dict)
        ]
        return {"mac_table": tables}

    def get_es(self) -> Dict[str, Any]:
        if not self._has_feature("evpn"):
            return {"es": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[_ES_PATH], datatype="all")
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"es": []}
                raise
        segments = first_payload(resp).get(_ES_ENVELOPE, {})
        records = []
        for bgp_inst in as_list(segments.get("bgp-instance")):
            for es in as_list(bgp_inst.get("ethernet-segment")):
                if not isinstance(es, dict):
                    continue
                records.append(
                    EthernetSegment(
                        name=str(es.get("name", "")),
                        esi=str(es.get("esi", "")),
                        type=str(es.get("type", "")),
                        mh_mode=str(es.get("multi-homing-mode", "")),
                        oper=str(es.get("oper-state", "")),
                        interfaces=tuple(
                            str(i.get("ethernet-interface", ""))
                            for i in as_list(es.get("interface"))
                            if isinstance(i, dict)
                        ),
                        # A virtual segment has no port: it tracks a next-hop,
                        # and the EVI under that next-hop is the ip-vrf it serves.
                        next_hops=tuple(
                            NextHop(address, tuple(evis)) for address, evis in _es_next_hops(es)
                        ),
                        associations=tuple(
                            Association(str(vrf.get("name", "")), _df_candidates(vrf))
                            for vrf in as_list(
                                (es.get("association") or {}).get("network-instance")
                            )
                            if isinstance(vrf, dict)
                        ),
                    )
                )
        return {"es": records}

    def get_es_dest(self) -> Dict[str, Any]:
        # The VTEPs of each destination alone: an es-destination also carries
        # counters and a MAC table, which on a fabric with a thousand
        # bridge-domains is tens of thousands of leaves per node per sample.
        path = ES_DEST_PATH
        if not self._has_feature("bridged"):
            return {"es_dest": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path], datatype="state")
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"es_dest": []}
                raise
        records = []
        for tun in as_list(first_payload(resp).get("tunnel-interface")):
            if not isinstance(tun, dict):
                continue
            tunnel = str(tun.get("name") or "")
            destinations = []
            for vxlan in as_list(tun.get("vxlan-interface")):
                if not isinstance(vxlan, dict):
                    continue
                unicast = (vxlan.get("bridge-table") or {}).get("unicast-destinations") or {}
                for es_dest in as_list(unicast.get("es-destination")):
                    if not isinstance(es_dest, dict):
                        continue
                    destinations.append(
                        EsDestination(
                            esi=str(es_dest.get("esi") or ""),
                            overlay=f"{tunnel}.{vxlan.get('index', '')}",
                            vteps=tuple(
                                str(vtep.get("address") or "")
                                for vtep in as_list(es_dest.get("vtep"))
                                if isinstance(vtep, dict)
                            ),
                        )
                    )
            records.append(EsDestinations(tunnel=tunnel, destinations=tuple(destinations)))
        return {"es_dest": records}

    def get_vxlan(self) -> Dict[str, Any]:
        # 'vxlan' rather than 'bridged': a 7220 IXR-H does bridging but no
        # VXLAN, and has no vxlan-interface under network-instance to ask for.
        if not self._has_feature("vxlan"):
            return {"vxlan": []}

        # vxlan-interface -> the network-instance it is bound to, from the
        # vxlan-interface lists alone (see get_irb for why not the subtree).
        ni_resp = self.get(paths=["/network-instance[name=*]/vxlan-interface"], datatype="config")
        ni_map: Dict[str, str] = {}
        for ni in as_list(first_payload(ni_resp).get("network-instance")):
            for vxlan_itf in as_list(ni.get("vxlan-interface")):
                ni_map[vxlan_itf["name"]] = ni["name"]

        # The VNI and the destinations alone, not the vxlan-interface subtree:
        # that also holds the counters and MAC tables of every ES destination,
        # tens of thousands of leaves per node per sample on a large fabric -
        # more than a server streaming a dozen nodes keeps up with.
        # ``all`` for the VNI: up to 25.3 the state datastore also carried the
        # configured ``ingress/vni``, and from 25.10 it does not.
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[VXLAN_VNI_PATH], datatype="all")
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"vxlan": []}
                raise
            try:
                dest_resp = self.get(paths=[VXLAN_DEST_PATH], datatype="all")
            except BaseException as e:
                if not _gnmi_path_missing(e):
                    raise
                dest_resp = []
        destinations: Dict[Tuple[str, str], List[Any]] = {}
        for tun in as_list(first_payload(dest_resp).get("tunnel-interface")):
            if not isinstance(tun, dict):
                continue
            for vxlan in as_list(tun.get("vxlan-interface")):
                if isinstance(vxlan, dict):
                    unicast = (vxlan.get("bridge-table") or {}).get("unicast-destinations") or {}
                    destinations[(str(tun.get("name")), str(vxlan.get("index")))] = as_list(unicast.get("destination"))
        records = []
        for tun in as_list(first_payload(resp).get("tunnel-interface")):
            if not isinstance(tun, dict):
                continue
            for vxlan in as_list(tun.get("vxlan-interface")):
                if not isinstance(vxlan, dict):
                    continue
                name = f"{tun['name']}.{vxlan['index']}"
                records.append(
                    VxlanInterface(
                        name=name,
                        ni=ni_map.get(name, ""),
                        vni=as_int((vxlan.get("ingress") or {}).get("vni")),
                        destinations=tuple(
                            VxlanDestination(str(d.get("vtep", "")), as_int(d.get("vni")))
                            for d in destinations.get((str(tun["name"]), str(vxlan["index"])), [])
                            if isinstance(d, dict)
                        ),
                    )
                )
        return {"vxlan": records}

    def get_irb(self) -> Dict[str, Any]:
        # The instances an irb is in, from the interface lists alone: the whole
        # network-instance subtree carries the BGP RIBs, which is far more
        # than a subscription serving this Get should have to stream. And
        # their irb entries alone, matched by the node: on a fabric of a
        # thousand bridge-domains the lists hold a thousand other bindings.
        bound = instances_by_interface(self.get, "irb*")
        resp = self.get(paths=["/interface[name=irb*]/subinterface"], datatype="all")

        records = []
        for itf in as_list(first_payload(resp).get("interface")):
            if not isinstance(itf, dict):
                continue
            for subitf in as_list(itf.get("subinterface")):
                if not isinstance(subitf, dict):
                    continue
                name = f"{itf.get('name', '')}.{subitf.get('index', '')}"
                ipv4 = subitf.get("ipv4") or {}
                ipv6 = subitf.get("ipv6") or {}
                arp = ipv4.get("arp") or {}
                nd = ipv6.get("neighbor-discovery") or {}
                anycast = subitf.get("anycast-gw") or {}
                records.append(
                    IrbInterface(
                        name=name,
                        nis=bound.get(name, ()),
                        ipv4=_irb_addresses(ipv4),
                        ipv6=_irb_addresses(ipv6),
                        anycast_gw=bool(anycast),
                        anycast_gw_mac=str(anycast.get("anycast-gw-mac") or ""),
                        virtual_router_id=as_int(anycast.get("virtual-router-id")),
                        arp=IrbArp(
                            proxy=bool(arp.get("proxy-arp")),
                            learn_unsolicited=bool(arp.get("learn-unsolicited")),
                            host_routes=_host_route_rules(arp),
                            evpn_advertise=_evpn_advertised(arp),
                            interface_less_routing=_interface_less_routing(arp),
                        ),
                        nd=IrbNd(
                            proxy=bool(nd.get("proxy-nd")),
                            learn_unsolicited=str(nd.get("learn-unsolicited") or ""),
                            host_routes=_host_route_rules(nd),
                            evpn_advertise=_evpn_advertised(nd),
                            interface_less_routing=_interface_less_routing(nd),
                        ),
                    )
                )
        return {"irb": records}

    def get_bridge_domains(self, nw_instance: str = "*") -> Dict[str, Any]:
        """Return EVPN Bridge Domains (mac-vrf) grouped by Route-Target."""
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "datatype": "all",
        }
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path_spec["path"]], datatype=path_spec["datatype"])
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"bridge_domains": []}
                raise

        if not first_payload(resp):
            return {"bridge_domains": []}

        ni_list = as_list(first_payload(resp).get("network-instance"))

        # for IRB IPv4/IPv6, anycast-gw, VLAN encapsulation, and system0
        subitf_details = self._subinterface_details()
        system_ipv4, system_ipv6 = _system0_addresses(subitf_details)
        underlay_hosts = self._default_underlay_hosts(ni_list)
        # A member that says 'port-down' does not say what is wrong with the
        # port; the parent's own reason is what does, and what tells a standby
        # ethernet-segment apart from a broken link.
        parents = ParentReasons(self.get)
        # A multi-homed bridge domain is only half a story without the segment
        # its members hang off: the mode and the DF decide which of the leaves
        # forwards for it, and which one stands by.
        segments = _EthernetSegments(self.get)

        # Build mapping of irb_subinterface -> associated ip-vrf / L3 network instances
        irb_to_ip_vrf: Dict[str, List[str]] = {}
        for ni in ni_list:
            if isinstance(ni, dict) and ni.get("type") != "mac-vrf":
                vrf_name = ni.get("name", "")
                for itf in ni.get("interface", []):
                    if isinstance(itf, dict) and itf.get("name"):
                        itf_name = itf["name"]
                        if itf_name.startswith("irb"):
                            if itf_name not in irb_to_ip_vrf:
                                irb_to_ip_vrf[itf_name] = []
                            irb_to_ip_vrf[itf_name].append(vrf_name)

        results = []

        def _extract_vlan_encap(itf_name: str, itf_dict: Dict[str, Any]) -> str:
            si = subitf_details.get(itf_name, {})
            for obj in (si, itf_dict):
                if not isinstance(obj, dict):
                    continue
                vlan = obj.get("vlan", {})
                if isinstance(vlan, dict):
                    encap = vlan.get("encap", {})
                    if isinstance(encap, dict):
                        if "untagged" in encap:
                            return "untagged"
                        single = encap.get("single-tagged", {})
                        if isinstance(single, dict) and "vlan-id" in single:
                            return str(single["vlan-id"])
                        if "vlan-id" in encap:
                            return str(encap["vlan-id"])
                    if "vlan-id" in vlan:
                        return str(vlan["vlan-id"])
                if "vlan-id" in obj:
                    return str(obj["vlan-id"])
                if "vlan" in obj and isinstance(obj["vlan"], (int, str)):
                    return str(obj["vlan"])

            if itf_name.endswith(".0"):
                return "untagged"
            parts = itf_name.split(".")
            if len(parts) > 1 and parts[-1].isdigit():
                return parts[-1]
            return "untagged"

        def _format_irb_item_and_subnets(itf_name: str, itf_dict: Dict[str, Any], assoc_vrfs: List[str]) -> Tuple[str, List[str], str]:
            si = subitf_details.get(itf_name, {})
            st = _subinterface_state_label(itf_name, itf_dict, si, parents)
            ips: List[str] = []
            is_anycast = False

            def _check_ip_block(ip_cfg: Any) -> None:
                nonlocal is_anycast
                if not isinstance(ip_cfg, dict):
                    return
                if ip_cfg.get("anycast-gw") is True or str(ip_cfg.get("anycast-gw")).lower() == "true":
                    is_anycast = True
                for addr in ip_cfg.get("address", []):
                    if isinstance(addr, dict):
                        pfx = addr.get("ip-prefix")
                        if pfx:
                            ips.append(str(pfx))
                        if addr.get("anycast-gw") is True or str(addr.get("anycast-gw")).lower() == "true":
                            is_anycast = True
                    elif isinstance(addr, str):
                        ips.append(addr)

            _check_ip_block(si.get("ipv4"))
            _check_ip_block(si.get("ipv6"))
            _check_ip_block(itf_dict.get("ipv4"))
            _check_ip_block(itf_dict.get("ipv6"))

            for obj in (si, itf_dict):
                if obj.get("anycast-gw") is True or str(obj.get("anycast-gw")).lower() == "true":
                    is_anycast = True
                if obj.get("anycast-gateway") is True or str(obj.get("anycast-gateway")).lower() == "true":
                    is_anycast = True

            subnets = [s for pfx in ips if (s := _to_subnet(pfx))]

            ip_part = f": {', '.join(ips)}" if ips else ""
            gw_part = f" (anycast-gw: {'true' if is_anycast else 'false'})"
            vrf_part = f" -> {', '.join(assoc_vrfs)}" if assoc_vrfs else ""

            return f"{itf_name} [{st}]{ip_part}{gw_part}{vrf_part}", subnets, st

        for ni in ni_list:
            if not isinstance(ni, dict):
                continue
            ni_name = ni.get("name", "")
            ni_type = ni.get("type", "")
            if ni_type != "mac-vrf":
                continue
            oper_state = ni.get("oper-state", "unknown")

            irb_subitfs = []
            bridge_subitfs = []
            all_subnets = []
            subitf_states = []
            for i in ni.get("interface", []):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    details = subitf_details.get(name, {})
                    # The bare state feeds the service's aggregate oper-state
                    # below, which counts up against down; the label is only for
                    # display and can carry a reason with it.
                    subitf_states.append(
                        _subinterface_state(name, i, details, parents)
                    )
                    if name.startswith("irb"):
                        assoc_vrfs = irb_to_ip_vrf.get(name, [])
                        irb_item_str, subnets, _st = _format_irb_item_and_subnets(name, i, assoc_vrfs)
                        irb_subitfs.append(irb_item_str)
                        # One subnet per entry: an IRB addressed more than once in
                        # the same subnet - a gateway address alongside an
                        # anycast one - is still a single subnet of the service.
                        for subnet in subnets:
                            if subnet not in all_subnets:
                                all_subnets.append(subnet)
                    else:
                        vlan_info = _extract_vlan_encap(name, i)
                        label = _subinterface_state_label(name, i, details, parents)
                        entry = f"{name} [{label}] (VLAN: {vlan_info})"
                        # On the member's own entry rather than in a list of its
                        # own: a bridge domain with several multi-homed members
                        # is unreadable if the reader has to pair them up.
                        es = segments.of(parent_interface(name))
                        if es:
                            entry += f" -> ES: {_es_label(es, ni_name)}"
                        bridge_subitfs.append(entry)

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            effective_oper = _service_oper_state(_clean_state(oper_state), subitf_states)

            for group in _vpn_tile_groups(ni, f"mac-vrf:{ni_name}"):
                rt_display = ", ".join(group["rts"]) if group["rts"] else f"mac-vrf:{ni_name}"
                results.append(
                    {
                        "Bridge Domain": group["primary"],
                        "MAC-VRF": ni_name,
                        "Oper State": effective_oper,
                        "Route Targets": rt_display,
                        "Subnets": ", ".join(all_subnets) if all_subnets else "",
                        "IRB Interface": ", ".join(irb_subitfs) if irb_subitfs else "-",
                        # Semicolons: an entry carrying a segment has commas of
                        # its own.
                        "Sub-Interfaces": "; ".join(bridge_subitfs) or "-",
                        "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
                        "Gateway": "Y" if group["gateway"] else "",
                        "BGP Instance": group["id"] if group["gateway"] else "",
                        "System IPv4": system_ipv4,
                        "System IPv6": system_ipv6,
                        "Underlay Hosts": underlay_hosts,
                    }
                )

        return {"bridge_domains": results}

    def get_routers(self, nw_instance: str = "*") -> Dict[str, Any]:
        """Return EVPN Routers (ip-vrf) grouped by Route-Target."""
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "datatype": "all",
        }
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path_spec["path"]], datatype=path_spec["datatype"])
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"routers": []}
                raise

        if not first_payload(resp):
            return {"routers": []}

        ni_list = as_list(first_payload(resp).get("network-instance"))

        # for the IPv4/IPv6 addresses of the interfaces placed in each router,
        # and system0 shown next to the node name
        subitf_details = self._subinterface_details()
        system_ipv4, system_ipv6 = _system0_addresses(subitf_details)
        underlay_hosts = self._default_underlay_hosts(ni_list)
        parents = ParentReasons(self.get)
        # A virtual ethernet-segment has no port to be listed under, so a
        # router is the only place it can be shown: it names an EVI, and the
        # ip-vrf advertising that EVI is the one it multi-homes.
        segments = _EthernetSegments(self.get)

        # Build mapping of irb_subinterface -> mac-vrf network instance name
        irb_to_mac_vrf: Dict[str, str] = {}
        for ni in ni_list:
            if isinstance(ni, dict) and ni.get("type") == "mac-vrf":
                mac_vrf_name = ni.get("name", "")
                for itf in ni.get("interface", []):
                    if isinstance(itf, dict) and itf.get("name"):
                        itf_name = itf["name"]
                        if itf_name.startswith("irb"):
                            irb_to_mac_vrf[itf_name] = mac_vrf_name

        def _get_ip_addresses(itf_name: str, itf_dict: Dict[str, Any]) -> List[str]:
            si = subitf_details.get(itf_name, {})
            ips: List[str] = []

            def _check_ip_block(ip_cfg: Any) -> None:
                if not isinstance(ip_cfg, dict):
                    return
                for addr in as_list(ip_cfg.get("address")):
                    if isinstance(addr, dict):
                        pfx = addr.get("ip-prefix")
                        if pfx:
                            ips.append(str(pfx))
                    elif isinstance(addr, str):
                        ips.append(addr)

            _check_ip_block(si.get("ipv4"))
            _check_ip_block(si.get("ipv6"))
            _check_ip_block(itf_dict.get("ipv4"))
            _check_ip_block(itf_dict.get("ipv6"))
            return ips

        results = []

        for ni in ni_list:
            if not isinstance(ni, dict):
                continue
            ni_name = ni.get("name", "")
            ni_type = ni.get("type", "")
            if ni_type != "ip-vrf" or ni_name.lower() == "mgmt":
                continue
            oper_state = ni.get("oper-state", "unknown")

            mac_vrfs_items = []
            routed_itfs_items = []
            subitf_states = []
            irb_subnets: List[str] = []

            for i in as_list(ni.get("interface")):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    details = subitf_details.get(name, {})
                    subitf_states.append(
                        _subinterface_state(name, i, details, parents)
                    )
                    st = _subinterface_state_label(name, i, details, parents)
                    ips = _get_ip_addresses(name, i)
                    ip_str = ", ".join(ips) if ips else ""

                    if name.startswith("irb"):
                        mac_name = irb_to_mac_vrf.get(name, "unknown")
                        if ip_str:
                            mac_vrfs_items.append(f"{mac_name} ({name} [{st}]: {ip_str})")
                        else:
                            mac_vrfs_items.append(f"{mac_name} ({name} [{st}])")
                        for pfx in ips:
                            subnet = _to_subnet(pfx)
                            if subnet and subnet not in irb_subnets:
                                irb_subnets.append(subnet)
                    else:
                        if ip_str:
                            routed_itfs_items.append(f"{name} [{st}] ({ip_str})")
                        else:
                            routed_itfs_items.append(f"{name} [{st}]")

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            effective_oper = _service_oper_state(_clean_state(oper_state), subitf_states)
            ni_evis = bgp_evpn_evis(ni)

            for group in _vpn_tile_groups(ni, "none (isolated)"):
                isolated = not group["rts"]
                rt_display = ", ".join(group["rts"]) if group["rts"] else "none (isolated)"
                primary = (
                    f"none (isolated) - {ni_name}" if isolated else group["primary"]
                )
                # A tile is one bgp-vpn instance of the ip-vrf, and the bgp-evpn
                # instance of the same id is the one that carries its EVI. A
                # single-instance ip-vrf has no id on its tile, and then every
                # EVI the instance advertises belongs to it.
                if group["id"] in ni_evis:
                    evis = [ni_evis[group["id"]]]
                elif group["id"]:
                    evis = []
                else:
                    evis = list(ni_evis.values())
                # Only an EVPN service can carry a virtual segment, so an
                # ip-vrf that runs no bgp-evpn is not worth a Get.
                ves_items = [
                    _es_label(entry["segment"], ni_name, entry["next-hop"])
                    for entry in (
                        segments.for_ni(ni_name, group["id"]) if ni_evis else []
                    )
                ]
                results.append(
                    {
                        "Router": primary,
                        "IP-VRF": ni_name,
                        "Oper State": effective_oper,
                        "Route Targets": rt_display,
                        "EVI": ", ".join(evis),
                        "MAC-VRFs": ", ".join(mac_vrfs_items) if mac_vrfs_items else "-",
                        "Routed Interfaces": ", ".join(routed_itfs_items) if routed_itfs_items else "-",
                        "BGP Peers": _bgp_peers_for_ni(ni),
                        # Virtual segments only: a router has no access port for
                        # a segment to be on, so there is nothing else here.
                        "Virtual ES": "; ".join(ves_items) if ves_items else "-",
                        "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
                        "Subnets": ", ".join(irb_subnets) if irb_subnets else "",
                        "Gateway": "Y" if group["gateway"] else "",
                        "BGP Instance": group["id"] if group["gateway"] else "",
                        "System IPv4": system_ipv4,
                        "System IPv6": system_ipv6,
                        "Underlay Hosts": underlay_hosts,
                    }
                )

        return {"routers": results}

    def get_services(self) -> Dict[str, Any]:
        bds = self.get_bridge_domains().get("bridge_domains", [])
        for bd in bds:
            bd["Service Type"] = "Bridge Domain"
        rts = self.get_routers().get("routers", [])
        for rt in rts:
            rt["Service Type"] = "Router"
        return {"services": bds + rts}

