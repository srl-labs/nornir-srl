# Routing related methods extracted from srlinux.py
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..records import (
    BgpPeers,
    BgpRib,
    BgpRoute,
    Egress,
    Family,
    Neighbor,
    Route,
    RouteNextHop,
    RouteTable,
    RouteTableSummary,
    StaticNextHop,
    StaticRoute,
    StaticRouteTable,
    Tunnel,
    TunnelNextHop,
    TunnelTable,
    as_int,
)
from .helpers import as_list, first_payload, lpm, model_version, version_bucket

logger = logging.getLogger(__name__)

#: The address families the peers report knows, by the name the newer BGP
#: model gives them under ``afi-safi``.
_BGP_FAMILIES = (
    "evpn",
    "ipv4-unicast",
    "ipv6-unicast",
    "l3vpn-ipv4-unicast",
    "l3vpn-ipv6-unicast",
)


def _family(name: str, afi: Dict[str, Any]) -> Family:
    """One address family of a session, as the neighbour state describes it."""
    return Family(
        name=name,
        enabled=afi.get("admin-state") == "enable",
        oper=str(afi.get("oper-state") or ""),
        received=as_int(afi.get("received-routes")) or 0,
        active=as_int(afi.get("active-routes")) or 0,
        sent=as_int(afi.get("sent-routes")) or 0,
    )


# CLI / API aliases (e.g. ``-r l3vpn-v4``) → YANG ``afi-safi-name`` used in paths.
BGP_RIB_ROUTE_FAM_ALIASES: Dict[str, str] = {
    "l3vpn-v4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-v6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}

#: What the BGP RIB report calls a family, and what the model calls it.
_BGP_RIB_FAMILY = {
    "evpn": "evpn",
    "ipv4": "ipv4-unicast",
    "ipv6": "ipv6-unicast",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}

#: The container holding each EVPN route type, in the singular the newer model
#: uses; the older one pluralises it.
_EVPN_ROUTE_CONTAINERS = {
    "1": "ethernet-ad-route",
    "2": "mac-ip-route",
    "3": "imet-route",
    "4": "ethernet-segment-route",
    "5": "ip-prefix-route",
}


def _rib_entries(ni: Dict[str, Any], family: str, steps: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """The route entries of *family* in one network-instance's bgp-rib.

    Whichever way the release lays them out: the family directly under
    ``bgp-rib`` or under an ``afi-safi`` list entry, and the last container
    named in the singular or the plural.
    """
    bgp_rib = ni.get("bgp-rib") or {}
    tables = [afi[family] for afi in as_list(bgp_rib.get("afi-safi")) if isinstance(afi, dict) and family in afi]
    if family in bgp_rib:
        tables.append(bgp_rib[family])
    entries: List[Dict[str, Any]] = []
    for table in tables:
        node: Any = table
        for step in steps[:-1]:
            node = node.get(step) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        last = steps[-1]
        entries.extend(
            entry for entry in as_list(node.get(last, node.get(last + "s"))) if isinstance(entry, dict)
        )
    return entries


def _ext_community(communities: List[str], prefix: str) -> Tuple[str, ...]:
    """The values of the extended communities of one kind, ``target:`` say."""
    return tuple(c.split(prefix, 1)[1] for c in communities if prefix in c)


def _domain_ids(obj: Any) -> List[str]:
    """Every ``domain-id`` in a D-PATH attribute, in order."""
    found: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "domain-id":
                found.extend(str(v) for v in as_list(value))
            else:
                found.extend(_domain_ids(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_domain_ids(item))
    return found


def _label(route: Dict[str, Any], key: str) -> Optional[int]:
    label = route.get(key)
    return as_int(label.get("value")) if isinstance(label, dict) else None


def _bgp_route(route: Dict[str, Any]) -> BgpRoute:
    """A route merged with its attr-set, as a record."""
    communities = route.get("communities") or {}
    ext = [str(c) for c in as_list(communities.get("ext-community"))]
    prefix = route.get("prefix") or route.get("ipv4-prefix") or route.get("ipv6-prefix")
    # From 24.3 a MAC/IP route carries its VNI as the first of two labels, an
    # RT-5 as a single label; before that, as a vni leaf.
    label1, label2 = _label(route, "label1"), _label(route, "label2")
    vni = label1 if "label1" in route else _label(route, "label") if "label" in route else as_int(route.get("vni"))
    return BgpRoute(
        neighbor=str(route.get("neighbor") or ""),
        used=bool(route.get("used-route")),
        valid=bool(route.get("valid-route")),
        best=bool(route.get("best-route")),
        rd=str(route.get("route-distinguisher") or ""),
        prefix=str(prefix or route.get("ip-prefix") or ""),
        esi=str(route.get("esi") or ""),
        tag=as_int(route.get("ethernet-tag-id")),
        mac=str(route.get("mac-address") or ""),
        ip=str(route.get("ip-address") or ""),
        gateway=str(route.get("gateway-ip") or ""),
        vni=vni,
        label1=label1,
        label2=label2,
        next_hop=str(route.get("next-hop") or ""),
        origin=str(route.get("origin") or ""),
        local_pref=as_int(route.get("local-pref")),
        med=as_int(route.get("med")),
        as_path=tuple(
            member
            for segment in as_list((route.get("as-path") or {}).get("segment"))
            if isinstance(segment, dict)
            for member in (as_int(m) for m in as_list(segment.get("member")))
            if member is not None
        ),
        route_targets=_ext_community(ext, "target:"),
        esi_labels=_ext_community(ext, "esi-label:"),
        soo=_ext_community(ext, "origin:"),
        tunnel_encap=_ext_community(ext, "bgp-tunnel-encap:"),
        communities=tuple(str(c) for c in as_list(communities.get("community"))),
        large_communities=tuple(str(c) for c in as_list(communities.get("large-community"))),
        ext_communities=tuple(ext),
        domain_path=tuple(_domain_ids(route.get("domain-path") or {})),
        tie_break=str(route.get("tie-break-reason") or ""),
        internal_tags=tuple(str(t) for t in as_list(route.get("internal-tags"))),
        neighbor_as=as_int(route.get("neighbor-as")),
    )


#: How far :meth:`RoutingMixin.get_rib` follows a chain of indirect next-hops
#: looking for the egress interface. Recursive resolution is a handful of hops
#: deep at most; the bound is what stops a next-hop table that points at itself.
_MAX_NH_RESOLVE_DEPTH = 8

_pygnmi_suppress_lock = threading.Lock()
_pygnmi_suppress_depth = 0
_pygnmi_suppress_saved: Tuple[List[logging.Handler], int, bool] | None = None


@contextmanager
def _suppress_pygnmi_client_logging() -> Iterator[None]:
    """Silence pygnmi's pre-raise CRITICAL log for expected invalid-path Get failures.

    pygnmi attaches a StreamHandler to ``pygnmi.client`` with a low handler level,
    so raising the logger level alone is not always enough to suppress output.

    fcli queries many hosts concurrently; without a refcount, one task could
    restore handlers while another host's L3VPN Get was still running, letting
    GRPC noise leak back onto stderr/stdout.
    """
    global _pygnmi_suppress_depth, _pygnmi_suppress_saved
    log = logging.getLogger("pygnmi.client")
    with _pygnmi_suppress_lock:
        _pygnmi_suppress_depth += 1
        if _pygnmi_suppress_depth == 1:
            _pygnmi_suppress_saved = (
                list(log.handlers),
                log.level,
                log.propagate,
            )
            log.handlers.clear()
            log.setLevel(logging.CRITICAL + 1)
            log.propagate = False
    try:
        yield
    finally:
        with _pygnmi_suppress_lock:
            _pygnmi_suppress_depth -= 1
            if _pygnmi_suppress_depth == 0 and _pygnmi_suppress_saved is not None:
                handlers, prev_level, prev_propagate = _pygnmi_suppress_saved
                _pygnmi_suppress_saved = None
                log.setLevel(prev_level)
                log.propagate = prev_propagate
                for h in handlers:
                    log.addHandler(h)


def _gnmi_path_missing(exc: BaseException) -> bool:
    """True when a gNMI Get failed because the path does not exist on the device."""
    text = str(exc).lower()
    # pygnmi embeds server text in gNMIException.args[0]; SR Linux uses this for unknown path elems.
    if "path not valid" in text and (
        "unknown element" in text or "l3vpn" in text or "unknown path" in text
    ):
        return True

    try:
        import grpc

        missing = (
            grpc.StatusCode.NOT_FOUND,
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.UNIMPLEMENTED,
        )
    except ImportError:  # pragma: no cover
        return False

    chain: List[Optional[BaseException]] = [exc]
    if exc.__cause__ is not None:
        chain.append(exc.__cause__)
    if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
        chain.append(exc.__context__)
    # pygnmi wraps grpc errors in gNMIException(..., orig_exc=...) without raise-from chaining.
    orig = getattr(exc, "orig_exc", None)
    if isinstance(orig, BaseException):
        chain.append(orig)

    def _code_match(obj: Any) -> bool:
        code_fn = getattr(obj, "code", None)
        if not callable(code_fn):
            return False
        try:
            return bool(code_fn() in missing)
        except Exception:
            return False

    for cur in chain:
        if cur is not None and _code_match(cur):
            return True
    if orig is not None and not isinstance(orig, BaseException) and _code_match(orig):
        return True
    return False


#: A Get envelope that names the network-instance: what a path with every key
#: of the instance spelled out is answered under.
_KEYED_INSTANCE = re.compile(r"^/?network-instance\[name=([^\]]+)\](?:/(.*))?$")
_STEP = re.compile(r"([^/\[]+)((?:\[[^\]]*\])*)")


def _instances(payloads: Sequence[Any]) -> Iterator[Dict[str, Any]]:
    """The network-instances in Get payloads, whichever envelope they came in.

    A wildcard instance is answered under ``network-instance``, as the list;
    a named one - ``/network-instance[name=default]/route-table/ipv4-unicast`` -
    under that path itself, holding only what is below it. The latter is
    rebuilt into one entry of the former, so a getter reads either the same.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if key == "network-instance":
                yield from (ni for ni in as_list(value) if isinstance(ni, dict))
                continue
            match = _KEYED_INSTANCE.match(str(key))
            if not match or not isinstance(value, dict):
                continue
            ni: Dict[str, Any] = {"name": match.group(1)}
            node: Dict[str, Any] = ni
            steps = _STEP.findall(match.group(2) or "")
            for i, (name, keys) in enumerate(steps):
                last = i == len(steps) - 1
                if keys:
                    entry = dict(k.split("=", 1) for k in re.findall(r"\[([^\]]*)\]", keys) if "=" in k)
                    if last:
                        entry.update(value)
                    node[name] = [entry]
                    node = entry
                elif last:
                    node[name] = value
                else:
                    node = node.setdefault(name, {})
            if not steps:
                ni.update(value)
            yield ni


def _afi_routes(ni: Any, afi: str) -> List[Dict[str, Any]]:
    """The route entries of one network-instance's *afi* table."""
    if not isinstance(ni, dict):
        return []
    table = (ni.get("route-table") or {}).get(afi) or {}
    return [r for r in as_list(table.get("route")) if isinstance(r, dict)]


def _route_tables(
    afi: str,
    route_payloads: Sequence[Dict[str, Any]],
    nhs: Sequence[Dict[str, Any]],
    nhgroups: Sequence[Dict[str, Any]],
    lpm_address: Optional[str] = None,
) -> List[RouteTable]:
    """Route tables, with each route's next-hops resolved as far as *nhs* and *nhgroups* go.

    Each argument is the payloads of one or more Gets, all merged.
    """
    prefix_key = "ipv4-prefix" if afi == "ipv4-unicast" else "ipv6-prefix"

    # The next-hop, next-hop-group and route tables are three separate Gets,
    # so they can disagree: a route can name a group, or a group a next-hop,
    # that the neighbouring Get did not (or no longer) return. Resolving
    # defensively degrades one row instead of failing the whole report.
    nh_mapping: Dict[str, Dict[str, Any]] = {}
    for ni in _instances(nhs):
        # One instance can arrive in several payloads, one per Get.
        tmp_map: Dict[str, Any] = nh_mapping.setdefault(ni.get("name"), {})
        for nh in as_list(ni.get("route-table", {}).get("next-hop")):
            entry: Dict[str, Any] = {
                "ip-address": nh.get("ip-address"),
                "type": nh.get("type"),
                "subinterface": nh.get("subinterface"),
            }
            indirect = nh.get("indirect", {})
            resolving_tunnel = indirect.get(
                "resolving-tunnel", nh.get("resolving-tunnel")
            )
            resolving_route = indirect.get(
                "resolving-route", nh.get("resolving-route")
            )
            # A next-hop already resolved onto a tunnel carries it directly,
            # under its own key and naming the type ``type`` rather than
            # ``tunnel-type``; an indirect one names the tunnel it recurses
            # on instead. Either way the VTEP is what the route egresses to.
            tunnel = resolving_tunnel or nh.get("tunnel") or {}
            if tunnel:
                entry["tunnel"] = Egress(
                    "tunnel",
                    str(tunnel.get("ip-prefix") or ""),
                    tunnel=str(tunnel.get("tunnel-type") or tunnel.get("type") or ""),
                )
            if resolving_route:
                entry["resolving-route"] = resolving_route.get("ip-prefix")
                # The resolving route names its own next-hop-group, which is
                # what lets an indirect next-hop be followed to a real port.
                entry["resolving-nhg"] = resolving_route.get("next-hop-group")
            tmp_map[nh.get("index")] = entry

    nhgroup_mapping: Dict[str, Dict[str, List[Any]]] = {}
    for ni in _instances(nhgroups):
        ni_name = ni.get("name")
        nh_map: Dict[str, List[Any]] = nhgroup_mapping.setdefault(ni_name, {})
        for nhgroup in as_list(ni.get("route-table", {}).get("next-hop-group")):
            nh_map[nhgroup.get("index")] = [
                nh_mapping.get(ni_name, {}).get(nh.get("next-hop"), {})
                for nh in as_list(nhgroup.get("next-hop"))
            ]

    def egress(
        ni_name: str, nh: Dict[str, Any], seen: Tuple[str, ...] = ()
    ) -> List[Egress]:
        """Where a next-hop leaves the node.

        A next-hop resolved down to a port or a tunnel says so itself. An
        indirect one only names the route it resolves through, so the port
        is one level further down, in the next-hop-group of *that* route -
        which is why a BGP route in an ip-vrf, whose next-hop is the BGP
        peer rather than a connected address, is followed rather than
        reported as the prefix it recurses on. The prefix is still what is
        shown when the chain cannot be walked to an interface.
        """
        if nh.get("subinterface"):
            return [Egress("interface", str(nh["subinterface"]))]
        if nh.get("tunnel"):
            return [nh["tunnel"]]
        via = nh.get("resolving-nhg")
        if via and via not in seen and len(seen) < _MAX_NH_RESOLVE_DEPTH:
            hops = [
                hop
                for onward in nhgroup_mapping.get(ni_name, {}).get(via, [])
                for hop in egress(ni_name, onward, seen + (via,))
            ]
            if hops:
                return hops
        if nh.get("resolving-route"):
            return [Egress("route", str(nh["resolving-route"]))]
        return []

    tables: List[RouteTable] = []
    for ni in _instances(route_payloads):
        ni_name = str(ni.get("name", ""))
        afi_table = ni.get("route-table", {}).get(afi) or {}
        if not afi_table:
            continue
        raw_routes = [r for r in as_list(afi_table.get("route")) if isinstance(r, dict)]
        if lpm_address:
            # Narrowing keeps the one prefix the address falls into, or
            # nothing: the instance then has no route to it. Nothing is
            # rewritten in place, so a payload a cache still holds on
            # behalf of the renders that want the table in full is intact.
            lpm_prefix = lpm(
                lpm_address, [r[prefix_key] for r in raw_routes if prefix_key in r]
            )
            if not lpm_prefix:
                continue
            raw_routes = [r for r in raw_routes if r.get(prefix_key) == lpm_prefix]

        routes = []
        for route in raw_routes:
            orig_ni = str(route.get("origin-network-instance") or ni_name)
            leaked = orig_ni != ni_name
            next_hops: List[RouteNextHop] = []
            if "next-hop-group" in route:
                nhg_ni = str(route.get("next-hop-group-network-instance") or orig_ni)
                for nh in nhgroup_mapping.get(nhg_ni, {}).get(route["next-hop-group"], []):
                    next_hops.append(
                        RouteNextHop(
                            address=str(nh.get("ip-address") or ""),
                            type=str(nh.get("type") or ""),
                            resolving_route=str(nh.get("resolving-route") or ""),
                            egress=tuple(
                                # A leaked route leaves through a port of
                                # the instance it came from.
                                replace(hop, ni=orig_ni)
                                if leaked and hop.kind == "interface"
                                else hop
                                for hop in egress(nhg_ni, nh)
                            ),
                        )
                    )
            routes.append(
                Route(
                    prefix=str(route.get(prefix_key) or ""),
                    type=str(route.get("route-type") or ""),
                    active=bool(route.get("active")),
                    metric=as_int(route.get("metric")),
                    preference=as_int(route.get("preference")),
                    leaked_from=orig_ni if leaked else "",
                    next_hops=tuple(next_hops),
                )
            )
        tables.append(RouteTable(ni=ni_name, routes=tuple(routes)))
    return tables


class RoutingMixin:
    """Mixin providing routing and BGP related getters."""

    capabilities: Optional[Dict[str, Any]]

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_bgp_rib(
        self,
        route_fam: str,
        route_type: Optional[str] = "2",
        network_instance: str = "*",
        detail: bool = False,
        rib: str = "in",
    ) -> Dict[str, Any]:
        """The BGP RIB of one family, as records carrying every path attribute.

        *detail* is accepted for the callers that used to ask for the extra
        attributes: a record carries all of them, and the table declared for
        the report decides which to show.

        *rib* ``out`` reads the rib-out-post instead: the routes sent to each
        peer, keyed by the peer they went to, with the attributes they were
        sent with and none of the used/valid/best flags a received route has.
        """
        if rib not in ("in", "out"):
            raise ValueError(f"Invalid rib {rib}: 'in' or 'out'")
        del detail
        mod_version = model_version(
            self.capabilities, "bgp-rib", "urn:nokia.com:srlinux:bgp:rib-bgp"
        )

        route_fam = BGP_RIB_ROUTE_FAM_ALIASES.get(route_fam.lower(), route_fam)

        BGP_EVPN_VERSION_MAP = {
            1: ("2021-", "2022-", "2023-", "2024-03", "2024-07"),
            2: ("20"),
        }
        BGP_IP_VERSION_MAP = {
            1: ("2021-", "2022-"),
            2: ("2023-03",),
            3: ("20"),
        }
        evpn_path_version = version_bucket(BGP_EVPN_VERSION_MAP, mod_version)
        ip_path_version = version_bucket(BGP_IP_VERSION_MAP, mod_version)

        if route_fam not in _BGP_RIB_FAMILY:
            raise ValueError(f"Invalid route family {route_fam}")
        family = _BGP_RIB_FAMILY[route_fam]
        if route_type and route_type not in _EVPN_ROUTE_CONTAINERS:
            raise ValueError(f"Invalid route type {route_type}")

        # The rib-in-post of an EVPN route type, or the local-rib of an IP
        # family; or for *rib* ``out``, the rib-out-post of either. Up to 24.7 the family sits directly under bgp-rib and the
        # containers are named in the plural; from 24.10 it sits under an
        # afi-safi list entry, and the containers are singular.
        under_afi_safi = f"/bgp-rib/afi-safi[afi-safi-name={family}]/{family}"
        if family == "evpn":
            container = _EVPN_ROUTE_CONTAINERS[str(route_type)]
            post = "rib-out-post" if rib == "out" else "rib-in-post"
            path = (
                f"/network-instance[name={network_instance}]"
                + (under_afi_safi if evpn_path_version == 2 else f"/bgp-rib/{family}")
                + f"/rib-in-out/{post}/{container}{'' if evpn_path_version == 2 else 's'}"
            )
            steps = ("rib-in-out", post, container)
        elif rib == "out":
            path = (
                f"/network-instance[name={network_instance}]"
                + (under_afi_safi if ip_path_version > 1 else f"/bgp-rib/{family}")
                + f"/rib-in-out/rib-out-post/route{'s' if ip_path_version < 3 else ''}"
            )
            steps = ("rib-in-out", "rib-out-post", "route")
        else:
            path = (
                f"/network-instance[name={network_instance}]"
                + (under_afi_safi if ip_path_version > 1 else f"/bgp-rib/{family}")
                + f"/local-rib/route{'s' if ip_path_version < 3 else ''}"
            )
            steps = ("local-rib", "route")

        attribs: Dict[str, Dict[str, Any]] = {}
        resp = self.get(
            paths=[f"/network-instance[name={network_instance}]/bgp-rib/attr-sets/attr-set"],
            datatype="state",
        )
        for ni in as_list(first_payload(resp).get("network-instance")):
            ni_name = ni.get("name")
            if ni_name is None:
                continue
            attribs.setdefault(ni_name, {})
            for attr_set in ni.get("bgp-rib", {}).get("attr-sets", {}).get("attr-set", []):
                attribs[ni_name][attr_set.get("index")] = attr_set

        # Leaves / platforms without IP-VPN have no l3vpn-* RIB path, and a
        # release that keeps no rib-out-post has none for what was sent.
        if family in ("l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast") or rib == "out":
            with _suppress_pygnmi_client_logging():
                try:
                    resp = self.get(paths=[path], datatype="state")
                except BaseException as e:
                    if _gnmi_path_missing(e):
                        logger.debug(
                            "%s: no %s RIB on this node, reporting it empty: %s",
                            getattr(self, "hostname", "?"),
                            route_fam,
                            e,
                        )
                        return {"bgp_rib": []}
                    raise
        else:
            resp = self.get(paths=[path], datatype="state")

        ribs = []
        for ni in as_list(first_payload(resp).get("network-instance")):
            if not isinstance(ni, dict):
                continue
            ni_name = str(ni.get("name", ""))
            # A network-instance can appear in the RIB without a matching
            # attr-set, e.g. when the two Gets straddle a routing change.
            attr_sets = attribs.get(ni_name, {})
            ribs.append(
                BgpRib(
                    ni=ni_name,
                    family=family,
                    route_type=str(route_type) if family == "evpn" else "",
                    routes=tuple(
                        _bgp_route({**route, **attr_sets.get(route.get("attr-id"), {})})
                        for route in _rib_entries(ni, family, steps)
                    ),
                )
            )
        return {"bgp_rib": ribs}

    def get_sum_bgp(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        mod_version = model_version(
            self.capabilities,
            "urn:srl_nokia/bgp:srl_nokia-bgp",
            "urn:nokia.com:srlinux:bgp:bgp:srl_nokia-bgp",
            exact=True,
        )
        BGP_VERSION_MAP = {1: ("2021-", "2022-"), 2: ("2023-3", "20")}
        our_version = version_bucket(BGP_VERSION_MAP, mod_version)

        def neighbor(peer: Dict[str, Any]) -> Neighbor:
            families: List[Family] = []
            if our_version == 1:
                # The older model keeps each family in a container of its own
                # and the local AS in a list.
                local_as = (as_list(peer.get("local-as")) or [{}])[0].get("as-number")
                for name in ("evpn", "ipv4-unicast"):
                    if isinstance(peer.get(name), dict):
                        families.append(_family(name, peer[name]))
            else:
                local_as = (peer.get("local-as") or {}).get("as-number")
                for afi in as_list(peer.get("afi-safi")):
                    if isinstance(afi, dict) and afi.get("afi-safi-name") in _BGP_FAMILIES:
                        families.append(_family(str(afi["afi-safi-name"]), afi))
            detection = peer.get("failure-detection") or {}
            transport = peer.get("transport") or {}
            return Neighbor(
                peer=str(peer.get("peer-address", "")),
                state=str(peer.get("session-state") or ""),
                peer_as=as_int(peer.get("peer-as")),
                local_as=as_int(local_as),
                local_address=str(transport.get("local-address") or ""),
                local_port=as_int(transport.get("local-port")),
                group=str(peer.get("peer-group") or ""),
                dynamic=bool(peer.get("dynamic-neighbor", False)),
                bfd=bool(detection.get("enable-bfd", False)),
                fast_failover=bool(detection.get("fast-failover", False)),
                import_policies=tuple(str(p) for p in as_list(peer.get("import-policy"))),
                export_policies=tuple(str(p) for p in as_list(peer.get("export-policy"))),
                families=tuple(families),
            )

        resp = self.get(
            paths=[f"/network-instance[name={network_instance}]/protocols/bgp/neighbor"],
            datatype="all",
        )
        records = [
            BgpPeers(
                ni=str(ni.get("name", "")),
                neighbors=tuple(
                    neighbor(peer)
                    for peer in as_list(((ni.get("protocols") or {}).get("bgp") or {}).get("neighbor"))
                    if isinstance(peer, dict)
                ),
            )
            for ni in as_list(first_payload(resp).get("network-instance"))
            if isinstance(ni, dict)
        ]
        return {"bgp_peers": records}

    def get_rib(
        self,
        afi: str,
        network_instance: Optional[str] = "*",
        lpm_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        nhgroups = self.get(
            paths=[
                f"/network-instance[name={network_instance}]/route-table/next-hop-group[index=*]"
            ],
            datatype="state",
        )
        nhs = self.get(
            paths=[
                f"/network-instance[name={network_instance}]/route-table/next-hop[index=*]"
            ],
            datatype="state",
        )
        resp = self.get(
            paths=[f"/network-instance[name={network_instance}]/route-table/{afi}"],
            datatype="state",
        )
        return {
            "ip_rib": _route_tables(
                afi, [first_payload(resp)], [first_payload(nhs)], [first_payload(nhgroups)], lpm_address
            )
        }

    def get_routes(self, afi: str, prefixes: Sequence[str]) -> Dict[str, Any]:
        """Only *prefixes*, in whichever network-instances have them.

        What a reading asks for when it follows a few prefixes without holding
        the route tables they are in: a Get per prefix, then only the
        next-hop-groups and next-hops those routes name, rather than every
        table of the node. A next-hop that resolves through another route is
        shown as that route, not followed on to a port: that would take the
        resolving route's groups as well.
        """
        prefix_key = "ipv4-prefix" if afi == "ipv4-unicast" else "ipv6-prefix"
        if not prefixes:
            return {"ip_rib": []}
        routes = self.get(
            paths=[
                f"/network-instance[name=*]/route-table/{afi}/route[{prefix_key}={prefix}]"
                for prefix in prefixes
            ],
            datatype="state",
        )
        # Asked for under every instance: the indexes are unique on the node,
        # and the route does not always say which instance its group is in.
        groups = sorted(
            {
                str(route["next-hop-group"])
                for ni in _instances(routes)
                for route in _afi_routes(ni, afi)
                if "next-hop-group" in route
            }
        )
        nhgroups = self.get(
            paths=[f"/network-instance[name=*]/route-table/next-hop-group[index={index}]" for index in groups],
            datatype="state",
        ) if groups else []
        hops = sorted(
            {
                str(member["next-hop"])
                for ni in _instances(nhgroups)
                for nhgroup in as_list((ni.get("route-table") or {}).get("next-hop-group"))
                for member in as_list(nhgroup.get("next-hop"))
                if isinstance(member, dict) and member.get("next-hop") is not None
            }
        )
        nhs = self.get(
            paths=[f"/network-instance[name=*]/route-table/next-hop[index={index}]" for index in hops],
            datatype="state",
        ) if hops else []
        return {"ip_rib": _route_tables(afi, routes, nhs, nhgroups)}

    def get_rib_summary(self) -> Dict[str, Any]:
        """How many active routes each network-instance holds, per address family.

        The one leaf, not the ``statistics`` container around it: a reading
        streams it for every instance of every node, and it is all a reading
        compares.
        """
        summaries: List[RouteTableSummary] = []
        for afi, family in (("ipv4-unicast", "ipv4"), ("ipv6-unicast", "ipv6")):
            resp = self.get(
                paths=[f"/network-instance[name=*]/route-table/{afi}/statistics/active-routes"],
                datatype="state",
            )
            for ni in as_list(first_payload(resp).get("network-instance")):
                if not isinstance(ni, dict):
                    continue
                stats = (((ni.get("route-table") or {}).get(afi) or {}).get("statistics")) or {}
                if not stats:
                    continue
                summaries.append(
                    RouteTableSummary(
                        ni=str(ni.get("name", "")),
                        family=family,
                        active=as_int(stats.get("active-routes")) or 0,
                    )
                )
        return {"rib_summary": summaries}

    def get_tunnel_table(self, network_instance: str = "*") -> Dict[str, Any]:
        """Get the IP tunnel-table (LDP, SR-ISIS, RSVP, VXLAN, ...).

        Resolves each tunnel's next-hop-group to the egress subinterface,
        next-hop IP and pushed MPLS label-stack, mirroring the next-hop
        resolution used by :meth:`get_rib`.
        """
        # Build next-hop and next-hop-group lookups (per network-instance).
        nhs = self.get(
            paths=[
                f"/network-instance[name={network_instance}]/route-table/next-hop[index=*]"
            ],
            datatype="state",
        )
        nhgroups = self.get(
            paths=[
                f"/network-instance[name={network_instance}]/route-table/next-hop-group[index=*]"
            ],
            datatype="state",
        )

        nh_mapping: Dict[str, Dict[str, TunnelNextHop]] = {}
        for ni in as_list(first_payload(nhs).get("network-instance")):
            if not isinstance(ni, dict):
                continue
            resolved: Dict[str, TunnelNextHop] = {}
            for nh in as_list((ni.get("route-table") or {}).get("next-hop")):
                if not isinstance(nh, dict):
                    continue
                label_stack = (nh.get("mpls-encapsulation") or {}).get(
                    "pushed-mpls-label-stack"
                ) or (nh.get("mpls") or {}).get("pushed-mpls-label-stack")
                resolved[str(nh.get("index"))] = TunnelNextHop(
                    address=str(nh.get("ip-address") or ""),
                    subinterface=str(nh.get("subinterface") or ""),
                    type=str(nh.get("type") or ""),
                    labels=tuple(str(label) for label in as_list(label_stack)),
                )
            nh_mapping[str(ni.get("name", ""))] = resolved

        nhgroup_mapping: Dict[str, Dict[str, Tuple[TunnelNextHop, ...]]] = {}
        for ni in as_list(first_payload(nhgroups).get("network-instance")):
            if not isinstance(ni, dict):
                continue
            ni_name = str(ni.get("name", ""))
            groups: Dict[str, Tuple[TunnelNextHop, ...]] = {}
            for nhgroup in as_list((ni.get("route-table") or {}).get("next-hop-group")):
                if not isinstance(nhgroup, dict):
                    continue
                groups[str(nhgroup.get("index"))] = tuple(
                    nh_mapping.get(ni_name, {}).get(str(member.get("next-hop")), TunnelNextHop())
                    for member in as_list(nhgroup.get("next-hop"))
                    if isinstance(member, dict)
                )
            nhgroup_mapping[ni_name] = groups

        resp = self.get(
            paths=[f"/network-instance[name={network_instance}]/tunnel-table"],
            datatype="state",
        )

        tables: List[TunnelTable] = []
        for ni in as_list(first_payload(resp).get("network-instance")):
            if not isinstance(ni, dict):
                continue
            ni_name = str(ni.get("name", ""))
            tunnel_table = ni.get("tunnel-table") or {}
            tunnels: List[Tunnel] = []
            for afi in ("ipv4", "ipv6"):
                prefix_key = f"{afi}-prefix"
                for tunnel in as_list((tunnel_table.get(afi) or {}).get("tunnel")):
                    if not isinstance(tunnel, dict):
                        continue
                    tunnels.append(
                        Tunnel(
                            prefix=str(tunnel.get(prefix_key) or ""),
                            type=str(tunnel.get("type") or ""),
                            owner=str(tunnel.get("owner") or ""),
                            preference=as_int(tunnel.get("preference")),
                            metric=as_int(tunnel.get("metric")),
                            next_hops=nhgroup_mapping.get(ni_name, {}).get(
                                str(tunnel.get("next-hop-group")), ()
                            ),
                        )
                    )
            # An instance without tunnels has no table to show, rather than
            # an empty one: most instances have none.
            if tunnels:
                tables.append(TunnelTable(ni=ni_name, tunnels=tuple(tunnels)))

        return {"tunnel_table": tables}

    def get_routing_policies(self) -> Dict[str, Any]:
        """
        Get routing policies from /routing-policy
        """
        paths = ["/routing-policy"]
        resp = self.get(paths=paths, datatype="config")

        policies = []
        for item in resp:
            if "routing-policy" in item:
                policies.append(item["routing-policy"])

        return {"routing_pol": policies}

    def get_static_routes(self, network_instance: str = "*") -> Dict[str, Any]:
        """
        Get static routes from /network-instance/static-routes.
        """
        paths = [
            f"/network-instance[name={network_instance}]/static-routes",
            f"/network-instance[name={network_instance}]/next-hop-groups",
        ]
        resp = self.get(paths=paths, datatype="all")

        # The two paths answer as separate notifications, each with its own
        # network-instance list, so both are read per instance before the
        # routes are resolved against their groups.
        groups: Dict[str, Dict[str, Tuple[StaticNextHop, ...]]] = {}
        routes: Dict[str, List[Dict[str, Any]]] = {}
        for item in resp:
            if not isinstance(item, dict):
                continue
            for ni in as_list(item.get("network-instance")):
                if not isinstance(ni, dict):
                    continue
                ni_name = str(ni.get("name", ""))
                for group in as_list((ni.get("next-hop-groups") or {}).get("group")):
                    if not isinstance(group, dict):
                        continue
                    groups.setdefault(ni_name, {})[str(group.get("name"))] = tuple(
                        StaticNextHop(
                            address=str(nh.get("ip-address")),
                            resolve=bool(nh.get("resolve", False)),
                        )
                        for nh in as_list(group.get("nexthop"))
                        if isinstance(nh, dict) and nh.get("ip-address")
                    )
                if "static-routes" in ni:
                    routes.setdefault(ni_name, []).extend(
                        route
                        for route in as_list((ni.get("static-routes") or {}).get("route"))
                        if isinstance(route, dict)
                    )

        tables = [
            StaticRouteTable(
                ni=ni_name,
                routes=tuple(
                    StaticRoute(
                        prefix=str(route.get("prefix") or ""),
                        admin=str(route.get("admin-state") or ""),
                        installed=(
                            bool(route["installed"]) if route.get("installed") is not None else None
                        ),
                        metric=as_int(route.get("metric")),
                        preference=as_int(route.get("preference")),
                        next_hop_group=str(route.get("next-hop-group") or ""),
                        next_hops=groups.get(ni_name, {}).get(str(route.get("next-hop-group")), ()),
                    )
                    for route in ni_routes
                ),
            )
            for ni_name, ni_routes in routes.items()
            if ni_routes
        ]
        return {"static_routes": tables}
