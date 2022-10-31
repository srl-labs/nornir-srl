from typing import Any, List, Dict, Optional
from pathlib import Path
import logging

from natsort import natsorted

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from ruamel.yaml import YAML

from nornir.core.task import Task, Result
from nornir_srl.connections.srlinux import CONNECTION_NAME


def configure_device(
    task: Task, 
    intent_path: str, 
    dry_run: Optional[bool] = None,
    **kwargs: Any) -> Result:
    r = task.run(
        name="load intent vars", 
        severity_level=logging.DEBUG,
        task=load_vars, 
        path=intent_path 
    )
    vars = r.result

    r = task.run(
        name="Render templates",
        severity_level=logging.DEBUG,
        task=render_template,
        path = "intent/templates",
        **vars
    )

    op = 'replace'
    for i, data in enumerate(r.result):
        if isinstance(data, list):
            r = task.run(
                name=f"DRY-RUN:{dry_run} {op}:{[list(rsc.keys())[0] for rsc in data]} to device",
                severity_level=logging.INFO,
                task=set_config,
                device_config=data,
                op=op,
                dry_run=dry_run or task.is_dry_run(override=False),
            )


def render_template(task: Task, path: str, **kwargs: Any) -> Result:
    env = Environment(loader=FileSystemLoader(path), 
            undefined=StrictUndefined, trim_blocks=True,
            lstrip_blocks=True,
    )
    p = Path(path)
    txt = ""
    for file in p.glob("**/*.j2"):
        txt += "---\n"
        tpl = env.get_template(str(file.parts[-1]))
        txt += tpl.render(host=task.host, **kwargs)

    yml = YAML(typ="safe")
    r = yml.load_all(txt)

    return Result(host=task.host, result=list(r))

def load_vars(task: Task, path: str, **kwargs: Any) -> Result:

    intent = dict()
    intent.update(kwargs)

    p = Path(path)
    for file in natsorted( [ f for f in p.glob("**/*.y?ml") ] ):
        yml = YAML(typ="safe")
        for data in yml.load_all(file):
            if not 'metadata' in data:
                continue
            metadata = data.pop('metadata', {})
            if "hostname" in metadata and task.host.hostname != metadata["hostname"]:
                continue
            if "groups" in metadata and len(
                    [ g for g in task.host.groups 
                        if str(g) in metadata["groups"] ]
            ) == 0:
                continue
            if "labels" in metadata and len(
                { k:v for k,v in task.host.extended_data().items()
                    if metadata["labels"].get(k) == v }
            ) < len(metadata["labels"]):
                continue
            _merge(intent, data)
    
    return Result(host=task.host, result=intent)

def set_config(
    task: Task, 
    device_config: List[Dict[str, Any]], 
    dry_run: Optional[bool] = True,
    op: Optional[str] = None,
    ) -> Result:
    
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    r = device.set_config(input=device_config, op=op, dry_run=dry_run)
    if dry_run:
        changed=False
    else:
        if len(r) > 0:
            changed=True
        else:
            changed=False
    return Result(host=task.host, result=r, changed=changed)


def _merge(a, b):
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                _merge(a[key], b[key])
            elif isinstance(a[key], list) and isinstance(b[key], list):
                a[key].extend(b[key])
            else:
                pass  # a always wins
        else:
            a[key] = b[key]

