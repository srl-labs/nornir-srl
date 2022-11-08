from typing import Any, List, Dict, Optional
from pathlib import Path
import json
import logging

from natsort import natsorted

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from ruamel.yaml import YAML

from nornir.core.task import Task, Result
from nornir_srl.connections.srlinux import CONNECTION_NAME


def configure_device(
    task: Task, 
    intent_path: str, 
    state_path: str,
    dry_run: Optional[bool] = None,
    **kwargs: Any
    ) -> Result:

    r = task.run(
        name=f"Load vars from {intent_path}", 
        severity_level=logging.DEBUG,
        task=load_vars, 
        path=intent_path 
    )
    vars = r.result

    r = task.run(
        name="Render templates",
        severity_level=logging.DEBUG,
        task=render_template,
        base_path = intent_path,
        **vars
    )

    r = task.run(
        name="Load YAML from template",
        severity_level=logging.DEBUG,
        task=load_yaml,
        doc=r.result
    )
    device_intent = r.result

    set_mode = 'replace'
    for data in device_intent:
        if isinstance(data, list):
            r = task.run(
                name=f"DRY-RUN:{dry_run} {set_mode}:{[list(rsc.keys())[0] for rsc in data]} to device",
                severity_level=logging.INFO,
                task=set_config,
                device_config=data,
                op=set_mode,
                dry_run=dry_run or task.is_dry_run(override=False),
            )
    
    r = task.run(
        name = "Check for purged resources",
        severity_level = logging.WARN,
        task=purge_resources,
        device_intent = device_intent,
        state_base_path = state_path,
        dry_run = dry_run,
    )

def purge_resources(
    task: Task, 
    device_intent: List[Dict[str, Any]], 
    state_base_path: str,
    dry_run: Optional[bool] = None,
    ) -> Result:
    
    new_rsc = dict()
    for l1 in device_intent:
        for l2 in l1:
            new_rsc.update(l2)
    
    state_file = Path(state_base_path) / f"{task.host.hostname}.json"
    if state_file.exists():
        with state_file.open(mode='r') as f:
            state = json.load(f)
        purged = { k:v for k,v in state.items() if k not in new_rsc }
    else:
        purged = {}
    if dry_run:
        changed = False
    else:
        if len(purged) > 0:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            purge_config = [ {r: purged[r]} for r in purged.keys()]
            r = device.set_config(input=purge_config, op='delete', dry_run=dry_run)
            purged = r
            changed = True
        else:
            changed = False
        state_file.open('w').write(json.dumps(new_rsc))

    return Result(host=task.host, result=purged, changed=changed)

def render_template(task: Task, base_path: str, **kwargs: Any) -> Result:

    p = Path(base_path) / "templates"
    env = Environment(loader=FileSystemLoader(str(p)), 
            undefined=StrictUndefined, trim_blocks=True,
            lstrip_blocks=True,
    )
    txt = ""
    for file in p.glob("*.j2"):
        txt += "---\n"
        tpl = env.get_template(str(file.parts[-1]))
        txt += tpl.render(host=task.host, **kwargs)

    return Result(host=task.host, result=txt)

def load_yaml(task: Task, doc: str) -> Any:
    
    yml = YAML(typ="safe")
    result = yml.load_all(doc)

    return Result(host=task.host, result=list(result))
        
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
