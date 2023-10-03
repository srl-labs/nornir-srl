from typing import Any, Dict, List, Tuple, Optional, Union
import json
import difflib
import re
import ipaddress


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
    for notif in resp.get("notification", {}):
        if "update" in notif:
            updates = [upd for upd in notif.get("update")]
            for u in updates:
                if u.get("path"):
                    r.append({u.get("path"): u.get("val")})
                else:
                    if (
                        isinstance(u.get("val"), dict) and len(u["val"]) > 1
                    ):  # no path with multiple dicts in val: path='/', as per gnmi-spec
                        r.append({"/": u["val"]})
                    else:
                        r.append(
                            u["val"]
                        )  # a yang-list that gets a None path in SRL, e.g. /interface
        else:
            r.append({})
    return r


def lpm(ip_address: str, prefix_list: List[str]) -> str:
    """
    longest prefix match

    Args:
        ip_address: ip address to match (v6 or v4)
        prefix_list: list of prefixes to match against

    Returns:
        str: longest prefix matched
    """
    ip_addr = ipaddress.ip_address(ip_address)
    longest_pfx_str: str = ""

    max_pfx_len = -1
    for prefix in prefix_list:
        ip_pfx = ipaddress.ip_network(prefix)
        if ip_addr in ip_pfx:
            if ip_pfx.prefixlen > max_pfx_len:
                max_pfx_len = ip_pfx.prefixlen
                longest_pfx_str = str(ip_pfx)
    return longest_pfx_str


def diff_obj(a: Dict, a_name: str, b: Dict, b_name: str) -> Tuple[bool, str]:
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


def filter_fields(d: Dict, *fields: str) -> Dict:
    return {k: v for k, v in d.items() if k in [f.replace("_", "-") for f in fields]}


def strip_modules(d: Dict) -> Any:
    p = re.compile(r"srl_nokia-[^:]+:")

    if isinstance(d, list):
        return [strip_modules(x) for x in d]
    elif isinstance(d, dict):
        return {strip_modules(k): strip_modules(v) for k, v in d.items()}
    elif isinstance(d, str):
        if d.startswith("srl_nokia-") and ":" in d:
            #            return d[(d.index(":") + 1):]
            return re.sub(p, "", d)
        return d
    else:
        return d


# def strip_modules(d: Dict) -> Dict:
#    stripped = {}
#    for k,v in d.items():
#        k = '/'.join([e.split(':')[-1] for e in k.split('/')])
#        stripped[k] = copy.deepcopy(v)
#    for k, v in stripped.items():
#        if isinstance(v, dict):
#            stripped[k] = strip_modules(v)
#        elif isinstance(v, list):
#            stripped[k] = [strip_modules(d) for d in v if isinstance(d, dict)]
#        elif isinstance(v, str):
#            stripped[k] = v.split(':')[-1] if v.startswith('srl_nokia') else v
#    return stripped


def get_fields_at_depth(d: Dict, depth: int) -> Dict:
    if depth == 1:
        return {k: v for k, v in d.items() if isinstance(v, (str, int, float, list))}
    return {
        k: get_fields_at_depth(v, depth - 1)
        for k, v in d.items()
        if isinstance(v, dict)
    }


def flatten_dict(d: Dict) -> Dict:
    r = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            for e in v:
                tmp = flatten_dict(e)
                r.update({k + "_" + k2: v2 for k2, v2 in tmp.items()})
        else:
            r[k] = v
    return r
