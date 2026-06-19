# Routing related methods extracted from srlinux.py
from __future__ import annotations

import copy
import logging
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import jmespath

from .helpers import lpm

# CLI / API aliases (e.g. ``-r l3vpn-v4``) → YANG ``afi-safi-name`` used in paths.
BGP_RIB_ROUTE_FAM_ALIASES: Dict[str, str] = {
    "l3vpn-v4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-v6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}

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
    ) -> Dict[str, Any]:
        BGP_RIB_MOD = "bgp-rib"
        BGP_RIB_MOD2 = "urn:nokia.com:srlinux:bgp:rib-bgp"
        if self.capabilities is not None:
            mod_version = [
                m
                for m in self.capabilities.get("supported_models", [])
                if BGP_RIB_MOD in m.get("name") or BGP_RIB_MOD2 in m.get("name")
            ][0].get("version")
        else:
            raise Exception("Cannot get gNMI capabilities")

        route_fam = BGP_RIB_ROUTE_FAM_ALIASES.get(route_fam.lower(), route_fam)

        BGP_EVPN_VERSION_MAP = {
            1: ("2021-", "2022-", "2023-", "2024-03", "2024-07"),
            2: ("20"),
        }
        BGP_EVPN_ROUTE_TYPE_MAP = {
            1: ("2021-", "2022-", "2023-", "2024-03", "2024-07"),
            2: ("20"),
        }
        BGP_IP_VERSION_MAP = {
            1: ("2021-", "2022-"),
            2: ("2023-03",),
            3: ("20"),
        }
        ROUTE_FAMILY = {
            "evpn": "evpn",
            "ipv4": "ipv4-unicast",
            "ipv6": "ipv6-unicast",
            "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
            "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
        }
        ROUTE_TYPE_VERSIONS = {
            1: {
                "1": "ethernet-ad-routes",
                "2": "mac-ip-routes",
                "3": "imet-routes",
                "4": "ethernet-segment-routes",
                "5": "ip-prefix-routes",
            },
            2: {
                "1": "ethernet-ad-route",
                "2": "mac-ip-route",
                "3": "imet-route",
                "4": "ethernet-segment-route",
                "5": "ip-prefix-route",
            },
        }

        def augment_routes(d, attribs):  # augment routes with attributes
            if isinstance(d, list):
                return [augment_routes(x, attribs) for x in d]
            elif isinstance(d, dict):
                if "attr-id" in d:
                    d.update(attribs.get(d["attr-id"], {}))
                    d["_r_state"] = (
                        ("u" if d["used-route"] else "")
                        + ("*" if d["valid-route"] else "")
                        + (">" if d["best-route"] else "")
                    )
                    if "label1" in d:  # from SRL 24.3 onwards for mac/ip routes
                        d["vni"] = d["label1"].get("value", "-")
                        d["_label1"] = d["label1"].get("value", "-")
                    elif "label" in d:  # for SRL 24.3 onwards
                        d["vni"] = d["label"].get("value", "-")
                        d["_label1"] = "-"
                    else:
                        d["vni"] = d.get("vni", "-")
                        d["_label1"] = "-"
                    if "label2" in d:
                        d["_label2"] = d["label2"].get("value", "-")
                    else:
                        d["_label2"] = "-"
                    d["_rt"] = ", ".join(
                        [
                            x_comm.split("target:")[1]
                            for x_comm in d.get("communities", {}).get(
                                "ext-community", []
                            )
                            if "target:" in x_comm
                        ]
                    )
                    d["_as_path"] = str(
                        attribs.get(d["attr-id"], {})
                        .get("as-path", {})
                        .get("segment", [{}])[0]
                        .get("member", [])
                    )
                    d["_esi_lbl"] = ",".join(
                        [
                            str(x_comm.split("esi-label:")[1])
                            .replace("Single-Active", "S-A")
                            .replace("All-Active", "A-A")
                            for x_comm in d.get("communities", {}).get(
                                "ext-community", []
                            )
                            if "esi-label:" in x_comm
                        ]
                    )
                    ext_comms = d.get("communities", {}).get("ext-community", [])
                    # Site-of-Origin (SoO) carried as origin: ext-community
                    d["_soo"] = ", ".join(
                        [c.split("origin:")[1] for c in ext_comms if "origin:" in c]
                    )
                    # BGP tunnel-encap ext-community (e.g. VXLAN / MPLS)
                    d["_tunnel_encap"] = ", ".join(
                        [
                            c.split("bgp-tunnel-encap:")[1]
                            for c in ext_comms
                            if "bgp-tunnel-encap:" in c
                        ]
                    )
                    # Standard + large communities (RT/SoO/encap live in ext-comm)
                    std_comms = d.get("communities", {}).get("community", []) or []
                    large_comms = (
                        d.get("communities", {}).get("large-community", []) or []
                    )
                    d["_communities"] = ", ".join(
                        [str(c) for c in list(std_comms) + list(large_comms)]
                    )
                    # D-PATH (BGP domain-path) - collect all domain-ids in order
                    dpath_ids: List[str] = []

                    def _collect_domain_ids(obj: Any) -> None:
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k == "domain-id":
                                    if isinstance(v, list):
                                        dpath_ids.extend(str(x) for x in v)
                                    else:
                                        dpath_ids.append(str(v))
                                else:
                                    _collect_domain_ids(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                _collect_domain_ids(item)

                    _collect_domain_ids(d.get("domain-path", {}))
                    d["_dpath"] = " ".join(dpath_ids)
                    return d
                else:
                    return {k: augment_routes(v, attribs) for k, v in d.items()}
            else:
                return d

        evpn_path_version = [
            k
            for k, v in sorted(BGP_EVPN_VERSION_MAP.items(), key=lambda item: item[0])
            if len([ver for ver in v if mod_version.startswith(ver)]) > 0
        ][0]
        evpn_route_type_version = [
            k
            for k, v in sorted(
                BGP_EVPN_ROUTE_TYPE_MAP.items(), key=lambda item: item[0]
            )
            if len([ver for ver in v if mod_version.startswith(ver)]) > 0
        ][0]
        ip_path_version = [
            k
            for k, v in sorted(BGP_IP_VERSION_MAP.items(), key=lambda item: item[0])
            if len([ver for ver in v if mod_version.startswith(ver)]) > 0
        ][0]

        if route_fam not in ROUTE_FAMILY:
            raise ValueError(f"Invalid route family {route_fam}")
        if (
            route_type
            and route_type not in ROUTE_TYPE_VERSIONS[evpn_route_type_version]
        ):
            raise ValueError(f"Invalid route type {route_type}")

        PATH_BGP_PATH_ATTRIBS = (
            "/network-instance[name="
            + network_instance
            + "]/bgp-rib/attr-sets/attr-set"
        )
        RIB_EVPN_PATH_VERSIONS: Dict[int, Dict[str, Any]] = {
            1: {
                "RIB_EVPN_PATH": (
                    "/network-instance[name=" + network_instance + "]/bgp-rib/"  # type: ignore
                    f"{ROUTE_FAMILY[route_fam]}/rib-in-out/rib-in-post/"
                    f"{ROUTE_TYPE_VERSIONS[evpn_route_type_version][route_type]}"  # type: ignore
                ),
                "RIB_EVPN_JMESPATH_COMMON": '"network-instance"[].{NI:name, Rib:"bgp-rib"."'
                + ROUTE_FAMILY[route_fam]
                + '"."rib-in-out"."rib-in-post"."'
                + ROUTE_TYPE_VERSIONS[evpn_route_type_version][route_type]  # type: ignore
                + '"[]',
                "RIB_EVPN_JMESPATH_ATTRS": {
                    "1": '.{RD:"route-distinguisher", peer:neighbor, ESI:esi, Tag:"ethernet-tag-id",vni:vni, "NextHop":"next-hop", RT:"_rt", "esi-lbl":"_esi_lbl", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "2": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "MAC":"mac-address", "IP":"ip-address",vni:vni,L1:"_label1",L2:"_label2","next-hop":"next-hop", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "3": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, Tag:"ethernet-tag-id", "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "4": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "5": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, lpref:"local-pref", "IP-Pfx":"ip-prefix",vni:vni, med:med, "next-hop":"next-hop", GW:"gateway-ip",origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                },
            },
            2: {
                "RIB_EVPN_PATH": (
                    "/network-instance[name=" + network_instance + f"]/bgp-rib/afi-safi[afi-safi-name={ROUTE_FAMILY[route_fam]}]/"  # type: ignore
                    f"{ROUTE_FAMILY[route_fam]}/rib-in-out/rib-in-post/"
                    f"{ROUTE_TYPE_VERSIONS[evpn_route_type_version][route_type]}"  # type: ignore
                ),
                "RIB_EVPN_JMESPATH_COMMON": '"network-instance"[].{NI:name, Rib:"bgp-rib"."afi-safi"[]."'
                + ROUTE_FAMILY[route_fam]
                + '"."rib-in-out"."rib-in-post"."'
                + ROUTE_TYPE_VERSIONS[evpn_route_type_version][route_type]  # type: ignore
                + '"[]',
                "RIB_EVPN_JMESPATH_ATTRS": {
                    "1": '.{RD:"route-distinguisher", peer:neighbor, ESI:esi, Tag:"ethernet-tag-id",vni:vni, "NextHop":"next-hop", RT:"_rt", "esi-lbl":"_esi_lbl", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "2": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "MAC":"mac-address", "IP":"ip-address",vni:vni,L1:"_label1",L2:"_label2","next-hop":"next-hop", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "3": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, Tag:"ethernet-tag-id", "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "4": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "5": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, lpref:"local-pref", "IP-Pfx":"ip-prefix",vni:vni, med:med, "next-hop":"next-hop", GW:"gateway-ip",origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                },
            },
        }
        if route_fam in ("l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast"):
            # Hyphenated YANG leaf names must be JMESPath quoted identifiers, not bare tokens.
            # Some releases expose the VPN NLRI as ``prefix`` instead of ``ipv4-prefix`` /
            # ``ipv6-prefix``; OR picks whichever is present.
            _pfx_expr = (
                '"ipv4-prefix" || prefix'
                if route_fam == "l3vpn-ipv4-unicast"
                else '"ipv6-prefix" || prefix'
            )
            ip_rib_jmespath_tail = {
                1: (
                    f'.{{neighbor:neighbor, "0_st":"_r_state", "RD":"route-distinguisher", '
                    f'"Pfx":{_pfx_expr}, "lpref":"local-pref", med:med, "next-hop":"next-hop",'
                    f'"as-path":"as-path".segment[0].member}}'
                ),
                2: (
                    f'.{{neighbor:neighbor, "0_st":"_r_state", "RD":"route-distinguisher", '
                    f'"Pfx":{_pfx_expr}, "lpref":"local-pref", med:med, "next-hop":"next-hop",'
                    f'"as-path":"as-path".segment[0].member,'
                    '"communities":[communities.community, communities."large-community"][]|join(\', \',@)}}'
                ),
                3: (
                    f'.{{neighbor:neighbor, "0_st":"_r_state", "RD":"route-distinguisher", '
                    f'"Pfx":{_pfx_expr}, "lpref":"local-pref", med:med, "next-hop":"next-hop",'
                    f'"as-path":"as-path".segment[0].member,'
                    '"communities":[communities.community, communities."large-community"][]|join(\',\',@)}}'
                ),
            }
        else:
            ip_rib_jmespath_tail = {
                1: (
                    '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, '
                    '"next-hop":"next-hop","as-path":"as-path".segment[0].member}}'
                ),
                2: (
                    '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, '
                    '"next-hop":"next-hop","as-path":"as-path".segment[0].member,'
                    '"communities":[communities.community, communities."large-community"][]|join(\', \',@)}}'
                ),
                3: (
                    '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, '
                    '"next-hop":"next-hop","as-path":"as-path".segment[0].member,'
                    '"communities":[communities.community, communities."large-community"][]|join(\',\',@)}}'
                ),
            }

        RIB_IP_PATH_VERSIONS = {
            1: {
                "RIB_IP_PATH": (
                    f"/network-instance[name={network_instance}]/bgp-rib/"
                    f"{ROUTE_FAMILY[route_fam]}/local-rib/routes"
                ),
                "RIB_IP_JMESPATH": '"network-instance"[].{NI:name, Rib:"bgp-rib"."'
                + ROUTE_FAMILY[route_fam]
                + '"."local-rib"."routes"[]'
                + ip_rib_jmespath_tail[1],
            },
            2: {
                "RIB_IP_PATH": (
                    f"/network-instance[name={network_instance}]/bgp-rib/afi-safi[afi-safi-name={ROUTE_FAMILY[route_fam]}]/"
                    f"{ROUTE_FAMILY[route_fam]}/local-rib/routes"
                ),
                "RIB_IP_JMESPATH": '"network-instance"[].{NI:name, Rib:"bgp-rib"."afi-safi"[]."'
                + ROUTE_FAMILY[route_fam]
                + '"."local-rib"."routes"[]'
                + ip_rib_jmespath_tail[2],
            },
            3: {
                "RIB_IP_PATH": (
                    f"/network-instance[name={network_instance}]/bgp-rib/afi-safi[afi-safi-name={ROUTE_FAMILY[route_fam]}]/"
                    f"{ROUTE_FAMILY[route_fam]}/local-rib/route"
                ),
                "RIB_IP_JMESPATH": '"network-instance"[].{NI:name, Rib:"bgp-rib"."afi-safi"[]."'
                + ROUTE_FAMILY[route_fam]
                + '"."local-rib"."route"[]'
                + ip_rib_jmespath_tail[3],
            },
        }

        # Extra path-attribute fields appended to the per-route projection when
        # detail=True. These are only added to structured (json/yaml) output so
        # the table report stays lean. Keys are unique vs. the lean projections.
        EXTRA_ATTRS_EVPN = (
            'communities:"_communities", soo:"_soo", '
            '"tunnel-encap":"_tunnel_encap", dpath:"_dpath", '
            'valid:"valid-route", best:"best-route", used:"used-route", '
            '"tie-break":"tie-break-reason", "internal-tags":"internal-tags", '
            '"neighbor-as":"neighbor-as"'
        )
        EXTRA_ATTRS_IP = (
            'soo:"_soo", "tunnel-encap":"_tunnel_encap", dpath:"_dpath", '
            'valid:"valid-route", best:"best-route", used:"used-route", '
            '"tie-break":"tie-break-reason", "internal-tags":"internal-tags", '
            '"neighbor-as":"neighbor-as"'
        )

        def _with_detail(attrs: str, extra: str) -> str:
            # The per-route projection ends with '}}' (closing the route dict and
            # the enclosing NI dict). Inject extra fields into the route dict.
            if detail and attrs.endswith("}}"):
                return attrs[:-2] + ", " + extra + "}}"
            return attrs

        evpn_attrs = _with_detail(
            RIB_EVPN_PATH_VERSIONS[evpn_path_version]["RIB_EVPN_JMESPATH_ATTRS"][
                route_type
            ],
            EXTRA_ATTRS_EVPN,
        )
        ip_jmespath = _with_detail(
            RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_JMESPATH"], EXTRA_ATTRS_IP
        )

        PATH_SPECS = {
            "evpn": {
                "path": RIB_EVPN_PATH_VERSIONS[evpn_path_version]["RIB_EVPN_PATH"],
                "jmespath": RIB_EVPN_PATH_VERSIONS[evpn_path_version][
                    "RIB_EVPN_JMESPATH_COMMON"
                ]
                + evpn_attrs,
                "datatype": "state",
            },
            "ipv4": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": ip_jmespath,
                "datatype": "state",
            },
            "ipv6": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": ip_jmespath,
                "datatype": "state",
            },
            "l3vpn-ipv4-unicast": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": ip_jmespath,
                "datatype": "state",
            },
            "l3vpn-ipv6-unicast": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": ip_jmespath,
                "datatype": "state",
            },
        }

        attribs: Dict[str, Dict[str, Any]] = dict()

        resp = self.get(paths=[PATH_BGP_PATH_ATTRIBS], datatype="state")
        for ni in resp[0].get("network-instance", []):
            if ni["name"] not in attribs:
                attribs[ni["name"]] = dict()
            for path in ni.get("bgp-rib", {}).get("attr-sets", {}).get("attr-set", []):
                path_copy = copy.deepcopy(path)
                attribs[ni["name"]].update({path_copy.pop("index"): path_copy})

        path_spec: Dict[str, str] = PATH_SPECS[route_fam]
        rib_path = str(path_spec.get("path"))
        if route_fam in ("l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast"):
            with _suppress_pygnmi_client_logging():
                try:
                    resp = self.get(paths=[rib_path], datatype=path_spec["datatype"])
                except BaseException as e:
                    # Leaves / platforms without IP-VPN have no l3vpn-* RIB path; skip instead of failing.
                    if _gnmi_path_missing(e):
                        return {"bgp_rib": []}
                    raise
        else:
            resp = self.get(paths=[rib_path], datatype=path_spec["datatype"])
        for ni in resp[0].get("network-instance", []):
            ni = augment_routes(ni, attribs[ni["name"]])

        res = jmespath.search(path_spec["jmespath"], resp[0])
        if res is None:
            res = []
        return {"bgp_rib": res}

    def get_sum_bgp(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        BGP_MOD = "urn:srl_nokia/bgp:srl_nokia-bgp"
        BGP_MOD2 = "urn:nokia.com:srlinux:bgp:bgp:srl_nokia-bgp"

        if self.capabilities is not None:
            mod_version = [
                m
                for m in self.capabilities.get("supported_models", [])
                if BGP_MOD == m.get("name") or BGP_MOD2 == m.get("name")
            ][0].get("version")
        else:
            raise Exception("Capabilities not set")
        BGP_VERSION_MAP = {1: ("2021-", "2022-"), 2: ("2023-3", "20")}
        our_version = [
            k
            for k, v in sorted(BGP_VERSION_MAP.items(), key=lambda item: item[0])
            if len([ver for ver in v if mod_version.startswith(ver)]) > 0
        ][0]

        def augment_resp(resp):
            for ni in resp[0].get("network-instance", []):
                if ni.get("protocols") and ni["protocols"].get("bgp"):
                    for peer in ni["protocols"]["bgp"]["neighbor"]:
                        peer_data = dict()
                        if our_version == 1:
                            peer_data["evpn"] = peer.get("evpn")
                            peer_data["ipv4-unicast"] = peer.get("ipv4-unicast")
                            peer_data["local-as"] = peer.get("local-as", [{}])[0].get(
                                "as-number", "-"
                            )
                        elif our_version == 2:
                            peer_data["local-as"] = peer.get("local-as", {}).get(
                                "as-number", "-"
                            )
                            for afi in peer.get("afi-safi", []):
                                if afi["afi-safi-name"] == "evpn":
                                    peer_data["evpn"] = afi
                                elif afi["afi-safi-name"] == "ipv4-unicast":
                                    peer_data["ipv4-unicast"] = afi
                                elif afi["afi-safi-name"] == "ipv6-unicast":
                                    peer_data["ipv6-unicast"] = afi
                                elif afi["afi-safi-name"] == "l3vpn-ipv4-unicast":
                                    peer_data["l3vpn-ipv4-unicast"] = afi
                                elif afi["afi-safi-name"] == "l3vpn-ipv6-unicast":
                                    peer_data["l3vpn-ipv6-unicast"] = afi
                        peer["_local-asn"] = peer_data["local-as"]
                        peer["_flags"] = ""
                        peer["_flags"] += (
                            "D" if peer.get("dynamic-neighbor", False) else "-"
                        )
                        peer["_flags"] += (
                            "B"
                            if peer.get("failure-detection", {}).get(
                                "enable-bfd", False
                            )
                            else "-"
                        )
                        peer["_flags"] += (
                            "F"
                            if peer.get("failure-detection", {}).get(
                                "fast-failover", False
                            )
                            else "-"
                        )
                        if peer_data.get("evpn"):
                            peer["_evpn"] = (
                                str(peer_data["evpn"]["received-routes"])
                                + "/"
                                + str(peer_data["evpn"]["active-routes"])
                                + "/"
                                + str(peer_data["evpn"]["sent-routes"])
                                if peer_data["evpn"]["admin-state"] == "enable"
                                else "disabled"
                            )
                        else:
                            peer["_evpn"] = "-"
                        if peer_data.get("ipv4-unicast"):
                            if peer_data["ipv4-unicast"]["admin-state"] == "enable":
                                peer["_ipv4"] = (
                                    str(peer_data["ipv4-unicast"]["received-routes"])
                                    + "/"
                                    + str(peer_data["ipv4-unicast"]["active-routes"])
                                    + "/"
                                    + str(peer_data["ipv4-unicast"]["sent-routes"])
                                )
                                if (
                                    peer_data["ipv4-unicast"].get("oper-state")
                                    == "down"
                                ):
                                    peer["_ipv4"] = "down"
                            else:
                                peer["_ipv4"] = "disabled"
                        else:
                            peer["_ipv4"] = "-"
                        if peer_data.get("ipv6-unicast"):
                            if peer_data["ipv6-unicast"]["admin-state"] == "enable":
                                peer["_ipv6"] = (
                                    str(peer_data["ipv6-unicast"]["received-routes"])
                                    + "/"
                                    + str(peer_data["ipv6-unicast"]["active-routes"])
                                    + "/"
                                    + str(peer_data["ipv6-unicast"]["sent-routes"])
                                )
                                if (
                                    peer_data["ipv6-unicast"].get("oper-state")
                                    == "down"
                                ):
                                    peer["_ipv6"] = "down"
                            else:
                                peer["_ipv6"] = "disabled"
                        else:
                            peer["_ipv6"] = "-"
                        if peer_data.get("l3vpn-ipv4-unicast"):
                            if peer_data["l3vpn-ipv4-unicast"]["admin-state"] == "enable":
                                peer["_l3vpn4"] = (
                                    str(
                                        peer_data["l3vpn-ipv4-unicast"][
                                            "received-routes"
                                        ]
                                    )
                                    + "/"
                                    + str(
                                        peer_data["l3vpn-ipv4-unicast"][
                                            "active-routes"
                                        ]
                                    )
                                    + "/"
                                    + str(
                                        peer_data["l3vpn-ipv4-unicast"]["sent-routes"]
                                    )
                                )
                                if (
                                    peer_data["l3vpn-ipv4-unicast"].get("oper-state")
                                    == "down"
                                ):
                                    peer["_l3vpn4"] = "down"
                            else:
                                peer["_l3vpn4"] = "disabled"
                        else:
                            peer["_l3vpn4"] = "-"
                        if peer_data.get("l3vpn-ipv6-unicast"):
                            if peer_data["l3vpn-ipv6-unicast"]["admin-state"] == "enable":
                                peer["_l3vpn6"] = (
                                    str(
                                        peer_data["l3vpn-ipv6-unicast"][
                                            "received-routes"
                                        ]
                                    )
                                    + "/"
                                    + str(
                                        peer_data["l3vpn-ipv6-unicast"][
                                            "active-routes"
                                        ]
                                    )
                                    + "/"
                                    + str(
                                        peer_data["l3vpn-ipv6-unicast"]["sent-routes"]
                                    )
                                )
                                if (
                                    peer_data["l3vpn-ipv6-unicast"].get("oper-state")
                                    == "down"
                                ):
                                    peer["_l3vpn6"] = "down"
                            else:
                                peer["_l3vpn6"] = "disabled"
                        else:
                            peer["_l3vpn6"] = "-"

        path_spec = {
            "path": f"/network-instance[name={network_instance}]/protocols/bgp/neighbor",
            "jmespath": '"network-instance"[].{NI:name, Neighbors: protocols.bgp.neighbor[].{"1_peer":"peer-address",\
                    "peer-as":"peer-as", state:"session-state","local-as":"_local-asn",flags:"_flags",\
                    "group":"peer-group", "export-policy":"export-policy", "import-policy":"import-policy",\
                    "U4\\nR/A/T":"_ipv4", "U6\\nR/A/T":"_ipv6", "EVPN\\nR/A/T":"_evpn",\
                    "VPNv4\\nR/A/T":"_l3vpn4", "VPNv6\\nR/A/T":"_l3vpn6"}}',
            "datatype": "all",
            "key": "index",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        augment_resp(resp)
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"bgp_peers": res}

    def get_rib(
        self,
        afi: str,
        network_instance: Optional[str] = "*",
        lpm_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        path_spec = {
            "path": f"/network-instance[name={network_instance}]/route-table/{afi}",
            "jmespath": '"network-instance"[?_hasrib].{NI:name, Rib:"route-table"."'
            + afi
            + '".route[].{"Prefix":"'
            + ("ipv4-prefix" if afi == "ipv4-unicast" else "ipv6-prefix")
            + '",\
                    "next-hop":"_next-hop",type:"route-type", Act:active, "orig-vrf":"_orig_vrf",metric:metric, pref:preference, itf:"_nh_itf"}}',
            "datatype": "state",
        }

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

        nh_mapping: Dict[str, Dict[str, Any]] = {}
        for ni in nhs[0].get("network-instance", {}):
            tmp_map: Dict[str, Any] = {}
            for nh in ni["route-table"]["next-hop"]:
                tmp_map[nh["index"]] = {
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
                if resolving_tunnel:
                    tmp_map[nh["index"]].update(
                        {
                            "tunnel": resolving_tunnel.get("tunnel-type")
                            + ":"
                            + resolving_tunnel.get("ip-prefix")
                        }
                    )
                if resolving_route:
                    tmp_map[nh["index"]].update(
                        {"resolving-route": resolving_route.get("ip-prefix")}
                    )

            nh_mapping.update({ni["name"]: tmp_map})
        nhgroup_mapping: Dict[str, Dict[str, List[Any]]] = {}
        for ni in nhgroups[0].get("network-instance", {}):
            network_instance = ni["name"]
            nh_map: Dict[str, List[Any]] = {}
            for nhgroup in ni["route-table"]["next-hop-group"]:
                nh_map[nhgroup["index"]] = [
                    nh_mapping[network_instance][nh.get("next-hop")]
                    for nh in nhgroup.get("next-hop", [])
                ]
            nhgroup_mapping.update({ni["name"]: nh_map})

        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for ni in resp[0].get("network-instance", {}):
            if len(ni["route-table"][afi]) == 0:
                ni["_hasrib"] = False
            else:
                ni["_hasrib"] = True
                if lpm_address:
                    lpm_prefix = lpm(
                        lpm_address,
                        [
                            route[
                                (
                                    "ipv4-prefix"
                                    if afi == "ipv4-unicast"
                                    else "ipv6-prefix"
                                )
                            ]
                            for route in ni["route-table"][afi]["route"]
                        ],
                    )
                    if lpm_prefix:
                        ni["route-table"][afi]["route"] = [
                            r
                            for r in ni["route-table"][afi]["route"]
                            if r[
                                (
                                    "ipv4-prefix"
                                    if afi == "ipv4-unicast"
                                    else "ipv6-prefix"
                                )
                            ]
                            == lpm_prefix
                        ]
                    else:
                        ni["route-table"][afi]["route"] = []
                        ni["_hasrib"] = False
                        continue
                for route in ni["route-table"][afi].get("route", []):
                    if route["active"]:
                        route["active"] = "yes"
                    else:
                        route["active"] = "no"
                    if "next-hop-group" in route:
                        leaked = False
                        if "origin-network-instance" in route:
                            nh_ni = route["origin-network-instance"]
                            if nh_ni != ni["name"]:
                                leaked = True
                                route["_orig_vrf"] = nh_ni
                        else:
                            nh_ni = ni["name"]
                        route["_next-hop"] = [
                            (
                                nh.get("resolving-route") + " (indirect)"
                                if nh.get("type") == "indirect"
                                and nh.get("resolving-route")
                                else nh.get("ip-address")
                            )
                            for nh in nhgroup_mapping[nh_ni].get(
                                route["next-hop-group"], {}
                            )
                        ]

                        route["_nh_itf"] = [
                            (
                                nh.get("subinterface") + f"@vrf:{nh_ni}"
                                if leaked
                                else nh.get("subinterface")
                            )
                            for nh in nhgroup_mapping[nh_ni].get(
                                route["next-hop-group"], {}
                            )
                            if nh.get("subinterface")
                        ]
                        if len(route["_nh_itf"]) == 0:
                            route["_nh_itf"] = [
                                nh.get("tunnel")
                                for nh in nhgroup_mapping[nh_ni].get(
                                    route["next-hop-group"], {}
                                )
                                if nh.get("tunnel")
                            ]
                        if len(route["_nh_itf"]) == 0:
                            route["_nh_itf"] = [
                                nh.get("resolving-route")
                                for nh in nhgroup_mapping[nh_ni].get(
                                    route["next-hop-group"], {}
                                )
                                if nh.get("resolving-route")
                            ]

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"ip_rib": res}

    def get_tunnel_table(self, network_instance: str = "*") -> Dict[str, Any]:
        """Get the IP tunnel-table (LDP, SR-ISIS, RSVP, VXLAN, ...).

        Resolves each tunnel's next-hop-group to the egress subinterface,
        next-hop IP and pushed MPLS label-stack, mirroring the next-hop
        resolution used by :meth:`get_rib`. Returns flat rows with the fields
        required to verify transport/forwarding paths in tests.
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

        nh_mapping: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for ni in nhs[0].get("network-instance", []):
            tmp_map: Dict[str, Dict[str, Any]] = {}
            for nh in ni.get("route-table", {}).get("next-hop", []):
                label_stack = nh.get("mpls-encapsulation", {}).get(
                    "pushed-mpls-label-stack"
                ) or nh.get("mpls", {}).get("pushed-mpls-label-stack")
                tmp_map[nh["index"]] = {
                    "ip-address": nh.get("ip-address"),
                    "subinterface": nh.get("subinterface"),
                    "type": nh.get("type"),
                    "labels": label_stack,
                }
            nh_mapping[ni["name"]] = tmp_map

        nhgroup_mapping: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for ni in nhgroups[0].get("network-instance", []):
            ni_name = ni["name"]
            nh_map: Dict[str, List[Dict[str, Any]]] = {}
            for nhgroup in ni.get("route-table", {}).get("next-hop-group", []):
                nh_map[nhgroup["index"]] = [
                    nh_mapping.get(ni_name, {}).get(nh.get("next-hop"), {})
                    for nh in nhgroup.get("next-hop", [])
                ]
            nhgroup_mapping[ni_name] = nh_map

        resp = self.get(
            paths=[f"/network-instance[name={network_instance}]/tunnel-table"],
            datatype="state",
        )

        rows: List[Dict[str, Any]] = []
        for ni in resp[0].get("network-instance", []):
            ni_name = ni["name"]
            tunnel_table = ni.get("tunnel-table", {})
            for afi in ("ipv4", "ipv6"):
                prefix_key = "ipv4-prefix" if afi == "ipv4" else "ipv6-prefix"
                for tunnel in tunnel_table.get(afi, {}).get("tunnel", []):
                    nhg_index = tunnel.get("next-hop-group")
                    resolved = nhgroup_mapping.get(ni_name, {}).get(nhg_index, [])
                    next_hops = [
                        nh.get("ip-address") for nh in resolved if nh.get("ip-address")
                    ]
                    egress_itfs = [
                        nh.get("subinterface")
                        for nh in resolved
                        if nh.get("subinterface")
                    ]
                    labels = [
                        str(lbl) for nh in resolved for lbl in (nh.get("labels") or [])
                    ]
                    rows.append(
                        {
                            "NI": ni_name,
                            "Prefix": tunnel.get(prefix_key),
                            "type": tunnel.get("type"),
                            "owner": tunnel.get("owner"),
                            "pref": tunnel.get("preference"),
                            "metric": tunnel.get("metric"),
                            "next-hop": next_hops,
                            "egress-itf": egress_itfs,
                            "label": labels,
                        }
                    )

        return {"tunnel_table": rows}

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

        # Map next-hop groups
        # nh_mapping[ni_name][group_name] = [ip1, ip2(R), ...]
        nh_mapping: Dict[str, Dict[str, List[str]]] = {}
        # static_routes_data[ni_name] = [route1, route2, ...]
        static_routes_data: Dict[str, List[Dict[str, Any]]] = {}

        for item in resp:
            if "network-instance" in item:
                for ni in item["network-instance"]:
                    ni_name = ni["name"]
                    if "next-hop-groups" in ni:
                        if ni_name not in nh_mapping:
                            nh_mapping[ni_name] = {}
                        for group in ni["next-hop-groups"].get("group", []):
                            group_name = group["name"]
                            nh_list = []
                            for nh in group.get("nexthop", []):
                                ip = nh.get("ip-address")
                                if ip:
                                    if nh.get("resolve", False):
                                        ip = f"{ip}(R)"
                                    nh_list.append(ip)
                            nh_mapping[ni_name][group_name] = nh_list

                    if "static-routes" in ni:
                        if ni_name not in static_routes_data:
                            static_routes_data[ni_name] = []
                        static_routes_data[ni_name].extend(
                            ni["static-routes"].get("route", [])
                        )

        processed_routes = []
        for ni_name, routes in static_routes_data.items():
            for route in routes:
                nh_group_name = route.get("next-hop-group")
                nhops = (
                    nh_mapping.get(ni_name, {}).get(nh_group_name, [])
                    if nh_group_name
                    else []
                )

                processed_routes.append(
                    {
                        "NI": ni_name,
                        "route": route.get("prefix"),
                        "admin-state": route.get("admin-state"),
                        "installed": route.get("installed"),
                        "metric": route.get("metric"),
                        "pref": route.get("preference"),
                        "nhops": nhops,
                    }
                )

        return {"static_routes": processed_routes}
