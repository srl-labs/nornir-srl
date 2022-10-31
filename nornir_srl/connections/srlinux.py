from typing import TYPE_CHECKING, Any, List, Dict, Optional, Tuple
import difflib
import json
from deepdiff import DeepDiff
from natsort import natsorted

from pygnmi.client import gNMIclient

from nornir.core.plugins.connections import ConnectionPlugin
from nornir.core.configuration import Config

from nornir_srl.exceptions import *
from .helpers import strip_modules, normalize_gnmi_resp, filter_dict, filter_fields


CONNECTION_NAME = "srlinux"

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
        self.capabilities = self._connection.capabilities()

    def gnmi_get(self, **kw):
        return self._connection.get(**kw)

    def gnmi_set(self, **kw):
        return self._connection.set(**kw)

    def close(self) -> None:
        self._connection.close()

    def _get_resource(self, path_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
        result = dict()
        if self._connection:
            for spec in path_specs:
                resp = normalize_gnmi_resp(self._connection.get(path=[spec["path"]], datatype=spec["datatype"], encoding="json_ietf"))
                for d in resp:
                    val = list(d.values())[0]
                    if isinstance(val, dict):
                        f = filter_fields(val, *spec["fields"])
                        result.update(f)
                    elif isinstance(val, list):
                        for v in val:
                            f = filter_fields(v, *spec["fields"])
                            key = f.pop(spec["key"])
                            if key in result:
                                result[key].update(f)
                            else:
                                result[key] = f
                    else:
                        raise ValueError(f"Unexpected type {type(val)}: {val}")
        else:
            raise Exception("no active connection")
        return result               


    def get_info(self) -> Dict[str, Any]:
        path_specs = [
                { 
                    "path": "/platform/chassis",
                    "datatype": "state",
                    "fields": ['type', 'serial_number', 'part_number', 'hw_mac_address'],
                },
                {
                    "path": "/platform/control[slot=A]",
                    "datatype": "state",
                    "fields": ['software_version', ],
                }
        ]                

        result = self._get_resource(path_specs = path_specs)
        if result.get('software-version'):
            result['software-version'] = result['software-version'].split('-')[0]

        return result

    def get_itfs(self) -> Dict[str, Any]:
        path_specs = [
            {
                "path": "/interface",
                "datatype": "config",
                "fields": ['name', 'admin_state', 'subinterface'],
                "key": "name",
            },
            {
                "path": "/interface",
                "datatype": "state",
                "fields": ['name', 'mtu', 'oper_state'],
                "key": "name",
            }   
        ]

        return self._get_resource(path_specs = path_specs)

    def show_methods(self) -> List[str]:
        return [ method for method in dir(self) if callable(getattr(self, method)) 
                    and not method.startswith('__')]
    

    def get_config(self, paths:List[str], strip_mod:Optional[bool] = True) -> List[Dict[str, Any]]:
        if self._connection:
            resp = normalize_gnmi_resp(
                    self._connection.get(path=paths, datatype="config", encoding="json_ietf")
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
            ) -> str:

        device_cfg_after = []
        r_list = [ list(r.keys())[0] for r in input ]
        device_cfg_before = self.get_config(paths=r_list)

        if not dry_run:
            paths = []
            for d in input:
                for p, v in d.items():
                    ### to check - hack
                    ### to address intents that are lists, e.g. /interface
                    if isinstance(v, list): 
                        v = { p: v }
                        p = '/'.join(p.split('/')[:-1])
                        if len(p) == 0:
                            p = "/"
                    ###
                    paths.append((p, v))
            if op == 'update':
                r = self._connection.set(update=paths, encoding='json_ietf')
            elif op == 'replace':
                r = self._connection.set(replace=paths, encoding='json_ietf')
            else:
                raise ValueError(f"invalid value for parameter 'op': {op}")
            device_cfg_after = self.get_config(paths=r_list)
        else:
            device_cfg_after = input

        dd = DeepDiff(device_cfg_before, device_cfg_after)        
        diff = ""
        for i in range(len(r_list)):
            before_json = json.dumps(device_cfg_before[i], indent=2)
            after_json = json.dumps(device_cfg_after[i], indent=2)
            for line in difflib.unified_diff(
                        before_json.splitlines(keepends=True),
                        after_json.splitlines(keepends=True),
                        fromfile="before",
                        tofile="after", 
                        n=5,
            ):
                diff += line

        return diff


                
    


