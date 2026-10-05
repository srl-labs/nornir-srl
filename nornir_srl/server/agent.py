"""In-process LLM agent for the fcli web UI.

Tools read the live :class:`~nornir_srl.server.store.FabricStore` (same tables
the browser shows) and, when that is not enough, a read-only JSON-RPC ``cli``
or a YANG Get on the existing gNMI session. Which provider answers - OpenAI,
Claude or Grok - is decided per turn in :mod:`nornir_srl.server.llm`; the API
keys never leave the server process.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import anyio

from ..connections.routing import BGP_RIB_ROUTE_FAM_ALIASES
from ..connections.srlinux import CONNECTION_NAME
from ..lenses import LensSpec, coerce_lens_params, lenses_for
from ..reports import SERVER, ReportSpec, get_report, reports_for
from ..rows import pass_filter
from .cli_guard import CliRejected, check_cli, check_gnmi_path
from .jsonrpc import DEFAULT_JSONRPC_PORT, JsonRpcUnreachable, jsonrpc_cli
from .llm import (
    KEY_ENVS,
    ProviderSpec,
    Reply,
    Transcript,
    build_client,
    check_effort,
    complete,
    configured_providers,
    default_provider,
    get_provider,
)
from .store import FabricStore

logger = logging.getLogger(__name__)

MAX_TOOL_CHARS = 48_000
MAX_MESSAGES = 30

#: How long a fabric stays marked as having no JSON-RPC, so one refused
#: connection does not cost a tool round on every other node.
JSONRPC_DOWN_TTL = 300.0


def max_rounds() -> int:
    try:
        value = int(os.environ.get("FCLI_CHAT_MAX_ROUNDS") or 16)
    except ValueError:
        return 16
    return max(1, value)

NO_PROVIDER = (
    "chat is disabled; set one of "
    + ", ".join(KEY_ENVS)
    + " on the fcli server process"
)

#: Appended for the answer-anyway round once the tool budget is gone.
OUT_OF_ROUNDS = (
    "You have used your tool budget for this turn and have no tools left. "
    "Answer now from what you gathered, name the nodes and fields you used, "
    "and state plainly what you could not determine and what would answer it."
)

#: Appended when a round came back with neither an answer nor a tool call.
NO_ANSWER = (
    "Your previous turn returned no content, so the user is still waiting. "
    "You have no tools left. Answer now in at most 200 words, directly from "
    "the tool output you already have. Lead with the answer, keep reasoning to "
    "a minimum, and say what you could not determine."
)

_SKIP_TABLE = frozenset({"overview", "topology"})

#: Config subtrees per area, so the model asks for "bgp" instead of inventing a
#: YANG path. ``{ni}`` is filled with the network-instance being asked about.
CONFIG_AREAS: Dict[str, Tuple[str, ...]] = {
    "bgp": ("/network-instance[name={ni}]/protocols/bgp",),
    "isis": ("/network-instance[name={ni}]/protocols/isis",),
    "ospf": ("/network-instance[name={ni}]/protocols/ospf",),
    "static_routes": ("/network-instance[name={ni}]/static-routes",),
    "routing": (
        "/network-instance[name={ni}]/protocols/bgp",
        "/network-instance[name={ni}]/protocols/isis",
        "/network-instance[name={ni}]/protocols/ospf",
        "/network-instance[name={ni}]/static-routes",
    ),
    "addressing": (
        "/interface[name=*]/subinterface[index=*]/ipv4",
        "/interface[name=*]/subinterface[index=*]/ipv6",
    ),
    "interfaces": ("/interface[name=*]",),
    "network_instances": (
        "/network-instance[name=*]/type",
        "/network-instance[name=*]/admin-state",
        "/network-instance[name=*]/interface",
        "/network-instance[name=*]/vxlan-interface",
    ),
    "evpn": (
        "/network-instance[name=*]/protocols/bgp-evpn",
        "/network-instance[name=*]/protocols/bgp-vpn",
        "/system/network-instance/protocols/evpn",
        "/system/network-instance/protocols/bgp-vpn",
    ),
    "vxlan": ("/tunnel-interface[name=*]",),
    "routing_policy": ("/routing-policy",),
    "system": (
        "/system/name",
        "/system/lldp",
        "/system/network-instance/protocols/evpn",
    ),
}

_FILTER_PROPS: Dict[str, Any] = {
    "inv_filter": {
        "type": "string",
        "description": (
            "Inventory filter as comma-separated key=value pairs "
            "(e.g. 'role=leaf'). Keys come from node labels. Omit to target all nodes."
        ),
    },
    "field_filter": {
        "type": "string",
        "description": (
            "Row filter as comma-separated field=regex pairs "
            "(e.g. 'state=up'). Values are case-insensitive regexes."
        ),
    },
}


def chat_enabled() -> bool:
    """True when at least one provider has a key in the environment."""
    return bool(configured_providers())


def sse(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


def parse_kv(values: Optional[str]) -> Optional[Dict[str, str]]:
    if not values:
        return None
    parsed: Dict[str, str] = {}
    for part in values.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed or None


def tool_error(result: str) -> Optional[str]:
    """The message of a failed tool result, or None when it succeeded.

    Tool failures are the short ``{"error": ...}`` payloads built below; a
    successful result can be 48KB, so it is not worth parsing to find out.
    """
    if not result.startswith('{"error"'):
        return None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return None
    message = parsed.get("error") if isinstance(parsed, dict) else None
    return str(message) if message else None


def dumps_truncated(payload: Any) -> str:
    text = json.dumps(payload, indent=2, default=str)
    if len(text) > MAX_TOOL_CHARS:
        return text[:MAX_TOOL_CHARS] + f"\n... truncated ({len(text)} bytes)"
    return text


def _table_tools() -> Dict[str, ReportSpec]:
    tools: Dict[str, ReportSpec] = {}
    for spec in reports_for(SERVER):
        if not spec.tabular or spec.name in _SKIP_TABLE:
            continue
        if spec.name.startswith("bgp_rib_"):
            continue
        tools[spec.tool_name] = spec
    return tools


def _lens_tools() -> Dict[str, LensSpec]:
    return {lens.tool_name: lens for lens in lenses_for(SERVER)}


def _bgp_rib_report(route_fam: str, route_type: Optional[str]) -> ReportSpec:
    fam = BGP_RIB_ROUTE_FAM_ALIASES.get(route_fam.lower(), route_fam.lower())
    if fam == "evpn":
        rt = str(route_type or "2")
        name = f"bgp_rib_evpn_{rt}"
    else:
        mapping = {
            "ipv4": "bgp_rib_ipv4",
            "ipv6": "bgp_rib_ipv6",
            "l3vpn-ipv4-unicast": "bgp_rib_l3vpn_v4",
            "l3vpn-ipv6-unicast": "bgp_rib_l3vpn_v6",
        }
        name = mapping.get(fam)
        if not name:
            raise ValueError(
                f"unknown BGP RIB family '{route_fam}'; "
                "use evpn, ipv4, ipv6, l3vpn-v4 or l3vpn-v6"
            )
    return get_report(name)


def tool_specs() -> List[Dict[str, Any]]:
    """The tools the agent may call, in a shape no provider dictates.

    :mod:`nornir_srl.server.llm` renders these as OpenAI functions or as Claude
    tools, so a tool is described once for every provider.
    """
    tools: List[Dict[str, Any]] = [
        {
            "name": "list_nodes",
            "description": (
                "List inventory nodes with labels and connection/streaming state."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "show_topology",
            "description": (
                "Fabric graph from LLDP: nodes with inferred tiers, clients, "
                "ethernet-segments and links. Use to understand how the fabric "
                "is cabled before diving into a report."
            ),
            "parameters": {
                "type": "object",
                "properties": {"inv_filter": _FILTER_PROPS["inv_filter"]},
            },
        },
        {
            "name": "overview",
            "description": (
                "Fabric-wide KPIs: node connectivity, BGP sessions, interface "
                "health, bridge domains and routers."
            ),
            "parameters": {
                "type": "object",
                "properties": {"inv_filter": _FILTER_PROPS["inv_filter"]},
            },
        },
        {
            "name": "bgp_rib",
            "description": (
                "BGP RIB-in-post. For EVPN, route_type 1=A-D, 2=MAC/IP, "
                "3=IMET, 4=ES, 5=IP prefix. Prefer field_filter to narrow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_fam": {
                        "type": "string",
                        "enum": [
                            "evpn",
                            "ipv4",
                            "ipv6",
                            "l3vpn-v4",
                            "l3vpn-v6",
                            "l3vpn-ipv4-unicast",
                            "l3vpn-ipv6-unicast",
                        ],
                    },
                    "route_type": {
                        "type": "string",
                        "enum": ["1", "2", "3", "4", "5"],
                        "description": "EVPN route type; default 2.",
                    },
                    **_FILTER_PROPS,
                },
                "required": ["route_fam"],
            },
        },
        {
            "name": "node_cli",
            "description": (
                "Run one read-only MD-CLI command on a single node via JSON-RPC "
                "(show, info, info from state, tree). Not gNMI Set. Prefer "
                "report tools first. If JSON-RPC is not enabled, use node_get."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Inventory name or hostname.",
                    },
                    "command": {
                        "type": "string",
                        "description": "A single show/info/tree command.",
                    },
                },
                "required": ["node", "command"],
            },
        },
        {
            "name": "node_config",
            "description": (
                "Read the running configuration of one node, by area. Use this "
                "for any 'how is X configured' question: report tools only show "
                "operational state. Areas: "
                + ", ".join(sorted(CONFIG_AREAS))
                + ". 'routing' returns BGP, IS-IS, OSPF and static routes at once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Inventory name or hostname.",
                    },
                    "area": {
                        "type": "string",
                        "enum": sorted(CONFIG_AREAS),
                    },
                    "network_instance": {
                        "type": "string",
                        "description": (
                            "Which network-instance the routing areas apply to; "
                            "default 'default' (the underlay)."
                        ),
                    },
                },
                "required": ["node", "area"],
            },
        },
        {
            "name": "node_get",
            "description": (
                "gNMI Get of any YANG path on one node, using the existing gNMI "
                "session. Use node_config first for configuration; this is for "
                "a path no area covers (e.g. /interface[name=ethernet-1/1])."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Inventory name or hostname.",
                    },
                    "path": {
                        "type": "string",
                        "description": "YANG path starting with /.",
                    },
                    "datatype": {
                        "type": "string",
                        "enum": ["state", "config", "all"],
                        "description": "gNMI Get datatype; default state.",
                    },
                },
                "required": ["node", "path"],
            },
        },
    ]
    for name, spec in _table_tools().items():
        tools.append(
            {
                "name": name,
                "description": spec.description,
                "parameters": {
                    "type": "object",
                    "properties": dict(_FILTER_PROPS),
                },
            }
        )
    # A lens is a question with arguments: the arguments it declares are the
    # tool's, and the ones it cannot answer without are required.
    for name, lens in _lens_tools().items():
        tools.append(
            {
                "name": name,
                "description": lens.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        **{
                            p.name: {"type": "string", "description": f"{p.help} (e.g. {p.placeholder})"}
                            for p in lens.params
                        },
                        "inv_filter": _FILTER_PROPS["inv_filter"],
                    },
                    "required": [p.name for p in lens.params if p.required],
                },
            }
        )
    return tools


def system_prompt(context: Optional[Dict[str, Any]], topo_name: Optional[str]) -> str:
    ctx = context or {}
    parts = [
        "You are a read-only SR Linux fabric troubleshooting assistant inside fcli.",
        "Prefer live report tools (bgp_peers, lldp_neighbors, ipv4_rib, mac_table, …) "
        "before logging into a node. Those tables are the same data the UI is streaming.",
        "For an executive overview of the fabric topology, roles and health, "
        "start with fabric_summary.",
        "For 'what is wrong', a triage, or any question about the fabric's health, "
        "start with fabric_incidents: it groups every check's findings by root "
        "cause, so a link down arrives together with the BGP, BFD and IGP sessions "
        "that went down over it. An incident's root is the cause to explain; its "
        "related findings are consequences, not separate problems to chase. A "
        "'pattern' incident is one cause repeated in many places - say so rather "
        "than listing each place. An incident marked acknowledged is known to "
        "the operators: mention it briefly, and focus on the open ones.",
        "Then call recent_changes: since='15m' (or longer) for what happened and "
        "when, since='baseline' for how the fabric drifted from its known-good "
        "state. A change that happened just before the incident is usually its "
        "trigger. Quote change times as given.",
        "A change of kind 'config' is a commit to a node's configuration, with who "
        "made it and how many lines it changed: when one precedes a fault, call "
        "config_diff with that node and commit to see exactly what it changed, and "
        "name the commit, the user and the lines in your answer. A 'server' change "
        "is fcli itself stopping or starting; changes found on its restart say "
        "'while fcli was not running' and happened at some point in that gap.",
        "For 'where is this address', 'how does A reach B' and 'show me this "
        "service everywhere' use locate_address, trace_path and service_detail: "
        "each joins several reports across every node in one call.",
        # The reports are all operational state, so a "how is this configured"
        # question has to go to the config datastore or it never gets answered.
        "Report tools show operational state, never configuration. For how "
        "something is configured, use node_config with an area: routing (BGP, "
        "IS-IS, OSPF and static routes together), bgp, isis, addressing, evpn, "
        "network_instances, routing_policy, vxlan, interfaces or system. "
        "Underlay routing lives in the 'default' network-instance.",
        "Use node_cli only for show/info/tree when a report is not enough. It "
        "needs the node's HTTP JSON-RPC interface, which many fabrics do not "
        "enable; if one call fails that way, stop using it and use node_get.",
        "Sample one or two representative nodes per role rather than every node, "
        "and say which nodes you looked at.",
        "If a tool returns an error, do not repeat it with the same arguments: "
        "change approach or answer with what you already have.",
        "Never suggest or attempt configuration changes. Never use set, commit, or bash.",
        "Inventory filters are key=value pairs from node labels (e.g. role=leaf).",
        "Field filters are field=regex pairs matching table columns.",
        f"You get at most {max_rounds()} tool rounds per turn, so leave room to "
        "answer: a partial answer that says what you checked beats no answer.",
        "Be concise. Cite node names and the fields you used.",
    ]
    viewing = ctx.get("report")
    inv = ctx.get("inv_filter")
    node = ctx.get("topo_node")
    name = ctx.get("topo_name") or topo_name
    extra = []
    if name:
        extra.append(f"fabric={name}")
    if viewing:
        extra.append(f"user is viewing report '{viewing}'")
    if inv:
        extra.append(f"inventory filter '{inv}'")
    if node:
        extra.append(f"selected node '{node}'")
    if extra:
        parts.append("Current UI context: " + "; ".join(extra) + ".")
    return " ".join(parts)


def _host_jsonrpc_target(host: Any) -> Tuple[str, int, str, str]:
    hostname = host.hostname or host.name
    data = host.data or {}
    try:
        port = int(
            data.get("jsonrpc_port")
            or os.environ.get("FCLI_JSONRPC_PORT")
            or DEFAULT_JSONRPC_PORT
        )
    except (TypeError, ValueError):
        port = DEFAULT_JSONRPC_PORT
    try:
        params = host.get_connection_parameters(CONNECTION_NAME)
        username = params.username or "admin"
        password = params.password or ""
    except Exception:  # noqa: BLE001 - inventory without connection options
        username = "admin"
        password = ""
    return str(hostname), port, str(username), str(password)


class ChatService:
    """Runs the tool loop against a fabric store, on the chosen provider."""

    def __init__(
        self,
        store: FabricStore,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
        jsonrpc_call: Callable[..., Any] = jsonrpc_cli,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        self.store = store
        #: Overrides client construction for every provider (tests).
        self.client_factory = client_factory
        self.jsonrpc_call = jsonrpc_call
        #: Overrides the model of whichever provider is chosen.
        self.model = model
        #: Overrides the reasoning effort the environment configures.
        self.effort = effort
        self._table_tools = _table_tools()
        self._lens_tools = _lens_tools()
        self._tools = tool_specs()
        #: (when, node, message) once a node refuses JSON-RPC outright.
        self._jsonrpc_down: Optional[Tuple[float, str, str]] = None

    def enabled(self) -> bool:
        if self.client_factory is not None:
            return True
        return chat_enabled()

    def providers(self) -> List[Dict[str, Any]]:
        """The configured providers, as the browser's selector shows them."""
        current = default_provider()
        return [
            {
                "id": spec.name,
                "label": spec.label,
                "model": self.model or spec.model(),
                "default": bool(current and current.name == spec.name),
                "efforts": list(spec.efforts),
                "effort": self.effort or spec.effort() or "",
            }
            for spec in configured_providers()
        ]

    def resolve_provider(self, requested: Optional[str]) -> ProviderSpec:
        """The provider to answer with, or a :class:`ValueError` saying why not."""
        if requested:
            spec = get_provider(str(requested).strip().lower())
            if not spec.configured() and self.client_factory is None:
                raise ValueError(
                    f"{spec.label} is not configured; set {spec.key_envs[0]} "
                    "on the fcli server process"
                )
            return spec
        spec = default_provider()
        if spec is not None:
            return spec
        if self.client_factory is not None:
            # A test or embedder supplied the client; nothing was configured to
            # pick from, so fall back to the first provider of the order.
            return get_provider("openai")
        raise ValueError(NO_PROVIDER)

    def resolve_effort(self, spec: ProviderSpec, requested: Optional[str]) -> Optional[str]:
        """The reasoning effort for this turn, or None for the model's default."""
        if requested:
            return check_effort(spec, requested)
        return check_effort(spec, self.effort or spec.effort())

    def _client(self, spec: ProviderSpec) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        return build_client(spec)

    def _run_table(
        self,
        spec: ReportSpec,
        inv_filter: Optional[str],
        field_filter: Optional[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        table = self.store.table(spec, parse_kv(inv_filter), params)
        rows = table.get("rows") or []
        f_filter = parse_kv(field_filter)
        if f_filter:
            rows = [row for row in rows if pass_filter(row, f_filter)]
        return dumps_truncated(
            {
                "report": table.get("report") or spec.name,
                "title": table.get("title") or spec.title,
                "columns": table.get("columns") or [],
                "rows": rows,
                "errors": table.get("errors") or [],
                "nodes": table.get("nodes"),
            }
        )

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        if name == "list_nodes":
            return dumps_truncated(self.store.inventory())
        if name == "show_topology":
            return dumps_truncated(
                self.store.topology(parse_kv(arguments.get("inv_filter")))
            )
        if name == "overview":
            return dumps_truncated(
                self.store.overview(parse_kv(arguments.get("inv_filter")))
            )
        if name == "bgp_rib":
            spec = _bgp_rib_report(
                str(arguments.get("route_fam") or "evpn"),
                arguments.get("route_type"),
            )
            return self._run_table(
                spec,
                arguments.get("inv_filter"),
                arguments.get("field_filter"),
            )
        if name == "node_cli":
            return self._node_cli(
                str(arguments.get("node") or ""),
                str(arguments.get("command") or ""),
            )
        if name == "node_config":
            return self._node_config(
                str(arguments.get("node") or ""),
                str(arguments.get("area") or ""),
                str(arguments.get("network_instance") or "default"),
            )
        if name == "node_get":
            return self._node_get(
                str(arguments.get("node") or ""),
                str(arguments.get("path") or ""),
                str(arguments.get("datatype") or "state"),
            )
        lens = self._lens_tools.get(name)
        if lens is not None:
            return self._run_lens(lens, arguments)
        spec = self._table_tools.get(name)
        if spec is None:
            return json.dumps({"error": f"unknown tool '{name}'"})
        return self._run_table(
            spec, arguments.get("inv_filter"), arguments.get("field_filter")
        )

    def _run_lens(self, lens: LensSpec, arguments: Dict[str, Any]) -> str:
        """Ask a lens, and hand the agent its records rather than its table."""
        try:
            params = coerce_lens_params(lens, arguments)
            answer = self.store.lens_table(lens, parse_kv(arguments.get("inv_filter")), params)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        payload = {
            "lens": lens.name,
            "title": lens.title,
            "records": answer["records"],
            "errors": answer["errors"],
            "nodes": answer["nodes"],
        }
        if lens.name == "changes":
            # A change is stamped with a Unix time; the rows carry it as the
            # time of day the user sees on the timeline, which is what an
            # answer should quote.
            payload["records"] = answer["rows"]
        return dumps_truncated(payload)

    def _jsonrpc_unreachable(self) -> Optional[str]:
        """Why node_cli is pointless right now, if a node already proved it is."""
        if not self._jsonrpc_down:
            return None
        when, node, message = self._jsonrpc_down
        if time.monotonic() - when > JSONRPC_DOWN_TTL:
            self._jsonrpc_down = None
            return None
        return (
            f"JSON-RPC is not reachable on this fabric ({node} failed: {message}) "
            "so node_cli cannot be used. Use node_get with a YANG path "
            "(datatype=config for configuration) or a report tool instead."
        )

    def _node_cli(self, node: str, command: str) -> str:
        try:
            command = check_cli(command)
            name, host = self.store.resolve_host(node)
        except (CliRejected, KeyError) as exc:
            return json.dumps({"error": str(exc)})
        blocked = self._jsonrpc_unreachable()
        if blocked:
            return json.dumps({"error": blocked})
        hostname, port, username, password = _host_jsonrpc_target(host)
        try:
            result = self.jsonrpc_call(hostname, port, username, password, command)
        except JsonRpcUnreachable as exc:
            self._jsonrpc_down = (time.monotonic(), name, str(exc))
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - returned to the model
            return json.dumps({"error": str(exc)})
        return dumps_truncated({"node": node, "command": command, "result": result})

    @staticmethod
    def _strip_annotations(value: Any) -> Tuple[Any, bool]:
        """Drop ``_annotate*`` keys, which are provenance comments, not config.

        Tools like EDA annotate nearly every leaf, which can double the payload
        the model has to read for no gain.
        """
        if isinstance(value, dict):
            found = False
            out: Dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.startswith("_annotate"):
                    found = True
                    continue
                out[key], hit = ChatService._strip_annotations(item)
                found = found or hit
            return out, found
        if isinstance(value, list):
            items = [ChatService._strip_annotations(item) for item in value]
            return [item for item, _ in items], any(hit for _, hit in items)
        return value, False

    @staticmethod
    def _is_unset(payload: Any) -> bool:
        """gNMI answers an unconfigured subtree with nothing, or ``[{}]``."""
        if not payload:
            return True
        if isinstance(payload, list):
            return all(not item for item in payload)
        return False

    def _node_config(self, node: str, area: str, network_instance: str) -> str:
        """Read one curated slice of a node's configuration."""
        paths = CONFIG_AREAS.get(area.strip().lower())
        if paths is None:
            return json.dumps(
                {
                    "error": f"unknown config area '{area}'; use one of "
                    + ", ".join(sorted(CONFIG_AREAS))
                }
            )
        config: Dict[str, Any] = {}
        unset: List[str] = []
        errors: Dict[str, str] = {}
        annotated = False
        for template in paths:
            path = template.format(ni=network_instance)
            try:
                result = self.store.node_get(node, path, "config")
            except Exception as exc:  # noqa: BLE001 - reported per path
                # One unreadable subtree should not lose the others.
                errors[path] = str(exc)
                continue
            if self._is_unset(result):
                unset.append(path)
                continue
            config[path], hit = self._strip_annotations(result)
            annotated = annotated or hit
        if not config and errors:
            return json.dumps({"error": "; ".join(errors.values())})
        payload: Dict[str, Any] = {
            "node": node,
            "area": area,
            "datatype": "config",
            "config": config,
        }
        if area in ("bgp", "isis", "ospf", "static_routes", "routing"):
            payload["network_instance"] = network_instance
        if unset:
            payload["not_configured"] = unset
        if errors:
            payload["errors"] = errors
        if annotated:
            payload["note"] = "_annotate provenance comments removed"
        return dumps_truncated(payload)

    def _node_get(self, node: str, path: str, datatype: str) -> str:
        try:
            path = check_gnmi_path(path)
            if datatype not in ("state", "config", "all"):
                datatype = "state"
            payload = self.store.node_get(node, path, datatype)
        except (CliRejected, KeyError, RuntimeError) as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - returned to the model
            return json.dumps({"error": str(exc)})
        return dumps_truncated(
            {"node": node, "path": path, "datatype": datatype, "result": payload}
        )

    async def _answer_anyway(
        self,
        spec: Any,
        client: Any,
        model: str,
        system: str,
        transcript: Transcript,
        level: Optional[str],
        empty: Optional[Reply],
    ) -> AsyncIterator[bytes]:
        """Last attempt at an answer, with the tools taken away.

        Reached when the model went quiet or spent its tool budget. The empty
        assistant turn is deliberately not added to the transcript: replaying a
        turn with no content is rejected by some providers and adds nothing.
        """
        if empty is not None and empty.truncated:
            yield sse(
                "notice",
                {"text": f"answer was cut off ({empty.stop}); retrying briefly"},
            )
        instruction = OUT_OF_ROUNDS if empty is None else NO_ANSWER
        final = await anyio.to_thread.run_sync(
            complete,
            spec,
            client,
            model,
            system + " " + instruction,
            transcript,
            [],
            level,
        )
        if final.text:
            yield sse("token", {"text": final.text})
            yield sse("done", {})
            return
        reason = final.stop or (empty.stop if empty else "") or "no content"
        yield sse(
            "error",
            {
                "error": (
                    f"the model returned no answer ({reason}). Try a narrower "
                    "question, a lower reasoning effort, or another provider."
                )
            },
        )
        yield sse("done", {})

    async def events(
        self,
        messages: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]],
        provider: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield SSE chunks for one chat turn."""
        try:
            spec = self.resolve_provider(provider)
            level = self.resolve_effort(spec, effort)
        except ValueError as exc:
            yield sse("error", {"error": str(exc)})
            return
        history = [
            msg
            for msg in messages
            if isinstance(msg, dict)
            and msg.get("role") in ("user", "assistant")
            and isinstance(msg.get("content"), str)
        ][-MAX_MESSAGES:]
        if not history or history[-1]["role"] != "user":
            yield sse("error", {"error": "messages must end with a user turn"})
            return
        transcript = Transcript()
        for message in history:
            if message["role"] == "user":
                transcript.user(message["content"])
            else:
                transcript.assistant(Reply(text=message["content"]))
        try:
            client = self._client(spec)
        except Exception as exc:  # noqa: BLE001 - shown in the drawer
            yield sse("error", {"error": str(exc)})
            return
        model = self.model or spec.model()
        system = system_prompt(context, self.store.topo_name)
        yield sse(
            "start",
            {"provider": spec.name, "model": model, "effort": level or ""},
        )
        rounds = max_rounds()
        seen: Dict[str, str] = {}
        try:
            for index in range(rounds):
                reply = await anyio.to_thread.run_sync(
                    complete,
                    spec,
                    client,
                    model,
                    system,
                    transcript,
                    self._tools,
                    level,
                )
                if reply.text:
                    yield sse("token", {"text": reply.text})
                if not reply.tool_calls:
                    if reply.text:
                        yield sse("done", {})
                        return
                    # No tools and nothing to say: a reasoning model that spent
                    # its output budget thinking. Ask once for the answer alone
                    # rather than leaving the drawer blank.
                    async for chunk in self._answer_anyway(
                        spec, client, model, system, transcript, level, reply
                    ):
                        yield chunk
                    return
                transcript.assistant(reply)
                for call in reply.tool_calls:
                    args = _parse_arguments(call.arguments)
                    yield sse(
                        "tool",
                        {
                            "id": call.id,
                            "name": call.name,
                            "args": args,
                            "round": index + 1,
                            "rounds": rounds,
                        },
                    )
                    key = f"{call.name}:{json.dumps(args, sort_keys=True, default=str)}"
                    started = time.monotonic()
                    repeat = key in seen
                    if repeat:
                        # Re-running it would spend a round to learn nothing;
                        # point the model back at the answer it already has.
                        result = json.dumps(
                            {
                                "note": (
                                    f"an identical {call.name} call was already made "
                                    "in this turn; reuse that result"
                                )
                            }
                        )
                    else:
                        result = await anyio.to_thread.run_sync(
                            self.execute_tool, call.name, args
                        )
                        seen[key] = result
                    yield sse(
                        "tool_result",
                        {
                            "id": call.id,
                            "name": call.name,
                            "ms": int((time.monotonic() - started) * 1000),
                            "bytes": len(result),
                            "error": tool_error(result),
                            "repeat": repeat,
                        },
                    )
                    transcript.tool_result(call, result)
            # Out of rounds. Ask once more with the tools taken away, so the
            # turn ends with an answer instead of an apology.
            yield sse("notice", {"text": f"tool budget reached after {rounds} rounds"})
            async for chunk in self._answer_anyway(
                spec, client, model, system, transcript, level, None
            ):
                yield chunk
        except Exception as exc:  # noqa: BLE001 - surfaced in the drawer
            logger.exception("chat agent failed")
            yield sse("error", {"error": str(exc)})


def _parse_arguments(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
