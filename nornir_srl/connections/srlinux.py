from typing import TYPE_CHECKING, Any, List, Dict, Optional, Union
import difflib
import json
import re
import copy
import datetime

from natsort import natsorted
import jmespath

from pygnmi.client import gNMIclient

from nornir.core.plugins.connections import ConnectionPlugin
from nornir.core.configuration import Config
from nornir.core.exceptions import ConnectionException


from .helpers import strip_modules, normalize_gnmi_resp, lpm

CONNECTION_NAME = "srlinux"


class GnmiPath:
    RE_PATH_COMPONENT = re.compile(
        r"""
    (?P<pname>[^/[]+)  # gNMI path name
    (\[(?P<key>\w\D+)   # gNMI path key
    =
    (?P<value>[^\]]+)    # gNMI path value
    \])?
    """,
        re.VERBOSE,
    )

    def __init__(self, path: str):
        self.path = path.strip("/")
        self.comp = GnmiPath.RE_PATH_COMPONENT.findall(
            self.path
        )  # list (1 item per path-el) of tuples (pname, [k=v], k, v)
        self.elems = ["".join(e[:2]) for e in self.comp]

    def __str__(self):
        return self.path

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.path}')"

    @property
    def resource(self) -> Dict[str, str]:
        return {
            "resource": self.comp[-1][0],
            "key": self.comp[-1][2],
            "val": self.comp[-1][3],
        }

    @property
    def with_no_prefix(self):
        return GnmiPath("/".join([e.split(":")[-1] for e in self.elems]))

    @property
    def parent(self):
        if len(self.elems) > 0:
            return GnmiPath("/".join(self.elems[:-1]))
        return None


class SrLinux:
    def open(
        self,
        hostname: Optional[str],
        username: Optional[str],
        password: Optional[str],
        port: Optional[int],
        platform: Optional[str],
        extras: Optional[Dict[str, Any]] = None,
        configuration: Optional[Config] = None,
    ) -> None:
        """
        Open a gNMI connection to a device
        """
        target = (hostname, port)
        _connection = gNMIclient(
            target=target, username=username, password=password, **extras  # type: ignore
        )
        _connection.connect()
        self._connection = _connection
        self.connection = self
        self.hostname = hostname
        self.capabilities = self._connection.capabilities()

    def gnmi_get(self, **kw):
        return self._connection.get(**kw)

    def gnmi_set(self, **kw):
        return self._connection.set(**kw)

    def close(self) -> None:
        self._connection.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__} on {self.hostname}"

    def get_info(self) -> Dict[str, Any]:
        path_specs: List[Dict[str, Any]] = [
            {
                "path": "/platform/chassis",
                "datatype": "state",
                "fields": [
                    "type",
                    "serial-number",
                    "part-number",
                    "hw-mac-address",
                    "last-booted",
                ],
            },
            {
                "path": "/platform/control[slot=A]",
                "datatype": "state",
                "fields": [
                    "software-version",
                ],
            },
        ]
        result = {}
        for spec in path_specs:
            resp = self.get(paths=[spec.get("path", "")], datatype=spec["datatype"])
            for path in resp[0]:
                result.update(
                    {k: v for k, v in resp[0][path].items() if k in spec["fields"]}
                )
        if result.get("software-version"):
            result["software-version"] = (
                result["software-version"].split("-")[0].lstrip("v")
            )

        return {"sys_info": [result]}

    def get_sum_subitf(self, interface: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name={interface}]/subinterface",
            "jmespath": 'interface[].{Itf:name, subitfs: subinterface[].{Subitf:name,\
                      type:type, admin:"admin-state",oper:"oper-state", \
                        ipv4: ipv4.address[]."ip-prefix", ipv6: ipv6.address[]."ip-prefix", vlan: vlan.encap."single-tagged"."vlan-id"}}',
            "datatype": "state",
            "key": "index",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"subinterface": res}

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
            1: ("20"),
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
        ROUTE_TYPE = {
            "1": "ethernet-ad-routes",
            "2": "mac-ip-routes",
            "3": "imet-routes",
            "4": "ethernet-segment-routes",
            "5": "ip-prefix-routes",
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
                    if d.get("vni", 0) == 0:
                        d["vni"] = "-"
                    d["_rt"] = ",".join(
                        [
                            x_comm.split("target:")[1]
                            for x_comm in d.get("communities", {}).get(
                                "ext-community", []
                            )
                            if "target:" in x_comm
                        ]
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
        ip_path_version = [
            k
            for k, v in sorted(BGP_IP_VERSION_MAP.items(), key=lambda item: item[0])
            if len([ver for ver in v if mod_version.startswith(ver)]) > 0
        ][0]

        if route_fam not in ROUTE_FAMILY:
            raise ValueError(f"Invalid route family {route_fam}")
        if route_type and route_type not in ROUTE_TYPE:
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
                    f"{ROUTE_TYPE[route_type]}"  # type: ignore
                ),
                "RIB_EVPN_JMESPATH_COMMON": '"network-instance"[].{NI:name, Rib:"bgp-rib"."'
                + ROUTE_FAMILY[route_fam]
                + '"."rib-in-out"."rib-in-post"."'
                + ROUTE_TYPE[route_type]  # type: ignore
                + '"[]',
                "RIB_EVPN_JMESPATH_ATTRS": {
                    "1": '.{RD:"route-distinguisher", peer:neighbor, ESI:esi, Tag:"ethernet-tag-id",vni:vni, "NextHop":"next-hop", RT:"_rt", "esi-lbl":"_esi_lbl", "0_st":"_r_state"}}',
                    "2": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "MAC":"mac-address", "IP":"ip-address",vni:vni,"next-hop":"next-hop", "0_st":"_r_state"}}',
                    "3": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, Tag:"ethernet-tag-id", "next-hop":"next-hop", origin:origin, "0_st":"_r_state"}}',
                    "4": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, ESI:esi, "next-hop":"next-hop", origin:origin, "0_st":"_r_state"}}',
                    "5": '.{RD:"route-distinguisher", RT:"_rt", peer:neighbor, lpref:"local-pref", "IP-Pfx":"ip-prefix",vni:vni, med:med, "next-hop":"next-hop", GW:"gateway-ip",origin:origin, "0_st":"_r_state"}}',
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
        if res:
            for ni in res:
                for route in ni.get("Rib", []):
                    route["as-path"] = (
                        str(route["as-path"]) + " i" if route.get("as-path") else "i"
                    )
        else:
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
            for ni in resp[0]["network-instance"]:
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
                    peer_as:"peer-as", state:"session-state",local_as:"_local-asn",\
                    "group":"peer-group", "export_policy":"export-policy", "import_policy":"import-policy",\
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

    def get_lldp_sum(self, interface: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/lldp/interface[name={interface}]/neighbor",
            "jmespath": '"system/lldp".interface[].{interface:name, Neighbors:neighbor[].{"Nbr-port":"port-id",\
                    "Nbr-System":"system-name", "Nbr-port-desc":"port-description"}}',
            "datatype": "state",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"lldp_nbrs": res}

    def get_mac_table(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/network-instance[name={network_instance}]/bridge-table/mac-table/mac",
            "jmespath": '"network-instance"[].{"NI":name, Fib:"bridge-table"."mac-table".mac[].{Address:address,\
                        Dest:destination, Type:type}}',
            "datatype": "state",
        }
        if (
            not "bridged"
            in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
            return {"mac_table": []}
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"mac_table": res}

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

        nh_mapping = {}
        for ni in nhs[0].get("network-instance", {}):
            tmp_map = {}
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
        nhgroup_mapping = {}
        for ni in nhgroups[0].get("network-instance", {}):
            network_instance = ni["name"]
            nh_map: Dict[str, List] = {}
            for nhgroup in ni["route-table"]["next-hop-group"]:
                #                    tmp_map[nhgroup["index"]] = [ nh["next-hop"] for nh in nhgroup["next-hop"] ]
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
                            nh.get("subinterface") + f"@vrf:{nh_ni}"
                            if leaked
                            else nh.get("subinterface")
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
        #                            if len(resolving_routes) > 0:
        #                                route["_nh_itf"] = [ rt.get("_nh_itf") for rt in ni["route-table"]["ipv4-unicast"]["route"] if rt.get("ipv4-prefix") in [ res_rt.get("ip-prefix") for res_rt in resolving_routes ] ]

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"ip_rib": res}

    def get_nwi_itf(self, nw_instance: str = "*") -> Dict[str, Any]:
        SUBITF_PATH = "/interface[name=*]/subinterface"
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "jmespath": '"network-instance"[].{NI:name,oper:"oper-state",type:type,"router-id":protocols.bgp."router-id",\
                    itfs: interface[].{Subitf:name,"assoc-ni":"_other_ni","if-oper":"oper-state", "ip-prefix":*.address[]."ip-prefix",\
                        vlan:vlan.encap."single-tagged"."vlan-id", "mtu":"_mtu"}}',
            "datatype": "state",
        }
        subitf = {}
        resp = self.get(paths=[SUBITF_PATH], datatype="state")
        for itf in resp[0].get("interface", []):
            for si in itf.get("subinterface", []):
                subif_name = itf["name"] + "." + str(si.pop("index"))
                subitf[subif_name] = si
                subitf[subif_name]["_mtu"] = (
                    si.get("l2-mtu") if "l2-mtu" in si else si.get("ip-mtu", "")
                )

        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for ni in resp[0].get("network-instance", {}):
            for ni_itf in ni.get("interface", []):
                ni_itf.update(subitf.get(ni_itf["name"], {}))
                if ni_itf["name"].startswith("irb"):
                    ni_itf["_other_ni"] = " ".join(
                        f"{vrf['name']}"
                        for vrf in resp[0].get("network-instance", {})
                        if ni_itf["name"] in [i["name"] for i in vrf["interface"]]
                        and vrf["name"] != ni["name"]
                    )

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"nwi_itfs": res}

    def get_lag(self, lag_id: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name=lag{lag_id}]",
            "jmespath": '"interface"[].{lag:name, oper:"oper-state",mtu:mtu,"min":lag."min-links",desc:description, type:lag."lag-type", speed:lag."lag-speed","stby-sig":ethernet."standby-signaling",\
                  "lacp-key":lag.lacp."admin-key","lacp-itvl":lag.lacp.interval,"lacp-mode":lag.lacp."lacp-mode","lacp-sysid":lag.lacp."system-id-mac","lacp-prio":lag.lacp."system-priority",\
                    members:lag.member[].{"member-itf":name, "member-oper":"oper-state","act":lacp."activity"}}',
            "datatype": "state",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for itf in resp[0].get("interface", []):
            for member in itf.get("lag", {}).get("member", []):
                member["name"] = str(member.get("name", "")).replace("ethernet", "et")
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"lag": res}

    def get_es(self) -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/network-instance/protocols/evpn/ethernet-segments",
            "jmespath": '"system/network-instance/protocols/evpn/ethernet-segments"."bgp-instance"[]."ethernet-segment"[].{name:name, esi:esi, "mh-mode":"multi-homing-mode",\
                oper:"oper-state",itf:interface[]."ethernet-interface"|join(\' \',@), "ni-peers":association."network-instance"[]."_ni_peers"|join(\', \',@) }',
            "datatype": "state",
        }

        def set_es_peers(resp):
            for bgp_inst in (
                resp[0]
                .get("system/network-instance/protocols/evpn/ethernet-segments", {})
                .get("bgp-instance", [])
            ):
                for es in bgp_inst.get("ethernet-segment", []):
                    if not "association" in es:
                        es["association"] = {}
                    if not "network-instance" in es["association"]:
                        es["association"]["network-instance"] = []
                    for vrf in es["association"]["network-instance"]:
                        es_peers = (
                            vrf["bgp-instance"][0]
                            .get("computed-designated-forwarder-candidates", {})
                            .get("designated-forwarder-candidate", [])
                        )
                        vrf["_peers"] = " ".join(
                            f"{peer['address']}(DF)"
                            if peer["designated-forwarder"]
                            else peer["address"]
                            for peer in es_peers
                        )
                        vrf["_ni_peers"] = f"{vrf['name']}:[{vrf['_peers']}]"

        if (
            not "evpn"
            in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
            return {"es": []}
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        set_es_peers(resp)
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"es": res}

    def get_arp(self) -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", NI:"_ni"|to_string(@), entries:ipv4.arp.neighbor[].{IPv4:"ipv4-address",MAC:"link-layer-address",Type:origin,expiry:"_rel_expiry" }}',
            "datatype": "state",
        }
        ni_itfs = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_itf_map: Dict[str, List[str]] = {}
        for ni in ni_itfs[0].get("network-instance", []):
            #            if ni.get("type", "") == "mac-vrf":
            #                continue
            for ni_itf in ni.get("interface", []):
                if ni_itf["name"] not in ni_itf_map:
                    ni_itf_map[ni_itf["name"]] = []
                ni_itf_map[ni_itf["name"]].append(ni["name"])
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for itf in resp[0].get("interface", []):
            for subitf in itf.get("subinterface", []):
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                subitf["_ni"] = ni_itf_map.get(subitf["_subitf"], [])
                for arp_entry in (
                    subitf.get("ipv4", {}).get("arp", {}).get("neighbor", [])
                ):
                    try:
                        ts = datetime.datetime.strptime(
                            arp_entry["expiration-time"], "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        arp_entry["_rel_expiry"] = (
                            str(ts - datetime.datetime.now()).split(".")[0] + "s"
                        )
                    except:
                        arp_entry["_rel_expiry"] = "-"

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"arp": res}

    def get_nd(self) -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", entries:ipv6."neighbor-discovery".neighbor[].{IPv6:"ipv6-address",MAC:"link-layer-address",State:"current-state",Type:origin,next_state:"_rel_expiry" }}',
            "datatype": "state",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for itf in resp[0].get("interface", []):
            for subitf in itf.get("subinterface", []):
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                for nd_entry in (
                    subitf.get("ipv6", {})
                    .get("neighbor-discovery", {})
                    .get("neighbor", [])
                ):
                    try:
                        ts = datetime.datetime.strptime(
                            nd_entry["next-state-time"], "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        nd_entry["_rel_expiry"] = (
                            str(ts - datetime.datetime.now()).split(".")[0] + "s"
                        )
                    except:
                        nd_entry["_rel_expiry"] = "-"

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"nd": res}

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        if self._connection:
            resp = normalize_gnmi_resp(
                self._connection.get(
                    path=paths, datatype=datatype, encoding="json_ietf"  # type: ignore
                )
            )
        else:
            raise Exception("no active connection")
        if strip_mod:
            return [strip_modules(d) for d in resp]
        else:
            return resp

    def set_config(
        self,
        input: List[Dict[str, Any]],
        op: Optional[str] = "update",
        dry_run: Optional[bool] = False,
        strip_mod: Optional[bool] = True,
    ) -> str:
        device_cfg_after = []
        r_list: List[str] = []
        for r in input:
            r_list += r.keys()
        #        r_list = [ list(r.keys())[0] for r in input ]
        device_cfg_before = self.get(paths=r_list, datatype="config")

        if not dry_run:
            paths = []
            for d in input:
                for p, v in d.items():
                    ### to check - hack
                    ### to address intents that are lists, e.g. /interface
                    #                    if isinstance(v, list):
                    #                        v = { p: v }
                    #                        p = '/'.join(p.split('/')[:-1])
                    #                        if len(p) == 0:
                    #                            p = "/"
                    ###
                    paths.append((p, v))
            if op == "update":
                r = self._connection.set(update=paths, encoding="json_ietf")
            elif op == "replace":
                r = self._connection.set(replace=paths, encoding="json_ietf")
            elif op == "delete":
                delete_paths = [list(p.keys())[0] for p in input]
                r = self._connection.set(delete=delete_paths, encoding="json_ietf")
            else:
                raise ValueError(f"invalid value for parameter 'op': {op}")
            device_cfg_after = self.get(paths=r_list, datatype="config")
        else:
            device_cfg_after = input

        #        dd = DeepDiff(device_cfg_before, device_cfg_after)
        diff = ""
        for i in range(len(r_list)):
            before_json = json.dumps(device_cfg_before[i], indent=2, sort_keys=True)
            after_json = json.dumps(device_cfg_after[i], indent=2, sort_keys=True)
            for line in difflib.unified_diff(
                before_json.splitlines(keepends=True),
                after_json.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
                n=5,
            ):
                diff += line
            if len(diff) > 0:
                diff += "\n"

        return diff
