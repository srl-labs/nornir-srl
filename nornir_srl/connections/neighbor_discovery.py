from __future__ import annotations

from typing import Any, Dict, List, Optional
import datetime
import jmespath


class NeighborDiscoveryMixin:
    """Mixin providing ARP and ND getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_arp(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", NI:"_ni"|to_string(@), entries:ipv4.arp.neighbor[].{IPv4:"ipv4-address",MAC:"link-layer-address",Type:origin,expiry:"_rel_expiry" }}',
            "datatype": "state",
        }
        ni_itfs = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_itf_map: Dict[str, List[str]] = {}
        for ni in ni_itfs[0].get("network-instance", []):
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
                    except Exception:
                        arp_entry["_rel_expiry"] = "-"
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"arp": res}

    def get_nd(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor",
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
                    except Exception:
                        nd_entry["_rel_expiry"] = "-"
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"nd": res}
