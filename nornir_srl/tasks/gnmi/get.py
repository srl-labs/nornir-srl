from typing import Optional, List, Tuple, Dict, Any

from nornir.core.task import Task, Result
from .helpers import strip_paths, normalize_gnmi_resp

def gnmi_get(
    task: Task,
    paths: List[str] = None,
    type: Optional[str] = "config",
    strip_module: bool = False,
) -> Result:
    """
    Get config or state from the device with gNMI

    Args:
        task: Nornir `Task` object
        paths: list of gNMI paths to get
        type: path types: 'config', 'state', 'all' 'operational'
        strip_module: remove yang module names from paths

    Returns:
        Result: Nornir `Result` object
    
    """
    gnmi_conn = task.host.get_connection("gnmi", task.nornir.config)
    resp = gnmi_conn.get(
        path=paths,
        datatype=type,
        encoding="json_ietf"
    )
    r = normalize_gnmi_resp(resp)
    
    if strip_module:
        r = strip_paths(r)
    return Result(host=task.host, result=r, changed=False)
