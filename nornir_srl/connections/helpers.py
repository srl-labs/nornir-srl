from typing import Any, Callable, Dict, List, Tuple, Optional
import json
import difflib
import re
import ipaddress


def first_payload(resp: Optional[List[Any]]) -> Dict[str, Any]:
    """The payload of a single-path gNMI Get response, or ``{}`` if there is none.

    SR Linux answers a Get for a subtree that holds nothing with an empty
    response, so indexing ``resp[0]`` is only safe once the device is known to
    have answered with data. Reports read paths that are legitimately empty all
    the time: a spine with no bridge table, a leaf that has not learned a MAC.
    """
    if not resp:
        return {}
    payload = resp[0]
    return payload if isinstance(payload, dict) else {}


def as_list(value: Any) -> List[Any]:
    """Normalize a YANG list that may arrive absent, as a single dict, or a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def instances_by_interface(
    get: Callable[..., List[Dict[str, Any]]],
    interfaces: str = "*",
) -> Dict[str, Tuple[str, ...]]:
    """Map ``<interface>.<index>`` to the network-instances that bind it.

    Only the interface lists are asked for. On the server a Get is served
    from what is subscribed, and a subscription to the whole
    network-instance subtree streams every node's BGP RIBs and statistics
    along with it - enough to fall behind on, and then everything on that
    node's stream goes stale.

    *interfaces* is a pattern the node matches the subinterface names to:
    ``irb*`` answers with the instances an IRB is in alone, where a fabric
    of a thousand bridge-domains binds a thousand other subinterfaces.
    """
    path = "/network-instance[name=*]/interface" + ("" if interfaces == "*" else f"[name={interfaces}]")
    resp = get(paths=[path], datatype="config")
    bound: Dict[str, List[str]] = {}
    for ni in as_list(first_payload(resp).get("network-instance")):
        if not isinstance(ni, dict):
            continue
        ni_name = str(ni.get("name", "") or "")
        if not ni_name:
            continue
        for itf in as_list(ni.get("interface")):
            name = itf.get("name") if isinstance(itf, dict) else itf
            if name:
                bound.setdefault(str(name), []).append(ni_name)
    return {name: tuple(nis) for name, nis in bound.items()}


def bgp_evpn_evis(ni: Any) -> Dict[str, str]:
    """The EVI of each ``bgp-evpn`` instance of a network-instance, by instance id.

    The EVI is the service identifier a router or bridge domain advertises its
    EVPN routes with, and the only thing that ties a virtual ethernet-segment
    to the network-instance it serves. A DCGW runs two instances with an EVI
    each, so the id has to be kept alongside.
    """
    if not isinstance(ni, dict):
        return {}
    protocols = ni.get("protocols")
    if not isinstance(protocols, dict):
        return {}
    bgp_evpn = protocols.get("bgp-evpn")
    if not isinstance(bgp_evpn, dict):
        return {}
    evis: Dict[str, str] = {}
    for position, inst in enumerate(as_list(bgp_evpn.get("bgp-instance")), start=1):
        if not isinstance(inst, dict):
            continue
        evi = inst.get("evi")
        if evi is None or str(evi) == "":
            continue
        iid = inst.get("id", inst.get("index"))
        key = str(iid) if iid is not None and str(iid) != "" else str(position)
        evis[key] = str(evi)
    return evis


def model_version(
    capabilities: Optional[Dict[str, Any]],
    *names: str,
    exact: bool = False,
) -> str:
    """The version of the first supported YANG model matching one of *names*.

    The gNMI paths a report needs differ between SR Linux releases, and this
    version is what picks between them. A device that does not advertise the
    model cannot serve the report at all, so name the model that is missing
    instead of failing on an empty list.
    """
    if capabilities is None:
        raise ValueError("no gNMI capabilities available for this device")
    for model in capabilities.get("supported_models") or []:
        if not isinstance(model, dict):
            continue
        model_name = model.get("name") or ""
        matched = any(
            name == model_name if exact else name in model_name for name in names
        )
        if matched and model.get("version"):
            return str(model["version"])
    raise ValueError(
        f"device advertises no version for the YANG model(s) {', '.join(names)}; "
        "it is probably running an unsupported release"
    )


def version_bucket(version_map: Dict[int, Any], version: str) -> int:
    """The lowest key of *version_map* whose version prefixes match *version*.

    Each key stands for one shape of the gNMI paths a report uses; the values are
    the model-version prefixes that shape applies to.
    """
    for bucket in sorted(version_map):
        prefixes = version_map[bucket]
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        if any(version.startswith(prefix) for prefix in prefixes):
            return bucket
    raise ValueError(f"unsupported YANG model version {version!r}")


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


_ORDER_PREFIX_RE = re.compile(r"^\d+_")


def clean_structured_key(key: Any) -> Any:
    """Normalize a column/field name for structured (JSON/YAML/CSV) output.

    Table reports prefix some field names with ``<n>_`` (e.g. ``0_st``,
    ``1_peer``) purely to control column ordering, and embed newlines in
    multi-line headers (e.g. ``"AF: EVPN\\nRx/Act/Tx"``). Neither is relevant
    for machine-readable output, so strip the ordering prefix and collapse
    whitespace/newlines for non-table formats.
    """
    if not isinstance(key, str):
        return key
    cleaned = _ORDER_PREFIX_RE.sub("", key)
    cleaned = re.sub(r"\s+", " ", cleaned.replace("\n", " ")).strip()
    return cleaned


def filter_fields(d: Dict, *fields: str) -> Dict:
    return {k: v for k, v in d.items() if k in [f.replace("_", "-") for f in fields]}


_MODULE_PREFIX = re.compile(r"srl_nokia-[^:]+:")


def strip_modules(d: Dict) -> Any:
    if isinstance(d, list):
        return [strip_modules(x) for x in d]
    elif isinstance(d, dict):
        return {strip_modules(k): strip_modules(v) for k, v in d.items()}
    elif isinstance(d, str):
        if d.startswith("srl_nokia-") and ":" in d:
            return _MODULE_PREFIX.sub("", d)
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
