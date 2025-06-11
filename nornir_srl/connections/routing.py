# Routing related methods extracted from srlinux.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import copy
import jmespath
from .helpers import lpm


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
                    elif "label" in d:  # for SRL 24.3 onwards
                        d["vni"] = d["label"].get("value", "-")
                    else:
                        d["vni"] = d.get("vni", "-")
                    d["_rt"] = ",".join(
                        [
                            x_comm.split("target:")[1]
                            for x_comm in d.get("communities", {}).get(
                                "ext-community", []
                            )
                            if "target:" in x_comm
                        ]
                    )
                    d["_as_path"] = str(
                        attribs[d["attr-id"]]
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
                    "2": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "MAC":"mac-address", "IP":"ip-address",vni:vni,"next-hop":"next-hop", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
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
                    "2": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "MAC":"mac-address", "IP":"ip-address",vni:vni,"next-hop":"next-hop", "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "3": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, Tag:"ethernet-tag-id", "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "4": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "next-hop":"next-hop", origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                    "5": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, lpref:"local-pref", "IP-Pfx":"ip-prefix",vni:vni, med:med, "next-hop":"next-hop", GW:"gateway-ip",origin:origin, "0_st":"_r_state", "as-path":"as-path".segment[0].member}}',
                },
            },
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
                + '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, "next-hop":"next-hop","as-path":"as-path".segment[0].member}}',
            },
            2: {
                "RIB_IP_PATH": (
                    f"/network-instance[name={network_instance}]/bgp-rib/afi-safi[afi-safi-name={ROUTE_FAMILY[route_fam]}]/"
                    f"{ROUTE_FAMILY[route_fam]}/local-rib/routes"
                ),
                "RIB_IP_JMESPATH": '"network-instance"[].{NI:name, Rib:"bgp-rib"."afi-safi"[]."'
                + ROUTE_FAMILY[route_fam]
                + '"."local-rib"."routes"[]'
                + '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, "next-hop":"next-hop","as-path":"as-path".segment[0].member,\
                      "communities":[communities.community, communities."large-community"][]|join(\', \',@)}}',
            },
            3: {
                "RIB_IP_PATH": (
                    f"/network-instance[name={network_instance}]/bgp-rib/afi-safi[afi-safi-name={ROUTE_FAMILY[route_fam]}]/"
                    f"{ROUTE_FAMILY[route_fam]}/local-rib/route"
                ),
                "RIB_IP_JMESPATH": '"network-instance"[].{NI:name, Rib:"bgp-rib"."afi-safi"[]."'
                + ROUTE_FAMILY[route_fam]
                + '"."local-rib"."route"[]'
                + '.{neighbor:neighbor, "0_st":"_r_state", "Prefix":prefix, "lpref":"local-pref", med:med, "next-hop":"next-hop","as-path":"as-path".segment[0].member,\
                      "communities":[communities.community, communities."large-community"][]|join(\',\',@)}}',
            },
        }

        PATH_SPECS = {
            "evpn": {
                "path": RIB_EVPN_PATH_VERSIONS[evpn_path_version]["RIB_EVPN_PATH"],
                "jmespath": RIB_EVPN_PATH_VERSIONS[evpn_path_version][
                    "RIB_EVPN_JMESPATH_COMMON"
                ]
                + RIB_EVPN_PATH_VERSIONS[evpn_path_version]["RIB_EVPN_JMESPATH_ATTRS"][
                    route_type
                ],
                "datatype": "state",
            },
            "ipv4": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_JMESPATH"],
                "datatype": "state",
            },
            "ipv6": {
                "path": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_PATH"],
                "jmespath": RIB_IP_PATH_VERSIONS[ip_path_version]["RIB_IP_JMESPATH"],
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
        resp = self.get(
            paths=[str(path_spec.get("path"))], datatype=path_spec["datatype"]
        )
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

        path_spec = {
            "path": f"/network-instance[name={network_instance}]/protocols/bgp/neighbor",
            "jmespath": '"network-instance"[].{NI:name, Neighbors: protocols.bgp.neighbor[].{"1_peer":"peer-address",\
                    "peer-as":"peer-as", state:"session-state","local-as":"_local-asn",flags:"_flags",\
                    "group":"peer-group", "export-policy":"export-policy", "import-policy":"import-policy",\
                    "AF: IPv4\\nRx/Act/Tx":"_ipv4", "AF: IPv6\\nRx/Act/Tx":"_ipv6", \
                    "AF: EVPN\\nRx/Act/Tx":"_evpn"}}',
            "datatype": "state",
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
                if "resolving-tunnel" in nh:
                    tmp_map[nh["index"]].update(
                        {
                            "tunnel": (nh.get("resolving-tunnel")).get("tunnel-type")
                            + ":"
                            + (nh.get("resolving-tunnel")).get("ip-prefix")
                        }
                    )
                if "resolving-route" in nh:
                    tmp_map[nh["index"]].update(
                        {
                            "resolving-route": (nh.get("resolving-route")).get(
                                "ip-prefix"
                            )
                        }
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
                for route in ni["route-table"][afi]["route"]:
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
                            nh.get("ip-address")
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
                            resolving_routes = [
                                nh.get("resolving-route", {})
                                for nh in nhgroup_mapping[nh_ni].get(
                                    route["next-hop-group"], {}
                                )
                                if nh.get("resolving-route")
                            ]

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"ip_rib": res}
