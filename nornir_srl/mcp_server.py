"""
MCP Server for fcli - exposes SR Linux fabric reports as MCP tools.

Supports both stdio and HTTP (SSE) transports.

Usage:
    # stdio transport (default)
    fcli-mcp

    # HTTP transport
    fcli-mcp --transport http --host 0.0.0.0 --port 8080

    # With containerlab topology file
    fcli-mcp --topo-file <path-to-topo.yml>

    # With nornir config file
    fcli-mcp --config-file nornir_config.yaml

    # With inventory filter
    fcli-mcp --topo-file <path> --inv-filter role=leaf --inv-filter site=dc1
"""

import argparse
import atexit
import glob
import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Literal

import yaml  # type: ignore[import-untyped]
from mcp.server.fastmcp import FastMCP
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task

from . import clab
from .changes import WATCH_REPORTS
from .checks import CHECKS, collect_fabric_state, run_checks
from .connections.srlinux import CONNECTION_NAME
from .connections.helpers import clean_structured_key
from .fabric import collect_fabric_state as collect_lens_state
from .lenses import get_lens
from .records import as_dict
from .reports import ReportSpec, get_report
from .rows import extract

logger = logging.getLogger(__name__)

SRL_DEFAULT_GNMI_PORT = clab.SRL_DEFAULT_GNMI_PORT
NORNIR_DEFAULT_CONFIG = clab.NORNIR_DEFAULT_CONFIG


# ---- Nornir initialization ----

# These hold the initialized nornir instance and persistent temp files
_nornir_instance: Optional[Nornir] = None
_temp_files: List[Any] = []  # prevent GC of NamedTemporaryFile objects


def _cleanup_temp_files() -> None:
    """Clean up any temporary files created during initialization."""
    for f in _temp_files:
        try:
            if os.path.exists(f.name):
                os.unlink(f.name)
        except Exception as e:
            logger.debug("Failed to clean up temp file %s: %s", f.name, e)


atexit.register(_cleanup_temp_files)


def _init_nornir_from_topo(
    topo_file: str,
    cert_file: Optional[str] = None,
    gnmi_port: int = SRL_DEFAULT_GNMI_PORT,
) -> Nornir:
    """Initialize Nornir from a containerlab topology file."""
    with open(topo_file, "r") as f:
        topo = yaml.safe_load(os.path.expandvars(f.read()))

    hosts = clab.srl_hosts(topo)
    groups = clab.srl_groups(gnmi_port, cert_file)

    hosts_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    yaml.safe_dump(hosts, hosts_f)
    hosts_f.seek(0)
    _temp_files.append(hosts_f)

    groups_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    yaml.safe_dump(groups, groups_f)
    groups_f.seek(0)
    _temp_files.append(groups_f)

    conf: Dict[str, Any] = dict(NORNIR_DEFAULT_CONFIG)
    conf["inventory"] = {
        "options": {
            "host_file": hosts_f.name,
            "group_file": groups_f.name,
        }
    }
    return InitNornir(**conf)


def _init_nornir_from_config(config_file: str) -> Nornir:
    """Initialize Nornir from a nornir config file."""
    return InitNornir(config_file=config_file)


def get_nornir() -> Nornir:
    """Get the initialized Nornir instance."""
    global _nornir_instance
    if _nornir_instance is None:
        raise RuntimeError(
            "Nornir not initialized. Use 'load_topology' or 'load_config' tools to "
            "initialize a fabric, or check if available topologies exist with 'list_topologies'."
        )
    return _nornir_instance


# ---- Report plumbing ----


def _parse_filters(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> tuple:
    """Parse comma-separated key=value filter strings into dicts."""

    def parse(spec: Optional[str]) -> Optional[Dict[str, str]]:
        if not spec:
            return None
        parsed: Dict[str, str] = {}
        for part in spec.split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                parsed[key.strip()] = value.strip()
        return parsed

    return parse(inv_filter), parse(field_filter)


def _error_row(_node: str, exception: Optional[BaseException]) -> Dict[str, Any]:
    """Failed hosts are reported in-band, so an agent sees why a node is missing."""
    return {"_error": str(exception)}


def _query(spec: ReportSpec, inv_filter: Optional[Dict[str, str]], **params: Any):
    """Run a report's getter across the filtered inventory."""

    def task_func(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=spec.getter(device, **params))

    nornir = get_nornir()
    target = nornir.filter(**inv_filter) if inv_filter else nornir
    return target.run(task=task_func, name=spec.resource, raise_on_error=False)


def _run_report(
    name: str,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
    **params: Any,
) -> str:
    """Run a report from the registry and return what it found as JSON.

    A report that returns records is emitted as those records, each saying
    which node it is from; one that does not yet is emitted as its table rows.
    """
    spec = get_report(name)
    i_filter, f_filter = _parse_filters(inv_filter, field_filter)
    result = _query(spec, i_filter, **params)
    table = spec.table_for(params)
    _columns, per_node = extract(
        spec.resource, result, field_filter=f_filter, on_error=_error_row, table=table
    )
    if table is not None:
        objects: List[Dict[str, Any]] = []
        for node in per_node:
            if node.records:
                objects.extend({"node": node.node, **as_dict(r)} for r in node.records)
            else:
                # A node whose report failed has a row saying so, and no records.
                objects.extend({"node": node.node, **row.values} for row in node.rows)
        return json.dumps(objects, indent=2, default=str)
    rows = [
        {"Node": node.node, **row.values} for node in per_node for row in node.rows
    ]
    return json.dumps(
        [{clean_structured_key(k): v for k, v in row.items()} for row in rows],
        indent=2,
        default=str,
    )


# ---- MCP Server definition ----

mcp = FastMCP(
    "fcli",
    instructions=(
        "MCP server for Nokia SR Linux fabric analysis via fcli/nornir-srl. "
        "Provides tools to query operational state of SR Linux devices in a containerlab or production fabric. "
        "All tools return structured JSON data. "
        "INVENTORY FILTERS (inv_filter): inv_filter matches against node labels defined in the containerlab "
        "topology file. Only keys present in a node's 'labels:' section can be used as filter keys. "
        "For example, 'role=leaf' only works if the topology file defines 'labels: {role: leaf}' on nodes. "
        "If labels are absent or the key does not exist, inv_filter returns NO results. "
        "Use 'show_topology' first to see available nodes and their filterable label keys before applying inv_filter. "
        "If no labels are available, omit inv_filter to target all nodes. "
        "FIELD FILTERS (field_filter): use field_filter to filter output rows (e.g. 'state=up'). "
        "field_filter values are regex patterns matched case-insensitively against field values. "
        "inv_filter supports wildcards (*, ?). Both accept comma-separated key=value pairs. "
        "Topologies can be loaded at runtime using 'load_topology' or 'load_config'."
    ),
)


@mcp.tool()
def list_topologies(directory: str = ".") -> str:
    """List available containerlab topology files and nornir configs in a directory.

    Args:
        directory: Directory to search (default: current directory).
    """
    clab_patterns = [
        os.path.join(directory, "**/*.clab.yml"),
        os.path.join(directory, "**/*.clab.yaml"),
        os.path.join(directory, "**/clab-*.yml"),
        os.path.join(directory, "**/clab-*.yaml"),
    ]
    nornir_patterns = [
        os.path.join(directory, "**/nornir_config.yaml"),
        os.path.join(directory, "**/nornir_config*.yaml"),
    ]

    clab_files = set()
    for p in clab_patterns:
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f):
                clab_files.add(os.path.abspath(f))

    nornir_files = set()
    for p in nornir_patterns:
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f):
                nornir_files.add(os.path.abspath(f))

    results = {
        "containerlab_topologies": sorted(list(clab_files)),
        "nornir_configs": sorted(list(nornir_files)),
    }
    return json.dumps(results, indent=2)


@mcp.tool()
def load_topology(
    topo_file: str,
    cert_file: Optional[str] = None,
    inv_filter: Optional[str] = None,
    gnmi_port: int = SRL_DEFAULT_GNMI_PORT,
) -> str:
    """Initialize or switch the active fabric from a containerlab topology file.

    Args:
        topo_file: Path to the containerlab .yml file.
        cert_file: Optional path to the TLS certificate file.
        inv_filter: Optional inventory filter as comma-separated key=value pairs (e.g. 'role=leaf').
            Only keys defined in node 'labels:' in the topology file can be used.
            If labels are not defined on nodes, omit this parameter.
        gnmi_port: gNMI port for SR Linux nodes (default: 57400). EDA-deployed labs typically use 57410.
    """
    global _nornir_instance
    _nornir_instance = _init_nornir_from_topo(topo_file, cert_file, gnmi_port)

    all_label_keys: set = set()
    for host in _nornir_instance.inventory.hosts.values():
        if host.data:
            all_label_keys.update(host.data.keys())

    if inv_filter:
        i_filt, _ = _parse_filters(inv_filter=inv_filter)
        if i_filt:
            _nornir_instance = _nornir_instance.filter(**i_filt)

    label_info = (
        f" Available inv_filter keys (from node labels): {sorted(all_label_keys)}."
        if all_label_keys
        else " No labels defined on nodes; inv_filter will not match any nodes."
    )
    return (
        f"Fabric initialized from {topo_file}. "
        f"{len(_nornir_instance.inventory.hosts)} nodes matched filter.{label_info}"
    )


@mcp.tool()
def load_config(
    config_file: str,
    inv_filter: Optional[str] = None,
) -> str:
    """Initialize or switch the active fabric from a Nornir config file.

    Args:
        config_file: Path to the nornir_config.yaml file.
        inv_filter: Optional inventory filter as comma-separated key=value pairs.
            Matches against host data attributes. Use 'show_topology' to see available keys.
    """
    global _nornir_instance
    _nornir_instance = _init_nornir_from_config(config_file)

    if inv_filter:
        i_filt, _ = _parse_filters(inv_filter=inv_filter)
        if i_filt:
            _nornir_instance = _nornir_instance.filter(**i_filt)

    return f"Fabric initialized from {config_file}. {len(_nornir_instance.inventory.hosts)} nodes matched filter."


@mcp.tool()
def show_topology() -> str:
    """Show the currently loaded fabric: nodes, their labels, and available inv_filter keys.

    Use this to discover which inv_filter keys are available before filtering.
    Labels originate from the 'labels:' section of nodes in the containerlab topology file.
    Only label keys present here can be used in inv_filter; if no labels exist, omit inv_filter.
    """
    nornir = get_nornir()
    nodes = {}
    all_label_keys: set = set()
    for name, host in nornir.inventory.hosts.items():
        labels = dict(host.data) if host.data else {}
        nodes[name] = {"labels": labels}
        all_label_keys.update(labels.keys())
    return json.dumps(
        {
            "node_count": len(nodes),
            "available_inv_filter_keys": (
                sorted(all_label_keys) if all_label_keys else []
            ),
            "hint": (
                "Use these keys in inv_filter (e.g. 'role=leaf'). "
                "If available_inv_filter_keys is empty, no labels are defined and inv_filter will match nothing."
            ),
            "nodes": nodes,
        },
        indent=2,
    )


@mcp.tool()
def sys_info(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get system information for SR Linux nodes.

    Returns one object per node: node, type (the chassis), serial_number,
    part_number, hw_mac_address, last_booted and software_version (the
    release alone, e.g. 26.7.1).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("sys_info", inv_filter, field_filter)


@mcp.tool()
def bgp_peers(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get BGP peer status and route statistics for all network instances.

    Returns one object per network-instance per node: node, ni and neighbors,
    each with peer, state, peer_as, local_as, local_address, local_port, group,
    dynamic, bfd, fast_failover, import_policies, export_policies and families.
    A family (ipv4-unicast, ipv6-unicast, evpn, l3vpn-ipv4-unicast,
    l3vpn-ipv6-unicast) is listed only where the session is configured for it,
    with enabled, oper, received, active and sent route counts.

    state is the session state as the device reports it (established, active,
    idle, connect...). The table shows an established session as 'up', and
    field_filter matches the table, so filter on 'state=up', not established.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("bgp_peers", inv_filter, field_filter)


@mcp.tool()
def bgp_rib(
    route_fam: Literal[
        "evpn",
        "ipv4",
        "ipv6",
        "l3vpn-v4",
        "l3vpn-v6",
        "l3vpn-ipv4-unicast",
        "l3vpn-ipv6-unicast",
    ],
    route_type: Optional[Literal["1", "2", "3", "4", "5"]] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get BGP RIB (Routing Information Base) entries.

    Returns one object per network-instance per node: node, ni, family,
    route_type (EVPN only) and routes. A route carries the peer it came from
    (neighbor), its status (used, valid, best, tie_break), the NLRI fields its
    family and type have (rd, prefix, esi, tag, mac, ip, gateway, vni, label1,
    label2) and every path attribute: next_hop, origin, local_pref, med,
    as_path, communities / large_communities / ext_communities as carried, and
    read out of the extended ones route_targets, esi_labels, soo and
    tunnel_encap; plus domain_path (D-PATH), internal_tags and neighbor_as.
    Useful for diagnosing EVPN/IP-VPN loop-prevention and route-leaking issues.

    Args:
        route_fam: BGP RIB address family: evpn, ipv4, ipv6, or L3VPN IPv4/IPv6 unicast
            (``l3vpn-v4`` / ``l3vpn-v6`` short names, or ``l3vpn-ipv4-unicast`` / ``l3vpn-ipv6-unicast``).
        route_type: Route type for EVPN (1-5). Only applicable when route_fam='evpn'.
            1=Ethernet Auto-Discovery, 2=MAC/IP, 3=Inclusive Multicast, 4=ES, 5=IP Prefix.
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report(
        "bgp_rib",
        inv_filter,
        field_filter,
        route_fam=route_fam,
        route_type=route_type,
        detail=True,
    )


@mcp.tool()
def ipv4_rib(
    address: Optional[str] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv4 routing table entries.

    Returns one object per network-instance per node: node, ni and routes,
    each with prefix, type (local, host, bgp, bgp-evpn, static, arp-nd...),
    active, metric, preference, leaked_from (the instance a leaked route came
    from) and next_hops. A next-hop has its address, type (direct, indirect),
    the resolving_route an indirect one recurses on, and egress: where it
    leaves the node, each of kind 'interface' (a subinterface, with ni when
    it is in another instance), 'tunnel' (a vxlan or other tunnel endpoint)
    or 'route' (a prefix the resolution could not be walked past).

    Args:
        address: Optional IP address for longest-prefix-match (LPM) lookup (e.g. '10.0.0.1').
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("ipv4_rib", inv_filter, field_filter, address=address)


@mcp.tool()
def ipv6_rib(
    address: Optional[str] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv6 routing table entries.

    Same shape as ipv4_rib: one object per network-instance per node with its
    routes, each with prefix, type, active, metric, preference, leaked_from and
    next_hops resolved to their egress (interface, tunnel or route).

    Args:
        address: Optional IPv6 address for longest-prefix-match (LPM) lookup.
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("ipv6_rib", inv_filter, field_filter, address=address)


@mcp.tool()
def static_routes(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get static routes from /network-instance[name=*]/static-routes.

    Returns one object per network-instance per node that has any: node, ni
    and routes - each with prefix, admin (enable/disable), installed,
    metric, preference, next_hop_group and next_hops, each an address and
    whether it is resolved through the route table (resolve).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("static_routes", inv_filter, field_filter)


@mcp.tool()
def tunnel_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get the IP tunnel-table from /network-instance[name=*]/tunnel-table.

    Returns one object per network-instance per node that has any: node, ni
    and tunnels - each with prefix (the endpoint), type (vxlan, ldp,
    sr-isis, rsvp...), owner, preference, metric and next_hops, each
    resolved to address, subinterface, type and the labels pushed. Useful
    for verifying which transport a remote endpoint (e.g. a loopback) is
    reached over, and on which port.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("tunnel_table", inv_filter, field_filter)


@mcp.tool()
def network_instances(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get network instances and their interfaces.

    Returns one object per network-instance per node: node, name, type
    (ip-vrf/mac-vrf/default), oper, router_id, overlays (its vxlan-interfaces),
    evis, instances (each bgp-vpn instance with id, import_rts, export_rts and
    rd - a DCI gateway has two, the DC side and the WAN side), and
    interfaces - each with name, oper, prefixes, mtu, vlan and associated (for
    an irb, the other instances it is in: the ip-vrf a mac-vrf's irb routes
    into).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("ni", inv_filter, field_filter)


@mcp.tool()
def subinterfaces(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get sub-interfaces of SR Linux nodes.

    Returns one object per interface per node: node, name and subinterfaces -
    each with name, type (routed/bridged), admin (enable/disable), oper (up,
    down, or down/standby for one held down on purpose by its
    ethernet-segment), down_reason (the root cause, resolved to the parent
    port), ip_mtu, vlan, ipv4 and ipv6 (the prefixes configured on it).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("subif", inv_filter, field_filter)


@mcp.tool()
def lag(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get LAG (Link Aggregation Group) information.

    Returns one object per LAG per node: node, name, oper, mtu, min_links,
    description, type (lacp/static), speed, standby_signaling, the LACP
    settings (lacp_key, lacp_interval, lacp_mode, lacp_system_id,
    lacp_priority) and members - each with name, oper and activity.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("lag", inv_filter, field_filter)


@mcp.tool()
def ifstats(
    interval: int = 5,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get per-interface traffic rates (in/out bps) computed from two consecutive gNMI samples.

    Queries interface statistics twice with a configurable interval, then calculates
    the delta to derive rates for each interface.

    Returns one object per interface per node: node, name, in_kbps/out_kbps and
    in_pps/out_pps (rates over the interval), in_errors/out_errors and
    in_discards/out_discards (how many during the interval), plus the
    cumulative counters in_packets/out_packets and in_octets/out_octets. Idle
    interfaces are included so raw counters are always available. oper and
    down_reason are empty here: the two samples read only the counters.

    Args:
        interval: Seconds between the two samples (default 5).
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("ifstats", inv_filter, field_filter, interval=interval)


@mcp.tool()
def mac_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get MAC address table entries.

    Returns one object per network-instance per node: node, ni and entries,
    each with address, type (learnt/evpn/evpn-static/irb-interface...), the
    destination as the device writes it, and that destination read apart:
    interface for a locally learned entry, or overlay plus either vtep and vni
    or esi for one learned over EVPN.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("mac", inv_filter, field_filter)


@mcp.tool()
def irb_interfaces(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IRB (Integrated Routing and Bridging) sub-interface details.

    Returns one object per irb per node: node, name, nis (the mac-vrf and
    the ip-vrf it is in), ipv4 and ipv6 (each address with primary and
    anycast_gw flags), anycast_gw with anycast_gw_mac and virtual_router_id,
    and arp and nd - each with proxy, learn_unsolicited, host_routes (the
    entry origins turned into host routes, and whether they are programmed
    in the datapath), evpn_advertise (the origins advertised into EVPN) and
    interface_less_routing.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("irb", inv_filter, field_filter)


@mcp.tool()
def ethernet_segments(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get EVPN Ethernet Segment information.

    Returns one object per segment per node: node, name, esi, type, mh_mode,
    oper, interfaces (the ports it hangs off), next_hops (for a virtual
    segment: address and the evis it serves) and associations - each
    network-instance it is in with its DF candidates (address, designated).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("es", inv_filter, field_filter)


@mcp.tool()
def es_destinations(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get Ethernet Segment destinations from bridge tables.

    Returns one object per tunnel-interface per node: node, tunnel and
    destinations - each an esi, the overlay (vxlan-interface) it is reached
    through and the vteps behind it. Learned from EVPN, so it reflects the
    segments the node currently forwards to.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("es_dest", inv_filter, field_filter)


@mcp.tool()
def vxlan_tunnels(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get VXLAN tunnel interfaces and unicast destinations.

    Returns one object per vxlan-interface per node: node, name, ni, vni (the
    ingress VNI) and destinations, each with vtep and vni.

    Note: unicast destinations are populated by receipt of EVPN type-2 (MAC/IP) routes,
    which typically requires actual traffic. If no destinations are shown, it means no
    type-2 routes have been received for that VNI yet.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("vxlan", inv_filter, field_filter)


@mcp.tool()
def lldp_neighbors(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get LLDP neighbor information.

    Returns one object per interface per node: node, name and neighbors - each
    with system_name, port_id and port_description. Useful for understanding
    physical topology and connectivity.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("lldp", inv_filter, field_filter)


@mcp.tool()
def arp_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get ARP table entries.

    Returns one object per sub-interface per node: node, interface, nis (the
    network-instances it is bound to; an irb is in two) and entries - each
    with address, mac, origin (dynamic/static/evpn...) and expires_in, the
    seconds until the entry ages out (null for a static entry).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("arp", inv_filter, field_filter)


@mcp.tool()
def ipv6_neighbors(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv6 Neighbor Discovery table entries.

    Returns one object per sub-interface per node: node, interface, nis (the
    network-instances it is bound to) and entries - each with address, mac,
    origin, state (reachable/stale/delay...) and expires_in, the seconds until
    it leaves that state.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("nd", inv_filter, field_filter)


@mcp.tool()
def bfd_sessions(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get BFD sessions: the liveness checks under BGP, IS-IS and static routes.

    Returns one object per network-instance per node: node, ni and sessions,
    each with local_address, remote_address, state ('up', 'down', 'init',
    'admin-down'), remote_state, remote_discriminator (0 while the far end has
    never answered - on a session that never came up, BFD is usually not
    enabled over there), interface (the subinterface of a link-local
    session), protocols (the clients it protects), last_transition, failures
    (times it went down after having been up), local_diagnostic,
    remote_diagnostic, tx_interval / rx_interval (microseconds) and multiplier.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("bfd", inv_filter, field_filter)


@mcp.tool()
def isis_adjacencies(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IS-IS interfaces and the adjacencies formed on them.

    Returns one object per IS-IS interface per node: node, ni, instance, name,
    oper, passive (a passive interface is meant to have no adjacency),
    circuit_type and adjacencies, each with system_id, hostname (the
    neighbour's dynamic hostname), level ('L1', 'L2'), state ('up', 'down',
    'init', 'failed'), down_reason, ipv4, ipv6, last_transition and
    transitions (how often it went up or down).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("isis", inv_filter, field_filter)


@mcp.tool()
def ospf_neighbors(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get OSPF interfaces and the neighbours on them.

    Returns one object per OSPF interface per node: node, ni, instance, area,
    name, oper, passive, interface_type and neighbors, each with router_id,
    address, state ('full' is a working adjacency, 'two-way' a correct one
    between two non-DR routers on a broadcast segment), priority,
    last_established and state_changes.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("ospf", inv_filter, field_filter)


@mcp.tool()
def platform_resources(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get CPU, memory and forwarding-table utilization per node.

    Returns one object per resource per node: node, component ('control A', or
    'linecard 1/0' for a forwarding complex), name ('cpu' as a five-minute
    average, 'memory', or a datapath table such as 'ip-lpm-routes',
    'mac-addresses', 'ecmp-groups'), used_percent, used and free (entries,
    or bytes for memory). A table close to full is a route in the RIB that
    never makes it into hardware.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("resources", inv_filter, field_filter)


@mcp.tool()
def hardware_components(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get the chassis components: control and line cards, fabric modules, fans and PSUs.

    Returns one object per component per node: node, kind ('control',
    'linecard', 'fabric', 'fan-tray', 'power-supply'), id, oper ('up',
    'down', 'empty', 'failed', ...), health ('healthy', 'unhealthy',
    'unspecified'), type and serial_number. Virtual nodes report fans and
    PSUs as 'empty'.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("components", inv_filter, field_filter)


@mcp.tool()
def transceivers(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get the optics plugged into each port, with their digital diagnostics.

    Returns one object per fitted transceiver per node: node, interface, oper,
    down_reason, form_factor, pmd, vendor, part_number, serial_number,
    temperature (C), voltage (V), channels (each with index, input_power and
    output_power in dBm, laser_bias in mA), and alarms / warnings: the DOM
    thresholds the optic itself reports as crossed, e.g. 'input-power low'.
    Empty cages are left out; virtual nodes have none.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'state=up'). Values are case-insensitive regexes.
    """
    return _run_report("transceivers", inv_filter, field_filter)


@mcp.tool()
def routing_policies(
    inv_filter: Optional[str] = None,
) -> str:
    """Get routing policies configured on the node.

    Returns the structured JSON response from /routing-policy.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
    """
    spec = get_report("routing_pol")
    i_filter, _ = _parse_filters(inv_filter, None)
    result = _query(spec, i_filter)

    policies: List[Dict[str, Any]] = []
    for host, host_result in result.items():
        r = host_result[0]
        node = r.host.hostname if r.host and r.host.hostname else host
        if r.failed:
            policies.append({"Node": node, "_error": str(r.exception)})
            continue
        for policy in (r.result or {}).get(spec.resource) or []:
            policies.append({"Node": node, "routing-policy": policy})

    return json.dumps(policies, indent=2, default=str)


@mcp.tool()
def fabric_checks(
    check: Optional[str] = None,
    inv_filter: Optional[str] = None,
) -> str:
    """Run the fabric sanity checks and return everything they found.

    The fastest way to find out what is wrong with a fabric. Each check
    correlates one or more reports across every node at once, so it can see
    faults a single table cannot: a link only one end reports, two leaves that
    disagree about the VNI of a service, an ethernet-segment with no designated
    forwarder. fabric_incidents groups these same findings by root cause and
    is usually the better first call; use this one for the flat list, or to
    run a single check.

    Returns a JSON list of findings, worst first, each with:
        Severity: 'error' (the fabric is not doing what it was built to do) or
            'warning' (legitimate in some fabrics, a fault in most).
        Check: which check found it. One of: bgp_down, bgp_af_down,
            bgp_no_routes, itf_down, itf_errors, lldp_one_sided, mtu_mismatch,
            mtu_outlier, evpn_service_mismatch, es_df, bfd_down,
            igp_adjacency_down, igp_no_adjacency, resource_high,
            hardware_fault, optic_dom. 'collection' means a report could not
            be read from a node, so that node went unchecked.
        Node, Subject, Detail: where it is and what is wrong.

    An empty list means every check passed on every node in scope.

    Args:
        check: Run only this check, by the name listed above. Omit to run all.
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
    """
    known = {c.name for c in CHECKS}
    if check and check not in known:
        return json.dumps(
            {"error": f"unknown check '{check}'", "available": sorted(known)}, indent=2
        )
    i_filter, _ = _parse_filters(inv_filter, None)
    nornir = get_nornir()
    target = nornir.filter(**i_filter) if i_filter else nornir
    findings = run_checks(collect_fabric_state(target), only=[check] if check else None)
    return json.dumps([f.as_row() for f in findings], indent=2, default=str)


def _run_lens(lens: str, inv_filter: Optional[str] = None, **params: Any) -> str:
    """Collect what a lens reads, run it, and return its records as JSON.

    The records rather than the table rows: an agent wants the VTEPs a service
    sends to as a list and the MAC count as a number, not the cell a table
    joins them into.

    *params* are the lens's own arguments, so nothing here may be called what
    one of them is: ``service`` takes a ``name``.
    """
    spec = get_lens(lens)
    i_filter, _ = _parse_filters(inv_filter, None)
    nornir = get_nornir()
    target = nornir.filter(**i_filter) if i_filter else nornir
    state = collect_lens_state(target, spec.requires)
    try:
        records = spec.run(state, **params)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    payload: Dict[str, Any] = {"records": [as_dict(r) for r in records]}
    if state.errors:
        payload["not_collected"] = [
            {"node": node, "report": report, "error": error}
            for (report, node), error in sorted(state.errors.items())
        ]
    return json.dumps(payload, indent=2, default=str)


@mcp.tool()
def fabric_summary(inv_filter: Optional[str] = None) -> str:
    """Executive fabric briefing: what it is built of, what it carries, and active incidents.

    The first tool to call to understand the fabric topology, roles and health before
    investigating specific tables or incidents.

    Returns a JSON object with:
        summary: The fabric in 3-4 briefing sentences.
        nodes: Total number of network devices.
        roles: Count of devices by role (spine, leaf, dcgw, core).
        services: Total number of active services.
        incidents: Open incident counts (errors, warnings, findings) and worst incident.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs. Omit to target all nodes.
    """
    from .checks import REQUIRED_REPORTS
    from .server.topology import summarize_fabric

    i_filter, _ = _parse_filters(inv_filter, None)
    nornir = get_nornir()
    target = nornir.filter(**i_filter) if i_filter else nornir
    reports = tuple(dict.fromkeys(REQUIRED_REPORTS + ("sys_info", "es")))
    state = collect_lens_state(target, reports)
    result = summarize_fabric(state)
    return json.dumps(
        {
            "summary": result["summary"],
            "nodes": result["nodes"],
            "roles": result["roles"],
            "services": result["services"],
            "incidents": result["incidents"],
        },
        indent=2,
    )


@mcp.tool()
def fabric_incidents(inv_filter: Optional[str] = None) -> str:
    """Every check's findings grouped by root cause: the first tool to call for 'what is wrong'.

    Runs the same checks as fabric_checks, then groups their findings by what
    they are about, so one broken cable reads as one incident - the interface
    down, with the BGP, BFD and IGP sessions that went down over it - rather
    than a dozen unrelated rows. The same cause in several places (BFD down
    on every leaf-spine link) is folded into one 'pattern' incident.

    Returns {"records": [...]}, worst incident first, each with:
        id, severity ('error' or 'warning'), kind ('link', 'port', 'node',
            'session', 'underlay', 'platform', 'segment', 'pattern' or
            'finding' for one that stands alone), title, node (where the root
            cause is), nodes (every node involved).
        root: the finding that explains the others - check, severity, node,
            subject, detail. 'node_unreachable' and 'underlay_unreachable' are
            roots no single check reports: a node that answered nothing, and
            an overlay session to a loopback there is no route to.
        related: every other finding the incident accounts for.
        explanation: one sentence saying what the incident is and holds.
    Plus "not_collected" when a node's report could not be read.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs. Omit it
            unless the fabric is large: correlation needs both ends of a link.
    """
    return _run_lens("incidents", inv_filter)


#: What 'mark_baseline' kept, for 'changes_since_baseline' to compare against.
_baseline: Dict[str, Any] = {}


@mcp.tool()
def mark_baseline(inv_filter: Optional[str] = None) -> str:
    """Remember the fabric as it is now, to compare it against later.

    Call it before a change - a maintenance, a config push, a test - and call
    changes_since_baseline afterwards to see exactly what the change did:
    sessions and ports that went down or came up, LLDP neighbours lost, DF
    elections that moved, MACs that moved, route counts that fell, findings
    raised and cleared. The baseline lives as long as this MCP server does,
    and is replaced by the next call.

    Returns when the baseline was taken, how many nodes it covers, and the
    findings the checks had at that point.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs. The
            comparison later covers the nodes both readings have in common.
    """
    import time as _time  # noqa: PLC0415


    i_filter, _ = _parse_filters(inv_filter, None)
    nornir = get_nornir()
    target = nornir.filter(**i_filter) if i_filter else nornir
    state = collect_lens_state(target, WATCH_REPORTS)
    findings = run_checks(state)
    _baseline.clear()
    _baseline.update(at=_time.time(), state=state, findings=findings, inv_filter=inv_filter)
    return json.dumps(
        {
            "baseline_at": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(_baseline["at"])),
            "nodes": len(target.inventory.hosts),
            "findings": len(findings),
            "not_collected": [
                {"node": node, "report": report, "error": error}
                for (report, node), error in sorted(state.errors.items())
            ],
        },
        indent=2,
    )


@mcp.tool()
def changes_since_baseline(watch_prefixes: Optional[str] = None) -> str:
    """What changed in the fabric since mark_baseline was called.

    Reads the fabric again, over the same nodes, and compares it with the
    baseline. Only differences are returned, worst first: an empty list means
    nothing the reports can see has changed.

    Returns {"baseline_at": ..., "changes": [...]}, each change with:
        time, node, kind ('bgp', 'bgp-routes', 'interface', 'lldp', 'bfd',
            'isis', 'ospf', 'es', 'es-df', 'mac', 'routes', 'hardware',
            'optic', 'arp', 'nd', 'routes', 'route' or 'finding'), subject (the
            peer, port, MAC, address, route table or prefix, or finding),
        before, after (empty where it did not exist on that side),
        severity ('error' something stopped working, 'warning', 'ok' something
            recovered, 'info' something new or gone that was not working
            anyway), detail.

    Route tables are summarized, one change per table ('routes': how many
    prefixes changed next-hops, were withdrawn or are new, with examples);
    the default routes, the host routes to every node's system address and
    *watch_prefixes* are reported one by one ('route'), ECMP width included.

    Args:
        watch_prefixes: Comma-separated prefixes or addresses to report one by
            one as well, e.g. '10.1.4.16,6.6.6.1/32'.
    """
    import time as _time  # noqa: PLC0415

    from .changes import as_row, diff_fabric, diff_findings, normalize_prefix  # noqa: PLC0415

    if not _baseline:
        return json.dumps({"error": "no baseline: call mark_baseline first"}, indent=2)
    i_filter, _ = _parse_filters(_baseline.get("inv_filter"), None)
    nornir = get_nornir()
    target = nornir.filter(**i_filter) if i_filter else nornir
    state = collect_lens_state(target, WATCH_REPORTS)
    now = _time.time()
    try:
        watched = [normalize_prefix(p) for p in (watch_prefixes or "").split(",") if p.strip()]
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    changes = diff_fabric(_baseline["state"], state, at=now, watched=watched) + diff_findings(
        _baseline["findings"], run_checks(state), at=now
    )
    return json.dumps(
        {
            "baseline_at": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(_baseline["at"])),
            "changes": [as_row(change) for change in changes],
        },
        indent=2,
        default=str,
    )


@mcp.tool()
def locate_address(address: str, inv_filter: Optional[str] = None) -> str:
    """Find every node in the fabric that knows about one MAC or IP address.

    Use this instead of reading mac_table or arp_table node by node when the
    question is 'where is this host'. An IP is resolved through ARP or ND to a
    MAC first, so either form of address works.

    Returns {"records": [...]}, one record per place the address is known, with:
        kind: 'configured' (the IP is this node's own: a loopback, a system
            address, an irb gateway), 'local' (this node learned the MAC on its
            own port), 'remote' (it learned it over the overlay from a VTEP or
            ethernet-segment), 'arp' or 'neighbor' (an address binding that
            named the MAC), 'multihomed' (this node learned it locally and so
            did the nodes in also_on, each on a port of the same
            ethernet-segment: expected, not a fault), 'bgp' (a host route to
            the IP - /32 or /128 - that BGP installed in this network-instance's
            route table), 'duplicate' (this node
            learned it locally and so did the nodes in also_on, not all on one
            ethernet-segment: a move or a fault), or 'not-found'.
        node, ni, address: where it was seen, and the IP or MAC seen.
        interface, vtep, esi: what it sits on - exactly one is set, except
            that a locally learned MAC on an ethernet-segment port also
            carries that segment's esi, and none is for 'bgp', where
            next_hops says it.
        prefix: for 'configured', the prefix as configured; for 'bgp', the host route.
        origin: how the entry got there ('learnt', 'evpn', 'static', 'dynamic';
            for 'bgp', the route type: 'bgp', 'bgp-evpn', 'bgp-vpn').
        next_hops: for 'bgp', each next-hop with the tunnels or interfaces it
            resolves over, e.g. '192.0.2.15 (vxlan 192.0.2.15)'.
        mac, expiry: for 'arp'/'neighbor', the MAC the binding resolved to.
        overlay, vni: for 'remote', the overlay it was learned over.
        segments: for 'remote' behind an ESI, the segment's configured names;
            for a locally learned MAC, the segment its port belongs to.
        also_on: for 'duplicate'/'multihomed', the other nodes that learned it
            locally.
        searched: for 'not-found', how many bridge tables were looked in.
    Plus "not_collected" when a node's report could not be read.

    Args:
        address: A MAC ('00:C1:AB:00:01:21') or an IP ('10.0.1.51').
        inv_filter: Inventory filter as comma-separated key=value pairs. Narrowing
            the inventory narrows the search, so omit it unless the fabric is large.
    """
    return _run_lens("where", inv_filter, target=address)


@mcp.tool()
def trace_path(
    source: str,
    destination: str,
    ni: str = "default",
    inv_filter: Optional[str] = None,
) -> str:
    """Walk the route tables hop by hop from a node towards a destination address.

    Computed from the route tables rather than probed, so it works without
    sending traffic and shows every ECMP branch instead of the one a probe
    happened to take. A lookup in a VRF that resolves onto a tunnel hands off
    to the underlay towards its endpoint and resumes in the VRF at the far end,
    so a DCI path traces VXLAN to the gateway, MPLS across, and VXLAN again.

    Use this for 'why does A not reach B': the hop whose outcome is 'no-route'
    or 'dead-end' is where the path stops.

    Returns {"records": [...]}, one record per lookup, ordered by hop. Several
    records share a hop number when the walk fans out over ECMP. Each has:
        hop, node, ni, address: where the lookup was done and what was looked
            up - the destination, or the VTEP being chased through the underlay.
        outcome: 'forwarded' (out of egress to peer, where the walk goes on),
            'dead-end' (no LLDP neighbour on egress, so it cannot), 'tunnel'
            (resolved onto a tunnel - vxlan to a VTEP, ldp or sr-isis to a
            far-end gateway - continuing in the underlay towards endpoint),
            'endpoint-reached' (the underlay delivered the endpoint; the packet
            is decapsulated and looked up in resumes_in, the VRF there),
            'leaked' (the route was leaked from resumes_in, whose next-hops
            forward it; the walk goes on in that instance on the same node),
            'delivered' (the destination is attached here), 'local-ip' (it is
            this node's own address), 'neighbor' or 'no-neighbor' (whether
            ARP/ND has the delivered address), 'no-route', 'loop' or 'too-long'.
        prefix, route_type, next_hops: the route that matched.
        egress: the subinterface, or 'vxlan:<vtep>' over the overlay.
        peer, peer_port: the node on the other end of that cable.
        tunnel, endpoint, resumes_in, mac, origin, visited: filled for the
            outcomes named.
    Plus "not_collected" when a node's report could not be read.

    Args:
        source: The node to start from, or an address attached to one.
        destination: The address being forwarded towards.
        ni: Network instance to look the destination up in. Defaults to 'default'
            (the underlay); name the IP-VRF for a tenant address.
        inv_filter: Inventory filter as comma-separated key=value pairs. A filter
            that excludes a node the path goes through will truncate the walk there.
    """
    return _run_lens(
        "path", inv_filter, source=source, destination=destination, ni=ni
    )


@mcp.tool()
def service_detail(name: str, inv_filter: Optional[str] = None) -> str:
    """Show one network-instance as every node that carries it sees it.

    The transpose of network_instances: one row per node for a single service,
    so the node whose VNI, route-target or oper-state does not match the others
    is a column to read down rather than several tables to compare by hand.

    Returns {"records": [...]}, one record per node carrying the instance, with
    node, ni, type, oper, evis, vnis, import_rts, export_rts (the union over
    its bgp-vpn instances, which are listed in instances with their own),
    interfaces (each with name and oper), bound (the instances attached to
    it), vteps, local_macs, remote_macs, segments (its ethernet-segments) and
    site (the underlay the node is in, numbered, when the service is carried
    in more than one - nodes in different underlays never have to agree). Plus
    "not_collected" when a node's report could not be read.

    Args:
        name: Network-instance name, matched as a case-insensitive regex, so
            'subnet' matches subnet-1 and subnet-2.
        inv_filter: Inventory filter as comma-separated key=value pairs. Omit it:
            the point of this tool is to see every node that carries the service.
    """
    return _run_lens("service", inv_filter, name=name)


# ---- CLI entry point ----


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP Server for fcli - SR Linux fabric analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  fcli-mcp --topo-file topo.yml                  # stdio transport with clab topology\n"
            "  fcli-mcp --topo-file topo.yml --transport http  # HTTP/SSE transport\n"
            "  fcli-mcp --config-file nornir_config.yaml       # Use nornir config file\n"
        ),
    )
    parser.add_argument(
        "--topo-file",
        "-t",
        help="Containerlab topology file (mutually exclusive with --config-file)",
    )
    parser.add_argument(
        "--config-file",
        "-c",
        help="Nornir config file (mutually exclusive with --topo-file)",
    )
    parser.add_argument(
        "--cert-file",
        help="TLS certificate file for containerlab",
    )
    parser.add_argument(
        "--inv-filter",
        "-i",
        action="append",
        help="Inventory filter in key=value format (can be repeated)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind HTTP server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for HTTP server (default: 8080)",
    )

    args = parser.parse_args()

    if args.topo_file and args.config_file:
        parser.error("--topo-file and --config-file are mutually exclusive")

    # Initialize Nornir
    global _nornir_instance
    try:
        if args.topo_file:
            _nornir_instance = _init_nornir_from_topo(args.topo_file, args.cert_file)
        elif args.config_file:
            _nornir_instance = _init_nornir_from_config(args.config_file)
        elif os.path.exists("nornir_config.yaml"):
            _nornir_instance = _init_nornir_from_config("nornir_config.yaml")
    except Exception as e:
        logger.error("Failed to initialize Nornir: %s", e)

    # Apply global inventory filter if provided
    if _nornir_instance and args.inv_filter:
        i_filter = {}
        for f in args.inv_filter:
            if "=" in f:
                k, v = f.split("=", 1)
                i_filter[k] = v
        if i_filter:
            _nornir_instance = _nornir_instance.filter(**i_filter)

    # Run server
    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
