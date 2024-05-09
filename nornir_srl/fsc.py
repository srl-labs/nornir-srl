from typing import Any, Dict, List, Optional, Callable
import importlib
import fnmatch
import sys
import tempfile
import pkg_resources

from ruamel.yaml import YAML

from nornir import InitNornir
from nornir.core import Nornir

from nornir.core.task import Result, Task, AggregatedResult
from nornir.core.inventory import Host

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.box import MINIMAL_DOUBLE_HEAD
from rich.style import Style
from rich.theme import Theme


from nornir_utils.plugins.functions import print_result
from nornir_srl.tasks.srl_config import configure_device, restore_config

from nornir_srl.connections.srlinux import CONNECTION_NAME

import click
from click.core import Context

PYTHON_PKG_NAME = "nornir_srl"

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
    "runner": {
        "plugin": "threaded",
        "options": {
            "num_workers": 20,
        },
    },
    "user_defined": {
        "intent_dir": "intent",
    },
}


def get_project_version():
    try:
        version = pkg_resources.get_distribution(PYTHON_PKG_NAME).version
    except pkg_resources.DistributionNotFound:
        version = "Version not found"

    return version


def print_table(
    title: str,
    resource: str,
    results: AggregatedResult,
    filter: Optional[Dict],
    **kwargs,
) -> None:
    table_theme = Theme(
        {
            "ok": "green",
            "warn": "orange3",
            "info": "blue",
            "err": "bold red",
        }
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
    if kwargs.get("box_type") and kwargs["box_type"] != None:
        box_type = str(kwargs["box_type"]).upper()
        try:
            box_t = getattr(importlib.import_module("rich.box"), box_type)
        except AttributeError:
            print(
                f"Unknown box type {box_type}. Check 'python -m rich.box' for valid box types."
            )
            box_t = MINIMAL_DOUBLE_HEAD
    else:
        box_t = MINIMAL_DOUBLE_HEAD
    #    table = Table(title=title, highlight=True, box=MINIMAL_DOUBLE_HEAD)
    table = Table(title=title, highlight=True, box=box_t)
    table.add_column("Node", no_wrap=True)

    # get fields across a nested dict with dicts and lists of dicts
    def get_fields(b, depth=0):
        fields = []
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
        if filter == None:
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
        node: Host = r.host if r.host else Host("unkown")
        if r.failed:
            print(f"Failed to get {resource} for {host}. Exception: {r.exception}")
            continue
        if r.result and r.result.get(resource) is not None:
            for n, l in enumerate(r.result.get(resource)):
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
                if (
                    len([v for v in l.values() if isinstance(v, list)]) == 0
                ):  # single row per host
                    if pass_filter(common, filter):
                        rows.append(common)
                #                if pass_filter(row, filter):
                #                    rows.append({k:v for k,v in l.items()})
                else:
                    for key, v in l.items():
                        if isinstance(v, list):
                            first_row = True
                            for item in v:
                                row = {}
                                row.update(
                                    {
                                        k: y
                                        for k, y in item.items()
                                        if isinstance(y, (str, int, float))
                                        or (
                                            isinstance(y, list)
                                            and len(y) > 0
                                            and not isinstance(y[0], dict)
                                        )
                                    }
                                )
                                if pass_filter(
                                    {
                                        k: v
                                        for k, v in list(common.items())
                                        + list(row.items())
                                    },
                                    filter,
                                ):
                                    if first_row:
                                        rows.append(
                                            {
                                                k: v
                                                for k, v in list(common.items())
                                                + list(row.items())
                                            }
                                        )
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


@click.group()
@click.option(
    "--cfg",
    "-c",
    default="nornir_config.yaml",
    show_default=True,
    type=click.Path(),
    help="Nornir config file. Mutually exclusive with -t",
)
@click.option(
    "--inv-filter",
    "-i",
    multiple=True,
    help="inventory filter, e.g. -i site=lab -i role=leaf. Possible filter-fields are defined in inventory. Multiple filters are ANDed",
)
# @click.option(
#    "--format",
#    "-f",
#    multiple=False,
#    type=click.Choice(["table", "json", "yaml"]),
#    default="table",
#    help="Output format",
# )
@click.option(
    "--box-type",
    "-b",
    multiple=False,
    help="box type of printed table, e.g. -b minimal_double_head. 'python -m rich.box' for options",
)
@click.option(
    "--topo-file",
    "-t",
    multiple=False,
    type=click.Path(exists=True),
    help="CLAB topology file, e.g. -t topo.yaml. Mutually exclusive with -c",
)
@click.option(
    "--cert-file",
    multiple=False,
    type=click.Path(exists=True),
    help="CLAB certificate file, e.g. -c ca-root.pem",
)
@click.pass_context
@click.version_option(version=get_project_version())
def cli(
    ctx: Context,
    cfg: str,
    format: Optional[str] = None,
    inv_filter: Optional[List] = None,
    #    field_filter: Optional[List] = None,
    box_type: Optional[str] = None,
    topo_file: Optional[str] = None,
    cert_file: Optional[str] = None,
) -> None:
    ctx.ensure_object(dict)
    if topo_file:  # CLAB mode, -c ignored, inventory generated from topo file
        yaml = YAML(typ="safe")
        try:
            with open(topo_file, "r") as f:
                topo = yaml.load(f)
        except Exception as e:
            print(f"Failed to load topology file {topo_file}: {e}")
            sys.exit(1)
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
        srlinux_def = (
            True
            if "srlinux:" in topo["topology"].get("defaults", {}).get("image", "")
            else False
        )
        srl_kinds = [
            k
            for k, v in topo["topology"].get("kinds", {}).items()
            if "srlinux:" in v.get("image")
        ]
        clab_nodes: Dict[str, Dict] = topo["topology"]["nodes"]
        for node, node_spec in clab_nodes.items():
            if (not "kind" in node_spec and srlinux_def) or node_spec.get(
                "kind"
            ) in srl_kinds:
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

        try:
            with tempfile.NamedTemporaryFile("w+") as hosts_f:
                yaml.dump(hosts, hosts_f)
                hosts_f.seek(0)
                with tempfile.NamedTemporaryFile("w+") as groups_f:
                    yaml.dump(groups, groups_f)
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
        except Exception as e:
            raise e
    else:
        fabric = InitNornir(config_file=cfg)

    i_filter = (
        {k: v for k, v in [f.split("=") for f in inv_filter]} if inv_filter else {}
    )
    target: Nornir
    if i_filter:
        target = fabric.filter(**i_filter)
    else:
        target = fabric
    ctx.obj["target"] = target
    ctx.obj["i_filter"] = i_filter

    if box_type:
        box_type = box_type.upper()
    ctx.obj["box_type"] = box_type
    ctx.obj["format"] = format


def print_report(
    result: AggregatedResult,
    name: str,
    failed_hosts: List,
    box_type: Optional[str] = None,
    f_filter: Optional[Dict] = None,
    i_filter: Optional[Dict] = None,
) -> None:
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


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def bgp_peers(ctx: Context, field_filter: Optional[List] = None):
    """Displays BGP Peers and their status"""

    def _bgp_peers(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_sum_bgp())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_bgp_peers, name="bgp_peers", raise_on_error=False
    )
    if ctx.obj["format"] == "json":
        print_result(result)
    elif ctx.obj["format"] == "yaml":
        yaml = YAML(typ="safe")
        yaml.dump(result, sys.stdout)
    else:
        print_report(
            result=result,
            name="BGP Peers",
            failed_hosts=result.failed_hosts,
            box_type=ctx.obj["box_type"],
            f_filter=f_filter,
            i_filter=ctx.obj["i_filter"],
        )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def sys_info(ctx: Context, field_filter: Optional[List] = None):
    """Displays System Info of nodes"""

    def _sys_info(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_info())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_sys_info, name="sys_info", raise_on_error=False
    )
    print_report(
        result=result,
        name="System Info",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def subif(ctx: Context, field_filter: Optional[List] = None):
    """Displays Sub-Interfaces of nodes"""

    def _subinterfaces(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_sum_subitf())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_subinterfaces, name="subinterface", raise_on_error=False
    )
    print_report(
        result=result,
        name="Sub-Interfaces",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def lag(ctx: Context, field_filter: Optional[List] = None):
    """Displays LAGs of nodes"""

    def _lag(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_lag())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(task=_lag, name="lag", raise_on_error=False)
    print_report(
        result=result,
        name="LAGs",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
@click.option(
    "--address",
    "-a",
    multiple=False,
    help="Look up specified address in the IPv4 RIB using LPM",
)
def ipv4_rib(
    ctx: Context, address: Optional[str] = None, field_filter: Optional[List] = None
):
    """Displays IPv4 RIB entries, LPM lookup"""

    def _ipv4_rib(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(
            host=task.host,
            result=device.get_rib(afi="ipv4-unicast", lpm_address=address),
        )

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(task=_ipv4_rib, name="ip_rib", raise_on_error=False)
    print_report(
        result=result,
        name=f"IPv4 RIB {'- hunting for ' + address if address else ''}",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
@click.option(
    "--address",
    "-a",
    multiple=False,
    help="Look up specified address in the IPv4 RIB using LPM",
)
def ipv6_rib(
    ctx: Context, address: Optional[str] = None, field_filter: Optional[List] = None
):
    """Displays IPv4 RIB entries, LPM lookup"""

    def _ipv6_rib(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(
            host=task.host,
            result=device.get_rib(afi="ipv6-unicast", lpm_address=address),
        )

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(task=_ipv6_rib, name="ip_rib", raise_on_error=False)
    print_report(
        result=result,
        name=f"IPv6 RIB {'- hunting for ' + address if address else ''}",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
@click.option(
    "--route-fam",
    "-r",
    multiple=False,
    required=True,
    type=click.Choice(["evpn", "ipv4", "ipv6"]),
    help="Route family for BGP RIB",
)
@click.option(
    "--route-type",
    "-t",
    multiple=False,
    type=click.Choice(["1", "2", "3", "4", "5"]),
    default="2",
    help="Route type for EVPN routes",
)
def bgp_rib(
    ctx: Context,
    route_fam: str,
    route_type: Optional[str] = None,
    field_filter: Optional[List] = None,
):
    """Displays BGP RIB"""

    def _bgp_rib(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(
            host=task.host,
            result=device.get_bgp_rib(route_fam=route_fam, route_type=route_type),
        )

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(task=_bgp_rib, name="bgp_rib", raise_on_error=False)
    print_report(
        result=result,
        name=f"BGP RIB - {route_fam.upper()}{' route-type ' + route_type if route_type and route_fam == 'evpn' else ''}",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def mac(ctx: Context, field_filter: Optional[List] = None):
    """Displays MAC Table"""

    def _mac_table(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_mac_table())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_mac_table, name="mac_table", raise_on_error=False
    )
    print_report(
        result=result,
        name="MAC Table",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def ni(ctx: Context, field_filter: Optional[List] = None):
    """Displays Network Instances and interfaces"""

    def _network_instances(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_nwi_itf())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_network_instances, name="nwi_itfs", raise_on_error=False
    )
    print_report(
        result=result,
        name="Network Instances and interfaces",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f state=up -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def lldp(ctx: Context, field_filter: Optional[List] = None):
    """Displays LLDP Neighbors"""

    def _lldp_neighbors(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_lldp_sum())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )
    result = ctx.obj["target"].run(
        task=_lldp_neighbors, name="lldp_nbrs", raise_on_error=False
    )
    print_report(
        result=result,
        name="LLDP Neighbors",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f name=ge-0/0/0 -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def es(ctx: Context, field_filter: Optional[List] = None):
    """Displays Ethernet Segments"""

    def _es(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_es())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )

    result = ctx.obj["target"].run(task=_es, name="es", raise_on_error=False)
    print_report(
        result=result,
        name="Ethernet Segments",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f name=ge-0/0/0 -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def arp(ctx: Context, field_filter: Optional[List] = None):
    """Displays ARP table"""

    def _arp(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_arp())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )

    result = ctx.obj["target"].run(task=_arp, name="arp", raise_on_error=False)
    print_report(
        result=result,
        name="ARP table",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


@cli.command()
@click.pass_context
@click.option(
    "--field-filter",
    "-f",
    multiple=True,
    help='filter fields with <field-name>=<glob-pattern>, e.g. -f name=ge-0/0/0 -f admin_state="ena*". Fieldnames correspond to column names of a report',
)
def nd(ctx: Context, field_filter: Optional[List] = None):
    """Displays IPv6 Neighbors"""

    def _nd(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=device.get_nd())

    f_filter = (
        {k: v for k, v in [f.split("=") for f in field_filter]} if field_filter else {}
    )

    result = ctx.obj["target"].run(task=_nd, name="nd", raise_on_error=False)
    print_report(
        result=result,
        name="IPv6 Neighbors",
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
    )


if __name__ == "__main__":
    cli(obj={})
