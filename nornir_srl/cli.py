import csv
import fnmatch
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from enum import Enum
import logging
import os

import typer
import yaml  # type: ignore
from rich.console import Console
from rich.table import Table
from rich.box import MINIMAL_DOUBLE_HEAD
from rich.theme import Theme
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task, AggregatedResult
from nornir.core.inventory import Host

from .connections.srlinux import CONNECTION_NAME
from .utils.logging_config import setup_logging
from . import __version__


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    YAML = "yaml"
    CSV = "csv"


def _version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(name="fcli", help="Nornir SRLinux CLI")
logger = logging.getLogger(__name__)


SRL_DEFAULT_USERNAME = "admin"
SRL_DEFAULT_PASSWORD = "NokiaSrl1!"
SRL_DEFAULT_GNMI_PORT = 57400

NORNIR_DEFAULT_CONFIG: Dict[str, Any] = {
    "inventory": {
        "plugin": "YAMLInventory",
        "options": {
            "host_file": "clab_hosts.yml",
            "group_file": "clab_groups.yml",
            "defaults_file": "clab_defaults.yml",
        },
    },
    "runner": {"plugin": "threaded", "options": {"num_workers": 20}},
    "user_defined": {"intent_dir": "intent"},
    "logging": {"enabled": False},
}


# ------------------------- helpers -------------------------


def _get_fields(b, depth=0):
    fields: List[str] = []
    if isinstance(b, list) and len(b) > 0:
        fields.extend(_get_fields(b[0], depth=depth + 1))
    elif isinstance(b, dict):
        for k, v in b.items():
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                fields.extend(_get_fields(v[0], depth=depth + 1))
            elif isinstance(v, dict):
                fields.extend(_get_fields(v, depth=depth + 1))
            else:
                fields.append(k)
        if depth > 0:
            fields = sorted(fields)
    return fields


def _pass_filter(row, filter):
    if filter is None:
        return True
    filter = {str(k).lower(): v for k, v in filter.items()}
    if len(
        {
            k: v
            for k, v in row.items()
            if filter.get(str(k).lower())
            and fnmatch.fnmatch(str(row[k]), str(filter[str(k).lower()]))
        }
    ) < len(filter):
        return False
    else:
        return True


def _extract_data(
    resource: str,
    results: AggregatedResult,
    filter: Optional[Dict],
) -> tuple:
    """Extract flat row data from AggregatedResult.

    Returns (col_names, all_rows) where each row is a dict with a 'Node' key.
    """
    col_names: List[str] = []
    all_rows: List[Dict[str, Any]] = []

    for host, host_result in results.items():
        r: Result = host_result[0]
        node: Host = r.host if r.host else Host("unknown")
        if r.failed:
            typer.echo(f"Failed to get {resource} for {host}. Exception: {r.exception}")
            continue
        if r.result and r.result.get(resource) is not None:
            for l in r.result.get(resource):
                if len(col_names) == 0:
                    col_names = _get_fields(l)
                common = {
                    x: y
                    for x, y in l.items()
                    if isinstance(y, (str, int, float))
                    or (
                        isinstance(y, list)
                        and len(y) > 0
                        and not isinstance(y[0], dict)
                    )
                }
                node_name = node.hostname if node.hostname else node.name
                if len([v for v in l.values() if isinstance(v, list)]) == 0:
                    if _pass_filter(common, filter):
                        all_rows.append({"Node": node_name, **common})
                else:
                    for key, v in l.items():
                        if isinstance(v, list):
                            for item in v:
                                row = {
                                    k: y
                                    for k, y in item.items()
                                    if isinstance(y, (str, int, float))
                                    or (
                                        isinstance(y, list)
                                        and len(y) > 0
                                        and not isinstance(y[0], dict)
                                    )
                                }
                                if _pass_filter({**common, **row}, filter):
                                    all_rows.append(
                                        {"Node": node_name, **common, **row}
                                    )
    return col_names, all_rows


def print_structured(
    col_names: List[str],
    rows: List[Dict[str, Any]],
    output_format: OutputFormat,
) -> None:
    """Print data in JSON, YAML, or CSV format."""
    if not rows:
        typer.echo("No data...")
        return

    all_cols = ["Node"] + col_names

    if output_format == OutputFormat.JSON:
        typer.echo(json.dumps(rows, indent=2, default=str))
    elif output_format == OutputFormat.YAML:
        typer.echo(yaml.safe_dump(rows, default_flow_style=False).rstrip())
    elif output_format == OutputFormat.CSV:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) for k, v in row.items()})
        typer.echo(buf.getvalue().rstrip())


def print_table(
    title: str,
    resource: str,
    results: AggregatedResult,
    filter: Optional[Dict],
    *,
    box_type: Optional[str] = None,
) -> None:
    table_theme = Theme(
        {"ok": "green", "warn": "orange3", "info": "blue", "err": "bold red"}
    )
    STYLE_MAP = {
        "up": "[ok]",
        "down": "[err]",
        "enable": "[ok]",
        "disable": "[info]",
        "routed": "[cyan]",
        "bridged": "[blue]",
        "established": "[ok]",
        "active": "[cyan]",
    }

    console = Console(theme=table_theme)
    console._emoji = False
    if box_type:
        box_type = str(box_type).upper()
        try:
            box_t = getattr(__import__("rich.box", fromlist=["box"]), box_type)
        except AttributeError:
            typer.echo(
                f"Unknown box type {box_type}. Check 'python -m rich.box' for valid box types."
            )
            box_t = MINIMAL_DOUBLE_HEAD
    else:
        box_t = MINIMAL_DOUBLE_HEAD
    table = Table(title=title, highlight=True, box=box_t)
    table.add_column("Node", no_wrap=True)

    def get_fields(b, depth=0):
        fields: List[str] = []
        if isinstance(b, list) and len(b) > 0:
            fields.extend(get_fields(b[0], depth=depth + 1))
        elif isinstance(b, dict):
            for k, v in b.items():
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    fields.extend(get_fields(v[0], depth=depth + 1))
                elif isinstance(v, dict):
                    fields.extend(get_fields(v, depth=depth + 1))
                else:
                    fields.append(k)
            if depth > 0:
                fields = sorted(fields)
        return fields

    def pass_filter(row, filter):
        if filter is None:
            return True
        filter = {str(k).lower(): v for k, v in filter.items()}
        if len(
            {
                k: v
                for k, v in row.items()
                if filter.get(str(k).lower())
                and fnmatch.fnmatch(str(row[k]), str(filter[str(k).lower()]))
            }
        ) < len(filter):
            return False
        else:
            return True

    col_names: List[str] = []
    for host, host_result in results.items():
        rows = []
        r: Result = host_result[0]
        node: Host = r.host if r.host else Host("unknown")
        if r.failed:
            typer.echo(f"Failed to get {resource} for {host}. Exception: {r.exception}")
            continue
        if r.result and r.result.get(resource) is not None:
            for l in r.result.get(resource):
                if len(col_names) == 0:
                    col_names = get_fields(l)
                    for col in col_names:
                        table.add_column(col, no_wrap=False)
                common = {
                    x: y
                    for x, y in l.items()
                    if isinstance(y, (str, int, float))
                    or (
                        isinstance(y, list)
                        and len(y) > 0
                        and not isinstance(y[0], dict)
                    )
                }
                if len([v for v in l.values() if isinstance(v, list)]) == 0:
                    if pass_filter(common, filter):
                        rows.append(common)
                else:
                    for key, v in l.items():
                        if isinstance(v, list):
                            first_row = True
                            for item in v:
                                row = {
                                    k: y
                                    for k, y in item.items()
                                    if isinstance(y, (str, int, float))
                                    or (
                                        isinstance(y, list)
                                        and len(y) > 0
                                        and not isinstance(y[0], dict)
                                    )
                                }
                                if pass_filter({**common, **row}, filter):
                                    if first_row:
                                        rows.append({**common, **row})
                                    else:
                                        rows.append(row)
                                    first_row = False
        first_row = True
        for row in rows:
            for k, v in row.items():
                row[k] = str(STYLE_MAP.get(str(v), "")) + str(v)
            values = [str(row.get(k, "")) for k in col_names]
            if first_row:
                node_name: str = node.hostname if node.hostname else node.name
                table.add_row(node_name, *values)
                first_row = False
            else:
                table.add_row("", *values)
        table.add_section()
    if len(table.columns) > 1:
        console.print(table)
    else:
        console.print("[i]No data...[/i]")
        logger.debug("No data returned for %s: %s", resource, results)


def print_report(
    result: AggregatedResult,
    name: str,
    failed_hosts: List[str],
    box_type: Optional[str] = None,
    f_filter: Optional[Dict] = None,
    i_filter: Optional[Dict] = None,
    output: OutputFormat = OutputFormat.TABLE,
) -> None:
    if output == OutputFormat.TABLE:
        title = "[bold]" + name + "[/bold]"
        if f_filter:
            title += "\nFields filter:" + str(f_filter)
        if i_filter:
            title += "\nInventory filter:" + str(i_filter)
        if len(failed_hosts) > 0:
            title += "\n[red]Failed hosts:" + str(failed_hosts)
        print_table(
            title=title,
            resource=result.name,
            results=result,
            filter=f_filter,
            box_type=box_type,
        )
    else:
        col_names, rows = _extract_data(
            resource=result.name,
            results=result,
            filter=f_filter,
        )
        print_structured(col_names, rows, output)


# ------------------------- root callback -------------------------


@app.callback()
def main(
    ctx: typer.Context,
    cfg: Path = typer.Option(
        Path("nornir_config.yaml"),
        "--cfg",
        "-c",
        exists=True,
        readable=True,
        help="Nornir config file. Mutually exclusive with -t",
    ),
    inv_filter: Optional[List[str]] = typer.Option(
        None,
        "--inv-filter",
        "-i",
        help="Inventory filter in key=value format. Can be provided multiple times",
    ),
    box_type: Optional[str] = typer.Option(
        None,
        "--box-type",
        "-b",
        help="Box type of printed table, e.g. -b minimal_double_head. 'python -m rich.box' for options",
    ),
    topo_file: Optional[Path] = typer.Option(
        None,
        "--topo-file",
        "-t",
        exists=True,
        help="CLAB topology file, mutually exclusive with -c",
    ),
    cert_file: Optional[Path] = typer.Option(
        None, "--cert-file", exists=True, help="CLAB certificate file"
    ),
    log_level: LogLevel = typer.Option(
        LogLevel.ERROR, "--log-level", "-l", help="Set logging level"
    ),
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", "-f", help="Optional log file"
    ),
    output: OutputFormat = typer.Option(
        OutputFormat.TABLE,
        "--output",
        "-o",
        help="Output format: table, json, yaml, csv",
        case_sensitive=False,
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    setup_logging(log_level.value, str(log_file) if log_file else None)
    ctx.ensure_object(dict)
    if topo_file:
        try:
            with open(topo_file, "r") as f:
                topo = yaml.safe_load(os.path.expandvars(f.read()))
        except Exception as e:
            typer.echo(f"Failed to load topology file {topo_file}: {e}")
            raise typer.Exit(1)
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
            groups["srl"]["connection_options"]["srlinux"]["extras"]["path_cert"] = str(
                cert_file
            )
        with tempfile.NamedTemporaryFile("w+") as hosts_f:
            yaml.safe_dump(hosts, hosts_f)
            hosts_f.seek(0)
            with tempfile.NamedTemporaryFile("w+") as groups_f:
                yaml.safe_dump(groups, groups_f)
                groups_f.seek(0)
                conf: Dict[str, Any] = NORNIR_DEFAULT_CONFIG
                conf.update(
                    {
                        "inventory": {
                            "options": {
                                "host_file": hosts_f.name,
                                "group_file": groups_f.name,
                            }
                        }
                    }
                )
                fabric = InitNornir(**conf)
    else:
        fabric = InitNornir(config_file=str(cfg))

    i_filter = (
        {k: v for k, v in (f.split("=") for f in inv_filter)} if inv_filter else {}
    )
    target: Nornir = fabric.filter(**i_filter) if i_filter else fabric
    ctx.obj["target"] = target
    ctx.obj["i_filter"] = i_filter
    ctx.obj["box_type"] = box_type.upper() if box_type else None
    ctx.obj["output"] = output


# ------------------------- command helpers -------------------------


def run_show(
    ctx: typer.Context,
    name: str,
    task_func: Callable[[Task], Result],
    field_filter: Optional[List[str]],
    title: Optional[str] = None,
) -> None:
    f_filter = (
        {k: v for k, v in (f.split("=") for f in field_filter)} if field_filter else {}
    )
    result = ctx.obj["target"].run(task=task_func, name=name, raise_on_error=False)
    logger.debug("Aggregated result for %s: %s", name, result)
    display_name = title if title else name.replace("_", " ").title()
    print_report(
        result=result,
        name=display_name,
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
        output=ctx.obj["output"],
    )


# ------------------------- commands -------------------------


@app.command()
def bgp_peers(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays BGP Peers and their status"""

    def _bgp(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_sum_bgp())

    run_show(ctx, "bgp_peers", _bgp, field_filter)


@app.command()
def sys_info(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays System Info of nodes"""

    def _sys_info(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_info())

    run_show(ctx, "sys_info", _sys_info, field_filter)


@app.command()
def subif(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays Sub-Interfaces of nodes"""

    def _sub(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_sum_subitf())

    run_show(ctx, "subinterface", _sub, field_filter)


@app.command()
def lag(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays LAGs of nodes"""

    def _lag(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_lag())

    run_show(ctx, "lag", _lag, field_filter)


@app.command()
def ipv4_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv4 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays IPv4 RIB entries"""

    def _ipv4(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(
            host=task.host,
            result=device.get_rib(afi="ipv4-unicast", lpm_address=address),
        )

    run_show(ctx, "ip_rib", _ipv4, field_filter)


@app.command()
def ipv6_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv6 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays IPv6 RIB entries"""

    def _ipv6(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(
            host=task.host,
            result=device.get_rib(afi="ipv6-unicast", lpm_address=address),
        )

    run_show(ctx, "ip_rib", _ipv6, field_filter)


@app.command()
def bgp_rib(
    ctx: typer.Context,
    route_fam: str = typer.Option(
        ..., "--route-fam", "-r", help="Route family", case_sensitive=False
    ),
    route_type: Optional[str] = typer.Option(
        None, "--route-type", "-t", help="Route type for EVPN"
    ),
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays BGP RIB"""

    def _bgp_rib(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        kwargs = {"route_fam": route_fam}
        if route_type is not None:
            kwargs["route_type"] = route_type
        return Result(host=task.host, result=device.get_bgp_rib(**kwargs))

    run_show(ctx, "bgp_rib", _bgp_rib, field_filter)


@app.command()
def mac(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays MAC Table"""

    def _mac(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_mac_table())

    run_show(ctx, "mac_table", _mac, field_filter)


@app.command()
def ni(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays Network Instances and interfaces"""

    def _ni(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_nwi_itf())

    run_show(ctx, "nwi_itfs", _ni, field_filter)


@app.command()
def lldp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays LLDP Neighbors"""

    def _lldp(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_lldp_sum())

    run_show(ctx, "lldp_nbrs", _lldp, field_filter)


@app.command()
def irb(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays IRB sub-interfaces"""

    def _irb(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_irb())

    run_show(ctx, "irb", _irb, field_filter)


@app.command()
def es(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays Ethernet Segments"""

    def _es(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_es())

    run_show(ctx, "es", _es, field_filter)


@app.command()
def es_dest(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays ES Destinations on the bridge table"""

    def _es_dest(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_es_dest())

    run_show(ctx, "es_dest", _es_dest, field_filter, title="L2-ES Destinations")


@app.command()
def vxlan(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays VXLAN tunnel interfaces and unicast destinations"""

    def _vxlan(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_vxlan())

    run_show(ctx, "vxlan", _vxlan, field_filter, title="VXLAN Tunnels")


@app.command()
def arp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays ARP table"""

    def _arp(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_arp())

    run_show(ctx, "arp", _arp, field_filter)


@app.command()
def ifstats(
    ctx: typer.Context,
    interval: int = typer.Option(5, "--interval", "-s", help="Seconds between samples"),
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays per-interface in/out bps from two consecutive samples"""

    def _ifstats(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_ifstats(interval=interval))

    run_show(ctx, "ifstats", _ifstats, field_filter, title=f"Interface Stats ({interval}s interval)")


@app.command()
def nd(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = typer.Option(None, "--field-filter", "-f"),
) -> None:
    """Displays IPv6 Neighbors"""

    def _nd(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_nd())

    run_show(ctx, "nd", _nd, field_filter)


if __name__ == "__main__":
    app()
