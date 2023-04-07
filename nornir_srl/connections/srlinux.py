from typing import TYPE_CHECKING, Any, List, Dict, Optional, Tuple
import difflib
import json
import re

from deepdiff import DeepDiff
from natsort import natsorted
import jmespath

from pygnmi.client import gNMIclient

from nornir.core.plugins.connections import ConnectionPlugin
from nornir.core.configuration import Config

from .helpers import strip_modules, normalize_gnmi_resp, filter_fields, flatten_dict

CONNECTION_NAME = "srlinux"

class GnmiPath:
    RE_PATH_COMPONENT = re.compile(r'''
    (?P<pname>[^/[]+)  # gNMI path name
    (\[(?P<key>\w\D+)   # gNMI path key
    =
    (?P<value>[^\]]+)    # gNMI path value
    \])?
    ''', re.VERBOSE)

    def __init__(self, path:str):
        self.path = path.strip('/')
        self.comp = GnmiPath.RE_PATH_COMPONENT.findall(self.path) # list (1 item per path-el) of tuples (pname, [k=v], k, v)
        self.elems = [''.join(e[:2]) for e in self.comp]

    def __str__(self):
        return self.path
    
    def __repr__(self):
        return f"{self.__class__.__name__}('{self.path}')"
    
    @property
    def resource(self) -> Dict[str,str]:
        return {
            "resource": self.comp[-1][0], 
            "key": self.comp[-1][2], 
            "val": self.comp[-1][3],
        }

    @property
    def with_no_prefix(self):
        return GnmiPath('/'.join([e.split(':')[-1] for e in self.elems ]))

    @property
    def parent(self):
        if len(self.elems) > 0:
            return GnmiPath('/'.join(self.elems[:-1]))
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
                target=target,
                username=username,
                password=password,
                **extras
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
        path_specs = [
                { 
                    "path": "/platform/chassis",
                    "datatype": "state",
                    "fields": ['type', 'serial-number', 'part-number', 'hw-mac-address', 'last-booted'],
                },
                {
                    "path": "/platform/control[slot=A]",
                    "datatype": "state",
                    "fields": ['software-version', ],
                }
        ]                
        result = {}
        for spec in path_specs:
            resp = self.get(paths = [spec.get("path") ], datatype=spec["datatype"])
            for path in resp[0]:
                result.update({k:v for k,v in resp[0][path].items() if k in spec["fields"] } )
        if result.get('software-version'):
                result['software-version'] = result['software-version'].split('-')[0]

        return {'sys_info': [result] }

    def get_sum_subitf(self, interface:Optional[str] = '*' ) -> Dict[str, Any]:
        path_spec = {
                "path": f"/interface[name={interface}]/subinterface",
                "jmespath": 'interface[].{Itf:name, subitfs: subinterface[].{Subitf:name,\
                      type:type, admin:"admin-state",oper:"oper-state", \
                        ipv4: ipv4.address[]."ip-prefix", vlan: vlan.encap."single-tagged"."vlan-id"}}',
                "datatype": "state",
                "key": "index",
            }
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'subinterface': res }
    
    def get_sum_bgp(self, network_instance:Optional[str] = '*' ) -> Dict[str, Any]:
        path_spec = {
                "path": f"/network-instance[name={network_instance}]/protocols/bgp/neighbor",
                "jmespath": '"network-instance"[].{NetwInst:name, Neighbors: protocols.bgp.neighbor[].{"1_Peer":"peer-address",\
                    peer_as:"peer-as", state:"session-state",local_as:"local-as"[]."as-number",\
                    "2_Group":"peer-group"}}',
                "datatype": "state",
                "key": "index",
            }
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'bgp_peers': res }

    def get_lldp_sum(self, interface:Optional[str] = '*' ) -> Dict[str, Any]:
        path_spec = {
                "path": f"/system/lldp/interface[name={interface}]/neighbor",
                "jmespath": '"system/lldp".interface[].{interface:name, Neighbors:neighbor[].{"Nbr-port":"port-id",\
                    "Nbr-System":"system-name", "Nbr-port-desc":"port-description"}}',
                "datatype": "state",
            }
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'lldp_nbrs': res }    

    def get_mac_table(self, network_instance:Optional[str] = '*') -> Dict[str, Any]:
        path_spec = {
            "path": f"/network-instance[name={network_instance}]/bridge-table/mac-table/mac",
            "jmespath": '"network-instance"[].{"Netw-Inst":name, Fib:"bridge-table"."mac-table".mac[].{Address:address,\
                        Dest:destination, Type:type}}',
            "datatype": "state",
        }
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'mac_table': res}
    
    def get_rib_ipv4(self, network_instance:Optional[str] = '*' ) -> Dict[str, Any]:   

        path_spec = {
                "path": f"/network-instance[name={network_instance}]/route-table/ipv4-unicast",
                "jmespath": '"network-instance"[].{"Netw-Inst":name, Rib:"route-table"."ipv4-unicast".route[].{"Prefix":"ipv4-prefix",\
                    "next-hop":"_next-hop",type:"route-type", metric:metric, pref:preference, itf:"_nh_itf"}}',
                "datatype": "state",
                "key": "index",
            }

        nhgroups = self.get(paths = [ f"/network-instance[name={network_instance}]/route-table/next-hop-group[index=*]" ],
                        datatype="state")
        nhs = self.get(paths = [ f"/network-instance[name={network_instance}]/route-table/next-hop[index=*]" ],
                        datatype="state")

        nh_mapping = {}
        for ni in nhs[0].get("network-instance"):
            tmp_map = {}
            for nh in ni["route-table"]["next-hop"]:
                tmp_map[nh["index"]] = { "ip-address":      nh.get("ip-address"),
                                        "type":             nh.get("type"),
                                        "subinterface":    nh.get("subinterface"),
                }
                if "resolving-tunnel" in nh:
                    tmp_map[nh["index"]].update( 
                        { 
                            "tunnel": (nh.get("resolving-tunnel")).get("tunnel-type") + ":" +  (nh.get("resolving-tunnel")).get("ip-prefix")        
                        })
                if "resolving-route" in nh:
                    tmp_map[nh["index"]].update( 
                        { 
                            "resolving-route": (nh.get("resolving-route")).get("ip-prefix")        
                        })
                    
            nh_mapping.update( { ni["name"]: tmp_map})
        nhgroup_mapping = {}
        for ni in nhgroups[0].get("network-instance"):
            network_instance = ni["name"]
            tmp_map = {}
            for nhgroup in ni["route-table"]["next-hop-group"]:
#                    tmp_map[nhgroup["index"]] = [ nh["next-hop"] for nh in nhgroup["next-hop"] ]
                tmp_map[nhgroup["index"]] = [ 
                        nh_mapping[network_instance][nh.get("next-hop")] 
                                for nh in nhgroup["next-hop"] 
                        ]
            nhgroup_mapping.update( { ni["name"]: tmp_map})
        
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        for ni in resp[0].get("network-instance"):
            if len(ni["route-table"]["ipv4-unicast"])>0:
                for route in ni["route-table"]["ipv4-unicast"]["route"]:
                    if "next-hop-group" in route:
                        route["_next-hop"] = [ nh.get("ip-address") for nh in nhgroup_mapping[ni["name"]][route["next-hop-group"]] ]
                        route["_nh_itf"] = [ nh.get("subinterface") for nh in nhgroup_mapping[ni["name"]][route["next-hop-group"]] ]
                        
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'ipv4_rib': res }    
    
    def get_nwi_itf(self, nw_instance:Optional[str] = '*') -> Dict[str, Any]:
        path_spec = {
                "path": f"/network-instance[name={nw_instance}]",
                "jmespath": '"network-instance"[].{name:name,oper:"oper-state",type:type,itfs: interface[].{Subitf:name,\
                      "if-oper":"oper-state", "if-dwn-reason":"oper-down-reason","mac-learning":"oper-mac-learning"}}',
                "datatype": "state",
            }
        resp = self.get(paths = [ path_spec.get("path")], datatype=path_spec["datatype"])
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {'nwi_itfs': res }

    def get(
            self, 
            paths:List[str], 
            datatype: Optional[str] = "config", 
            strip_mod:Optional[bool] = True
        ) -> List[Dict[str, Any]]:

        if self._connection:
            resp = normalize_gnmi_resp(
                    self._connection.get(path=paths, datatype=datatype, encoding="json_ietf")
                    )
        else:
            raise Exception("no active connection")
        if strip_mod:
            return [ strip_modules(d) for d in resp ]
        else:
            return resp

    def set_config(
            self, 
            input: List[Dict[str, Any]], 
            op: Optional[str] = 'update',
            dry_run: Optional[bool] = False,
            strip_mod: Optional[bool] = True,
            ) -> str:

        device_cfg_after = []
        r_list = []
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
            if op == 'update':
                r = self._connection.set(update=paths, encoding='json_ietf')
            elif op == 'replace':
                r = self._connection.set(replace=paths, encoding='json_ietf')
            elif op == 'delete':
                delete_paths = [ list(p.keys())[0] for p in input ]
                r = self._connection.set(delete=delete_paths, encoding="json_ietf")
            else:
                raise ValueError(f"invalid value for parameter 'op': {op}")
            device_cfg_after = self.get(paths=r_list, datatype="config")
        else:
            device_cfg_after = input

        dd = DeepDiff(device_cfg_before, device_cfg_after)        
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

                
    

