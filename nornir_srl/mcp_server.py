"""MCP server for the fcli tool.

Exposes fcli commands as MCP tools so that LLM clients can query
SRLinux network resources via the Model Context Protocol.

Usage:
    fcli-mcp --cfg nornir_config.yaml
    fcli-mcp --topo-file lab.clab.yml

The server runs over stdio by default (the standard MCP transport for
local tool-use).
"""

import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # type: ignore
from mcp.server.fastmcp import FastMCP
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task

from .cli import (
    NORNIR_DEFAULT_CONFIG,
    SRL_DEFAULT_GNMI_PORT,
    SRL_DEFAULT_PASSWORD,
    SRL_DEFAULT_USERNAME,
    _extract_data,
    _get_fields,
)
from .connections.srlinux import CONNECTION_NAME
from .utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nornir initialisation helpers (mirrors cli.py callback logic)
# ---------------------------------------------------------------------------


def _init_nornir_from_config(cfg_path: str) -> Nornir:
    """Initialise Nornir from a YAML config file."""
    return InitNornir(config_file=cfg_path)


def _init_nornir_from_topo(
    topo_file: str, cert_file: Optional[str] = None
) -> Nornir:
    """Initialise Nornir from a Containerlab topology file."""
    with open(topo_file, "r") as fh:
        topo = yaml.safe_load(os.path.expandvars(fh.read()))

    lab_name = topo["name"]
    if "prefix" not in topo:
        prefix = f"clab-{lab_name}-"
    else:
        if topo["prefix"] == "__lab-name":
            prefix = f"{lab_name}-"
        elif topo["prefix"] == "":
            prefix = ""
        else:
            prefix = f"{topo['prefix']}-{lab_name}-"

    hosts: Dict[str, Dict[str, Any]] = {}
    def_kind = topo["topology"].get("defaults", {}).get("kind")
    def_image = (
        topo["topology"].get("defaults", {}).get("image")
        or topo["topology"]["kinds"].get(def_kind, {}).get("image")
        if def_kind
        else None
    )
    srlinux_def = False
    if def_image and "srlinux" in def_image:
        srlinux_def = True
    if def_kind in {"srl", "nokia_srlinux"}:
        srlinux_def = True

    srl_kinds = [
        k
        for k, v in topo["topology"].get("kinds", {}).items()
        if "/srlinux" in v.get("image")
    ]
    for extra in ("srl", "nokia_srlinux"):
        if extra not in srl_kinds:
            srl_kinds.append(extra)

    clab_nodes: Dict[str, Dict] = topo["topology"]["nodes"]
    for node, node_spec in clab_nodes.items():
        node_kind = node_spec.get("kind")
        if (node_kind is None and srlinux_def) or node_kind in srl_kinds:
            hosts[f"{prefix}{node}"] = {
                "hostname": f"{prefix}{node}",
                "platform": "srlinux",
                "groups": ["srl"],
                "data": node_spec.get("labels", {}),
            }

    groups: Dict[str, Dict[str, Any]] = {
        "srl": {
            "connection_options": {
                "srlinux": {
                    "username": SRL_DEFAULT_USERNAME,
                    "password": SRL_DEFAULT_PASSWORD,
                    "port": SRL_DEFAULT_GNMI_PORT,
                    "extras": {},
                }
            }
        }
    }
    if cert_file:
        groups["srl"]["connection_options"]["srlinux"]["extras"][
            "path_cert"
        ] = cert_file

    hosts_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    groups_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    try:
        yaml.safe_dump(hosts, hosts_f)
        hosts_f.flush()
        yaml.safe_dump(groups, groups_f)
        groups_f.flush()

        conf: Dict[str, Any] = dict(NORNIR_DEFAULT_CONFIG)
        conf["inventory"] = {
            "options": {
                "host_file": hosts_f.name,
                "group_file": groups_f.name,
            }
        }
        return InitNornir(**conf)
    finally:
        hosts_f.close()
        groups_f.close()


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _run_task_json(
    nr: Nornir,
    name: str,
    task_func,
    inv_filter: Optional[Dict[str, str]] = None,
    field_filter: Optional[Dict[str, str]] = None,
) -> str:
    """Run a Nornir task and return JSON-formatted results."""
    target = nr.filter(**inv_filter) if inv_filter else nr
    result = target.run(task=task_func, name=name, raise_on_error=False)
    col_names, rows = _extract_data(
        resource=result.name,
        results=result,
        filter=field_filter if field_filter else None,
    )
    return json.dumps(rows, indent=2, default=str)


def _parse_filter(raw: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a comma-separated ``key=value`` filter string into a dict."""
    if not raw:
        return None
    pairs: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            pairs[k.strip()] = v.strip()
    return pairs or None


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------


def _build_server(nr: Nornir) -> FastMCP:
    """Create the FastMCP server instance with all fcli tools registered."""

    mcp = FastMCP(
        name="fcli",
        instructions=(
            "Network-wide CLI for SRLinux devices. "
            "Use the tools below to query different network resources "
            "across the fabric managed by Nornir/gNMI."
        ),
    )

    # -- tool definitions ------------------------------------------------

    @mcp.tool()
    def bgp_peers(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve BGP peers and their status across SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs
                        (e.g. "site=dc1,role=leaf").
            field_filter: Field filter as comma-separated key=value pairs
                          to filter output rows (e.g. "State=established").
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_sum_bgp())

        return _run_task_json(
            nr, "bgp_peers", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def sys_info(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve system information of SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_info())

        return _run_task_json(
            nr, "sys_info", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def subinterfaces(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve sub-interfaces of SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_sum_subitf())

        return _run_task_json(
            nr, "subinterface", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def lag(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve LAG (Link Aggregation Group) information from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_lag())

        return _run_task_json(
            nr, "lag", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def ipv4_rib(
        address: str = "",
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve IPv4 RIB (Routing Information Base) entries.

        Args:
            address: Optional IPv4 address for LPM (Longest Prefix Match) lookup.
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """
        lpm = address if address else None

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(
                host=task.host,
                result=device.get_rib(afi="ipv4-unicast", lpm_address=lpm),
            )

        return _run_task_json(
            nr, "ip_rib", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def ipv6_rib(
        address: str = "",
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve IPv6 RIB (Routing Information Base) entries.

        Args:
            address: Optional IPv6 address for LPM (Longest Prefix Match) lookup.
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """
        lpm = address if address else None

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(
                host=task.host,
                result=device.get_rib(afi="ipv6-unicast", lpm_address=lpm),
            )

        return _run_task_json(
            nr, "ip_rib", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def bgp_rib(
        route_fam: str = "",
        route_type: str = "",
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve BGP RIB (Routing Information Base) entries.

        Args:
            route_fam: Route family (required, e.g. "evpn", "ipv4-unicast").
            route_type: Route type for EVPN (optional).
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """
        if not route_fam:
            return json.dumps({"error": "route_fam is required"})

        rt = route_type if route_type else None

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            kwargs: Dict[str, Any] = {"route_fam": route_fam}
            if rt is not None:
                kwargs["route_type"] = rt
            return Result(host=task.host, result=device.get_bgp_rib(**kwargs))

        return _run_task_json(
            nr, "bgp_rib", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def mac_table(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve MAC address table from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_mac_table())

        return _run_task_json(
            nr, "mac_table", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def network_instances(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve network instances and their interfaces from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_nwi_itf())

        return _run_task_json(
            nr, "nwi_itfs", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def lldp_neighbors(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve LLDP (Link Layer Discovery Protocol) neighbors from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_lldp_sum())

        return _run_task_json(
            nr, "lldp_nbrs", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def irb_interfaces(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve IRB (Integrated Routing and Bridging) sub-interfaces from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_irb())

        return _run_task_json(
            nr, "irb", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def ethernet_segments(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve Ethernet Segment information from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_es())

        return _run_task_json(
            nr, "es", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def es_destinations(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve Ethernet Segment destinations on the bridge table from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_es_dest())

        return _run_task_json(
            nr, "es_dest", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def vxlan_tunnels(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve VXLAN tunnel interfaces and unicast destinations from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_vxlan())

        return _run_task_json(
            nr, "vxlan", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def arp_table(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve ARP (Address Resolution Protocol) table from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_arp())

        return _run_task_json(
            nr, "arp", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    @mcp.tool()
    def ipv6_neighbors(
        inv_filter: str = "",
        field_filter: str = "",
    ) -> str:
        """Retrieve IPv6 neighbor discovery table from SRLinux nodes.

        Args:
            inv_filter: Inventory filter as comma-separated key=value pairs.
            field_filter: Field filter as comma-separated key=value pairs.
        """

        def _task(task: Task) -> Result:
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=device.get_nd())

        return _run_task_json(
            nr, "nd", _task, _parse_filter(inv_filter), _parse_filter(field_filter)
        )

    return mcp


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry-point for the ``fcli-mcp`` console script."""
    import argparse

    parser = argparse.ArgumentParser(
        description="MCP server exposing fcli network queries as tools",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cfg",
        "-c",
        metavar="FILE",
        help="Nornir YAML config file",
    )
    source.add_argument(
        "--topo-file",
        "-t",
        metavar="FILE",
        help="Containerlab topology file",
    )
    parser.add_argument(
        "--cert-file",
        metavar="FILE",
        default=None,
        help="TLS certificate file (used with --topo-file)",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        default="ERROR",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: ERROR)",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="MCP transport (default: stdio)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.cfg:
        nr = _init_nornir_from_config(args.cfg)
    else:
        nr = _init_nornir_from_topo(args.topo_file, args.cert_file)

    server = _build_server(nr)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
