from typing import List, Dict, Optional, Tuple, Any
from copy import deepcopy
from pathlib import Path
import re

from ruamel.yaml import YAML
from ruamel.yaml.parser import ParserError

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from nornir.core.task import Task
from nornir.core.inventory import Host


class Intent:
    def __init__(
        self, 
        intent: Optional[Dict[str, Any]] = None,
        src_path: str = None,
        ) -> None:
        self.intent_data = intent
        self.j2_env = Environment(
            loader=FileSystemLoader(src_path),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined
        )


    @classmethod
    def from_files(
            cls,
            intent_dir: str
        ) -> "Intent":
        intent_data = {}
        p = Path(intent_dir)
        for value_file in p.glob("**/*.yaml"):
            with open(value_file, "r") as f:
                yaml = YAML(typ="safe")
                data = yaml.load(f)
            Intent._merge(intent_data, data)
        return cls(intent=intent_data, src_path=intent_dir)
    
    @staticmethod
    def _merge(a, b):
        for k in b:
            if k in a:
                if isinstance(a[k], dict) and isinstance(b[k], dict):
                    Intent._merge(a[k], b[k])
                elif isinstance(a[k], list) and isinstance(b[k], list):
                    for e in b[k]:
                        a[k].append(e)
                else:
                    pass
            else:
                a[k] = b[k]
    
    def get_values(self, device:str=None, path:str = None) -> Dict:
        if not device in self.intent_data:
            return {}
        p1 = self.intent_data[device]
        if not path:
            return deepcopy(p1)
        path_list = re.findall(r"\w+(?:\[[^\]]+\])?", path.strip("/"))
        d = {}
        ptr = d
        for el in path_list:
            if el == "":
                continue
            m = re.match(r"(\w+)\[(\w+)=(\S+)\]", el)
            if m:
                found = False
                resource, key, val = m.group(1, 2, 3)
                if isinstance(p1.get(resource), list):
                    for rsc in p1[resource]:
                        if rsc.get(key) == val:
                            p1 = rsc
                            found = True
                else:
                    raise ValueError(f"intent has no list of resources for element {el} in {path}")
                if not found:
                    return {}
                ptr[resource] = [{}]
                ptr = ptr[resource][0]
            else:
                if el in p1:
                    if isinstance(p1[el], list):
                        ptr[el] = [ r for r in p1[el] ]
                        return ptr 
                    ptr[el] = {}
                    ptr = ptr[el]
                else:
                    return {}
        ptr.update(p1)
        return ptr

    def merge(self, device1:str, device2:str) -> Dict:
        d1 = self.get_values(device=device1)
        d2 = self.get_values(device=device2)
        Intent._merge(d1, d2)
        return d1

    def get(self, device:str = None) -> List[Dict]:
        """
        returns per-device intent via template rendering
        
        Args:
            device: name of device or pseudo-device
            
        Returns:
            list of device intents
        """
        yml = YAML(typ="safe")
        data = []
        for tpl in sorted(self.j2_env.list_templates(extensions="j2")):
            t = self.j2_env.get_template(tpl)
            text = t.render(**self.get_values(device=device))
            try:
                d = yml.load(text)
            except ParserError as e:
                raise ParserError(f"Cannot parse {tpl}: {e}")
            if d:
                data += d
        return data

            
