"""Replay recorded gNMI exchanges through the real report getters.

A recording holds every ``get(paths=..., datatype=...)`` a report made against
one node on one SR Linux release, together with the response - or the error - the
device gave back. :class:`ReplayDevice` answers those same calls from the
recording, so the production getters run unchanged with no device present.
"""

from __future__ import annotations

import copy
import datetime
import gzip
import inspect
import itertools
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from unittest import mock

from nornir_srl.connections import ifstats as ifstats_module
from nornir_srl.connections import neighbor_discovery as nd_module
from nornir_srl.reports import REPORTS_BY_NAME, ReportSpec
from nornir_srl.connections.helpers import clean_structured_key
from nornir_srl.connections.routing import pick_entries
from nornir_srl.rows import clean_columns, flatten
from nornir_srl.server.devices import MixinDevice
from nornir_srl.server.tree import select_path, split_path

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "releases"

#: Recording format version, so a stale fixture is rejected rather than
#: misinterpreted if the layout ever changes.
FORMAT = 1


class ReplayError(LookupError):
    """A getter asked for a path the recording does not hold."""

    def __init__(self, path: str, datatype: str) -> None:
        super().__init__(
            f"no recorded response for get(path={path!r}, datatype={datatype!r})"
        )
        self.path = path
        self.datatype = datatype


@contextmanager
def deterministic_clock(
    now: datetime.datetime, interval: float, *, skip_sleep: bool
) -> Iterator[None]:
    """Pin the clocks two reports read, so a replay can reproduce them.

    ``arp`` and ``nd`` render an expiry as the time left until it, and
    ``ifstats`` divides a counter delta by the measured time between its two
    samples. Both would come out differently every run, which no recording could
    match. Capture and replay therefore both run under this: the same frozen
    ``now`` and the same nominal interval, so the recorded table is reproducible.

    *skip_sleep* is what differs between them - the capture has to really wait
    between the two counter samples for the counters to move, while the replay
    already holds both samples.
    """

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: Optional[datetime.tzinfo] = None) -> datetime.datetime:
            return now if tz else now.replace(tzinfo=None)

    # Advancing by the interval on every reading keeps the interval exact however
    # many samples are taken, rather than relying on a fixed pair of readings.
    ticks = itertools.count()
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                nd_module,
                "datetime",
                SimpleNamespace(datetime=FrozenDatetime, timezone=datetime.timezone),
            )
        )
        stack.enter_context(
            mock.patch.object(
                ifstats_module.time, "monotonic", lambda: next(ticks) * float(interval)
            )
        )
        if skip_sleep:
            stack.enter_context(
                mock.patch.object(ifstats_module.time, "sleep", lambda _seconds: None)
            )
        yield


class RecordedGnmiError(Exception):
    """A gNMI Get that failed on the device, re-raised during replay.

    Carries the gRPC status code as well as the message, because the getters
    decide whether a path is simply absent by looking at both.
    """

    def __init__(self, message: str, grpc_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.grpc_code = grpc_code

    def code(self) -> Any:
        if not self.grpc_code:
            return None
        try:
            import grpc

            return getattr(grpc.StatusCode, self.grpc_code, None)
        except ImportError:  # pragma: no cover - grpc ships with pygnmi
            return None


@dataclass
class GetCall:
    """One ``get`` a report made, and what came back."""

    path: str
    datatype: str
    response: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    grpc_code: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def as_dict(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"path": self.path, "datatype": self.datatype}
        if self.error is not None:
            entry["error"] = self.error
            if self.grpc_code:
                entry["grpc_code"] = self.grpc_code
        else:
            entry["response"] = self.response
        return entry

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]) -> "GetCall":
        return cls(
            path=entry["path"],
            datatype=entry.get("datatype", "config"),
            response=entry.get("response"),
            error=entry.get("error"),
            grpc_code=entry.get("grpc_code"),
        )


@dataclass
class ReportRecording:
    """The gNMI exchange of one report, plus the table it produced live.

    ``columns`` and ``rows`` are the report as a user sees it - the same
    flattening ``fcli``, the MCP tools and the browser all apply - so replaying
    the recording and re-flattening has to reproduce them exactly.
    """

    report: str
    resource: str
    calls: List[GetCall] = field(default_factory=list)
    #: Column names the report produced, which is what a datamodel change moves.
    columns: List[str] = field(default_factory=list)
    #: The flattened rows, as the golden output to compare a replay against.
    rows: List[Dict[str, Any]] = field(default_factory=list)
    #: The exception the getter raised on the live device, if it did.
    getter_error: Optional[str] = None

    @property
    def failed_paths(self) -> List[GetCall]:
        return [c for c in self.calls if c.failed]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "report": self.report,
            "resource": self.resource,
            "getter_error": self.getter_error,
            "columns": self.columns,
            "rows": self.rows,
            "calls": [c.as_dict() for c in self.calls],
        }

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]) -> "ReportRecording":
        return cls(
            report=entry["report"],
            resource=entry["resource"],
            calls=[GetCall.from_dict(c) for c in entry.get("calls", [])],
            columns=entry.get("columns") or [],
            rows=entry.get("rows") or [],
            getter_error=entry.get("getter_error"),
        )


@dataclass
class Recording:
    """Everything captured from one node on one release."""

    release: str
    node: str
    role: str
    captured: str
    capabilities: Dict[str, Any]
    #: Seconds between the two counter samples ``ifstats`` was captured with.
    ifstats_interval: int = 1
    reports: Dict[str, ReportRecording] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "format": FORMAT,
            "release": self.release,
            "node": self.node,
            "role": self.role,
            "captured": self.captured,
            "ifstats_interval": self.ifstats_interval,
            "capabilities": self.capabilities,
            "reports": [r.as_dict() for r in self.reports.values()],
        }

    def save(self, path: Path) -> None:
        """Write the recording, gzipped when *path* ends in ``.gz``.

        A recording of a whole fabric report set runs to a couple of megabytes of
        gNMI payload, and compresses to around a hundred kilobytes - worth it for
        something committed once per release. What a reviewer wants to read is
        the summary in ``MATRIX.md``, not the payloads.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(self.as_dict(), separators=(",", ":")).encode()
        if path.suffix == ".gz":
            with gzip.open(path, "wb", compresslevel=9) as handle:
                handle.write(blob)
        else:
            path.write_bytes(blob)

    @classmethod
    def load(cls, path: Path) -> "Recording":
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                raw = json.loads(handle.read())
        else:
            raw = json.loads(path.read_text())
        if raw.get("format") != FORMAT:
            raise ValueError(
                f"{path}: recording format {raw.get('format')} is not {FORMAT}"
            )
        return cls(
            release=raw["release"],
            node=raw["node"],
            role=raw.get("role", "unknown"),
            captured=raw.get("captured", ""),
            capabilities=raw.get("capabilities") or {},
            ifstats_interval=raw.get("ifstats_interval", 1),
            reports={
                entry["report"]: ReportRecording.from_dict(entry)
                for entry in raw.get("reports", [])
            },
        )

    @property
    def captured_at(self) -> datetime.datetime:
        try:
            return datetime.datetime.fromisoformat(self.captured)
        except ValueError:
            return datetime.datetime.now(datetime.timezone.utc)

    def device(self, report: str) -> "ReplayDevice":
        """A device that answers the gets *report* made on this node."""
        return ReplayDevice(self.reports[report].calls, self.capabilities)

    def run(self, report: str) -> Dict[str, Any]:
        """Run *report*'s getter against this recording and return its result.

        Goes through :func:`deterministic_clock` so the clock-derived fields come
        out as they were recorded; calling the getter directly would not.
        """
        device = self.device(report)
        getter = REPORTS_BY_NAME[report].getter
        # Recorded before the BGP RIB kept its used paths alone by default:
        # what the device gave then is every path.
        params = inspect.signature(getter).parameters
        kwargs = {"paths": "all"} if "paths" in params else {}
        if "scope" in params:
            kwargs["scope"] = "all"
        with deterministic_clock(
            self.captured_at, self.ifstats_interval, skip_sleep=True
        ):
            return getter(device, **kwargs) or {}

    def table(self, report: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """The ``(columns, rows)`` replaying *report* produces."""
        return flatten_report(REPORTS_BY_NAME[report], self.run(report))


class ReplayDevice(MixinDevice):
    """Serves recorded gNMI responses to the real report getters.

    Repeated gets of the same path are replayed in the order they were captured,
    which is what lets a report that samples the same path twice - ``ifstats``
    derives its rates that way - reproduce a rate rather than a flat zero.
    """

    def __init__(
        self,
        calls: List[GetCall],
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.capabilities = capabilities or {}
        self._calls: Dict[Tuple[str, str], List[GetCall]] = {}
        for call in calls:
            self._calls.setdefault((call.path, call.datatype), []).append(call)
        self._cursor: Dict[Tuple[str, str], int] = {}
        self.requested: List[Tuple[str, str]] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        response: List[Dict[str, Any]] = []
        for path in paths:
            key = (path, datatype or "config")
            self.requested.append(key)
            recorded = self._calls.get(key)
            narrow = None
            if not recorded:
                # A getter that has since learned to ask for less than it used
                # to is answered from what the device gave for the wider path,
                # cut down the way the device would have cut it.
                narrow = self._recorded_ancestor(path, datatype or "config")
                if narrow is None:
                    raise ReplayError(path, datatype or "config")
                key, recorded = narrow, self._calls[narrow]
            index = min(self._cursor.get(key, 0), len(recorded) - 1)
            self._cursor[key] = index + 1
            call = recorded[index]
            if call.failed:
                raise RecordedGnmiError(call.error or "", call.grpc_code)
            # A copy, because several getters enrich the response in place and a
            # recording is replayed by more than one test.
            payload = copy.deepcopy(call.response or [])
            if narrow is not None:
                payload = [
                    {env: select_path(value, path, env) for env, value in notification.items()}
                    for notification in payload
                ]
            response.extend(payload)
        return response

    def lookup(
        self, paths: List[str], datatype: Optional[str] = "state", table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Entries by key, picked out of the recorded *table* in one pass.

        Recordings read the table whole, keys and all; answering each key on
        its own through :meth:`get` would narrow a copy of it once per key.
        """
        datatype = datatype or "state"
        if table is not None and not all((path, datatype) in self._calls for path in paths):
            for recorded in (table, table.rsplit("[", 1)[0]):
                if (recorded, datatype) in self._calls:
                    payload = self.get([recorded], datatype)
                    return pick_entries(table, payload, paths)
        return self.get(paths, datatype)

    def _recorded_ancestor(self, path: str, datatype: str) -> Optional[Tuple[str, str]]:
        """The longest recorded path *path* lies under, with the same datatype.

        A wildcard key holds every entry a literal one names, so a list read
        whole - ``next-hop-group[index=*]`` - answers for the entries a getter
        now looks up one by one.
        """
        # A key matched by * is no key at all: the lists a getter now names
        # its wildcard keys on were recorded without them.
        loose = {(_drop_wildcard_keys(p), d): (p, d) for p, d in self._calls}
        # A subtree read as state holds its config leaves as well: what is
        # now asked leaf by leaf as 'all' was recorded whole as 'state'.
        datatypes = (datatype, "state") if datatype == "all" else (datatype,)
        elems = split_path(path)
        for cut in range(len(elems), 0, -1):
            prefix = "/" + "/".join(elems[:cut])
            for kind in datatypes:
                found = loose.get((_drop_wildcard_keys(prefix), kind))
                if found is not None and found[0] != path:
                    return found
        for cut in range(len(elems), 0, -1):
            kept = elems[:cut]
            variants = [kept] if cut < len(elems) else []
            variants += [
                kept[:-1] + [_wildcard_keys(kept[-1])],
                [_wildcard_keys(e) for e in kept],
                # The list read without naming its keys at all.
                kept[:-1] + [kept[-1].split("[", 1)[0]],
                [_wildcard_keys(e) for e in kept[:-1]] + [kept[-1].split("[", 1)[0]],
            ]
            for variant in variants:
                candidate = ("/" + "/".join(variant), datatype)
                if candidate[0] != path and candidate in self._calls:
                    return candidate
        return None


def _drop_wildcard_keys(path: str) -> str:
    """*path* without its ``[key=*]`` predicates, which match what no predicate does."""
    return re.sub(r"\[[^=\]]+=\*\]", "", path)


def _wildcard_keys(elem: str) -> str:
    """A path element with every key value replaced by ``*``."""
    return re.sub(r"\[([^=\]]+)=[^\]]*\]", r"[\1=*]", elem)


def flatten_report(
    spec: ReportSpec, result: Optional[Dict[str, Any]]
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """A getter's result as the ``(columns, rows)`` a user of the report sees.

    Capture and replay both go through here, so a recorded table and a replayed
    one are comparable by construction. Rows are keyed the way the columns are
    named - see :func:`comparable_rows` for what that leaves out.
    """
    raw_columns, rows = flatten("", (result or {}).get(spec.resource), spec.table_for({}))
    # ``flatten`` labels each row with the node it came from; the recording
    # already names the node, so it would only be noise repeated on every row.
    return clean_columns(raw_columns), comparable_rows(
        {k: v for k, v in row.items() if k != "Node"} for row in rows
    )


def comparable_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """*rows* as what they render as, so two ways of producing one compare.

    A cell renders the same whether its key carried a ``<n>_`` ordering
    prefix or a newline, and whether an empty cell was left out of the row,
    given as ``None``, as ``""``, as an empty list or as a list of nothing
    but those - the ``[None]`` a host route's next-hop used to flatten to.
    Recordings made before a report returned records keep the raw keys and
    the gaps, so both sides are brought to this shape.
    """

    def cell(value: Any) -> Any:
        if isinstance(value, list):
            value = [v for v in value if v is not None and v != ""]
        return None if value is None or value == "" or value == [] else value

    return [
        {clean_structured_key(k): cell(v) for k, v in row.items() if cell(v) is not None}
        for row in rows
    ]


def recording_paths(root: Path = FIXTURE_ROOT) -> List[Path]:
    """Every recording on disk, ordered by release then node."""
    if not root.is_dir():
        return []
    return sorted(list(root.glob("*/*.json.gz")) + list(root.glob("*/*.json")))


def load_recordings(root: Path = FIXTURE_ROOT) -> List[Recording]:
    return [Recording.load(p) for p in recording_paths(root)]


def iter_report_recordings(
    root: Path = FIXTURE_ROOT,
) -> Iterator[Tuple[Recording, ReportRecording]]:
    """Every (node, report) pair captured, across all releases."""
    for recording in load_recordings(root):
        for report in recording.reports.values():
            yield recording, report
