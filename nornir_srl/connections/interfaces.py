# Network instance related methods extracted from srlinux.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import jmespath

from ..records import (
    BgpVpnInstance,
    Interface,
    Lag,
    LagMember,
    NetworkInstance,
    Subinterface,
    SubinterfaceState,
    as_int,
)
from .down_reason import ParentReasons
from .helpers import as_list, bgp_evpn_evis, first_payload


def _uses_vpn_policy(ni: Dict[str, Any]) -> bool:
    """Whether a bgp-vpn instance of *ni* takes its route-targets from a policy."""
    bgp_vpn = ((ni.get("protocols") or {}).get("bgp-vpn")) or {}
    return any(
        isinstance(inst, dict) and (inst.get("import-policy") or inst.get("export-policy"))
        for inst in as_list(bgp_vpn.get("bgp-instance"))
    )


def _referenced_sets(node: Any) -> List[str]:
    """Every extended-community-set named anywhere under *node*."""
    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("extended-community-set", "referenced-sets"):
                found.extend(str(v) for v in as_list(value) if isinstance(v, (str, int)))
            else:
                found.extend(_referenced_sets(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_referenced_sets(value))
    return found


def _policy_route_targets(routing_policy: Dict[str, Any], names: List[str], direction: str) -> Tuple[str, ...]:
    """The route-targets policies *names* give a bgp-vpn instance.

    With an export policy, SR Linux advertises with whatever route-targets the
    policy adds - the instance's own ``export-rt`` no longer applies - so they
    are read from its accepting statements' extended-community actions. An
    import policy takes in routes whose route-targets an accepting statement
    matches on. A statement that rejects contributes neither way.
    """
    sets = {
        str(s.get("name")): [str(m) for m in as_list(s.get("member"))]
        for s in as_list(routing_policy.get("extended-community-set"))
        if isinstance(s, dict)
    }
    policies = {
        str(p.get("name")): p for p in as_list(routing_policy.get("policy")) if isinstance(p, dict)
    }
    targets = set()
    for name in names:
        policy = policies.get(name) or {}
        for statement in [*as_list(policy.get("statement")), {"action": policy.get("default-action") or {}}]:
            if not isinstance(statement, dict):
                continue
            action = statement.get("action") or {}
            if str(action.get("policy-result") or "").endswith("reject"):
                continue
            where = (action.get("bgp") or {}) if direction == "export" else (statement.get("match") or {})
            for set_name in _referenced_sets(where):
                targets.update(
                    m.replace("target:", "") for m in sets.get(set_name, []) if m.startswith("target:")
                )
    return tuple(sorted(targets))


def _route_targets(
    inst: Dict[str, Any], direction: str, routing_policy: Optional[Dict[str, Any]] = None
) -> Tuple[str, ...]:
    """The ``import`` or ``export`` route-targets of one bgp-vpn instance.

    Where a policy sets them instead of a target list, they are read from the
    policy in *routing_policy*; failing that - no policies read, or one that
    names no route-target - the policy's name is what there is to show.
    """
    policy = inst.get(f"{direction}-policy")
    if policy:
        names = [str(p) for p in as_list(policy)]
        return _policy_route_targets(routing_policy or {}, names, direction) or tuple(names)
    targets = []
    for rt in as_list((inst.get("route-target") or {}).get(f"{direction}-rt")):
        target = rt.get("target") if isinstance(rt, dict) else rt
        if target:
            targets.append(str(target).replace("target:", ""))
    return tuple(sorted(set(targets)))


def _bgp_vpn_instances(
    bgp_vpn: Dict[str, Any], routing_policy: Optional[Dict[str, Any]] = None
) -> Tuple[BgpVpnInstance, ...]:
    """The bgp-vpn instances of a network-instance, each with its own targets."""
    return tuple(
        BgpVpnInstance(
            id=as_int(inst.get("id")) or index,
            import_rts=_route_targets(inst, "import", routing_policy),
            export_rts=_route_targets(inst, "export", routing_policy),
            rd=str((inst.get("route-distinguisher") or {}).get("rd") or ""),
        )
        for index, inst in enumerate(as_list(bgp_vpn.get("bgp-instance")), start=1)
        if isinstance(inst, dict)
    )


def _prefixes(family: Any) -> Tuple[str, ...]:
    """The addresses configured under an ``ipv4`` or ``ipv6`` container."""
    if not isinstance(family, dict):
        return ()
    return tuple(
        str(addr.get("ip-prefix"))
        for addr in as_list(family.get("address"))
        if isinstance(addr, dict) and addr.get("ip-prefix")
    )


class NetworkInstanceMixin:
    """Mixin providing network-instance related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_nwi_itf(self, nw_instance: str = "*") -> Dict[str, Any]:
        SUBITF_PATH = "/interface[name=*]/subinterface"
        subitf: Dict[str, Dict[str, Any]] = {}
        resp = self.get(paths=[SUBITF_PATH], datatype="all")
        for itf in as_list(first_payload(resp).get("interface")):
            for si in as_list(itf.get("subinterface")):
                subitf[f"{itf.get('name', '')}.{si.get('index', '')}"] = si

        resp = self.get(paths=[f"/network-instance[name={nw_instance}]"], datatype="all")
        ni_list = [
            ni for ni in as_list(first_payload(resp).get("network-instance")) if isinstance(ni, dict)
        ]
        # Route-targets a policy sets are only known from the policy itself;
        # read once, and only when some instance has one.
        # A failed read leaves the policies named rather than the report down.
        routing_policy: Dict[str, Any] = {}
        if any(_uses_vpn_policy(ni) for ni in ni_list):
            try:
                resp = self.get(paths=["/routing-policy"], datatype="config")
                routing_policy = first_payload(resp).get("routing-policy") or {}
            except Exception:  # noqa: BLE001 - the instances are still worth reporting
                routing_policy = {}

        # interface -> the network-instances it is bound to, which is how an
        # irb names the ip-vrf a mac-vrf routes into.
        bound: Dict[str, List[str]] = {}
        for ni in ni_list:
            for itf in as_list(ni.get("interface")):
                if isinstance(itf, dict) and itf.get("name"):
                    bound.setdefault(str(itf["name"]), []).append(str(ni.get("name", "")))

        records = []
        for ni in ni_list:
            name = str(ni.get("name", ""))
            protocols = ni.get("protocols") or {}
            bgp_vpn = protocols.get("bgp-vpn") or {}
            interfaces = []
            for itf in as_list(ni.get("interface")):
                if not isinstance(itf, dict):
                    continue
                itf_name = str(itf.get("name", ""))
                details = subitf.get(itf_name, {})
                interfaces.append(
                    Subinterface(
                        name=itf_name,
                        oper=str(details.get("oper-state") or itf.get("oper-state") or ""),
                        prefixes=tuple(
                            str(p) for p in jmespath.search('*.address[]."ip-prefix"', details) or []
                        ),
                        mtu=as_int(details.get("l2-mtu") if "l2-mtu" in details else details.get("ip-mtu")),
                        vlan=as_int(
                            jmespath.search('vlan.encap."single-tagged"."vlan-id"', details)
                        ),
                        associated=tuple(
                            other for other in bound.get(itf_name, []) if other != name
                        )
                        if itf_name.startswith("irb")
                        else (),
                    )
                )
            records.append(
                NetworkInstance(
                    name=name,
                    type=str(ni.get("type") or ""),
                    oper=str(ni.get("oper-state") or ""),
                    router_id=str((protocols.get("bgp") or {}).get("router-id") or ""),
                    overlays=tuple(
                        str(v.get("name", ""))
                        for v in as_list(ni.get("vxlan-interface"))
                        if isinstance(v, dict)
                    ),
                    # The EVI the service advertises with, which is also what a
                    # virtual ethernet-segment names to say which
                    # network-instance it serves.
                    evis=tuple(bgp_evpn_evis(ni).values()),
                    instances=_bgp_vpn_instances(bgp_vpn, routing_policy),
                    interfaces=tuple(interfaces),
                )
            )
        return {"nwi_itfs": records}

    def get_lag(self, lag_id: str = "*") -> Dict[str, Any]:
        resp = self.get(paths=[f"/interface[name=lag{lag_id}]"], datatype="all")
        records = []
        for itf in as_list(first_payload(resp).get("interface")):
            if not isinstance(itf, dict):
                continue
            lag = itf.get("lag") or {}
            lacp = lag.get("lacp") or {}
            records.append(
                Lag(
                    name=str(itf.get("name") or ""),
                    oper=str(itf.get("oper-state") or ""),
                    mtu=as_int(itf.get("mtu")),
                    min_links=as_int(lag.get("min-links")),
                    description=str(itf.get("description") or ""),
                    type=str(lag.get("lag-type") or ""),
                    speed=as_int(lag.get("lag-speed")),
                    standby_signaling=str((itf.get("ethernet") or {}).get("standby-signaling") or ""),
                    lacp_key=as_int(lacp.get("admin-key")),
                    lacp_interval=str(lacp.get("interval") or ""),
                    lacp_mode=str(lacp.get("lacp-mode") or ""),
                    lacp_system_id=str(lacp.get("system-id-mac") or ""),
                    lacp_priority=as_int(lacp.get("system-priority")),
                    members=tuple(
                        LagMember(
                            name=str(member.get("name") or ""),
                            oper=str(member.get("oper-state") or ""),
                            activity=str((member.get("lacp") or {}).get("activity") or ""),
                        )
                        for member in as_list(lag.get("member"))
                        if isinstance(member, dict)
                    ),
                )
            )
        return {"lag": records}

    def get_sum_subitf(self, interface: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name={interface}]/subinterface",
            "datatype": "all",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )

        # resp[0] is usually a dict like {'interface[name=...]': {...}} or {'interface': [...]}
        itf_list = []
        if resp and isinstance(resp[0], dict):
            for k, v in resp[0].items():
                if k.startswith("interface"):
                    if isinstance(v, list):
                        itf_list.extend(v)
                    elif isinstance(v, dict):
                        # For specific interface name, v is {'subinterface': [...]}
                        # and we might need to add back the name if it's missing from the dict
                        if "name" not in v:
                            if "[" in k and "]" in k:
                                v["name"] = k.split("[name=")[1].split("]")[0]
                        itf_list.append(v)

        # A subinterface reports itself 'port-down' without saying what is wrong
        # with the port, so the reason worth showing lives one level up.
        parents = ParentReasons(self.get)

        records = []
        for itf in itf_list:
            itf_name = str(itf.get("name", ""))
            subinterfaces = []
            for si in as_list(itf.get("subinterface")):
                if not isinstance(si, dict):
                    continue
                # Construct proper subinterface name
                index = si.get("index", "")
                si_name = str(si.get("name", "") or "")
                if not si_name:
                    si_name = f"{itf_name}.{index}"
                elif si_name.isdigit():
                    si_name = f"{itf_name}.{si_name}"

                # A port held in standby by its ethernet-segment is down on
                # purpose, so its subinterfaces are called what they are rather
                # than counted as faults.
                own_reason = si.get("oper-down-reason")
                oper = parents.state(si.get("oper-state"), si_name, own_reason)

                subinterfaces.append(
                    SubinterfaceState(
                        name=si_name,
                        type=str(si.get("type") or ""),
                        admin=str(si.get("admin-state") or ""),
                        oper=oper,
                        down_reason="" if oper == "up" else parents.resolve(si_name, own_reason),
                        ip_mtu=as_int(si.get("ip-mtu")),
                        vlan=as_int(jmespath.search('vlan.encap."single-tagged"."vlan-id"', si)),
                        ipv4=_prefixes(si.get("ipv4")),
                        ipv6=_prefixes(si.get("ipv6")),
                    )
                )
            records.append(Interface(name=itf_name, subinterfaces=tuple(subinterfaces)))

        return {"subinterface": records}
