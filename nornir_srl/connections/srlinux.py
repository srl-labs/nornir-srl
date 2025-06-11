from typing import Any, List, Dict, Optional, Union
import difflib
import json
import re


from pygnmi.client import gNMIclient

from nornir.core.configuration import Config
from nornir.core.exceptions import ConnectionException

from .helpers import strip_modules, normalize_gnmi_resp
from .interfaces import NetworkInstanceMixin
from .routing import RoutingMixin
from .layer2 import Layer2Mixin
from .neighbor_discovery import NeighborDiscoveryMixin
from .system import SystemMixin

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


class SrLinux(
    NetworkInstanceMixin, RoutingMixin, Layer2Mixin, NeighborDiscoveryMixin, SystemMixin
):
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
