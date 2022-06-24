from typing import List, Dict, Optional, Tuple, Any
from copy import deepcopy
from pathlib import Path

import ruamel.yaml

from nornir.core.task import Task


class Intent:
    def __init__(
        self, 
        intent: Optional[Dict[str, Any]] = None,
        ) -> None:
        self.intent_data = intent


    @classmethod
    def from_files(
            cls,
            intent_dir: str
        ) -> "Intent":
        intent_data = []
        p = Path(intent_dir)
        for value_file in p.glob("**/*.yaml"):
            with open(value_file, "r") as f:
                yaml = ruamel.yaml.YAML(typ="safe")
                data = yaml.load(f)
                data["_metadata"] = {
                    "dir": str(value_file.parent)
                }
                intent_data.append(deepcopy(data))
#            for hostname in data:
#               if hostname in intent_data:
#                    intent_data[hostname].update(data[hostname])
#                else:
#                    intent_data[hostname] = deepcopy(data[hostname])
        return cls(intent=intent_data)

    def get (
            self,
            hostname:str=None) -> List[Dict]:
        return [ i.get(hostname) for i in self.intent_data ]
