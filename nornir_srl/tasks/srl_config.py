import json
import logging
import re
import difflib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from natsort import natsorted
from nornir.core.task import Result, Task
from ruamel.yaml import YAML

from nornir_srl.connections.srlinux import CONNECTION_NAME

from .helpers import _merge


def configure_device(
    task: Task,
    intent_path: str,
    state_path: str,
    backup_path: str,
    dry_run: Optional[bool] = None,
    **kwargs: Any,
) -> Result:
    """
    A Nornir task that configures a device from an intent. It is meant to be called via the Nornir.run() method.
    It runs various sub-tasks to load vars from files, render templates, apply configuration and potentially delete
    configuration in case of purged resources from a previous version of intent.

    Args:
    task: A Nornir Task object that holds device details (e.g. connection)
    intent_path: path to the directory that holds the variables (intent)
    state_path: path to the directory to store state (i.e. to prune resourcesvthat are no longer part of the intent)
    backup_path: path to directory to hold config backups
    dry_run: boolean to indicate if this is a dry-run (dry-run == True) (no config changes applied to device)
    kwargs: optional key, value pairs to pass intent vars directly to the task

    Returns a Nornir Result object that holds the result of the outcome)
    """
    config_changed = False

    r = task.run(
        name=f"Load vars from {intent_path}",
        severity_level=logging.DEBUG,
        task=load_vars,
        path=intent_path,
    )
    vars = r.result

    r = task.run(
        name="Render templates",
        severity_level=logging.DEBUG,
        task=render_template,
        base_path=intent_path,
        **vars,
    )

    r = task.run(
        name="Load YAML from template",
        severity_level=logging.DEBUG,
        task=load_yaml,
        doc=r.result,
    )
    device_intent = r.result

    for data in device_intent:
        for set_mode, resources in data.items():
            if set_mode not in ("update", "replace"):
                raise ValueError(f"Unexpected set_mode: {set_mode}")
            if isinstance(resources, list):
                r = task.run(
                    name=f"DRY-RUN:{dry_run} {set_mode}:{[list(rsc.keys())[0] for rsc in resources]} to device",
                    severity_level=logging.INFO,
                    task=set_config,
                    device_config=resources,
                    op=set_mode,
                    dry_run=dry_run or task.is_dry_run(override=False),
                )
                config_changed |= r.changed

    r = task.run(
        name="Check for purged resources",
        severity_level=logging.WARN,
        task=purge_resources,
        device_intent=device_intent,
        state_base_path=state_path,
        dry_run=dry_run,
    )
    config_changed |= r.changed

    if config_changed:
        r = task.run(
            name=f"Backup configs to {backup_path}",
            severity_level=logging.INFO,
            task=backup_config,
            backup_base_path=backup_path,
        )
    return Result(host=task.host, result={}, changed=config_changed)


def backup_config(
    task: Task,
    backup_base_path: str,
    history_len: int = 10,
) -> None:
    p = Path(backup_base_path)
    suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    if task.host.hostname:
        p = p / task.host.hostname / f"{task.host.hostname}.{suffix}.json"
    else:
        raise ValueError(f"Hostname not set in task {task.name}")
    p.parent.mkdir(parents=True, exist_ok=True)

    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    cfg = device.get(paths=["/"], datatype="config", strip_mod=True)
    p.write_text(json.dumps(cfg, indent=4))

    backup_files = sorted(p.parent.glob("*.json"))
    if len(backup_files) > history_len:
        for f in backup_files[history_len:]:
            f.unlink()


def restore_config(
    task: Task,
    backup_base_path: str,
    version: int,
    dry_run: Optional[bool] = True,
) -> Result:
    p = Path(backup_base_path)
    if task.host.hostname:
        p = p / task.host.hostname
    else:
        raise ValueError(f"Hostname not set in task {task.name}")
    backup_files = sorted(p.glob("*.json"), reverse=True)
    if len(backup_files) < version:
        raise ValueError(
            f"Version {version} asked but only {len(backup_files)} versions available"
        )
    p = backup_files[version - 1]

    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    if dry_run:
        diff = ""
        before_json = json.dumps(
            device.get(paths=["/"], datatype="config", strip_mod=True), indent=4
        )
        after_json = p.read_text()
        for line in difflib.unified_diff(
            before_json.splitlines(keepends=True),
            after_json.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
        ):
            diff += line
        return Result(host=task.host, result=diff, changed=False)
    else:
        r = device.set_config(
            input=json.loads(p.read_text()), op="replace", dry_run=False
        )
        return Result(host=task.host, result=r, changed=len(r) > 0)


def purge_resources(
    task: Task,
    device_intent: List[Dict[str, Any]],
    state_base_path: str,
    dry_run: Optional[bool] = None,
) -> Result:
    new_rsc = dict()
    for l1 in device_intent:
        if l1.get("update", []):
            l2 = l1["update"]
        else:
            l2 = []
        if l1.get("replace", []):  # might be a dict entry with value None
            l2 += l1["replace"]
        for d in l2:
            new_rsc.update(d)

    state_file = Path(state_base_path) / f"{task.host.hostname}.json"
    if state_file.exists():
        with state_file.open(mode="r") as f:
            state = json.load(f)
        purged = {k: v for k, v in state.items() if k not in new_rsc}
    else:
        purged = {}
    if dry_run:
        changed = False
    else:
        if len(purged) > 0:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            purge_config = [{r: purged[r]} for r in purged.keys()]
            r = device.set_config(input=purge_config, op="delete", dry_run=dry_run)
            purged = r
            changed = True
        else:
            changed = False
        state_file.write_text(json.dumps(new_rsc, indent=4))

    return Result(host=task.host, result=purged, changed=changed)


def render_template(task: Task, base_path: str, **kwargs: Any) -> Result:
    p = Path(base_path) / "templates"
    env = Environment(
        loader=FileSystemLoader(str(p)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rendered = ""
    for file in natsorted(t for t in p.glob("*.j2")):
        tpl = env.get_template(str(file.parts[-1]))
        txt = tpl.render(host=task.host, **kwargs)
        if len(txt) > 0:
            rendered += f"---\n{txt}"

    return Result(host=task.host, result=rendered)


def load_yaml(task: Task, doc: str) -> Any:
    yml = YAML(typ="safe")
    result = yml.load_all(doc)

    return Result(host=task.host, result=list(result))


def load_vars(task: Task, path: str, **kwargs: Any) -> Result:
    intent = dict()
    intent.update(kwargs)

    def _render(matchobj):
        var = matchobj.group(2)

        return str(task.host.get(matchobj.group(2)))

    p = Path(path)
    for file in natsorted([f for f in p.glob("**/*.y?ml")]):
        yml = YAML(typ="safe")
        with open(file, "r") as f:
            y_str = f.read()
        y_str = re.sub(r"(__(\S+))", _render, y_str)
        for data in yml.load_all(y_str):
            if not "metadata" in data:
                continue
            metadata = data.pop("metadata", {})
            if "hostname" in metadata and task.host.hostname != metadata["hostname"]:
                continue
            if (
                "groups" in metadata
                and len([g for g in task.host.groups if str(g) in metadata["groups"]])
                == 0
            ):
                continue
            if "labels" in metadata and len(
                {
                    k: v
                    for k, v in task.host.extended_data().items()
                    if metadata["labels"].get(k) == v
                }
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
        changed = False
    else:
        if len(r) > 0:
            changed = True
        else:
            changed = False
    return Result(host=task.host, result=r, changed=changed)
