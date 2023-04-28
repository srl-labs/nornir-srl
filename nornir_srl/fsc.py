from typing import Any, Dict, List, Optional, Callable
import importlib

from nornir import InitNornir

from nornir.core.task import Result, Task, AggregatedResult

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.box import    MINIMAL_DOUBLE_HEAD
from rich.style import Style
from rich.theme import Theme

from nornir_utils.plugins.functions import print_result
from nornir_srl.tasks.srl_config import configure_device, restore_config

from nornir_srl.connections.srlinux import CONNECTION_NAME

import click

def sys_info(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result=device.get_info())

def get_itfs(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result=device.get_itf())

def subinterface(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result=device.get_sum_subitf())

def bgp_peers(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result = device.get_sum_bgp())

def ipv4_rib(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result = device.get_rib_ipv4())

def bgp_rib(task: Task, **kwargs) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    route_fam = kwargs.get("route_fam")
    route_type = kwargs.get("route_type", "2")
    if not route_fam:
        raise Exception("bgp_rib task requires route_fam report option")
    return Result(host=task.host, result = device.get_bgp_rib(route_fam=route_fam, route_type=route_type))

def mac_table(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result = device.get_mac_table())

def nwi_itfs(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result = device.get_nwi_itf())

def lldp_nbrs(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result = device.get_lldp_sum())

def print_table ( title: str, resource: str, results: AggregatedResult, filter: Optional[Dict], **kwargs) -> None:

    table_theme = Theme({
        "ok": "green",
        "warn": "orange3",
        "info": "blue",
        "err": "bold red",
    })
    STYLE_MAP = {
        'up': '[ok]',
        'down': '[err]',
        'enable': '[ok]',
        'disable': '[info]',
        'routed': '[cyan]',
        'bridged': '[blue]',
        'established': '[ok]',
        'active': '[cyan]',
    }

    console = Console(theme=table_theme)
    if kwargs.get("box_type") and kwargs["box_type"] != None:
        box_type = str(kwargs["box_type"]).upper()
        try:
            box_type = getattr(importlib.import_module("rich.box"), box_type)
        except AttributeError:
            print(f"Unknown box type {box_type}. Check 'python -m rich.box' for valid box types.")
            box_type = MINIMAL_DOUBLE_HEAD
    else:
        box_type = MINIMAL_DOUBLE_HEAD
#    table = Table(title=title, highlight=True, box=MINIMAL_DOUBLE_HEAD)
    table = Table(title=title, highlight=True, box=box_type)
    table.add_column('Node', no_wrap=True)

    # get fields across a nested dict with dicts and lists of dicts
    def get_fields(b, depth=0):
        fields = []
        if isinstance(b, list) and len(b) > 0:
            fields.extend(get_fields(b[0], depth=depth+1))
        elif isinstance(b, dict):
            for k,v in b.items():
                if isinstance(v, list) and len(v)>0  and isinstance(v[0], dict):
                    fields.extend(get_fields(v[0], depth=depth+1))
                elif isinstance(v, dict):
                    fields.extend(get_fields(v, depth=depth+1))
                else:
                    fields.append(k)
            if depth>0:
                fields = sorted(fields)
        return fields

    def pass_filter(row, filter):
        if filter == None:
            return True
        if len(
            {
                k:v for k,v in row.items() 
                    if filter.get(k) and str(filter[k]) in str(row[k])
            }
        ) < len(filter):
            return False
        else:
            return True
    
    col_names = []   
    for host, host_result in results.items():
        rows = []
        r = host_result[0]  # only look at result of first task, 1 task per table
        if r.failed:
            print(f"Failed to get {resource} for {host}. Exception: {r.exception}")
            continue
        if r.result.get(resource) == None:
            continue
        for n, l in enumerate(r.result.get(resource)):
            if len(col_names) == 0:
                col_names = get_fields(l)
                for col in col_names:
                    table.add_column(col, no_wrap=True)
            common = {x:y for x,y in l.items() if  isinstance(y, (str, int, float)) or 
                      (isinstance(y, list) and len(y) > 0 and not isinstance(y[0], dict)) }
            if len ( [v for v in l.values() if isinstance(v, list)] ) == 0: # single row per host
                if pass_filter(common, filter):
                    rows.append(common)
#                if pass_filter(row, filter):
#                    rows.append({k:v for k,v in l.items()})
            else:
                for key,v in l.items():
                    if isinstance(v, list):
                        first_row=True
                        for item in v:
                            row = {}
                            row.update({k:y for k,y in item.items() if isinstance(y, (str, int, float)) or
                                        ( isinstance(y, list) and len(y)>0 and not isinstance(y[0], dict))})
                            if pass_filter({k:v for k,v in list(common.items()) + list(row.items()) }, filter):
                                if first_row:
                                    rows.append( {k:v for k,v in list(common.items()) + list(row.items()) } )
                                else:
                                    rows.append(row)
                                first_row = False
                                    
        first_row = True
        for row in rows:
            for k, v in row.items():
                row[k] = str(STYLE_MAP.get(str(v),'')) + str(v)
            values = [ row.get(k,'') for k in col_names ]
            if first_row:
                table.add_row(host, *values) 
                first_row = False
            else:
                table.add_row( "", *values)

        table.add_section()
    console.print(table)


@click.command
@click.argument('report')
@click.option('--cfg', '-c', default='nornir_config.yaml', show_default=True, help='Nornir config file')
@click.option('--inv-filter', '-i', multiple=True, help='filter inventory, e.g. -i site=lab -i role=leaf')
@click.option('--field-filter', '-f', multiple=True, help='filter fields, e.g. -f state=up -f admin_state=enable')
@click.option('--box-type', '-b', multiple=False, help="box type of printed table, e.g. -b minimal_double_head. 'python -m rich.box' for options")
@click.option('--report-options', '-r', multiple=True, help="report-specific options, e.g. -o route_fam=evpn -o route_type=2 for 'bgp-rib report")
def cli(
    report: str, 
    cfg: str = 'config.yaml',
    inv_filter:Optional[List] = None,
    field_filter:Optional[List] = None,
    report_options:Optional[List] = None,
    box_type:Optional[str] = None,
    ) -> None: 

    i_filter = {k:v for k,v in [ f.split('=') for f in inv_filter]} if inv_filter else {}
    f_filter = {k:v for k,v in [ f.split('=') for f in field_filter]} if field_filter else {}
    report_options = {k:v for k,v in [ f.split('=') for f in report_options]} if report_options else {}

    if box_type:
        box_type = box_type.upper()

    reports = {
        'bgp-peers': (bgp_peers, "BGP Peers"),
        'subinterface': (subinterface, "Sub-Interfaces"),
        'ipv4-rib': (ipv4_rib, "IPv4 RIB"),
        'mac-table': (mac_table, "MAC Table"),
        'sys-info': (sys_info, "System Info"),
        'nwi-itfs': (nwi_itfs, "Network-Instance Interfaces"),
        'lldp-nbrs': (lldp_nbrs, "LLDP Neighbors"),
        'bgp-rib': (bgp_rib, "BGP RIB"),
    }

    if report not in reports:
        click.echo(f"Report {report} not found. Available reports: {list(reports.keys())}")
        return
    if not field_filter:
        field_filter = {}
    fabric = InitNornir(config_file=cfg)
    if i_filter:
        target = fabric.filter(**i_filter)
    else:
        target = fabric
    result = target.run(task=reports[report][0], raise_on_error=False, **report_options)
    title = "[bold]" + reports[report][1] + "[/bold]"
    if f_filter:
        title += "\nFields:" + str(f_filter)
    if i_filter:
        title += "\nInventory:" + str(i_filter)
    if report_options:
        title += "\nReport options:" + str(report_options)
    if len(target.data.failed_hosts)>0:
        title += "\n[red]Failed hosts:" + str(target.data.failed_hosts)
    print_table(
            title=title, 
            resource=result.name, 
            results=result, 
            filter=f_filter,
            box_type=box_type,
            )


if __name__ == "__main__":
     cli()


