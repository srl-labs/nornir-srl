from typing import Any, Dict, List, Tuple
import json
import difflib
import fnmatch

def strip_paths(d: Dict[str,Any]) -> Dict:
    """
    strips module name from path names

    Args:
        d: dictionary as returned by gnmi client
    
    Returns:
        dict: with module names stripped
    """
    stripped_d = dict()
    for path, val in d.items():
        el_list = path.split("/")
        stripped_p = "/".join([ el.split(":")[-1] for el in el_list])
        stripped_d[stripped_p] = val
        if isinstance(val, dict):
            stripped_d[stripped_p] = strip_paths(val)
    return stripped_d

def normalize_gnmi_resp(resp: Dict) -> List[Dict[str, Any]]:
    """
    remove gnmi notification and update envelopes from payload
    to make it comparable to intent struct

    Args:
        resp: dictionary as returned by gnmi client (get)
    
    Returns:
        dict: with notif and update envelopes removed
    """
    r = []
    for notif in resp.get("notification"):
        if "update" in notif:
            updates = [ upd for upd in notif.get("update")]
            for u in updates:
                if u.get("path"):
                    r.append( { u.get("path") : u.get("val") } )
                else:
                    r.append( u.get("val"))
        else:
            r.append({})
    return r

def diff_obj(
        a:Dict, 
        a_name: str,
        b:Dict,
        b_name: str) -> Tuple[bool, str]:
    """
    compares to dicts and show diff
    
    Args:
        a: dict to compare against b
        a_name: name of source of 'a' to show in diff output
        b: dict to compare against a
        b_name: name of source of 'b' to show in diff output

    Returns:
        Tuple(changed, diff-string)
            changed: indicates if a and b are different (True) or not (False)
            diff-string: string showing diffs beteen a and b
    """

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

def filter_dict(d: Dict, **kwargs: Any) -> Dict:
    f = dict()
    for k, v in kwargs.items():
        k = k.replace('_', '-')
        if k in d:
            if fnmatch.fnmatch(d[k], v):
                f[k] = d[k]
    return f

def filter_fields(d: Dict, *fields: str) -> Dict:
    return {
        k: v for k,v in d.items() 
            if k in [ f.replace('_', '-') for f in fields]
    }


def strip_modules(d: Dict) -> Dict:
    stripped = { k.split(':')[-1]:v for k, v in d.items() }
    for k, v in stripped.items():
        if isinstance(v, dict):
            stripped[k] = strip_modules(v)
        elif isinstance(v, list):
            stripped[k] = [strip_modules(d) for d in v if isinstance(d, dict)]
        else:
            pass
    return stripped


def _strip_modules(d: Dict) -> Dict:
    stripped = { k.split(':')[-1]:v for k, v in d.items() }
    for k, v in stripped.items():
        if isinstance(v, dict):
            stripped[k] = strip_modules(v)
    return stripped

