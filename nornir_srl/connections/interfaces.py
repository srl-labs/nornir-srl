# Network instance related methods extracted from srlinux.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import jmespath


class NetworkInstanceMixin:
    """Mixin providing network-instance related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_nwi_itf(self, nw_instance: str = "*") -> Dict[str, Any]:
        SUBITF_PATH = "/interface[name=*]/subinterface"
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "jmespath": '"network-instance"[].{NI:name,oper:"oper-state",type:type,"router-id":protocols.bgp."router-id",\
                    itfs: interface[].{Subitf:name,"assoc-ni":"_other_ni","if-oper":"oper-state", "ip-prefix":*.address[]."ip-prefix",\
                        vlan:vlan.encap."single-tagged"."vlan-id", "mtu":"_mtu"}}',
            "datatype": "state",
        }
        subitf: Dict[str, Any] = {}
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

    def get_sum_subitf(self, interface: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name={interface}]/subinterface",
            "jmespath": 'interface[].{Itf:name, subitfs: subinterface[].{Subitf:name,                      type:type, admin:"admin-state",oper:"oper-state",                       ipv4: ipv4.address[]."ip-prefix", ipv6: ipv6.address[]."ip-prefix", vlan: vlan.encap."single-tagged"."vlan-id"}}',
            "datatype": "state",
            "key": "index",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"subinterface": res}
