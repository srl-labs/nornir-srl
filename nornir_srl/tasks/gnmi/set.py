from importlib.resources import path
from typing import Optional, List, Dict, Tuple

from nornir.core.task import Task, Result
from .helpers import normalize_gnmi_resp, diff_obj, strip_paths

def gnmi_set (
    task: Task,
    paths: List[Tuple] = None,
    action: Optional[str] = None,
    dry_run: Optional[bool] = None,
    encoding: Optional[str] = None,
) -> Result:
    """
    sets config paths to specified value on device
    
    Args:
        task: Nornir Task object
        paths: list of (path, value) 
        action: replace or update config on device
        dry_run: compare config-state with new intent and show diff
                    no config is applied on the device
        encoding: 'json_ietf' or 'json'
    
    Returns:
        Result: Nornir Result object
    """
    gnmi_conn = task.host.get_connection("gnmi", task.nornir.config)
    diffs = ""
    changed=False
    for path_tuple in paths:
        p, v = path_tuple
        device_cfg = gnmi_conn.get(
            path=[p],
            datatype="config",
            encoding="json_ietf",
        )
        device_cfg = normalize_gnmi_resp(device_cfg)
        device_cfg = strip_paths(device_cfg)
        change, diff = diff_obj(
                        a=v, 
                        a_name=f"{p}@intent", 
                        b=device_cfg.get(p),
                        b_name=f"{p}@device")
        if change:
            changed=True
            diffs += diff
        if not dry_run and change:
            if action == "update":
                resp = gnmi_conn.set(update=paths, encoding=encoding)
            elif action == "replace":
                resp = gnmi_conn.set(replace=paths, encoding=encoding)
            else:
                raise ValueError(f"Invalid action:(action).")
    if dry_run:
        changed=False
    return Result(host=task.host, result=diff, changed=changed)

