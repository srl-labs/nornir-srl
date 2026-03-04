from __future__ import annotations

from typing import Any, Dict, List, Optional
import jmespath


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

    def get_lldp_sum(self, interface: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/lldp/interface[name={interface}]/neighbor",
            "jmespath": '"system/lldp".interface[].{interface:name, Neighbors:neighbor[].{"Nbr-port":"port-id","Nbr-System":"system-name", "Nbr-port-desc":"port-description"}}',
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
            "jmespath": '"network-instance"[].{"NI":name, Fib:"bridge-table"."mac-table".mac[].{Address:address, Dest:destination, Type:type}}',
            "datatype": "state",
        }
        if (
            "bridged"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
            return {"mac_table": []}
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"mac_table": res}

    def get_es(self) -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/network-instance/protocols/evpn/ethernet-segments",
            "jmespath": '"system/network-instance/protocols/evpn/ethernet-segments"."bgp-instance"[]."ethernet-segment"[].{name:name, esi:esi, type:type, "mh-mode":"multi-homing-mode", oper:"oper-state", "itf/nh":"_itf_or_nh", "ni-peers":association."network-instance"[]."_ni_peers"|join(\', \',@) }',
            "datatype": "all",
        }

        def set_es_fields(resp: List[Dict[str, Any]]) -> None:
            for bgp_inst in (
                resp[0]
                .get("system/network-instance/protocols/evpn/ethernet-segments", {})
                .get("bgp-instance", [])
            ):
                for es in bgp_inst.get("ethernet-segment", []):
                    # compute interface or next-hop display field
                    if "interface" in es:
                        es["_itf_or_nh"] = " ".join(
                            i["ethernet-interface"] for i in es["interface"]
                        )
                    elif "next-hop" in es:
                        es["_itf_or_nh"] = " ".join(
                            nh["l3-next-hop"] for nh in es["next-hop"]
                        )
                    else:
                        es["_itf_or_nh"] = ""
                    if "association" not in es:
                        es["association"] = {}
                    if "network-instance" not in es["association"]:
                        es["association"]["network-instance"] = []
                    for vrf in es["association"]["network-instance"]:
                        es_peers = (
                            vrf["bgp-instance"][0]
                            .get("computed-designated-forwarder-candidates", {})
                            .get("designated-forwarder-candidate", [])
                        )
                        vrf["_peers"] = " ".join(
                            (
                                f"{peer['address']}(DF)"
                                if peer["designated-forwarder"]
                                else peer["address"]
                            )
                            for peer in es_peers
                        )
                        vrf["_ni_peers"] = f"{vrf['name']}:[{vrf['_peers']}]"

        if (
            "evpn"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
            return {"es": []}
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        set_es_fields(resp)
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"es": res}

    def get_irb(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=irb*]/subinterface",
            "jmespath": (
                '"interface"[].subinterface[].{name:"_subitf",'
                ' "net-inst":"_ni",'
                ' "ipv4-addr":"_ipv4_addrs",'
                ' "ipv6-addr":"_ipv6_addrs",'
                ' "AGW?":"_anycast_gw",'
                ' arp:"_arp_summary",'
                ' nd:"_nd_summary",'
                ' "arp-evpn":"_arp_evpn",'
                ' "nd-evpn":"_nd_evpn",'
                ' "IFL?":"_ilr"}'
            ),
            "datatype": "all",
        }

        # build NI-to-interface map
        ni_itfs = self.get(
            paths=["/network-instance[name=*]"], datatype="config"
        )
        ni_itf_map: Dict[str, List[str]] = {}
        for ni in ni_itfs[0].get("network-instance", []):
            for ni_itf in ni.get("interface", []):
                if ni_itf["name"] not in ni_itf_map:
                    ni_itf_map[ni_itf["name"]] = []
                ni_itf_map[ni_itf["name"]].append(ni["name"])

        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )

        def _format_addrs(addrs: List[Dict[str, Any]]) -> str:
            parts = []
            for a in addrs:
                s = a.get("ip-prefix", "")
                flags = []
                if a.get("primary") is not None:
                    flags.append("P")
                if a.get("anycast-gw"):
                    flags.append("AGW")
                if flags:
                    s += f" ({','.join(flags)})"
                parts.append(s)
            return ", ".join(parts) if parts else ""

        def _arp_summary(ipv4: Dict[str, Any]) -> str:
            arp = ipv4.get("arp", {})
            parts = []
            if arp.get("proxy-arp"):
                parts.append("proxy")
            if arp.get("learn-unsolicited"):
                parts.append("learn-unsol")
            for hr in arp.get("host-route", {}).get("populate", []):
                dp = "dp" if hr.get("datapath-programming") else "no-dp"
                parts.append(f"host-rt:{hr.get('route-type', '?')}/{dp}")
            return ", ".join(parts) if parts else "-"

        def _nd_summary(ipv6: Dict[str, Any]) -> str:
            nd = ipv6.get("neighbor-discovery", {})
            parts = []
            if nd.get("proxy-nd"):
                parts.append("proxy")
            learn = nd.get("learn-unsolicited", "none")
            if learn and learn != "none":
                parts.append(f"learn-unsol:{learn}")
            for hr in nd.get("host-route", {}).get("populate", []):
                dp = "dp" if hr.get("datapath-programming") else "no-dp"
                parts.append(f"host-rt:{hr.get('route-type', '?')}/{dp}")
            return ", ".join(parts) if parts else "-"

        def _evpn_adv(proto_cfg: Dict[str, Any]) -> str:
            evpn = proto_cfg.get("evpn", {})
            advs = evpn.get("advertise", [])
            if not advs:
                return "-"
            return ", ".join(a.get("route-type", "?") for a in advs)

        for itf in resp[0].get("interface", []):
            for subitf in itf.get("subinterface", []):
                subitf_name = f"{itf['name']}.{subitf['index']}"
                subitf["_subitf"] = subitf_name
                subitf["_ni"] = ", ".join(
                    ni_itf_map.get(subitf_name, [])
                )

                ipv4 = subitf.get("ipv4", {})
                ipv6 = subitf.get("ipv6", {})

                subitf["_ipv4_addrs"] = _format_addrs(
                    ipv4.get("address", [])
                )
                subitf["_ipv6_addrs"] = _format_addrs(
                    ipv6.get("address", [])
                )

                agw = subitf.get("anycast-gw", {})
                if agw:
                    subitf["_anycast_gw"] = "Y"
                else:
                    subitf["_anycast_gw"] = "N"

                subitf["_arp_summary"] = _arp_summary(ipv4)
                subitf["_nd_summary"] = _nd_summary(ipv6)
                subitf["_arp_evpn"] = _evpn_adv(ipv4.get("arp", {}))
                subitf["_nd_evpn"] = _evpn_adv(
                    ipv6.get("neighbor-discovery", {})
                )

                arp_advs = (
                    ipv4.get("arp", {}).get("evpn", {}).get("advertise", [])
                )
                nd_advs = (
                    ipv6.get("neighbor-discovery", {})
                    .get("evpn", {})
                    .get("advertise", [])
                )
                has_ilr = any(
                    "interface-less-routing" in a
                    for a in arp_advs + nd_advs
                )
                subitf["_ilr"] = "Y" if has_ilr else "N"

        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"irb": res}
