"""Fabric-wide state: gNMI connections, subscriptions and rendered tables."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from nornir.core import Nornir

from ..checks import CHECKS_COLUMNS, CHECKS_REPORT, FabricState, Finding, REQUIRED_REPORTS, run_checks
from ..acks import AckStore, finding_key, mark as mark_acknowledged
from ..changes import INFO, WATCHED_ROUTE_REPORTS, Change
from ..incidents import Incident, correlate, locate
from ..connections.down_reason import STANDBY_STATE, is_intent
from ..connections.srlinux import CONNECTION_NAME
from ..fabric import containerlab_nodes
from ..connections.layer2 import stamp_underlay_sites
from ..lenses import LensSpec
from ..records import as_dict
from ..reports import ReportSpec, SubscriptionSpec, get_report, reading_reports, subscription_mode
from ..rows import cell, clean_columns, flatten, merge_fields, sub_item_keys
from .devices import CachedDevice, DirectDevice, RecordingDevice
from .stream import HostStream
from .timeline import Reading, Timeline, Watcher
from .topology import annotate_aliasing, annotate_health, build_topology, node_facts

logger = logging.getLogger(__name__)


class FabricStore:
    """Owns one :class:`HostStream` per node and renders reports from them."""

    def __init__(
        self,
        nornir: Nornir,
        *,
        sample_interval: Optional[int] = None,
        resync_interval: int = 300,
        workers: int = 20,
        restart_debounce: float = 1.0,
        idle_timeout: float = 900.0,
        connect_retry_interval: float = 30.0,
        topo_name: Optional[str] = None,
        watch_interval: float = 0.0,
        cabling_file: Optional[Path] = None,
        ack_file: Optional[Path] = None,
    ) -> None:
        self.nornir = nornir
        self.topo_name = topo_name
        self.sample_interval = sample_interval
        self.resync_interval = resync_interval
        self.restart_debounce = restart_debounce
        self.idle_timeout = idle_timeout
        self.connect_retry_interval = connect_retry_interval
        self._pool = ThreadPoolExecutor(
            max_workers=max(workers, 1), thread_name_prefix="fcli-srv"
        )
        self._streams: Dict[str, HostStream] = {}
        self._connect_errors: Dict[str, str] = {}
        #: When each unconnected node was last attempted, to rate-limit retries.
        self._connect_attempts: Dict[str, float] = {}
        #: Discovered paths per (node, report). Discovery runs the report getter
        #: against the live device, so its result is cached; re-asserting the
        #: paths themselves is cheap and happens on every render.
        self._specs: Dict[Tuple[str, str], List[SubscriptionSpec]] = {}
        #: Why a node could not serve a report, and when that was decided.
        self._activation_errors: Dict[Tuple[str, str], Tuple[float, str]] = {}
        #: Centralized table cache per (report_name, inv_filter, report params)
        #: -> (timestamp, table)
        self._table_cache: Dict[
            Tuple[
                str,
                Optional[Tuple[Tuple[str, str], ...]],
                Optional[Tuple[Tuple[str, Any], ...]],
            ],
            Tuple[float, Dict[str, Any]],
        ] = {}
        #: Timestamp of the most recent fabric state update or topology change.
        self._last_state_change: float = time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._stopped = False
        self._resync_thread: Optional[threading.Thread] = None
        #: What the fabric was, as the watcher read it every watch_interval.
        self.timeline = Timeline()
        #: The findings someone acknowledged, shared by everyone on this server.
        self.acks = AckStore(ack_file)
        #: Where the cables LLDP showed are kept across restarts, if anywhere.
        self.cabling_file = cabling_file
        if cabling_file is not None:
            self.timeline.load_cabling(cabling_file)
        self.watch_interval = watch_interval
        self.watcher = Watcher(self, self.timeline, interval=watch_interval)
        #: inv_filter key -> (when, reading), for health asked with no fresh
        #: watcher reading to answer from.
        self._health_cache: Dict[Any, Tuple[float, Reading]] = {}
        #: The (node, service) tables last fetched for the topology's
        #: aliasing: (when, which, report -> node -> tables).
        self._alias_ribs: Optional[Tuple[float, Tuple[Tuple[str, str], ...], Dict[str, Dict[str, List[Any]]]]] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def _on_host_update(self) -> None:
        """Callback invoked when a HostStream receives a telemetry update."""
        with self._lock:
            self._last_state_change = time.time()

    def start(self) -> None:
        """Open a gNMI connection to every node in the inventory."""
        hosts = list(self.nornir.inventory.hosts.items())
        futures = [self._pool.submit(self._connect, name, host) for name, host in hosts]
        for fut in futures:
            while not self._stop.is_set():
                try:
                    fut.result(timeout=0.1)
                    break
                except TimeoutError:
                    continue
                except Exception:
                    break
        connected = len(self._streams)
        logger.info("connected to %d/%d node(s)", connected, len(hosts))
        with self._lock:
            unreachable = sorted(self._connect_errors)
        if unreachable:
            logger.debug("not connected: %s", ", ".join(unreachable))
        if self.resync_interval > 0:
            self._resync_thread = threading.Thread(
                target=self._resync_loop, name="fcli-resync", daemon=True
            )
            self._resync_thread.start()
        self.watcher.start()

    def _connect(self, name: str, host: Any) -> None:
        if self._stop.is_set():
            # Opening one now would hold a session on the node with nothing left
            # to close it: connects are queued, so they can outlive the store.
            return
        with self._lock:
            self._connect_attempts[name] = time.time()
        logger.debug("%s: opening a gNMI connection", name)
        try:
            device = host.get_connection(CONNECTION_NAME, self.nornir.config)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning("%s: connection failed: %s", name, exc)
            logger.debug("%s: connection failed", name, exc_info=exc)
            with self._lock:
                self._connect_errors[name] = str(exc)
                self._last_state_change = time.time()
            return
        with self._lock:
            if self._stop.is_set():
                # Lost the race with stop(). Keeping this would leave a session
                # open on the node with nothing left to close it.
                stopping = True
            else:
                stopping = False
                self._connect_errors.pop(name, None)
                self._streams[name] = HostStream(
                    name,
                    device,
                    default_sample_interval=self.sample_interval or 15,
                    restart_debounce=self.restart_debounce,
                    idle_timeout=self.idle_timeout,
                    on_update=self._on_host_update,
                )
                self._last_state_change = time.time()
                logger.debug(
                    "%s: connected, streaming at a %ds sample interval",
                    name,
                    self.sample_interval or 15,
                )
        if stopping:
            try:
                host.close_connection(CONNECTION_NAME)
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("%s: closing a late connection failed: %s", name, exc)

    def _heal_connections(self, names: List[str]) -> None:
        """Reconnect the nodes among *names* whose gNMI connection does not work.

        Two situations end up here. A node that was unreachable when the server
        started has no connection at all, because opening one reaches the node
        to fetch its TLS certificate. A node that went away afterwards - one
        that rebooted, or a whole lab that was redeployed - does have one, but
        its gRPC channel belongs to the instance that disappeared and keeps
        answering every call from its own failed state, so it has to be replaced
        rather than waited on.

        Both are rate-limited to one attempt per ``connect_retry_interval`` and
        run in the background: the render that schedules one still reports the
        node as failing, and a later render picks it up. That keeps a node that
        is slow to fail from holding up the render of every other node.
        """
        if self._stop.is_set():
            return
        now = time.time()
        due: List[str] = []
        with self._lock:
            for name in names:
                stream = self._streams.get(name)
                if stream is not None:
                    failing = stream.failing_since
                    # Updates that stopped arriving count as much as calls that
                    # fail: a silently dead subscription is only recovered by a
                    # new connection, since nothing about it looks broken.
                    stale = stream.stale_for
                    if failing is None and stale is None:
                        continue  # the node is answering
                    bad_for = max(
                        now - failing if failing is not None else 0.0, stale or 0.0
                    )
                    if self.connect_retry_interval > 0 and bad_for < self.connect_retry_interval:
                        continue
                last = self._connect_attempts.get(name)
                if self.connect_retry_interval > 0 and last is not None and 0 <= (now - last) < self.connect_retry_interval:
                    continue
                # Stamped before submitting, so concurrent renders queue a node
                # once rather than once each.
                self._connect_attempts[name] = now
                due.append(name)
        if due:
            logger.debug("queued for reconnect: %s", ", ".join(due))
        for name in due:
            host = self.nornir.inventory.hosts.get(name)
            if host is not None:
                self._pool.submit(self._reconnect, name, host)

    def _reconnect(self, name: str, host: Any) -> None:
        """Give *name* a fresh gNMI connection, discarding anything stale."""
        with self._lock:
            stream = self._streams.pop(name, None)
            # Both were learned through the connection that stopped working, so
            # they are re-discovered against the new one.
            for key in [k for k in self._specs if k[0] == name]:
                del self._specs[key]
            for key in [k for k in self._activation_errors if k[0] == name]:
                del self._activation_errors[key]
            self._table_cache.clear()
            self._last_state_change = time.time()
        if stream is not None:
            logger.info("%s: gNMI calls stopped working, reconnecting", name)
            stream.stop()
            try:
                host.close_connection(CONNECTION_NAME)
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("%s: closing the old connection failed: %s", name, exc)
        self._connect(name, host)

    def _resync_loop(self) -> None:
        """Re-read one node per tick, spreading a sweep over ``resync_interval``.

        SAMPLE subscriptions refresh the values they carry but rely on the target
        reporting deletes for entries that disappear, so a periodic full re-read
        is the safety net. It costs one ``Get`` per subscribed path, which is
        why nodes are walked round-robin rather than all at once.
        """
        cursor = 0
        while True:
            with self._lock:
                node_count = len(self._streams)
            if self._stop.wait(self.resync_interval / max(node_count, 1)):
                return
            with self._lock:
                names = list(self._streams)
                if not names:
                    continue
                stream = self._streams.get(names[cursor % len(names)])
            cursor += 1
            if stream is None:
                continue
            try:
                stream.resync()
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.debug("%s: resync failed: %s", stream.name, exc)

    @property
    def stopping(self) -> bool:
        """True once shutdown has been requested."""
        return self._stop.is_set()

    def stop(self) -> None:
        """Tear down streams and gNMI connections. Safe to call more than once.

        Does not use ``self._pool``: table renders may already occupy every
        worker with an in-flight Get, and queuing teardown behind them would
        deadlock. A private executor closes the RPCs so those Gets fail and the
        SSE tasks uvicorn is waiting on can finish.
        """
        self._stop.set()
        self.watcher.stop()
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
            with self._lock:
                self._table_cache.clear()
                streams = list(self._streams.values())
                self._streams.clear()
            if self._resync_thread is not None:
                self._resync_thread.join(timeout=1)

            hosts = list(self.nornir.inventory.hosts.items())

            def _close_host(item: Tuple[str, Any]) -> None:
                name, host = item
                try:
                    host.close_connections()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s: error closing connection: %s", name, exc)

            def _stop_stream(stream: Any) -> None:
                try:
                    stream.stop(timeout=1.0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s: error stopping stream: %s", stream.name, exc)

            workers = min(16, max(len(streams), len(hosts), 1))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="fcli-stop"
            ) as closer:
                # Streams first: stopping one cancels its Subscribe RPC, which is
                # what actually releases the session on the node. Closing the
                # channel next aborts any Get still in flight.
                list(closer.map(_stop_stream, streams))
                list(closer.map(_close_host, hosts))
            self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # inventory
    # ------------------------------------------------------------------ #

    def inventory(self) -> List[Dict[str, Any]]:
        with self._lock:
            streams = dict(self._streams)
            connect_errors = dict(self._connect_errors)
        result = []
        for name, host in self.nornir.inventory.hosts.items():
            stream = streams.get(name)
            result.append(
                {
                    "name": name,
                    "hostname": host.hostname or name,
                    "labels": {k: v for k, v in (host.data or {}).items()},
                    # 'connected' says the node is answering: holding a stream
                    # object proves nothing, since the gRPC channel behind it
                    # outlives the node it was opened to. 'streaming' says a
                    # Subscribe RPC is running on it, which only happens once a
                    # report has been opened.
                    # Three different ways of losing a node, because no single
                    # one of them catches the others: the subscription reporting
                    # an error, a Get failing or hanging, and updates that were
                    # due never arriving on a connection nobody declared dead.
                    "connected": bool(
                        stream
                        and stream.error is None
                        and stream.failing_since is None
                        and stream.stale_for is None
                    ),
                    "streaming": bool(stream and stream.connected),
                    # A Get in flight, including ones still inside the hang grace
                    # that have not yet flipped 'connected'. The Nodes pane shows
                    # a transfer mark for this rather than treating the node as down.
                    "getting": bool(stream and stream.getting),
                    # Sampling 'getting' alone almost never catches a Get: they
                    # finish in milliseconds. This counter is what tells the
                    # Nodes pane that one happened between two polls.
                    "gets": stream.gets if stream else 0,
                    "error": connect_errors.get(name)
                    or (stream.last_error if stream else None),
                    "last_update": stream.last_update if stream else None,
                }
            )
        return result

    def resolve_host(self, node: str) -> Tuple[str, Any]:
        """Inventory name and host for *node*, matching name or hostname."""
        hosts = self.nornir.inventory.hosts
        if node in hosts:
            return node, hosts[node]
        for name, host in hosts.items():
            if (host.hostname or name) == node:
                return name, host
        raise KeyError(f"unknown node '{node}'")

    def node_get(
        self, node: str, path: str, datatype: str = "state"
    ) -> List[Dict[str, Any]]:
        """A serialized gNMI Get on *node*, sharing the stream's Get lock."""
        name, _host = self.resolve_host(node)
        with self._lock:
            stream = self._streams.get(name)
            error = self._connect_errors.get(name)
        if stream is None:
            raise RuntimeError(error or f"node {name} is not connected")
        return stream.direct_get(path, datatype)

    def targets(self, inv_filter: Optional[Dict[str, str]] = None) -> List[str]:
        """The inventory nodes *inv_filter* selects, whether or not they answer."""
        return self._targets(inv_filter)

    def _targets(self, inv_filter: Optional[Dict[str, str]]) -> List[str]:
        target = self.nornir.filter(**inv_filter) if inv_filter else self.nornir
        return list(target.inventory.hosts)

    def _streams_for(self, names: List[str]) -> List[HostStream]:
        with self._lock:
            return [self._streams[n] for n in names if n in self._streams]

    # ------------------------------------------------------------------ #
    # report activation
    # ------------------------------------------------------------------ #

    def activate(self, report: ReportSpec, hosts: Optional[List[str]] = None) -> None:
        """Make sure every node streams the paths *report* needs.

        This runs on every render, not just the first one: re-asserting the paths
        is what marks them as still in use, so a report someone is watching is
        never retired from under it.
        """
        with self._lock:
            names = hosts if hosts is not None else list(self._streams)
            pending = [n for n in names if n in self._streams]
        if not pending:
            return
        list(self._pool.map(lambda n: self._activate_host(report, n), pending))

    def _activate_host(self, report: ReportSpec, name: str) -> None:
        key = (name, report.name)
        with self._lock:
            stream = self._streams.get(name)
            if stream is None:
                return
            failed = self._activation_errors.get(key)
            if failed is not None:
                # Discovery runs the report's getter against the device, so a
                # node that cannot serve it is not re-probed on every render.
                # The reason is often temporary though - the node was rebooting
                # - so the verdict expires instead of standing for good.
                if time.time() - failed[0] < self.connect_retry_interval:
                    logger.debug(
                        "%s: skipping report '%s', it failed %.0fs ago: %s",
                        name,
                        report.name,
                        time.time() - failed[0],
                        failed[1],
                    )
                    return
                del self._activation_errors[key]
            specs = self._specs.get(key)
        try:
            if specs is None:
                started = time.perf_counter()
                specs = self._discover(report, stream)
                logger.debug(
                    "%s: report '%s' needs %d path(s), discovered in %.3fs: %s",
                    name,
                    report.name,
                    len(specs),
                    time.perf_counter() - started,
                    ", ".join(s.path for s in specs) or "none",
                )
                with self._lock:
                    self._specs[key] = specs
            stream.ensure_paths(specs)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning(
                "%s: activating report '%s' failed: %s", name, report.name, exc
            )
            logger.debug(
                "%s: activating report '%s' failed", name, report.name, exc_info=exc
            )
            with self._lock:
                self._activation_errors[key] = (time.time(), str(exc))

    def _discover(self, report: ReportSpec, stream: HostStream) -> List[SubscriptionSpec]:
        """Determine which gNMI paths a report needs on this node."""
        if report.subscribe:
            interval = self.sample_interval
            if interval is None:
                return list(report.subscribe)
            return [
                SubscriptionSpec(
                    s.path, s.datatype, s.mode, min(interval, s.sample_interval)
                )
                for s in report.subscribe
            ]
        recorder = RecordingDevice(stream.device, stream.discovery_get)
        report.getter(recorder)
        interval = self.sample_interval or report.sample_interval
        return [
            SubscriptionSpec(path=path, datatype=datatype, mode=subscription_mode(path), sample_interval=interval)
            for path, datatype in recorder.recorded
        ]

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #

    def table(
        self,
        report: ReportSpec,
        inv_filter: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Render *report* across the (filtered) inventory from streamed state.

        *params* are the report's own arguments, as declared by
        :attr:`ReportSpec.params`. They only ever narrow what the getter makes
        of the state already streamed, so they cost no gNMI and never change
        which paths a node subscribes to.
        """
        if self._stop.is_set():
            return {
                "report": report.name,
                "title": report.title,
                "columns": ["Node"],
                "rows": [],
                "errors": [],
                "nodes": 0,
                "generated": time.time(),
                "render_ms": 0.0,
                "oldest_update": None,
            }
        names = self._targets(inv_filter)
        self._heal_connections(names)
        self.activate(report, names)

        inv_key = tuple(sorted(inv_filter.items())) if inv_filter else None
        param_key = tuple(sorted((params or {}).items())) or None
        cache_key = (report.name, inv_key, param_key)
        now = time.time()
        with self._lock:
            cached = self._table_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_table = cached
                if (cached_at >= self._last_state_change or (now - cached_at < 0.5)) and not cached_table.get("errors"):
                    logger.debug(
                        "report '%s': serving the table cached %.2fs ago",
                        report.name,
                        now - cached_at,
                    )
                    return cached_table

        started = time.time()
        errors: List[Dict[str, str]] = []
        if report.name == CHECKS_REPORT:
            # Findings are about the fabric rather than about one node, so they
            # are gathered across it rather than merged per host. A node the
            # checks could not read becomes a finding, not a table-level error.
            all_columns = list(CHECKS_COLUMNS)
            clean_rows = self._checks_rows(inv_filter)
        else:
            results = list(
                self._pool.map(lambda n: self._host_rows(report, n, params), names)
            )

            columns: List[str] = []
            rows: List[Dict[str, Any]] = []
            # A node with no routes at all cannot tell that 'Rib' groups rows
            # rather than holding a value, so the fabric decides it together.
            containers: Set[str] = set()
            for name, cols, host_rows, error, host_containers in results:
                if error:
                    errors.append({"node": name, "error": error})
                    continue
                merge_fields(columns, cols)
                containers |= host_containers
                rows.extend(host_rows)

            columns = [c for c in columns if c not in containers]
            all_columns = ["Node"] + clean_columns(columns)
            clean_rows = [
                {
                    c: _cell(row.get(raw))
                    for c, raw in zip(all_columns, ["Node"] + columns)
                }
                for row in rows
            ]
            if report.name in ("bridge_domains", "routers", "services"):
                if stamp_underlay_sites(clean_rows):
                    if "Site" not in all_columns:
                        all_columns.append("Site")
        res_table = {
            "report": report.name,
            "title": report.title,
            "columns": all_columns,
            "rows": clean_rows,
            "errors": errors,
            "nodes": len(names),
            "generated": started,
            "render_ms": round((time.time() - started) * 1000, 1),
            "oldest_update": _oldest_update(self._streams_for(names)),
        }
        with self._lock:
            # The cache is keyed by the inventory filter and the report's own
            # parameters as well as its name, and an API client picks both, so
            # the key space has no natural bound.
            if (
                cache_key not in self._table_cache
                and len(self._table_cache) >= _MAX_CACHED_TABLES
            ):
                oldest = min(self._table_cache, key=lambda k: self._table_cache[k][0])
                del self._table_cache[oldest]
            self._table_cache[cache_key] = (started, res_table)
        logger.debug(
            "report '%s': rendered %d row(s) over %d column(s) from %d node(s) "
            "in %.1fms, %d node(s) in error%s",
            report.name,
            len(clean_rows),
            len(all_columns),
            len(names),
            res_table["render_ms"],
            len(errors),
            f" ({', '.join(e['node'] for e in errors)})" if errors else "",
        )
        return res_table

    def _host_payload(
        self,
        report: ReportSpec,
        name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[List[Any]], Optional[str]]:
        """What *report*'s getter makes of one node, before it becomes a table.

        The items are ``None`` rather than empty when there was nothing to ask:
        a node that is going away as the store shuts down has no state, which
        is not the same as having none.
        """
        with self._lock:
            stream = self._streams.get(name)
            if stream is None:
                return name, None, self._connect_errors.get(name, "not connected")
            activation_error = self._activation_errors.get((name, report.name))
        if activation_error:
            return name, None, activation_error[1]
        if self._stop.is_set():
            return name, None, None
        try:
            result = report.getter(CachedDevice(stream), **(params or {}))
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.debug(
                "%s: report '%s' failed: %s", name, report.name, exc, exc_info=exc
            )
            return name, None, str(exc)
        return name, (result or {}).get(report.resource) or [], None

    def _host_rows(
        self,
        report: ReportSpec,
        name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[str], List[Dict[str, Any]], Optional[str], Set[str]]:
        name, items, error = self._host_payload(report, name, params)
        if error is not None or items is None:
            return name, [], [], error, set()
        host = self.nornir.inventory.hosts.get(name)
        node = (host.hostname if host and host.hostname else name) or name
        columns, rows = flatten(node, items, report.table_for(params))
        return name, columns, rows, None, sub_item_keys(items)

    # ------------------------------------------------------------------ #
    # checks
    # ------------------------------------------------------------------ #

    def fabric_state(
        self,
        inv_filter: Optional[Dict[str, str]] = None,
        reports: Sequence[str] = reading_reports(REQUIRED_REPORTS),
        history: bool = True,
        watched: Sequence[str] = (),
    ) -> FabricState:
        """Collect what the sanity checks read, across the filtered inventory.

        A report that stands in for another - the underlay's route table for
        the whole RIB - is kept under the name of the one it stands in for,
        which is what the checks look for.

        With *history*, the state carries the timeline too, scoped to the
        same nodes: the recent changes a flap is counted in, and what answers
        "what changed". The watcher asks without, as it is what keeps it.

        *watched* prefixes are looked up one by one on every node, wherever
        they are installed; see :meth:`_watched_routes`.
        """
        names = self._targets(inv_filter)
        self._heal_connections(names)
        state = FabricState()
        state.hostnames = {
            name: (host.hostname or name)
            for name, host in self.nornir.inventory.hosts.items()
            if name in set(names)
        }
        state.containerlab = containerlab_nodes(self.nornir.inventory.hosts) & set(names)
        for report_name in reports:
            spec = get_report(report_name)
            key = spec.stands_in_for or report_name
            try:
                self.activate(spec, names)
            except Exception as exc:  # noqa: BLE001 - the other reports still answer
                logger.warning("activating report '%s' for checks failed: %s", report_name, exc)
            payloads: Dict[str, Any] = {}
            collected = self._pool.map(
                lambda n, s=spec: self._host_payload(s, n), names
            )
            for node, items, error in collected:
                if error is not None:
                    state.errors[(key, node)] = error
                elif items is not None:
                    payloads[node] = items
            state.reports[key] = payloads
        if watched:
            self._watched_routes(state, names, watched)
        if history:
            state.changes = self.timeline.recent(nodes=names)
            state.history = self.timeline.scoped(names)
        state.acknowledged = self.acks.keys()
        return state

    def _watched_routes(self, state: FabricState, names: List[str], prefixes: Sequence[str]) -> None:
        """Look *prefixes* up on every node in *names*, into *state*.

        A reading holds the underlay's route table and only the size of the
        others, and a prefix someone asked to follow can be in any of them.
        A Get of each, rather than a subscription: a handful of paths per
        reading, where streaming them would take a subscription slot each.
        """
        for report, _family, afi in WATCHED_ROUTE_REPORTS:
            wanted = [p for p in prefixes if (":" in p) == (afi == "ipv6-unicast")]
            if not wanted:
                continue

            def fetch(name: str, afi: str = afi, wanted: List[str] = wanted) -> Tuple[str, Optional[List[Any]], Optional[str]]:
                with self._lock:
                    stream = self._streams.get(name)
                if stream is None:
                    return name, None, self._connect_errors.get(name, "not connected")
                try:
                    return name, DirectDevice(stream).get_routes(afi, wanted)["ip_rib"], None
                except Exception as exc:  # noqa: BLE001 - one node's answer, not the reading's
                    return name, None, str(exc)

            payloads: Dict[str, Any] = {}
            for node, tables, error in self._pool.map(fetch, names):
                if error is not None:
                    state.errors[(report, node)] = error
                elif tables is not None:
                    payloads[node] = tables
            state.reports[report] = payloads

    #: How long the service route tables fetched for the topology are reused.
    ALIASING_TTL = 30.0

    def _service_ribs(self, graph: Dict[str, Any], state: FabricState) -> FabricState:
        """*state*, with the route tables of every service a virtual segment is in.

        A reading holds the underlay's tables only, and what says which remote
        VTEPs spread traffic over a virtual segment is the service's own table
        on the other nodes. Only those services are fetched, only from the
        nodes that have them, and kept a while: the topology is redrawn far
        more often than aliasing changes.
        """
        services = {ni for n in graph.get("nodes", []) if n.get("virtual") for ni in n.get("services", [])}
        wanted = tuple(sorted({(node, instance.name) for node, instance in state.items("ni") if instance.name in services}))
        if not wanted:
            return state
        cached = self._alias_ribs
        if cached is not None and cached[1] == wanted and time.time() - cached[0] < self.ALIASING_TTL:
            fetched = cached[2]
        else:
            by_node: Dict[str, List[str]] = {}
            for node, ni in wanted:
                by_node.setdefault(node, []).append(ni)

            def fetch(item: Tuple[str, List[str]]) -> Tuple[str, Dict[str, List[Any]]]:
                node, nis = item
                with self._lock:
                    stream = self._streams.get(node)
                tables: Dict[str, List[Any]] = {}
                if stream is None:
                    return node, tables
                device = DirectDevice(stream)
                for report, afi in (("ipv4_rib", "ipv4-unicast"), ("ipv6_rib", "ipv6-unicast")):
                    for ni in nis:
                        try:
                            tables.setdefault(report, []).extend(device.get_rib(afi, network_instance=ni)["ip_rib"])
                        except Exception as exc:  # noqa: BLE001 - the rest of the drawing stands
                            logger.debug("%s: route table of %s for aliasing: %s", node, ni, exc)
                return node, tables

            fetched = {}
            for node, tables in self._pool.map(fetch, sorted(by_node.items())):
                for report, found in tables.items():
                    fetched.setdefault(report, {})[node] = found
            self._alias_ribs = (time.time(), wanted, fetched)
        reports = dict(state.reports)
        for report, per_node in fetched.items():
            merged = {node: list(tables) for node, tables in reports.get(report, {}).items()}
            for node, tables in per_node.items():
                merged[node] = merged.get(node, []) + list(tables)
            reports[report] = merged
        return replace(state, reports=reports)

    # ------------------------------------------------------------------ #
    # health: findings and incidents
    # ------------------------------------------------------------------ #

    #: How long an on-demand health reading is reused for.
    HEALTH_TTL = 10.0

    def health(self, inv_filter: Optional[Dict[str, str]] = None) -> Reading:
        """The findings and incidents of the (filtered) fabric, as recently as they are known.

        The watcher's latest reading answers when it is fresh and nothing is
        filtered out of it; otherwise the checks are run here, and the answer
        is kept a few seconds so that a page polling for it does not run every
        check on every poll.
        """
        latest = self.timeline.latest
        fresh = self.watch_interval > 0 and latest is not None and (
            time.time() - latest.at < 3 * self.watch_interval
        )
        if fresh and not inv_filter:
            return latest
        key = tuple(sorted(inv_filter.items())) if inv_filter else None
        now = time.time()
        with self._lock:
            cached = self._health_cache.get(key)
        if cached is not None and now - cached[0] < self.HEALTH_TTL:
            return cached[1]
        state = self.fabric_state(inv_filter)
        findings = run_checks(state)
        reading = Reading(at=now, state=state, findings=findings, incidents=correlate(findings, state))
        with self._lock:
            if len(self._health_cache) >= 16:
                self._health_cache.clear()
            self._health_cache[key] = (now, reading)
        return reading

    # ------------------------------------------------------------------ #
    # acknowledging
    # ------------------------------------------------------------------ #

    def _incident(self, incident_id: str, inv_filter: Optional[Dict[str, str]] = None) -> Optional[Incident]:
        """The incident *incident_id*, as the page that shows it was rendered.

        An incident's id is what it is anchored to, and with an inventory
        filter that can differ from the whole fabric's - a link seen from one
        end only - so it is looked up in the same view.
        """
        for incident in self.health(inv_filter).incidents:
            if incident.id == incident_id:
                return incident
        return None

    def acknowledge(
        self, incident_id: str, note: str = "", inv_filter: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Acknowledge every finding the incident *incident_id* holds now."""
        incident = self._incident(incident_id, inv_filter)
        if incident is None:
            raise KeyError(f"no incident '{incident_id}': it may have cleared")
        made = self.acks.acknowledge(
            incident.findings, note=note, incident=incident.title, incident_id=incident.id
        )
        self._record_ack(incident.node, incident.title, "acknowledged", note or f"{len(made)} finding(s) acknowledged")
        return {"incident": incident.id, "acknowledged": len(made)}

    def acknowledge_all(
        self, note: str = "", inv_filter: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Acknowledge every incident still open in the (filtered) fabric."""
        acked = self.acks.keys()
        done = []
        for incident in mark_acknowledged(self.health(inv_filter).incidents, acked):
            if incident.acknowledged:
                continue
            made = self.acks.acknowledge(
                incident.findings, note=note, incident=incident.title, incident_id=incident.id
            )
            self._record_ack(
                incident.node, incident.title, "acknowledged", note or f"{len(made)} finding(s) acknowledged"
            )
            done.append(incident.id)
        return {"acknowledged": len(done), "incidents": done}

    def unacknowledge(
        self, incident_id: str, inv_filter: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Take the acknowledgement off the incident *incident_id*.

        Both what it holds now and what was acknowledged under its id: an
        incident can change shape between the two - regrouped, seen through
        another filter, restored after a restart - and taking an
        acknowledgement off must not depend on it looking the same.
        """
        incident = self._incident(incident_id, inv_filter)
        keys = self.acks.of_incident(incident_id)
        if incident is not None:
            keys |= {finding_key(f) for f in incident.findings}
        dropped = self.acks.unacknowledge(keys)
        if incident is None and not dropped:
            raise KeyError(f"nothing is acknowledged as incident '{incident_id}'")
        node = incident.node if incident else dropped[0].node
        title = incident.title if incident else dropped[0].incident
        self._record_ack(node, title, "unacknowledged", f"{len(dropped)} finding(s) no longer acknowledged")
        return {"incident": incident_id, "unacknowledged": len(dropped)}

    def _record_ack(self, node: str, title: str, what: str, detail: str) -> None:
        """Acknowledging is an event on the timeline, like any other."""
        self.timeline.record([Change(time.time(), node, "ack", title, "", what, INFO, detail)])
        with self._lock:
            self._table_cache.clear()

    def set_baseline(self) -> Dict[str, Any]:
        """Keep the fabric as it is now as what it is compared against."""
        reading = self.timeline.latest
        if reading is None or self.watch_interval <= 0:
            reading = self.health()
        self.timeline.set_baseline(reading)
        return self.timeline.status()

    def _checks_rows(
        self, inv_filter: Optional[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """The findings of every check, as the rows of a table."""
        return [f.as_row() for f in run_checks(self.fabric_state(inv_filter))]

    # ------------------------------------------------------------------ #
    # lenses
    # ------------------------------------------------------------------ #

    def lens_table(
        self,
        lens: LensSpec,
        inv_filter: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ask *lens* its question of the streamed state.

        The same shape :meth:`table` renders a report into, with the records
        and the hierarchy the browser draws them as alongside the rows. A
        lens is run rather than streamed: the reports it reads are what is
        streamed, and are activated here the way the checks activate theirs.
        A question it cannot answer - an address that is not one, a service
        that matches nothing - raises :class:`ValueError` for the caller to
        show.
        """
        started = time.time()
        if lens.name == "incidents":
            # The watcher has just run every check and grouped the findings;
            # answering from its reading spares a page refreshing every two
            # seconds from running them all again.
            reading = self.health(inv_filter)
            state, records = reading.state, mark_acknowledged(reading.incidents, self.acks.keys())
        else:
            state = self.fabric_state(inv_filter, reports=lens.requires)
            records = lens.run(state, **(params or {}))
        rows = lens.rows(records)
        if lens.group_by_node:
            rows.sort(key=lambda r: str(r.get("Node", "")))
        errors = [
            {"node": node, "error": f"{report} not collected: {error}"}
            for (report, node), error in sorted(state.errors.items())
        ]
        names = self._targets(inv_filter)
        return {
            "report": lens.name,
            "title": lens.title,
            "columns": ["Node", *lens.column_names],
            "rows": [{k: cell(v) for k, v in row.items()} for row in rows],
            "records": [as_dict(record) for record in records],
            "tree": [as_dict(card) for card in lens.tree(records)],
            "graph": lens.graph(records) if lens.graph else None,
            "errors": errors,
            "nodes": len(names),
            "generated": started,
            "render_ms": round((time.time() - started) * 1000, 1),
            "oldest_update": _oldest_update(self._streams_for(names)),
        }

    def network_instances(
        self, inv_filter: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        """The network-instances the fabric has, for a surface to offer a choice of.

        One entry per name across the filtered nodes, with its type and how
        many nodes carry it. The default instance comes first, then the
        routed ones, then the bridged: the order someone looking a route up
        wants them in.
        """
        state = self.fabric_state(inv_filter, reports=("ni",))
        found: Dict[str, Dict[str, Any]] = {}
        for _node, instance in state.items("ni"):
            entry = found.setdefault(instance.name, {"name": instance.name, "type": instance.type, "nodes": 0})
            entry["nodes"] += 1
        rank = {"default": 0, "ip-vrf": 1, "mac-vrf": 2}
        return sorted(found.values(), key=lambda e: (rank.get(e["type"], 3), e["name"]))

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        with self._lock:
            host_streams = list(self._streams.values())
            connect_errors = dict(self._connect_errors)
        streams = [s.status() for s in host_streams]
        return {
            "nodes": streams,
            "unreachable": [{"node": n, "error": e} for n, e in connect_errors.items()],
            "subscriptions": sum(len(s["paths"]) for s in streams),
            # The gRPC sessions a single node spends on us. SR Linux allows 20
            # per gRPC server by default, shared with every other client, so
            # this is the number to watch.
            "max_sessions_per_node": max((s["sessions"] for s in streams), default=0),
            "resync_interval": self.resync_interval,
        }

    def overview(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Aggregate fabric-wide health and topology metrics for the dashboard."""
        names = self._targets(inv_filter)
        self._heal_connections(names)
        try:
            self.activate(get_report("overview"), names)
        except Exception as exc:  # noqa: BLE001 - the summary still renders
            logger.warning("activating the overview report failed: %s", exc)

        hosts = self.inventory()
        with self._lock:
            streams = [self._streams[n] for n in names if n in self._streams]
            unreachable_nodes = len(self._connect_errors)
            cached_tables = len(self._table_cache)
            all_streams = list(self._streams.values())

        # Each snapshot is taken under its own node's lock and nothing else, so
        # summarizing the fabric does not stall the renders of every report on it.
        health = _Health()
        # A veth discards what a real port forwards, so on containerlab the
        # error counters say nothing about the fabric; see check_itf_errors.
        virtual = containerlab_nodes(self.nornir.inventory.hosts)
        for stream in streams:
            snapshot = stream.snapshot_roots(_OVERVIEW_ROOTS)
            itfs = snapshot.get("interface")
            _tally_interfaces(health, itfs, count_errors=stream.name not in virtual)
            _tally_network_instances(health, snapshot.get("network-instance"), itfs)

        return {
            "health": self._health_summary(inv_filter, names),
            "nodes": {
                "total": len(hosts),
                "connected": sum(1 for h in hosts if h["connected"]),
                "streaming": sum(1 for h in hosts if h["streaming"]),
                "unreachable": unreachable_nodes,
            },
            "bgp": {
                "total": health.bgp_total,
                "established": health.bgp_established,
                "down": health.bgp_down,
            },
            "interfaces": {
                "total": health.itf_total,
                "down": health.itf_down,
                "errors": health.itf_errors,
            },
            "bridge_domains": _roll_up(health.bridge_domains),
            "routers": _roll_up(health.routers),
            "telemetry": {
                "subscriptions": sum(len(s.status()["paths"]) for s in all_streams),
                "resync_interval": self.resync_interval,
                "cached_tables": cached_tables,
            },
        }

    def _health_summary(
        self, inv_filter: Optional[Dict[str, str]], names: List[str]
    ) -> Optional[Dict[str, Any]]:
        """The incidents and recent changes, counted, for the dashboard."""
        try:
            reading = self.health(inv_filter)
        except Exception as exc:  # noqa: BLE001 - the rest of the dashboard still renders
            logger.warning("summarizing fabric health failed: %s", exc)
            return None
        recent = self.timeline.changes(since=time.time() - 900, nodes=names)
        baseline = self.timeline.baseline
        # What is acknowledged is known, and is not what this card is for.
        incidents = mark_acknowledged(reading.incidents, self.acks.keys())
        open_ = [i for i in incidents if not i.acknowledged]
        return {
            "at": reading.at,
            "incidents": len(open_),
            "errors": sum(1 for i in open_ if i.severity == "error"),
            "warnings": sum(1 for i in open_ if i.severity == "warning"),
            "findings": sum(len(i.findings) for i in open_),
            "acknowledged": len(incidents) - len(open_),
            "worst": open_[0].title if open_ else "",
            "changes_15m": len(recent),
            "failures_15m": sum(1 for c in recent if c.severity == "error"),
            "baseline_at": baseline.at if baseline else None,
            "watching": self.watch_interval > 0,
        }

    def topology(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """The fabric graph: LLDP adjacencies, with a tier per node.

        Nodes that are down or have not streamed anything yet are still part of
        the answer, as unclassified ones - a topology missing the node that
        failed would be the opposite of useful.
        """
        started = time.time()
        names = self._targets(inv_filter)
        self._heal_connections(names)
        try:
            self.activate(get_report("topology"), names)
        except Exception as exc:  # noqa: BLE001 - the graph still renders
            logger.warning("activating the topology report failed: %s", exc)

        hosts = {h["name"]: h for h in self.inventory()}
        with self._lock:
            streams = {n: self._streams[n] for n in names if n in self._streams}

        facts = []
        for name in names:
            host = self.nornir.inventory.hosts.get(name)
            stream = streams.get(name)
            status = hosts.get(name, {})
            facts.append(
                node_facts(
                    name,
                    hostname=(host.hostname if host else "") or name,
                    labels=(host.data if host else None) or {},
                    # Taken under this node's lock only, so summarizing the
                    # fabric does not stall the renders running on the others.
                    snapshot=stream.snapshot_roots(_TOPOLOGY_ROOTS) if stream else None,
                    connected=bool(status.get("connected")),
                    error=status.get("error"),
                    egress=_interface_egress(stream),
                    remembered=self.timeline.cables(name),
                )
            )

        graph = build_topology(facts)
        try:
            health = self.health(inv_filter)
            acked = self.acks.keys()
            annotate_health(
                graph,
                locate(health.findings, health.state),
                mark_acknowledged(health.incidents, acked),
                acked=acked,
            )
            graph["health_at"] = health.at
            # The route tables the checks read are also what says which
            # remote VTEPs load-balance over a virtual segment.
            annotate_aliasing(graph, self._service_ribs(graph, health.state))
        except Exception as exc:  # noqa: BLE001 - the drawing is worth having without its colours
            logger.warning("annotating the topology with health failed: %s", exc)
            logger.debug("annotating the topology failed", exc_info=exc)
        graph["generated"] = started
        graph["render_ms"] = round((time.time() - started) * 1000, 1)
        graph["oldest_update"] = _oldest_update(list(streams.values()))
        return graph


#: Tree roots the overview summarizes.
_OVERVIEW_ROOTS: Tuple[str, ...] = ("interface", "network-instance")

#: Tree roots the topology is inferred from: LLDP and the host-name under
#: ``system``, the services under ``network-instance``, port states under
#: ``interface``, chassis type under ``platform``.
_TOPOLOGY_ROOTS: Tuple[str, ...] = (
    "system",
    "network-instance",
    "interface",
    "platform",
)


def _interface_egress(stream: Optional[HostStream]) -> Dict[str, int]:
    """Bits per second leaving each interface, from streamed counter samples."""
    if stream is None:
        return {}
    result: Dict[str, int] = {}
    for name, rates in stream.rates.all_rates().items():
        if "out-octets" in rates:
            result[name] = round(rates["out-octets"] * 8)
    return result

#: Cap on cached rendered tables, evicting the oldest beyond it.
_MAX_CACHED_TABLES = 256

_UP_STATES = frozenset({"up", "enable", "enabled", "active"})
_DOWN_STATES = frozenset({"down", "disable", "disabled"})
#: A network-instance reports itself established rather than up.
_MEMBER_UP_STATES = _UP_STATES | {"established"}

_ERROR_COUNTERS: Tuple[str, ...] = (
    "in-error-packets",
    "out-error-packets",
    "in-discarded-packets",
    "out-discarded-packets",
)


@dataclass
class _Health:
    """Tallies accumulated across the nodes of the fabric."""

    bgp_total: int = 0
    bgp_established: int = 0
    bgp_down: int = 0
    itf_total: int = 0
    itf_down: int = 0
    itf_errors: int = 0
    #: (service name, state) per node, rolled up by :func:`_roll_up`.
    bridge_domains: List[Tuple[str, str]] = field(default_factory=list)
    routers: List[Tuple[str, str]] = field(default_factory=list)


def _leaf(value: Any) -> str:
    """Normalize a YANG enum leaf to its bare, lower-case value."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]


def _is_configured(itf: Dict[str, Any], oper_state: str) -> bool:
    """Whether an interface is in use, and so worth counting as healthy or not.

    Most ports of a fabric leaf are never patched, and an unused port is down by
    definition; counting those as faults would bury the ones that matter. A port
    counts once anything says it is meant to carry traffic.
    """
    name = str(itf.get("name", ""))
    if name.startswith(("mgmt", "system", "lo", "lag")):
        return True
    if oper_state == "up" or itf.get("description"):
        return True
    subinterfaces = itf.get("subinterface", [])
    if subinterfaces:
        return True
    ethernet = itf.get("ethernet", {})
    return isinstance(ethernet, dict) and bool(ethernet.get("aggregate-id"))


def _tally_interfaces(health: _Health, itfs: Any, count_errors: bool = True) -> None:
    """Count the configured interfaces of one node by health."""
    if not isinstance(itfs, list):
        return
    for itf in itfs:
        if not isinstance(itf, dict):
            continue
        if _leaf(itf.get("admin-state")) in _DOWN_STATES:
            continue
        oper_state = _leaf(itf.get("oper-state"))
        if not _is_configured(itf, oper_state):
            continue
        health.itf_total += 1
        # A port an ethernet-segment holds in standby is down because it was
        # told to be; counting it as a fault puts a permanent red number on a
        # healthy multi-homed fabric.
        if oper_state == "down" and not is_intent(itf.get("oper-down-reason")):
            health.itf_down += 1
        stats = itf.get("statistics", {})
        if not count_errors or not isinstance(stats, dict):
            continue
        errors = 0
        for counter in _ERROR_COUNTERS:
            try:
                errors += int(stats.get(counter, 0) or 0)
            except (TypeError, ValueError):
                continue
        if errors > 0:
            health.itf_errors += 1


def _route_targets(ni: Dict[str, Any]) -> List[str]:
    """The route-targets of a network-instance, as ``target:x:y`` strings."""
    instances = _branch(ni, "protocols", "bgp-vpn").get("bgp-instance", [])
    if isinstance(instances, dict):
        instances = [instances]
    if not isinstance(instances, list):
        return []
    targets = set()
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        config = instance.get("route-target", {})
        if not isinstance(config, dict):
            continue
        for key in ("import-rt", "export-rt"):
            raw = config.get(key, [])
            if isinstance(raw, (str, dict)):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            for item in raw:
                target = item.get("target") if isinstance(item, dict) else item
                if not target:
                    continue
                text = str(target)
                targets.add(text if text.startswith("target:") else f"target:{text}")
    return sorted(targets)


def _branch(node: Any, *names: str) -> Dict[str, Any]:
    """Descend through nested containers, yielding ``{}`` at the first miss."""
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _effective_state(ni: Dict[str, Any], oper_state: str, itf_states: Dict[str, str]) -> str:
    """The state of a network-instance, refined by the interfaces attached to it.

    A network-instance reports itself up while some of the interfaces placed in
    it are down, which is what 'degraded' is for: the service exists on the node
    but is not carrying everything it was meant to.
    """
    if oper_state == "down":
        return "down"
    attached = ni.get("interface", [])
    if not isinstance(attached, list):
        return oper_state
    states = [
        state
        for state in (
            itf_states.get(str(itf.get("name", "")))
            for itf in attached
            if isinstance(itf, dict) and itf.get("name")
        )
        # A member in standby is counted neither way: an ethernet-segment leaves
        # the non-forwarding leaf's port down by design, and counting that as
        # down would degrade every multi-homed service on that node.
        if state and state != STANDBY_STATE
    ]
    if not states:
        return oper_state
    if all(state in _UP_STATES for state in states):
        return "up"
    if all(state in _DOWN_STATES for state in states):
        return "down"
    return "degraded"


def _tally_network_instances(health: _Health, nis: Any, itfs: Any) -> None:
    """Count BGP sessions and record the services of one node."""
    if not isinstance(nis, list):
        return
    itf_states: Dict[str, str] = {}
    if isinstance(itfs, list):
        for itf in itfs:
            if isinstance(itf, dict) and itf.get("name"):
                state = _leaf(itf.get("oper-state"))
                if state == "down" and is_intent(itf.get("oper-down-reason")):
                    state = STANDBY_STATE
                if state:
                    itf_states[str(itf["name"])] = state
    for ni in nis:
        if not isinstance(ni, dict):
            continue
        name = str(ni.get("name", ""))
        ni_type = _leaf(ni.get("type"))
        oper_state = _leaf(ni.get("oper-state")) or "unknown"

        neighbors = _branch(ni, "protocols", "bgp").get("neighbor", [])
        if isinstance(neighbors, list):
            for neighbor in neighbors:
                if not isinstance(neighbor, dict):
                    continue
                health.bgp_total += 1
                if _leaf(neighbor.get("session-state")) == "established":
                    health.bgp_established += 1
                else:
                    health.bgp_down += 1

        state = _effective_state(ni, oper_state, itf_states)
        targets = _route_targets(ni)
        # A service spans nodes, and its route-target is what identifies it
        # across them; the local name is only a fallback for an unnamed one.
        if ni_type == "mac-vrf":
            health.bridge_domains.append(
                (targets[0] if targets else f"mac-vrf:{name}", state)
            )
        elif ni_type in ("ip-vrf", "vrf") and name != "mgmt":
            health.routers.append(
                (targets[0] if targets else f"ip-vrf:{name}", state)
            )


def _roll_up(instances: List[Tuple[str, str]]) -> Dict[str, int]:
    """Group per-node service instances into fabric-wide service health."""
    by_name: Dict[str, List[str]] = {}
    for name, state in instances:
        by_name.setdefault(name, []).append(state)
    up = degraded = down = 0
    for states in by_name.values():
        up_count = sum(1 for s in states if s in _MEMBER_UP_STATES)
        if up_count == len(states):
            up += 1
        elif up_count == 0:
            down += 1
        else:
            degraded += 1
    return {
        "total": len(by_name),
        "up": up,
        "degraded": degraded,
        "down": down,
        "instances": len(instances),
    }


#: Kept as a module-level name because the tables both surfaces render have to
#: agree cell for cell; see :func:`nornir_srl.rows.cell`.
_cell = cell


def _oldest_update(streams: List[HostStream]) -> Optional[float]:
    stamps = [s.last_update for s in streams if s.last_update]
    return min(stamps) if stamps else None
