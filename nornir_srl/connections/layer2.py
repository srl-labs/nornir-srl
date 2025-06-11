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
            "jmespath": '"system/network-instance/protocols/evpn/ethernet-segments"."bgp-instance"[]."ethernet-segment"[].{name:name, esi:esi, "mh-mode":"multi-homing-mode", oper:"oper-state",itf:interface[]."ethernet-interface"|join(\' \',@), "ni-peers":association."network-instance"[]."_ni_peers"|join(\', \',@) }',
            "datatype": "state",
        }

        def set_es_peers(resp: List[Dict[str, Any]]) -> None:
            for bgp_inst in (
                resp[0]
                .get("system/network-instance/protocols/evpn/ethernet-segments", {})
                .get("bgp-instance", [])
            ):
                for es in bgp_inst.get("ethernet-segment", []):
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
        set_es_peers(resp)
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"es": res}
