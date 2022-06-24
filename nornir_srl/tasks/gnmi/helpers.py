from typing import Any, Dict, Tuple
import json
import difflib

def strip_paths(d: Dict[str,Any]) -> Dict:
    """
    strips module from path names
    """
    stripped_d = dict()
    for path, val in d.items():
        el_list = path.split("/")
        stripped_p = "/".join([ el.split(":")[-1] for el in el_list])
        stripped_d[stripped_p] = val
        if isinstance(val, dict):
            stripped_d[stripped_p] = strip_paths(val)
    return stripped_d

def normalize_gnmi_resp(resp: Dict) -> Dict:
    r = dict()
    for notif in resp.get("notification"):
        if "update" in notif:
            updates = [ upd for upd in notif.get("update")]
            for u in updates:
                if u.get("path"):
                    r[u["path"]] = u["val"]
    return r

def diff_obj(
        a:Dict, 
        a_name: str,
        b:Dict,
        b_name: str) -> Tuple[bool, str]:

    a_json = json.dumps(a, indent=2, sort_keys=True)
    b_json = json.dumps(b, indent=2, sort_keys=True)

    diff = ""
    for line in difflib.unified_diff(
        a_json.splitlines(keepends=True),
        b_json.splitlines(keepends=True),
        fromfile=a_name,
        tofile=b_name,
    ):
        diff += line
    if not diff == "":
        return (True, diff)
    else:
        return (False, "")
