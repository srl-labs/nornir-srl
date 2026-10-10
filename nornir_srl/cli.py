import csv
import glob
import io
import json
import logging
import os
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional

import typer
import yaml  # type: ignore
from rich.console import Console
from rich.table import Table
from rich.box import MINIMAL_DOUBLE_HEAD
from rich.theme import Theme
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.inventory import ConnectionOptions
from nornir.core.task import Result, Task, AggregatedResult

from . import clab
from .checks import CHECKS, CHECKS_COLUMNS, Finding, collect_fabric_state, run_checks
from .connections.srlinux import CONNECTION_NAME
from .connections.routing import BGP_RIB_ROUTE_FAM_ALIASES
from .connections.helpers import clean_structured_key
from .fabric import collect_fabric_state as collect_lens_state
from .history import DEFAULT_RETENTION_DAYS
from .lenses import LensSpec, get_lens
from .records import as_dict
from .reports import ReportSpec, fabric_args, get_report
from .rows import NodeRows, Row, Table as ReportTable, cell, clean_columns, extract, pass_filter
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


app = typer.Typer(name="fcli", help="SR Linux Observability tool")
logger = logging.getLogger(__name__)


SRL_DEFAULT_GNMI_PORT = clab.SRL_DEFAULT_GNMI_PORT
NORNIR_DEFAULT_CONFIG = clab.NORNIR_DEFAULT_CONFIG


# ------------------------- helpers -------------------------


def _apply_tls_options(
    fabric: Nornir,
    cert_file: Optional[Path],
    skip_verify: Optional[bool],
    tls_server_name: Optional[str],
) -> None:
    """Force the TLS settings given on the command line onto every host.

    Nornir resolves an ``extras`` dict from the most specific level that
    defines one and does not merge across levels, so an inventory carrying any
    extras of its own would otherwise mask what was asked for here.
    """
    overrides: Dict[str, Any] = {}
    if cert_file:
        overrides["path_cert"] = str(cert_file)
    if skip_verify is not None:
        overrides["skip_verify"] = bool(skip_verify)
    if tls_server_name:
        overrides["override"] = tls_server_name
    if not overrides:
        return
    for host in fabric.inventory.hosts.values():
        resolved = host.get_connection_parameters(CONNECTION_NAME)
        extras = dict(resolved.extras or {})
        extras.update(overrides)
        options = host.connection_options.get(CONNECTION_NAME)
        if options is None:
            host.connection_options[CONNECTION_NAME] = ConnectionOptions(
                hostname=resolved.hostname,
                port=resolved.port,
                username=resolved.username,
                password=resolved.password,
                platform=resolved.platform,
                extras=extras,
            )
        else:
            options.extras = extras


def _report_failure(resource: str) -> Callable[[str, Optional[BaseException]], None]:
    """Report a host that failed, and leave it out of the table."""

    def on_error(node: str, exception: Optional[BaseException]) -> None:
        # stderr, so -o json|yaml|csv on stdout still parses with a node down.
        typer.echo(f"Failed to get {resource} for {node}. Exception: {exception}", err=True)

    return on_error


def print_structured(
    col_names: List[str],
    rows: List[Dict[str, Any]],
    output_format: OutputFormat,
) -> None:
    """Print data in JSON, YAML, or CSV format."""
    if not rows:
        typer.echo("No data...")
        return

    col_names = clean_columns(col_names)
    rows = [{clean_structured_key(k): v for k, v in row.items()} for row in rows]

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


TABLE_THEME = Theme(
    {"ok": "green", "warn": "orange3", "info": "blue", "err": "bold red"}
)

#: Wide or server-oriented columns shown in live/structured output but not CLI tables.
CLI_TABLE_OMIT: Dict[str, FrozenSet[str]] = {
    "bgp_rib": frozenset({"communities"}),
    "bgp_peers": frozenset({"local-address", "local-port"}),
    # The port state streams alongside the counters on the server; the CLI's
    # two samples read only the counters, so here these would always be blank.
    "ifstats": frozenset({"oper-state", "down-reason"}),
}


def _cli_table_omit(name: Optional[str]) -> FrozenSet[str]:
    if not name:
        return frozenset()
    if name.startswith("bgp_rib_"):
        return CLI_TABLE_OMIT["bgp_rib"]
    return CLI_TABLE_OMIT.get(name, frozenset())


def _cli_table_columns(name: str, columns: List[str]) -> List[str]:
    omit = _cli_table_omit(name)
    if not omit:
        return columns
    return [column for column in columns if column not in omit]


#: Values worth colouring wherever they turn up in a table.
STYLE_MAP = {
    "up": "[ok]",
    "down": "[err]",
    "enable": "[ok]",
    "disable": "[info]",
    "routed": "[cyan]",
    "bridged": "[blue]",
    "established": "[ok]",
    "active": "[cyan]",
    "error": "[err]",
    "warning": "[warn]",
}


def _box(box_type: Optional[str]) -> Any:
    if not box_type:
        return MINIMAL_DOUBLE_HEAD
    name = str(box_type).upper()
    try:
        return getattr(__import__("rich.box", fromlist=["box"]), name)
    except AttributeError:
        typer.echo(
            f"Unknown box type {name}. Check 'python -m rich.box' for valid box types."
        )
        return MINIMAL_DOUBLE_HEAD


def _cell(value: Any) -> str:
    text = str(value)
    return STYLE_MAP.get(text, "") + text


def print_table(
    title: str,
    columns: List[str],
    per_node: List[NodeRows],
    *,
    box_type: Optional[str] = None,
    node_prefix: Optional[str] = None,
) -> None:
    """Render the extracted rows as a rich table, one section per node."""
    console = Console(theme=TABLE_THEME)
    console._emoji = False
    table = Table(title=title, highlight=True, box=_box(box_type))
    table.add_column("Node", no_wrap=True)
    for col in columns:
        table.add_column(col, no_wrap=False)

    if not node_prefix and per_node:
        names = [n.node for n in per_node if n.node]
        if len(names) > 1 and all(n.startswith("clab-") for n in names):
            common = os.path.commonprefix(names)
            if "-" in common:
                node_prefix = common.rsplit("-", 1)[0] + "-"

    for node in per_node:
        first = True
        node_display = node.node
        if node_prefix and node_display.startswith(node_prefix):
            node_display = node_display[len(node_prefix):]
        for row in node.rows:
            # Fields a row inherited from its parent item are shown once, so a
            # parent with many sub-rows reads as one entry spanning them.
            cells = row.cells(group=True)
            values = [_cell(cells.get(col, "")) for col in columns]
            table.add_row(node_display if first else "", *values)
            first = False
        table.add_section()

    if len(table.columns) > 1:
        console.print(table)
    else:
        console.print("[i]No data...[/i]")


def print_report(
    result: AggregatedResult,
    name: str,
    failed_hosts: List[str],
    box_type: Optional[str] = None,
    f_filter: Optional[Dict] = None,
    i_filter: Optional[Dict] = None,
    output: OutputFormat = OutputFormat.TABLE,
    table: Optional[ReportTable] = None,
    node_prefix: Optional[str] = None,
) -> None:
    columns, per_node = extract(
        result.name,
        result,
        field_filter=f_filter,
        on_error=_report_failure(result.name),
        table=table,
    )
    if table is not None and output in (OutputFormat.JSON, OutputFormat.YAML):
        # The records themselves, each saying which node it is from.
        print_records(
            [{"node": node.node, **as_dict(record)} for node in per_node for record in node.records],
            output,
        )
        return
    if output == OutputFormat.TABLE:
        columns = _cli_table_columns(result.name, columns)
        title = "[bold]" + name + "[/bold]"
        if f_filter:
            title += "\nFields filter:" + str(f_filter)
        if i_filter:
            title += "\nInventory filter:" + str(i_filter)
        if len(failed_hosts) > 0:
            title += "\n[red]Failed hosts:" + str(failed_hosts)
        if not columns:
            logger.debug("No data returned for %s: %s", result.name, result)
        print_table(title, columns, per_node, box_type=box_type, node_prefix=node_prefix)
    else:
        rows = [
            {"Node": node.node, **row.values} for node in per_node for row in node.rows
        ]
        print_structured(columns, rows, output)


def _is_help_requested() -> bool:
    """True if --help or -h was passed on the command line or via runner invoke."""
    if any(arg in sys.argv for arg in ("--help", "-h")):
        return True
    import inspect

    frame = inspect.currentframe()
    while frame:
        args = frame.f_locals.get("args")
        if isinstance(args, (list, tuple)) and any(a in ("--help", "-h") for a in args):
            return True
        frame = frame.f_back
    return False


@app.callback()
def main(
    ctx: typer.Context,
    cfg: Optional[Path] = typer.Option(
        None,
        "--cfg",
        "-c",
        help="Nornir config file. Mutually exclusive with -t. Defaults to nornir_config.yaml",
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
    fabric_name: Optional[str] = typer.Option(
        None,
        "--fabric",
        envvar="FCLI_FABRIC",
        help=(
            "Name the fabric's history, snapshots, acks and cabling are kept under. "
            "Defaults to the topology's name, or the directory of the Nornir config file"
        ),
    ),
    cert_file: Optional[Path] = typer.Option(
        None,
        "--cert-file",
        exists=True,
        help="PEM trust anchor used to verify the gNMI certificate of every node",
    ),
    skip_verify: Optional[bool] = typer.Option(
        None,
        "--skip-verify/--verify",
        help=(
            "Trust the certificate a node presents without verifying it. "
            "The default when no --cert-file is given"
        ),
    ),
    tls_server_name: Optional[str] = typer.Option(
        None,
        "--tls-server-name",
        help="Name to verify the gNMI certificate against, when it differs from the node's hostname",
    ),
    gnmi_port: int = typer.Option(
        SRL_DEFAULT_GNMI_PORT,
        "--gnmi-port",
        "-p",
        help="gNMI port for SR Linux nodes (default: 57400)",
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
    if _is_help_requested():
        return

    lab_name = None
    node_prefix = None
    if topo_file is None and cfg is None:
        topo_env = os.environ.get("FCLI_TOPO") or os.environ.get("CLAB_TOPO")
        if topo_env and Path(topo_env).exists():
            topo_file = Path(topo_env)
        else:
            clab_files = [Path(f) for f in glob.glob("*.clab.y*ml") if os.path.isfile(f)]
            if len(clab_files) == 1:
                topo_file = clab_files[0]
                logger.debug("Auto-discovered containerlab topology: %s", topo_file)
            elif len(clab_files) > 1:
                non_examples = [f for f in clab_files if not f.name.startswith("example.")]
                if len(non_examples) == 1:
                    topo_file = non_examples[0]
                    logger.debug("Auto-discovered containerlab topology: %s", topo_file)

    if topo_file:
        try:
            with open(topo_file, "r") as f:
                topo = yaml.safe_load(os.path.expandvars(f.read()))
        except Exception as e:
            typer.echo(f"Failed to load topology file {topo_file}: {e}", err=True)
            raise typer.Exit(1)
        lab_name = topo.get("name", "")
        node_prefix = clab.node_prefix(topo)
        hosts = clab.srl_hosts(topo)
        logger.debug(
            "topology '%s' from %s holds %d SR Linux node(s): %s",
            lab_name,
            topo_file,
            len(hosts),
            ", ".join(sorted(hosts)),
        )
        groups = clab.srl_groups(
            gnmi_port,
            str(cert_file) if cert_file else None,
            skip_verify=skip_verify,
            tls_server_name=tls_server_name,
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
                try:
                    fabric = InitNornir(**conf)
                except Exception as exc:
                    typer.echo(f"Error initializing inventory from topology '{topo_file}': {exc}", err=True)
                    raise typer.Exit(1)
    else:
        if cfg is None:
            cfg = Path("nornir_config.yaml")
        if not cfg.exists():
            typer.echo(
                f"Config file '{cfg}' does not exist. Provide -c/--cfg or -t/--topo-file.",
                err=True,
            )
            raise typer.Exit(1)
        logger.debug("initializing Nornir from %s", cfg)
        try:
            fabric = InitNornir(config_file=str(cfg))
            _apply_tls_options(fabric, cert_file, skip_verify, tls_server_name)
        except Exception as exc:
            typer.echo(f"Error loading inventory from '{cfg}': {exc}", err=True)
            typer.echo("Tip: Provide -t/--topo-file <topo.clab.yml> or -c/--cfg <nornir_config.yaml> with a valid inventory.", err=True)
            raise typer.Exit(1)

    i_filter = (
        {k: v for k, v in (f.split("=") for f in inv_filter)} if inv_filter else {}
    )
    resolved_filter = {}
    for k, v in i_filter.items():
        attr = "name" if k == "node" else k
        val = v
        if attr == "name" and node_prefix and not val.startswith(node_prefix):
            prefixed = f"{node_prefix}{val}"
            if prefixed in fabric.inventory.hosts:
                val = prefixed
        resolved_filter[attr] = val

    target: Nornir = fabric.filter(**resolved_filter) if resolved_filter else fabric
    logger.debug(
        "inventory holds %d node(s), %d selected by filter %s: %s",
        len(fabric.inventory.hosts),
        len(target.inventory.hosts),
        i_filter or "-",
        ", ".join(sorted(target.inventory.hosts)),
    )
    ctx.obj["target"] = target
    # The whole inventory, for the reports that ask which neighbours are
    # the fabric's whatever the filter selected.
    ctx.obj["fabric"] = fabric
    ctx.obj["i_filter"] = resolved_filter
    ctx.obj["box_type"] = box_type.upper() if box_type else None
    ctx.obj["output"] = output
    ctx.obj["log_level"] = log_level.value
    # Whatever the inventory came from, the state kept on disk belongs to one
    # fabric: named on the command line, after the lab, or after the directory
    # the Nornir config lives in, so two inventories never share a history.
    if fabric_name:
        fabric_source = "option"
    elif topo_file and lab_name:
        fabric_name, fabric_source = lab_name, "clab"
    else:
        fabric_name = (cfg.resolve().parent.name if cfg else "") or None
        fabric_source = "nornir" if fabric_name else None
    logger.debug("fabric '%s' (%s)", fabric_name, fabric_source or "-")
    ctx.obj["topo_name"] = fabric_name
    ctx.obj["fabric_source"] = fabric_source
    ctx.obj["node_prefix"] = node_prefix


# ------------------------- command helpers -------------------------


def _task_for(spec: ReportSpec, params: Dict[str, Any]) -> Callable[[Task], Result]:
    """Wrap a report's getter as a Nornir task."""

    def task_func(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=spec.getter(device, **params))

    return task_func


def run_query(
    ctx: typer.Context, spec: ReportSpec, **params: Any
) -> AggregatedResult:
    """Run a report's getter across the filtered inventory."""
    target: Nornir = ctx.obj["target"]
    logger.debug(
        "running report '%s' (resource '%s') on %d node(s) with params %s",
        spec.name,
        spec.resource,
        len(target.inventory.hosts),
        params or "-",
    )
    started = time.perf_counter()
    result = target.run(
        task=_task_for(spec, {**params, **fabric_args(spec, ctx.obj["fabric"].inventory.hosts)}),
        name=spec.resource,
        raise_on_error=False,
    )
    logger.debug(
        "report '%s' finished in %.3fs, %d/%d node(s) failed: %s",
        spec.name,
        time.perf_counter() - started,
        len(result.failed_hosts),
        len(result),
        ", ".join(sorted(result.failed_hosts)) or "none",
    )
    logger.debug("Aggregated result for %s: %s", spec.name, result)
    return result


def report_table(
    ctx: typer.Context, spec: ReportSpec, **params: Any
) -> Dict[str, Any]:
    """Render a report into the table shape the server and snapshots share.

    The same keys, the same cleaned column names and the same cells the live
    server produces, so a snapshot taken here compares against one taken there.
    """
    result = run_query(ctx, spec, **params)
    errors: List[Dict[str, str]] = []

    def on_error(node: str, exception: Optional[BaseException]) -> None:
        errors.append({"node": node, "error": str(exception)})

    raw_columns, per_node = extract(spec.resource, result, on_error=on_error, table=spec.table_for(params))
    columns = clean_columns(raw_columns)
    rows = [
        {
            "Node": node.node,
            **{c: cell(row.values.get(raw)) for c, raw in zip(columns, raw_columns)},
        }
        for node in per_node
        for row in node.rows
    ]
    return {
        "report": spec.name,
        "title": spec.title,
        "columns": ["Node"] + columns,
        "rows": rows,
        "errors": errors,
        "nodes": len(per_node),
        "generated": time.time(),
    }


def print_table_shape(
    table: Dict[str, Any],
    *,
    box_type: Optional[str] = None,
    output: OutputFormat = OutputFormat.TABLE,
    node_prefix: Optional[str] = None,
) -> None:
    """Print a table in the shape :func:`report_table` returns."""
    # A comparison of two nodes has no Node column: it is the one thing the
    # two are guaranteed to disagree about, so it is dropped from the table.
    grouped = "Node" in table["columns"]
    columns = [c for c in table["columns"] if c != "Node"]
    if output == OutputFormat.TABLE:
        columns = _cli_table_columns(table.get("report", ""), columns)
    rows = table["rows"]
    for error in table.get("errors") or []:
        typer.echo(f"{error['node']}: {error['error']}", err=True)
    if output != OutputFormat.TABLE:
        print_structured(columns, rows, output)
        return
    per_node: Dict[str, NodeRows] = {}
    for row in rows:
        node = str(row.get("Node", "")) if grouped else ""
        per_node.setdefault(node, NodeRows(node=node)).rows.append(
            Row({c: row.get(c, "") for c in columns})
        )
    print_table(
        f"[bold]{table['title']}[/bold]",
        columns,
        list(per_node.values()),
        box_type=box_type,
        node_prefix=node_prefix,
    )


def print_findings(
    findings: List[Finding],
    *,
    box_type: Optional[str] = None,
    f_filter: Optional[Dict[str, str]] = None,
    output: OutputFormat = OutputFormat.TABLE,
    node_prefix: Optional[str] = None,
) -> None:
    """Print what the checks found, worst node first."""
    columns = [c for c in CHECKS_COLUMNS if c != "Node"]
    rows = [f.as_row() for f in findings]
    if f_filter:
        rows = [row for row in rows if pass_filter(row, f_filter)]

    if output != OutputFormat.TABLE:
        print_structured(columns, rows, output)
        return

    per_node: Dict[str, NodeRows] = {}
    for row in rows:
        node = per_node.setdefault(str(row["Node"]), NodeRows(node=str(row["Node"])))
        node.rows.append(Row({c: row[c] for c in columns}))
    if not per_node:
        Console(theme=TABLE_THEME).print("[ok]No findings.[/ok]")
        return
    print_table(
        f"[bold]Fabric checks[/bold]\n{len(rows)} finding(s) on {len(per_node)} node(s)",
        columns,
        list(per_node.values()),
        box_type=box_type,
        node_prefix=node_prefix,
    )


def run_report(
    ctx: typer.Context,
    name: str,
    field_filter: Optional[List[str]] = None,
    title: Optional[str] = None,
    **params: Any,
) -> None:
    """Run the named report from the registry and print it."""
    spec = get_report(name)
    f_filter = (
        {k: v for k, v in (f.split("=") for f in field_filter)} if field_filter else {}
    )
    result = run_query(ctx, spec, **params)
    print_report(
        result=result,
        name=title or spec.title,
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
        output=ctx.obj["output"],
        table=spec.table_for(params),
        node_prefix=ctx.obj.get("node_prefix"),
    )


def print_records(
    records: List[Dict[str, Any]],
    output_format: OutputFormat,
) -> None:
    """Print records as JSON or YAML, as the objects they are.

    A record is what a lens found, with its lists as lists and its counts as
    numbers; the row a table makes of it joins and truncates those for a
    reader. Whatever reads the output by machine wants the former.
    """
    if not records:
        typer.echo("No data...")
        return
    if output_format == OutputFormat.JSON:
        typer.echo(json.dumps(records, indent=2, default=str))
    else:
        typer.echo(
            yaml.safe_dump(records, default_flow_style=False, sort_keys=False).rstrip()
        )


def print_lens(
    spec: LensSpec,
    records: List[Any],
    *,
    box_type: Optional[str] = None,
    f_filter: Optional[Dict[str, str]] = None,
    output: OutputFormat = OutputFormat.TABLE,
    errors: Optional[List[str]] = None,
    subtitle: str = "",
    node_prefix: Optional[str] = None,
) -> None:
    """Print what a lens answered, one section per node."""
    for error in errors or []:
        typer.echo(error, err=True)
    # A field filter names columns, so a record is kept by the row it makes.
    answered = [(record, spec.row(record)) for record in records]
    if f_filter:
        answered = [(rec, row) for rec, row in answered if pass_filter(row, f_filter)]
    if output in (OutputFormat.JSON, OutputFormat.YAML):
        print_records([as_dict(rec) for rec, _row in answered], output)
        return
    columns = spec.column_names
    rows = [row for _rec, row in answered]
    if output != OutputFormat.TABLE:
        print_structured(columns, rows, output)
        return
    if not rows:
        Console(theme=TABLE_THEME).print("[i]No data...[/i]")
        return

    # Consecutive rows of one node are one section. A lens whose rows are an
    # ordered walk rather than a list keeps that order: grouping every row of a
    # node together would pull hop 3 up beside hop 1 and lose the sequence.
    if spec.group_by_node:
        rows = sorted(rows, key=lambda r: str(r.get("Node", "")))
    sections: List[NodeRows] = []
    for row in rows:
        node = str(row.get("Node", ""))
        if not sections or sections[-1].node != node:
            sections.append(NodeRows(node=node))
        sections[-1].rows.append(Row({c: row.get(c, "") for c in columns}))

    title = f"[bold]{spec.title}[/bold]"
    if subtitle:
        title += f"\n{subtitle}"
    print_table(title, columns, sections, box_type=box_type, node_prefix=node_prefix)


def run_lens(
    ctx: typer.Context,
    lens: str,
    field_filter: Optional[List[str]] = None,
    subtitle: str = "",
    **params: Any,
) -> None:
    """Collect what a lens reads, run it, and print the answer.

    *params* are the lens's own arguments, so nothing here may be called what
    one of them is: ``service`` takes a ``name``.
    """
    spec = get_lens(lens)
    started = time.perf_counter()
    state = collect_lens_state(ctx.obj["target"], spec.requires)
    logger.debug(
        "lens '%s' collected %s from %d node(s) in %.3fs",
        spec.name,
        ", ".join(spec.requires),
        len(ctx.obj["target"].inventory.hosts),
        time.perf_counter() - started,
    )
    try:
        records = spec.run(state, **params)
    except ValueError as exc:
        # A lens is given an address or a name by hand, so being told it is not
        # one is an ordinary answer rather than a crash.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    print_lens(
        spec,
        records,
        box_type=ctx.obj["box_type"],
        f_filter=(
            {k: v for k, v in (f.split("=") for f in field_filter)}
            if field_filter
            else {}
        ),
        output=ctx.obj["output"],
        errors=[
            f"{node}: {report} not collected: {error}"
            for (report, node), error in sorted(state.errors.items())
        ],
        subtitle=subtitle,
        node_prefix=ctx.obj.get("node_prefix"),
    )


# ------------------------- commands -------------------------


@app.command()
def server(
    ctx: typer.Context,
    listen: str = typer.Option(
        "127.0.0.1",
        "--listen",
        "-L",
        help="Address to bind the web server to. Use 0.0.0.0 to expose it on all interfaces",
    ),
    port: int = typer.Option(8080, "--port", "-P", help="TCP port to listen on"),
    sample_interval: Optional[int] = typer.Option(
        None,
        "--sample-interval",
        "-S",
        help="Override the gNMI SAMPLE interval (seconds) of every subscription",
    ),
    refresh: float = typer.Option(
        2.0,
        "--refresh",
        "-R",
        help="How often (seconds) a table is re-rendered and pushed to the browser",
    ),
    resync: int = typer.Option(
        300,
        "--resync",
        help="Interval (seconds) for a full gNMI re-read per node; 0 disables it",
    ),
    workers: int = typer.Option(
        20,
        "--workers",
        "-W",
        help="Thread pool size for parallel connect, activate and table render",
    ),
    idle_timeout: int = typer.Option(
        900,
        "--idle-timeout",
        help="Stop streaming paths no report has read for this long (seconds); "
        "0 keeps every path subscribed for the lifetime of the server",
    ),
    snapshot_dir: Optional[Path] = typer.Option(
        None,
        "--snapshot-dir",
        help="Where saved report snapshots are kept "
        "(default: ~/.local/state/fcli/snapshots)",
    ),
    watch_interval: float = typer.Option(
        15.0,
        "--watch-interval",
        help="How often (seconds) the fabric is read to keep the change timeline, "
        "the baseline and the health on the topology; 0 disables the timeline",
    ),
    persist_acks: bool = typer.Option(
        False,
        "--persist-acks",
        help="Keep acknowledged incidents across server restarts "
        "(in ~/.local/state/fcli/acks/, next to --snapshot-dir). "
        "By default they last as long as the server runs",
    ),
    watch_prefix: Optional[List[str]] = typer.Option(
        None,
        "--watch-prefix",
        help="A prefix or address whose changes - withdrawn, installed, next-hops "
        "or ECMP width - the timeline reports one by one, in any network-instance. "
        "Repeatable; more can be added in the browser",
    ),
    history: bool = typer.Option(
        True,
        "--history/--no-history",
        help="Keep the timeline, the baselines and every configuration the nodes "
        "commit on disk, one SQLite file per fabric, so they outlive a restart. "
        "Needs --watch-interval",
    ),
    history_dir: Optional[Path] = typer.Option(
        None,
        "--history-dir",
        help="Where the history files are kept (default: ~/.local/state/fcli/history, "
        "next to --snapshot-dir)",
    ),
    history_days: float = typer.Option(
        DEFAULT_RETENTION_DAYS,
        "--history-days",
        help="Days of changes the history keeps; 0 keeps them all. Baselines and "
        "configurations are kept until deleted",
    ),
) -> None:
    """Serves live report tables over HTTP, fed by gNMI subscriptions"""
    from .server.app import serve

    target: Nornir = ctx.obj["target"]
    if not target.inventory.hosts:
        typer.echo("No hosts in the inventory. Check your -c/-t and -i options.")
        raise typer.Exit(1)
    from .changes import normalize_prefix

    try:
        watched = [normalize_prefix(prefix) for prefix in watch_prefix or []]
    except ValueError as exc:
        typer.echo(f"--watch-prefix: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"fcli server on http://{listen}:{port} "
        f"({len(target.inventory.hosts)} node(s))"
    )
    serve(
        target,
        host=listen,
        port=port,
        sample_interval=sample_interval,
        resync_interval=resync,
        refresh=refresh,
        workers=workers,
        idle_timeout=idle_timeout,
        log_level=ctx.obj["log_level"],
        topo_name=ctx.obj.get("topo_name"),
        fabric_source=ctx.obj.get("fabric_source"),
        snapshot_dir=snapshot_dir,
        watch_interval=watch_interval,
        persist_acks=persist_acks,
        watch_prefixes=watched,
        history=history,
        history_dir=history_dir,
        retention_days=history_days,
    )


FIELD_FILTER = typer.Option(
    None,
    "--field-filter",
    "-f",
    help="Filter rows on field values, e.g. -f oper-state=down. Values are "
    "case-insensitive regexes; repeat the option to filter on several fields",
)


@app.command()
def sys_info(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays System Info of nodes"""
    run_report(ctx, "sys_info", field_filter)


@app.command()
def config_commits(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays the commits each node logged to its configuration"""
    run_report(ctx, "config_commits", field_filter)


@app.command()
def bgp_peers(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays BGP Peers and their status"""
    run_report(ctx, "bgp_peers", field_filter)


@app.command()
def bgp_rib(
    ctx: typer.Context,
    route_fam: str = typer.Option(
        ...,
        "--route-fam",
        "-r",
        help="evpn | ipv4 | ipv6 | l3vpn-v4 | l3vpn-v6 (IP-VPN unicast; full names "
        "l3vpn-ipv4-unicast / l3vpn-ipv6-unicast also accepted)",
        case_sensitive=False,
    ),
    route_type: Optional[str] = typer.Option(
        None, "--route-type", "-t", help="Route type for EVPN"
    ),
    detail: bool = typer.Option(
        False,
        "--detail",
        "-d",
        help="Include all path attributes (communities, SoO, D-PATH, tunnel-encap, "
        "status). Automatically enabled for non-table output (json/yaml/csv).",
    ),
    all_routes: bool = typer.Option(
        False,
        "--all",
        help="Every path received, not only the routes the node uses.",
    ),
    key: Optional[List[str]] = typer.Option(
        None,
        "--key",
        "-k",
        help="Look routes up by a key of the route list, as key=value - e.g. "
        "mac-address=1A:A4:02:FF:00:01 or esi=00:00:00:00:01:* - in the gNMI Get "
        "itself. Repeat for several keys.",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays BGP RIB (the used routes, unless --all)"""
    keys: Dict[str, str] = {}
    for item in key or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip() or not value.strip():
            raise typer.BadParameter(f"'{item}' is not key=value", param_hint="--key")
        keys[name.strip()] = value.strip()
    family = BGP_RIB_ROUTE_FAM_ALIASES.get(route_fam.lower(), route_fam)
    run_report(
        ctx,
        "bgp_rib",
        field_filter,
        title=f"BGP RIB ({family})",
        route_fam=route_fam,
        route_type=route_type,
        paths="all" if all_routes else "used",
        keys=keys,
        # Structured output has room for every attribute, so always include them.
        detail=detail or ctx.obj["output"] != OutputFormat.TABLE,
    )


@app.command()
def ipv4_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv4 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv4 RIB entries"""
    run_report(ctx, "ipv4_rib", field_filter, address=address)


@app.command()
def ipv6_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv6 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv6 RIB entries"""
    run_report(ctx, "ipv6_rib", field_filter, address=address)


@app.command()
def static_routes(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays static routes"""
    run_report(ctx, "static_routes", field_filter)


@app.command()
def tunnel_table(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays the IP tunnel-table (LDP, SR-ISIS, RSVP, VXLAN, ...)"""
    run_report(ctx, "tunnel_table", field_filter)


@app.command()
def ni(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Network Instances and interfaces"""
    run_report(ctx, "ni", field_filter)


@app.command()
def subif(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Sub-Interfaces of nodes"""
    run_report(ctx, "subif", field_filter)


@app.command()
def lag(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays LAGs of nodes"""
    run_report(ctx, "lag", field_filter)


@app.command()
def ifstats(
    ctx: typer.Context,
    interval: int = typer.Option(5, "--interval", "-s", help="Seconds between samples"),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays per-interface in/out bps from two consecutive samples"""
    run_report(
        ctx,
        "ifstats",
        field_filter,
        title=f"Interface Stats ({interval}s interval)",
        interval=interval,
    )


@app.command()
def mac(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays MAC Table"""
    run_report(ctx, "mac", field_filter)


@app.command()
def irb(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IRB sub-interfaces"""
    run_report(ctx, "irb", field_filter)


@app.command()
def es(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Ethernet Segments"""
    run_report(ctx, "es", field_filter)


@app.command()
def es_dest(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays ES Destinations on the bridge table"""
    run_report(ctx, "es_dest", field_filter)


@app.command()
def vxlan(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays VXLAN tunnel interfaces and unicast destinations"""
    run_report(ctx, "vxlan", field_filter)


@app.command()
def lldp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays LLDP Neighbors"""
    run_report(ctx, "lldp", field_filter)


@app.command()
def arp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays ARP table"""
    run_report(ctx, "arp", field_filter)


@app.command()
def nd(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv6 Neighbors"""
    run_report(ctx, "nd", field_filter)


@app.command()
def endpoints(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays the hosts ARP/ND know, with their VRFs, access port and ES"""
    run_report(ctx, "endpoints", field_filter)


@app.command()
def bfd(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays BFD sessions and how often they failed"""
    run_report(ctx, "bfd", field_filter)


@app.command()
def isis(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IS-IS interfaces and adjacencies"""
    run_report(ctx, "isis", field_filter)


@app.command()
def ospf(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays OSPF interfaces and neighbors"""
    run_report(ctx, "ospf", field_filter)


@app.command()
def resources(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays CPU, memory and forwarding-table utilization"""
    run_report(ctx, "resources", field_filter)


@app.command()
def components(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays cards, fabric modules, fans and power supplies"""
    run_report(ctx, "components", field_filter)


@app.command()
def transceivers(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays optics with their light levels and DOM alarms"""
    run_report(ctx, "transceivers", field_filter)


@app.command()
def routing_pol(ctx: typer.Context) -> None:
    """Displays Routing Policies"""
    spec = get_report("routing_pol")
    # Policies nest arbitrarily deep, so there is no table to render them as.
    if ctx.obj["output"] not in (OutputFormat.JSON, OutputFormat.YAML):
        typer.echo(
            f"Warning: the {spec.name.replace('_', '-')} report only supports json "
            "or yaml output.",
            err=True,
        )
        raise typer.Exit(1)

    result = run_query(ctx, spec)
    policies: List[Dict[str, Any]] = []
    for host, host_result in result.items():
        r: Result = host_result[0]
        node = r.host.hostname if r.host and r.host.hostname else host
        if r.failed:
            typer.echo(
                f"Failed to get {spec.resource} for {host}. Exception: {r.exception}",
                err=True,
            )
            continue
        for policy in (r.result or {}).get(spec.resource) or []:
            policies.append({"Node": node, "routing-policy": policy})

    if not policies:
        typer.echo("No data...")
        return
    if ctx.obj["output"] == OutputFormat.JSON:
        typer.echo(json.dumps(policies, indent=2, default=str))
    else:
        typer.echo(yaml.safe_dump(policies, default_flow_style=False).rstrip())


@app.command()
def checks(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
    only: Optional[List[str]] = typer.Option(
        None,
        "--check",
        help="Run only this check. Repeatable. Omit to run them all",
    ),
) -> None:
    """Runs the fabric sanity checks and lists what they found"""
    known = {check.name for check in CHECKS}
    unknown = sorted(set(only or []) - known)
    if unknown:
        typer.echo(
            f"Unknown check(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(known))}",
            err=True,
        )
        raise typer.Exit(1)

    state = collect_fabric_state(ctx.obj["target"])
    findings = run_checks(state, only=only or None)
    print_findings(
        findings,
        box_type=ctx.obj["box_type"],
        f_filter=(
            {k: v for k, v in (f.split("=") for f in field_filter)}
            if field_filter
            else {}
        ),
        output=ctx.obj["output"],
        node_prefix=ctx.obj.get("node_prefix"),
    )
    # A fabric with something wrong with it exits non-zero, so this is usable
    # as the last step of a deployment as well as by hand.
    if any(f.severity == "error" for f in findings):
        raise typer.Exit(1)


# ------------------------- history -------------------------

HISTORY_DIR = typer.Option(
    None,
    "--history-dir",
    help="Where the server keeps its history files (default: ~/.local/state/fcli/history)",
)


def _history(ctx: typer.Context, directory: Optional[Path]) -> Any:
    """The history file of the fabric in use, as the server names it."""
    from .history import HistoryError
    from .oneshot import open_history

    try:
        return open_history(ctx.obj.get("topo_name"), directory)
    except (HistoryError, OSError) as exc:
        typer.echo(f"history: {exc}", err=True)
        raise typer.Exit(1) from None


def _print_plain(ctx: typer.Context, title: str, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    """Rows that are not one node's report: a table, or JSON/YAML/CSV as asked."""
    output = ctx.obj["output"]
    if output != OutputFormat.TABLE:
        if output == OutputFormat.JSON:
            typer.echo(json.dumps(rows, indent=2, default=str))
        elif output == OutputFormat.YAML:
            typer.echo(yaml.safe_dump(rows, default_flow_style=False).rstrip())
        else:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: str(v) for k, v in row.items()})
            typer.echo(buf.getvalue().rstrip())
        return
    console = Console(theme=TABLE_THEME)
    if not rows:
        console.print(f"[i]{title}: nothing[/i]")
        return
    table = Table(title=title, highlight=True, box=_box(ctx.obj["box_type"]))
    for column in columns:
        table.add_column(column)
    prefix = ctx.obj.get("node_prefix") or ""
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column, "")
            text = str(value) if value is not None else ""
            if column in ("node", "Node") and prefix and text.startswith(prefix):
                text = text[len(prefix):]
            values.append(_cell(text))
        table.add_row(*values)
    console.print(table)


def _when(at: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else ""


@app.command()
def history(
    ctx: typer.Context,
    since: str = typer.Option("1d", "--since", "-s", help="How far back: 15m, 2h, 7d"),
    kind: Optional[List[str]] = typer.Option(
        None, "--kind", "-k", help="Only changes of this kind (bgp, interface, config, server, ...). Repeatable"
    ),
    limit: int = typer.Option(500, "--limit", "-n", help="At most this many changes, newest first"),
    history_dir: Optional[Path] = HISTORY_DIR,
) -> None:
    """Lists what the server's timeline recorded, from its history on disk"""
    from .changes import as_row, parse_since

    try:
        start = parse_since(since)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    kept = _history(ctx, history_dir)
    nodes = list(ctx.obj["target"].inventory.hosts) if ctx.obj.get("i_filter") else None
    found = kept.changes(since=start, nodes=nodes, kinds=kind or None, limit=limit)
    rows = [dict(as_row(c)) for c in found]
    _print_plain(ctx, f"History since {since}", ["time", "node", "severity", "kind", "subject", "before", "after", "detail"], rows)


@app.command()
def baseline(
    ctx: typer.Context,
    name: str = typer.Argument("baseline", help="What to keep it as"),
    note: str = typer.Option("", "--note", help="Why it was taken: a change ticket, a maintenance window"),
    activate: bool = typer.Option(
        True, "--activate/--keep-only", help="Make it the baseline the server compares against"
    ),
    history_dir: Optional[Path] = HISTORY_DIR,
) -> None:
    """Keeps the fabric as it is now as a named baseline, to compare against later"""
    from .oneshot import keep_baseline

    kept = _history(ctx, history_dir)
    try:
        saved, state, findings = keep_baseline(kept, ctx.obj["target"], name, note=note, activate=activate)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    for (report, node), error in sorted(state.errors.items()):
        typer.echo(f"{node}: {report} not collected: {error}", err=True)
    typer.echo(
        f"baseline '{saved.name}' kept: {saved.nodes} node(s), {len(findings)} finding(s), in {kept.path}"
        + (" (active)" if activate else "")
    )


@app.command()
def baselines(ctx: typer.Context, history_dir: Optional[Path] = HISTORY_DIR) -> None:
    """Lists the baselines kept for this fabric"""
    kept = _history(ctx, history_dir)
    active = kept.get_meta("baseline")
    rows = [
        {**r.as_dict(), "taken": _when(r.at), "active": "yes" if r.name == active else ""}
        for r in kept.readings()
    ]
    _print_plain(ctx, "Baselines", ["name", "taken", "nodes", "findings", "active", "note"], rows)


@app.command()
def drift(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="The baseline to compare with (default: the active one)"),
    watch_prefix: Optional[List[str]] = typer.Option(
        None, "--watch-prefix", help="A prefix to report one by one as well. Repeatable"
    ),
    history_dir: Optional[Path] = HISTORY_DIR,
) -> None:
    """Shows how the fabric now differs from a kept baseline; exits non-zero if anything stopped working"""
    from .changes import as_row, normalize_prefix
    from .oneshot import drift as compute_drift

    kept = _history(ctx, history_dir)
    try:
        watched = [normalize_prefix(p) for p in watch_prefix or []]
        saved, changes = compute_drift(kept, ctx.obj["target"], name, watched=watched)
    except (KeyError, ValueError) as exc:
        typer.echo(exc.args[0] if exc.args else str(exc), err=True)
        raise typer.Exit(1) from None
    rows = [dict(as_row(c)) for c in changes]
    _print_plain(
        ctx,
        f"Drift from baseline '{saved.name}' ({_when(saved.at)})",
        ["node", "severity", "kind", "subject", "before", "after", "detail"],
        rows,
    )
    if any(c.severity == "error" for c in changes):
        raise typer.Exit(1)


@app.command()
def config_history(
    ctx: typer.Context,
    diff: bool = typer.Option(False, "--diff", "-d", help="Show what a commit changed rather than the list"),
    commit: Optional[int] = typer.Option(None, "--commit", help="The commit to show (default: the newest kept)"),
    against: Optional[int] = typer.Option(
        None, "--against", help="Compare with the configuration after this commit, not the one before"
    ),
    history_dir: Optional[Path] = HISTORY_DIR,
) -> None:
    """Lists the configurations the server kept after each commit, or what one changed"""
    from . import configs as config_module

    kept = _history(ctx, history_dir)
    nodes = list(ctx.obj["target"].inventory.hosts)
    if not diff:
        rows = [
            {**v.as_dict(), "kept": _when(v.at)}
            for node in nodes
            for v in kept.config_versions(node)
        ]
        _print_plain(ctx, "Configurations kept", ["node", "commit", "kept", "username", "comment"], rows)
        return
    status = 0
    for node in nodes:
        after = kept.config(node, commit)
        if after is None:
            typer.echo(f"{node}: no configuration kept" + (f" after commit {commit}" if commit else ""), err=True)
            status = 1
            continue
        if against is not None:
            before = kept.config(node, against)
        else:
            older = kept.latest_config(node, before=after[0].commit_id)
            before = kept.config(node, older.commit_id) if older else None
        result = config_module.diff_trees(before[1] if before else None, after[1])
        if ctx.obj["output"] != OutputFormat.TABLE:
            _print_plain(
                ctx,
                node,
                ["node", "commit", "against", "op", "line"],
                [
                    {"node": node, "commit": after[0].commit_id, "against": before[0].commit_id if before else "", "op": op, "line": line}
                    for op, line in result.lines
                ],
            )
            continue
        header = f"# {node}: commit {after[0].commit_id} by {after[0].username or '?'}"
        if after[0].comment:
            header += f" '{after[0].comment}'"
        header += f" against commit {before[0].commit_id}" if before else " (nothing kept before it)"
        typer.echo(f"{header}: {result.summary}")
        if result.lines:
            typer.echo(result.text())
    if status:
        raise typer.Exit(status)


@app.command()
def running_config(
    ctx: typer.Context,
    match: Optional[str] = typer.Option(None, "--match", "-m", help="Only the lines matching this regex"),
    history_dir: Optional[Path] = HISTORY_DIR,
) -> None:
    """Prints each node's running configuration as set lines, with secrets redacted"""
    import re

    from . import configs as config_module
    from .oneshot import running_configs

    salt = _history(ctx, history_dir).salt()
    pattern = re.compile(match, re.IGNORECASE) if match else None
    status = 0
    for node, tree in running_configs(ctx.obj["target"], salt=salt).items():
        if isinstance(tree, str):
            typer.echo(f"{node}: {tree}", err=True)
            status = 1
            continue
        lines = [line for line in config_module.flatten(tree) if pattern is None or pattern.search(line)]
        if ctx.obj["output"] != OutputFormat.TABLE:
            _print_plain(ctx, node, ["node", "line"], [{"node": node, "line": line} for line in lines])
            continue
        typer.echo(f"# {node}")
        typer.echo("\n".join(lines))
    if status:
        raise typer.Exit(status)


# ------------------------- lenses -------------------------


@app.command()
def summary(
    ctx: typer.Context,
) -> None:
    """Displays an executive summary of the fabric topology, services and health"""
    from .checks import REQUIRED_REPORTS
    from .server.topology import _ROLE_NOUNS, summarize_fabric

    target = ctx.obj["target"]
    reports = tuple(dict.fromkeys(REQUIRED_REPORTS + ("sys_info", "es")))
    state = collect_lens_state(target, reports)
    result = summarize_fabric(state)

    output = ctx.obj["output"]
    if output in (OutputFormat.JSON, OutputFormat.YAML):
        print_structured(
            ["summary", "nodes", "roles", "services", "incidents"],
            [
                {
                    "summary": result["summary"],
                    "nodes": result["nodes"],
                    "roles": result["roles"],
                    "services": result["services"],
                    "incidents": result["incidents"],
                }
            ],
            output,
        )
    else:
        console = Console(theme=TABLE_THEME)
        console.print("\n[bold]Fabric Summary[/bold]")
        console.print("─" * 40)
        for line in result["summary"]:
            console.print(f"• {line}")
        console.print("")

        graph = result.get("graph", {})
        devices = [n for n in graph.get("nodes", []) if n.get("role") in _ROLE_NOUNS]
        if devices:
            table = Table(title="Nodes & Roles", highlight=True, box=_box(ctx.obj["box_type"]))
            table.add_column("Node", no_wrap=True)
            table.add_column("Role")
            table.add_column("Platform")
            table.add_column("Services")
            table.add_column("Peers")
            table.add_column("Status")

            node_prefix = ctx.obj.get("node_prefix")
            for dev in sorted(devices, key=lambda d: (d.get("layer", 0), d.get("name", "")), reverse=True):
                name = dev.get("name", "")
                disp = name
                if node_prefix and disp.startswith(node_prefix):
                    disp = disp[len(node_prefix):]
                role = dev.get("role", "")
                plat = dev.get("platform", "") or "-"
                srv_count = str(len(dev.get("services", [])))
                peer_count = str(len(dev.get("peers", [])))
                status = "connected" if dev.get("connected", True) else "[err]down[/err]"
                table.add_row(disp, role, plat, srv_count, peer_count, status)

            console.print(table)

    if result["incidents"]["errors"] > 0:
        raise typer.Exit(1)


@app.command()
def incidents(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Groups the checks' findings by root cause, worst first"""
    spec = get_lens("incidents")
    state = collect_lens_state(ctx.obj["target"], spec.requires)
    found = spec.run(state)
    print_lens(
        spec,
        found,
        box_type=ctx.obj["box_type"],
        f_filter=(
            {k: v for k, v in (f.split("=") for f in field_filter)}
            if field_filter
            else {}
        ),
        output=ctx.obj["output"],
        errors=[
            f"{node}: {report} not collected: {error}"
            for (report, node), error in sorted(state.errors.items())
        ],
        subtitle=f"{sum(len(i.findings) for i in found)} finding(s) in {len(found)} incident(s)",
        node_prefix=ctx.obj.get("node_prefix"),
    )
    # Like 'checks': a fabric with something wrong with it exits non-zero.
    if any(i.severity == "error" for i in found):
        raise typer.Exit(1)


@app.command()
def where(
    ctx: typer.Context,
    address: str = typer.Argument(
        ..., help="MAC or IP address to locate, e.g. 00:C1:AB:00:01:21 or 10.0.1.51"
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Finds which nodes know about a MAC or IP address"""
    run_lens(
        ctx,
        "where",
        field_filter=field_filter,
        subtitle=f"Looking for {address}",
        target=address,
    )


@app.command()
def path(
    ctx: typer.Context,
    source: str = typer.Argument(
        ..., help="Node to start from, or an address attached to one"
    ),
    destination: str = typer.Argument(..., help="Address being forwarded towards"),
    ni: str = typer.Option(
        "default",
        "--ni",
        "-n",
        help="Network instance to look the destination up in",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Walks the route tables hop by hop towards a destination"""
    run_lens(
        ctx,
        "path",
        field_filter=field_filter,
        subtitle=f"{source} -> {destination} in {ni}",
        source=source,
        destination=destination,
        ni=ni,
    )


@app.command()
def service(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Network-instance name, matched as a regex"),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Shows one service as every node that carries it sees it"""
    run_lens(
        ctx,
        "service",
        field_filter=field_filter,
        subtitle=f"Matching '{name}'",
        name=name,
    )


# ------------------------- snapshots and comparison -------------------------

snapshot_app = typer.Typer(
    name="snapshot",
    help="Keep a report as it is now, to compare a fabric against later",
)
app.add_typer(snapshot_app)

SNAPSHOT_DIR = typer.Option(
    None,
    "--snapshot-dir",
    help="Where snapshots are kept (default: ~/.local/state/fcli/snapshots)",
)


def _snapshot_store(directory: Optional[Path]) -> "SnapshotStore":
    from .server.snapshots import SnapshotStore

    return SnapshotStore(directory)


def _comparable_report(name: str) -> ReportSpec:
    try:
        spec = get_report(name.replace("-", "_"))
    except KeyError:
        typer.echo(f"Unknown report '{name}'.", err=True)
        raise typer.Exit(1)
    if not spec.tabular:
        typer.echo(f"The {name} report has no table to compare.", err=True)
        raise typer.Exit(1)
    return spec


@snapshot_app.command("save")
def snapshot_save(
    ctx: typer.Context,
    report: str = typer.Argument(..., help="Report to snapshot, e.g. bgp-peers"),
    label: str = typer.Option("", "--label", "-n", help="Name this snapshot"),
    snapshot_dir: Optional[Path] = SNAPSHOT_DIR,
) -> None:
    """Renders a report now and keeps it"""
    spec = _comparable_report(report)
    table = report_table(ctx, spec)
    saved = _snapshot_store(snapshot_dir).save(
        spec.name,
        table,
        label=label,
        inv_filter=ctx.obj["i_filter"],
        fabric=ctx.obj.get("topo_name") or "",
        inventory=list(ctx.obj["target"].inventory.hosts),
    )
    typer.echo(
        f"{saved.id}  {saved.label}  "
        f"{len(table['rows'])} row(s) from {table['nodes']} node(s)"
    )


@snapshot_app.command("list")
def snapshot_list(
    ctx: typer.Context,
    report: Optional[str] = typer.Argument(None, help="Only snapshots of this report"),
    snapshot_dir: Optional[Path] = SNAPSHOT_DIR,
) -> None:
    """Lists the snapshots saved so far, newest first"""
    name = _comparable_report(report).name if report else None
    saved = _snapshot_store(snapshot_dir).list(name)
    if ctx.obj["output"] != OutputFormat.TABLE:
        print_structured(
            ["report", "fabric", "label", "taken", "rows", "inv-filter"],
            [
                {
                    "Node": "-",
                    "report": s.report,
                    "fabric": s.fabric,
                    "label": s.label,
                    "taken": s.taken_at,
                    "rows": s.as_dict()["rows"],
                    "inv-filter": s.inv_filter,
                    "id": s.id,
                }
                for s in saved
            ],
            ctx.obj["output"],
        )
        return
    if not saved:
        typer.echo("No snapshots.")
        return
    # One directory holds the snapshots of every fabric, so which one each was
    # taken of belongs in the listing rather than only in the refusal.
    here = ctx.obj.get("topo_name") or ""
    for entry in saved:
        taken = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.taken_at))
        elsewhere = entry.fabric and here and entry.fabric != here
        typer.echo(
            f"{entry.id}  {taken}  {entry.report:<16} "
            f"{entry.as_dict()['rows']:>6} row(s)  "
            f"{entry.fabric or '-':<16}{' (other fabric)' if elsewhere else ''}  "
            f"{entry.label}"
        )


@snapshot_app.command("rm")
def snapshot_rm(
    snapshot_id: str = typer.Argument(..., help="Snapshot to delete"),
    snapshot_dir: Optional[Path] = SNAPSHOT_DIR,
) -> None:
    """Deletes a snapshot"""
    if not _snapshot_store(snapshot_dir).delete(snapshot_id):
        typer.echo(f"No snapshot '{snapshot_id}'.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted {snapshot_id}")


@app.command()
def diff(
    ctx: typer.Context,
    report: str = typer.Argument(..., help="Report to compare, e.g. bgp-peers"),
    against: Optional[str] = typer.Option(
        None,
        "--against",
        "-a",
        help="Snapshot id to compare this report against",
    ),
    nodes: Optional[str] = typer.Option(
        None,
        "--nodes",
        "-N",
        help="Two node names, comma separated, to compare against each other",
    ),
    show_same: bool = typer.Option(
        False, "--same", help="Include the rows that are identical"
    ),
    snapshot_dir: Optional[Path] = SNAPSHOT_DIR,
) -> None:
    """Compares a report against a snapshot of it, or one node against another"""
    from .diff import diff_nodes, diff_tables
    from .server.snapshots import comparable

    if bool(against) == bool(nodes):
        typer.echo("Give either --against <snapshot> or --nodes <a>,<b>.", err=True)
        raise typer.Exit(1)

    spec = _comparable_report(report)

    # Settle what we are comparing against before polling the fabric: there is
    # no point running every node to then say the snapshot is not there.
    snapshot = None
    wanted: List[str] = []
    if nodes:
        wanted = [n.strip() for n in nodes.split(",") if n.strip()]
        if len(wanted) != 2:
            typer.echo("--nodes takes exactly two node names.", err=True)
            raise typer.Exit(1)
    else:
        snapshot = _snapshot_store(snapshot_dir).get(str(against))
        if snapshot is None:
            typer.echo(f"No snapshot '{against}'.", err=True)
            raise typer.Exit(1)
        if snapshot.report != spec.name:
            typer.echo(
                f"That snapshot is of the {snapshot.report} report.", err=True
            )
            raise typer.Exit(1)
        mismatch = comparable(
            snapshot,
            ctx.obj["i_filter"],
            {},
            fabric=ctx.obj.get("topo_name") or "",
            inventory=list(ctx.obj["target"].inventory.hosts),
        )
        if mismatch:
            typer.echo(f"Not comparable: {mismatch}", err=True)
            raise typer.Exit(1)

    table = report_table(ctx, spec)

    if snapshot is None:
        result = diff_nodes(
            table, wanted[0], wanted[1], spec.key_columns, include_same=show_same
        )
    else:
        result = diff_tables(
            snapshot.table,
            table,
            spec.key_columns,
            labels=(snapshot.label, "now"),
            include_same=show_same,
        )

    print_table_shape(
        result, box_type=ctx.obj["box_type"], output=ctx.obj["output"]
    )
    counts = result["diff"]["counts"]
    summary = [f"{counts['removed']} gone", f"{counts['added']} new"]
    if result["diff"]["keyed"]:
        summary.insert(1, f"{counts['changed']} changed")
    summary.append(f"{counts['same']} unchanged")
    typer.echo(" · ".join(summary))


if __name__ == "__main__":
    app()
