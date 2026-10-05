"""A fabric reading kept as the gNMI data it was made of, and read back.

A :class:`~nornir_srl.server.timeline.Reading` is records - interfaces, BGP
sessions, route tables - built by the report getters from what the nodes
answered. Kept as those records, a baseline would have to be decoded back
into dataclasses that change from one fcli release to the next. Kept as the
answers themselves, it is the device's own data: the getters of whichever
fcli reads it back turn it into records the way they would have on the day
it was taken. A baseline taken before an upgrade of fcli still compares.

:class:`TapDevice` serves the getters from the store's streams exactly like
:class:`~nornir_srl.server.devices.CachedDevice`, and writes down every Get
they made and what it answered. :func:`replay` runs the same getters against
:class:`ReplayDevice`, which answers those Gets from the recording - the way
the release matrix in the tests replays a lab with none present.

What a getter does not read through ``get`` is not recorded: the interface
rates, which the server derives from counter samples rather than a Get.
A reading read back has no ``ifstats``, and so no ``itf_errors`` findings.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..checks import run_checks
from ..connections.helpers import as_list
from ..connections.routing import _instances, pick_entries
from ..fabric import FabricState
from ..incidents import correlate
from ..reports import get_report
from .devices import CachedDevice, DirectDevice, MixinDevice
from .stream import HostStream
from .tree import select_path

logger = logging.getLogger(__name__)

#: Bumped when the layout of a recording changes. A recording of another
#: format is not read rather than misread.
FORMAT = 1

#: Reports a recording cannot hold, because their getter reads no Get.
NOT_RECORDED = frozenset({"ifstats"})

#: A next-hop or next-hop-group asked for by key, and the table it is in.
_KEYED_ENTRY = re.compile(
    r"^/network-instance\[name=([^\]]+)\]/(route-table/(next-hop-group|next-hop))\[index=([^\]*]+)\]$"
)

#: One Get as written down: the paths, the datatype, and what it answered.
Call = Dict[str, Any]


def _loose(path: str) -> str:
    """*path* without its ``[key=*]`` predicates, which match what no predicate does."""
    return re.sub(r"\[[^=\]]+=\*\]", "", path).rstrip("/")


def _key(paths: Sequence[str], datatype: Optional[str]) -> Tuple[Tuple[str, ...], str]:
    return tuple(paths), datatype or "config"


class Recorder:
    """The Gets of one reading, by node and report."""

    def __init__(self) -> None:
        #: node -> report -> the calls its getter made.
        self.calls: Dict[str, Dict[str, List[Call]]] = {}
        #: node -> the capabilities its connection reported.
        self.capabilities: Dict[str, Any] = {}
        #: report -> node -> the arguments it was asked with: the watched
        #: routes are looked up by prefix, not read whole.
        self.params: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def for_report(self, node: str, report: str, capabilities: Any = None) -> List[Call]:
        if capabilities is not None:
            self.capabilities.setdefault(node, capabilities)
        return self.calls.setdefault(node, {}).setdefault(report, [])

    def bound(self, node: str, report: str, params: Mapping[str, Any]) -> None:
        self.params.setdefault(report, {})[node] = dict(params)


def _write_down(calls: List[Call], paths: Sequence[str], datatype: Optional[str], response: Any) -> None:
    key = _key(paths, datatype)
    if any(_key(c["paths"], c["datatype"]) == key for c in calls):
        return
    calls.append({"paths": list(key[0]), "datatype": key[1], "response": response})


class TapDevice(CachedDevice):
    """Report getters served from the streams, with every Get written down."""

    def __init__(self, stream: HostStream, calls: List[Call]) -> None:
        super().__init__(stream)
        self._calls = calls

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        response = super().get(paths, datatype, strip_mod)
        _write_down(self._calls, paths, datatype, response)
        return response

    def lookup(
        self, paths: List[str], datatype: Optional[str] = "state", table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        response = super().lookup(paths, datatype, table)
        _write_down(self._calls, paths, datatype, response)
        return response

    def query(self, paths: List[str], datatype: Optional[str] = "state") -> List[Dict[str, Any]]:
        response = super().query(paths, datatype)
        _write_down(self._calls, paths, datatype, response)
        return response


class TapDirectDevice(DirectDevice):
    """:class:`DirectDevice`, with every Get written down."""

    def __init__(self, stream: HostStream, calls: List[Call]) -> None:
        super().__init__(stream)
        self._calls = calls

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        response = super().get(paths, datatype, strip_mod)
        _write_down(self._calls, paths, datatype, response)
        return response

    def lookup(
        self, paths: List[str], datatype: Optional[str] = "state", table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        response = super().lookup(paths, datatype, table)
        _write_down(self._calls, paths, datatype, response)
        return response

    def query(self, paths: List[str], datatype: Optional[str] = "state") -> List[Dict[str, Any]]:
        response = super().query(paths, datatype)
        _write_down(self._calls, paths, datatype, response)
        return response


class TapConnection(MixinDevice):
    """Any device a getter runs on - a live gNMI connection - with its Gets written down.

    The one-shot surfaces (the CLI, the MCP server) read the fabric through
    the Nornir connection rather than a stream; this records what they read
    the same way, so a baseline they keep reads back like the server's.
    """

    def __init__(self, device: Any, calls: List[Call]) -> None:
        self._device = device
        self._calls = calls
        self.capabilities = getattr(device, "capabilities", None)

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        response = self._device.get(paths=paths, datatype=datatype, strip_mod=strip_mod)
        _write_down(self._calls, paths, datatype, response)
        return response


class ReplayMissing(LookupError):
    """A getter asked for something the recording does not hold."""


class ReplayDevice(MixinDevice):
    """Report getters answered from a recording, with no device present."""

    def __init__(self, calls: Sequence[Call], capabilities: Any = None) -> None:
        self._answers = {_key(c["paths"], c["datatype"]): c["response"] for c in calls}
        self.capabilities = capabilities

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        key = _key(paths, datatype)
        if key in self._answers:
            return self._answers[key]
        if len(paths) == 1:
            narrowed = self._from_wider(paths[0], key[1])
            if narrowed is not None:
                return narrowed
        raise ReplayMissing(f"not in the recording: {', '.join(paths)} ({key[1]})")

    def _from_wider(self, path: str, datatype: str) -> Optional[List[Dict[str, Any]]]:
        """*path* cut out of a wider one the recording holds, as the node would cut it.

        A reading kept before a getter learned to ask for less - the VNIs of
        the vxlan-interfaces rather than all of them, a line card's state rather
        than its forwarding tables - still reads back. A key matched by ``*``
        is no key at all, and a subtree recorded as state holds its config
        leaves as well.
        """
        wanted = _loose(path)
        kinds = (datatype, "state") if datatype == "all" else (datatype,)
        best: Optional[Tuple[str, Any]] = None
        for (recorded_paths, kind), response in self._answers.items():
            if len(recorded_paths) != 1 or kind not in kinds:
                continue
            wider = _loose(recorded_paths[0])
            if wanted != wider and wanted.startswith((wider + "/", wider + "[")) and (best is None or len(wider) > len(_loose(best[0]))):
                best = (recorded_paths[0], response)
        if best is None:
            return None
        return [
            {env: select_path(copy.deepcopy(value), path, "" if env in ("/", "") else env) for env, value in item.items()}
            for item in best[1]
            if isinstance(item, dict)
        ]

    def lookup(
        self, paths: List[str], datatype: Optional[str] = "state", table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Entries by key, as recorded - or out of the whole table, in a reading
        taken before getters looked them up by key and read it all instead."""
        key = _key(paths, datatype)
        if key in self._answers:
            return self._answers[key]
        if table is not None:
            whole = _key([table], datatype)
            if whole in self._answers:
                return pick_entries(table, self._answers[whole], paths)
        return [self._entry_from_table(path, key[1]) for path in paths]

    def _entry_from_table(self, path: str, datatype: str) -> Dict[str, Any]:
        match = _KEYED_ENTRY.match(path)
        if match is None:
            raise ReplayMissing(f"not in the recording: {path} ({datatype})")
        ni_name, list_path, list_name, index = match.groups()
        for table_ni in (ni_name, "*"):
            table = _key([f"/network-instance[name={table_ni}]/{list_path}[index=*]"], datatype)
            if table not in self._answers:
                continue
            for ni in _instances(self._answers[table]):
                if str(ni.get("name", "")) != ni_name:
                    continue
                for entry in as_list((ni.get("route-table") or {}).get(list_name)):
                    if isinstance(entry, dict) and str(entry.get("index")) == index:
                        return {path.lstrip("/"): entry}
            return {}
        raise ReplayMissing(f"not in the recording: {path} ({datatype})")


def payload(
    recorder: Recorder,
    *,
    at: float,
    state: FabricState,
    connected: Mapping[str, bool],
    findings: int = 0,
) -> Dict[str, Any]:
    """A reading, as the JSON it is kept as."""
    from .. import __version__  # noqa: PLC0415

    return {
        "format": FORMAT,
        "fcli": __version__,
        "at": at,
        "hostnames": dict(state.hostnames),
        "containerlab": sorted(state.containerlab),
        "connected": dict(connected),
        "errors": [[report, node, error] for (report, node), error in sorted(state.errors.items())],
        "findings": findings,
        "capabilities": recorder.capabilities,
        "params": recorder.params,
        "nodes": recorder.calls,
    }


def replay(recorded: Mapping[str, Any]) -> Tuple[FabricState, Dict[str, bool], float]:
    """The fabric state a kept reading was taken of, rebuilt by today's getters.

    Returns the state, which nodes were answering, and when it was taken.
    A report a node's recording cannot answer is an error of that node, as
    it would have been on the day: the rest of the reading still stands.
    """
    if recorded.get("format") != FORMAT:
        raise ValueError(f"a reading of format {recorded.get('format')!r}, not {FORMAT}")
    state = FabricState()
    state.hostnames = dict(recorded.get("hostnames") or {})
    state.containerlab = set(recorded.get("containerlab") or ())
    for report, node, error in recorded.get("errors") or ():
        state.errors[(report, node)] = error
    capabilities = recorded.get("capabilities") or {}
    params = recorded.get("params") or {}
    for node, reports in (recorded.get("nodes") or {}).items():
        for report_name, calls in reports.items():
            try:
                spec = get_report(report_name)
            except KeyError:
                # A report this fcli no longer has: nothing reads it anyway.
                continue
            key = spec.stands_in_for or report_name
            device = ReplayDevice(calls, capabilities.get(node))
            bound = (params.get(report_name) or {}).get(node)
            try:
                if bound is not None:
                    result = device.get_routes(bound["afi"], bound["prefixes"])
                    items = result.get("ip_rib") or []
                else:
                    items = (spec.getter(device) or {}).get(spec.resource) or []
            except Exception as exc:  # noqa: BLE001 - one report of one node
                state.errors[(key, node)] = f"not in the kept reading: {exc}"
                continue
            state.reports.setdefault(key, {})[node] = items
    connected = {str(k): bool(v) for k, v in (recorded.get("connected") or {}).items()}
    return state, connected, float(recorded.get("at") or time.time())


def replay_reading(recorded: Mapping[str, Any]) -> Any:
    """A kept reading as a :class:`~nornir_srl.server.timeline.Reading`, findings and all."""
    from .timeline import Reading  # noqa: PLC0415 - the timeline uses this module

    state, connected, at = replay(recorded)
    findings = run_checks(state)
    return Reading(at=at, state=state, findings=findings, incidents=correlate(findings, state), connected=connected)


__all__ = [
    "FORMAT",
    "NOT_RECORDED",
    "Recorder",
    "ReplayDevice",
    "ReplayMissing",
    "TapConnection",
    "TapDevice",
    "TapDirectDevice",
    "payload",
    "replay",
    "replay_reading",
]
