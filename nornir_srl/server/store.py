"""Fabric-wide state: gNMI connections, subscriptions and rendered tables."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from nornir.core import Nornir

from ..checks import CHECKS_COLUMNS, CHECKS_REPORT, FabricState, Finding, REQUIRED_REPORTS, check_results, run_checks
from .. import configs
from ..acks import AckStore, finding_key, mark as mark_acknowledged
from ..changes import INFO, WATCHED_ROUTE_REPORTS, Change
from ..incidents import Incident, correlate, locate, unreachable_nodes
from ..connections.down_reason import STANDBY_STATE, is_intent
from ..connections.srlinux import CONNECTION_NAME
from ..fabric import containerlab_nodes
from ..history import DEFAULT_RETENTION_DAYS, HistoryStore
from ..connections.layer2 import stamp_underlay_sites
from ..lenses import LensSpec
from ..records import as_dict
from ..reports import ReportSpec, SubscriptionSpec, fabric_args, get_report, reading_reports, subscription_mode
from ..rows import cell, clean_columns, flatten, merge_fields, sub_item_keys
from .devices import CachedDevice, DirectDevice, RecordingDevice
from .readings import NOT_RECORDED, Recorder, TapDevice, TapDirectDevice
from .stream import MAX_EVICTION_BACKLOG, DiscoveryRead, HostStream
from .timeline import Reading, Timeline, Watcher
from .topology import annotate_aliasing, annotate_health, build_topology, node_facts
from .tree import key_matches
from .table import RowPart, Table
from .versions import ReadDependencies

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class ReportLoad:
    """One browser stream's interest in unfinished node reads."""

    closed: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Activation:
    future: Future
    cancelled: threading.Event
    readers: Set[Optional[ReportLoad]]


@dataclass(eq=False)
class _HostRender:
    """One node's share of a report table, kept while what it read is unchanged.

    The rows are kept cleaned, per set of table columns, as the part a table
    is assembled from; the raw ones are dropped once cleaned. A table whose
    columns differ - another inventory filter - renders the node anew.
    """

    stream: Optional[HostStream]
    reads: Optional[ReadDependencies]
    columns: List[str]
    containers: Set[str]
    error: Optional[str]
    rows: Optional[List[Dict[str, Any]]]
    cleaned: Dict[Tuple[str, ...], RowPart] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def current(self, stream: Optional[HostStream]) -> bool:
        return (
            stream is not None
            and self.stream is stream
            and self.error is None
            and self.reads is not None
            and bool(self.reads.versions)
            and stream.reads_current(self.reads)
        )


#: Reports whose rows get the underlay site stamped on them across nodes:
#: a node's rows there depend on the other nodes', so they are not kept.
_SITE_STAMPED = ("bridge_domains", "routers", "services")

#: How many nodes' renders are kept, across reports and their parameters.
_MAX_HOST_RENDERS = 4096


@dataclass
class _CachedTable:
    at: float
    table: Dict[str, Any]
    reads: Dict[str, Tuple[HostStream, ReadDependencies]] = field(default_factory=dict)
    expires: float = float("inf")


def full_table_read(report: ReportSpec, params: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether *report*'s first read downloads every route of a node's BGP RIB.

    Those reads take minutes on a large fabric: they arrive node by node in
    a browser, and run on activation workers of their own.
    """
    return report.name.startswith("bgp_rib") and (params or {}).get("scope") == "all"


def _baseline_name(name: Optional[str]) -> str:
    """A baseline's name as it is kept: letters, digits, ``.``, ``_`` and ``-``."""
    import re  # noqa: PLC0415

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")[:60]
    if cleaned.startswith("__"):
        raise ValueError(f"'{name}' is a name fcli keeps for itself")
    return cleaned or "baseline"


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
        history: Optional[HistoryStore] = None,
        retention_days: float = DEFAULT_RETENTION_DAYS,
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
        #: (node, report) pairs whose activation has run to the end - the
        #: paths discovered and read once, or the attempt failed. Knowing the
        #: paths is not enough: the first Gets behind them are the slow part.
        self._activated: Set[Tuple[str, str]] = set()
        #: Why a node could not serve a report, and when that was decided.
        self._activation_errors: Dict[Tuple[str, str], Tuple[float, str]] = {}
        # Full-table first reads decode and build trees in Python. Bound their
        # concurrency so the first few nodes finish sooner, and keep their
        # workers separate from those rendering completed nodes.
        self._activation_pool = ThreadPoolExecutor(
            max_workers=max(1, min(workers, 4)), thread_name_prefix="fcli-activate"
        )
        # Every other first read: a second-long report must not queue for
        # minutes behind a RIB download on the bounded workers above.
        self._light_activation_pool = ThreadPoolExecutor(
            max_workers=max(workers, 1), thread_name_prefix="fcli-activate-light"
        )
        self._activating: Dict[Tuple[str, str, HostStream], _Activation] = {}
        #: The tables being rendered right now, by cache key: a client asking
        #: for one already under way waits for it rather than rendering it too.
        self._rendering: Dict[Any, "Future[Dict[str, Any]]"] = {}
        #: Each table keeps the stream identities, path revisions and Get
        #: expirations it was read from, scoped to its selected nodes.
        self._table_cache: Dict[Any, _CachedTable] = {}
        #: Each node's last render of a report, by (report, params, node):
        #: what a table re-render reuses for the nodes that did not change.
        self._host_renders: "OrderedDict[Any, _HostRender]" = OrderedDict()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._stopped = False
        self._resync_thread: Optional[threading.Thread] = None
        #: What the fabric was, as the watcher read it every watch_interval.
        self.timeline = Timeline(history=history, retention_days=retention_days)
        #: Where the timeline, the baselines and the configurations are kept.
        self.history = history
        #: The findings someone acknowledged, shared by everyone on this server.
        self.acks = AckStore(ack_file)
        #: Where the cables LLDP showed are kept across restarts, if anywhere.
        self.cabling_file = cabling_file
        if cabling_file is not None:
            self.timeline.load_cabling(cabling_file)
        self.watch_interval = watch_interval
        self.watcher = Watcher(self, self.timeline, interval=watch_interval)
        #: Work a page started without waiting for it to finish - a report's
        #: first activation, a health reading - by what it is, so that a page
        #: asking again joins it rather than starting it over.
        self._background: Dict[Any, "Future[Any]"] = {}
        self._background_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fcli-bg")
        #: The keys of a report's rows on a node, when they were read; see key_values().
        self._keys_cache: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}
        self._keys_fetching: Dict[Tuple[str, str], "Future[List[Dict[str, Any]]]"] = {}
        #: Set once the dashboards' reports have been read; see start().
        self.warmed = threading.Event()
        #: inv_filter key -> (when, reading), for health asked with no fresh
        #: watcher reading to answer from.
        self._health_cache: Dict[Any, Tuple[float, Reading]] = {}
        #: Per inventory filter: the watcher's reading and that reading narrowed.
        self._narrowed_cache: Dict[Any, Tuple[Reading, Reading]] = {}
        #: The (node, service) tables last fetched for the topology's
        #: aliasing: (when, which, report -> node -> tables).
        self._alias_ribs: Optional[Tuple[float, Tuple[Tuple[str, str], ...], Dict[str, Dict[str, List[Any]]]]] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

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
        threading.Thread(target=self._warm_up, name="fcli-warm", daemon=True).start()
        self.watcher.start()

    #: The reports read first at start-up: what the dashboards draw. A node
    #: answers one Get at a time, and the checks read tables far larger than
    #: these, so behind the checks the dashboards stay empty for a minute.
    WARM_UP_REPORTS: Tuple[str, ...] = ("overview", "topology")

    def _warm_up(self) -> None:
        """Read the dashboards' reports, in order, then let the watcher start."""
        try:
            names = self._targets(None)
            for report in self.WARM_UP_REPORTS:
                if self._stop.is_set():
                    return
                started = time.perf_counter()
                self._activate_within(report, names, wait=None)
                logger.debug("warm-up: '%s' read in %.2fs", report, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - the pages still activate on demand
            logger.warning("warming up the dashboards failed: %s", exc)
        finally:
            self.warmed.set()

    def _in_background(self, key: Any, work: Any) -> "Future[Any]":
        """Run *work* once in the background: a run already under way for *key* is joined."""
        with self._lock:
            running = self._background.get(key)
            if running is not None and not running.done():
                return running
            future = self._background_pool.submit(work)
            self._background[key] = future
            for done in [k for k, f in self._background.items() if f.done()]:
                del self._background[done]
            return future

    def _activate_within(self, report_name: str, names: List[str], wait: Optional[float]) -> bool:
        """Activate *report_name* on *names*, waiting at most *wait* seconds.

        Returns whether it is done. A report already read only has its paths
        re-asserted, which is quick; the first activation - the Gets of every
        path on every node - goes on in the background past *wait*, and a page
        asking meanwhile renders what has come in so far.
        """
        report = get_report(report_name)
        with self._lock:
            done = all((n, report_name) in self._activated for n in names if n in self._streams)
        if done:
            self.activate(report, names)
            return True
        future = self._in_background(
            ("activate", report_name, tuple(sorted(names))), lambda: self.activate(report, names)
        )
        try:
            future.result(timeout=wait)
        except TimeoutError:
            return False
        except Exception as exc:  # noqa: BLE001 - what streams still renders
            logger.warning("activating the %s report failed: %s", report_name, exc)
        return True

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
                )
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
            self._activated = {k for k in self._activated if k[0] != name}
            self._table_cache.clear()
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
        self._background_pool.shutdown(wait=False, cancel_futures=True)
        self._activation_pool.shutdown(wait=False, cancel_futures=True)
        self._light_activation_pool.shutdown(wait=False, cancel_futures=True)
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

    def _targets(
        self, inv_filter: Optional[Dict[str, str]], hosts: Optional[Sequence[str]] = None
    ) -> List[str]:
        """The inventory the (filtered) request is about, cut down to *hosts* if named.

        *hosts* is how a request about one node - the routes one peer sent it -
        asks only that node, rather than every node for a table then filtered
        to it in the browser.
        """
        target = self.nornir.filter(**inv_filter) if inv_filter else self.nornir
        names = list(target.inventory.hosts)
        if hosts:
            wanted = set(hosts)
            names = [n for n in names if n in wanted]
        return names

    def _streams_for(self, names: List[str]) -> List[HostStream]:
        with self._lock:
            return [self._streams[n] for n in names if n in self._streams]

    # ------------------------------------------------------------------ #
    # report activation
    # ------------------------------------------------------------------ #

    def activate(
        self,
        report: ReportSpec,
        hosts: Optional[List[str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        *,
        load: Optional[ReportLoad] = None,
    ) -> None:
        """Make sure every node streams the paths *report* needs.

        This runs on every render, not just the first one: re-asserting the paths
        is what marks them as still in use, so a report someone is watching is
        never retired from under it.

        Of *params*, those the report reads other paths with - its
        :attr:`~ReportSpec.path_params` - are a report of their own here: the
        paths are discovered, streamed and counted for each value of them apart.
        """
        for future in self._start_activation(report, hosts, params, load):
            future.result()

    def cancel_load(self, load: ReportLoad) -> None:
        """Release this viewer; cancel work that no other reader still needs.

        An in-flight gNMI Get is allowed to return, then discovery stops before
        its next read. None denotes a one-shot or background caller, whose
        work must continue independently of any browser disconnecting.
        """
        with self._lock:
            load.closed.set()
            for job in list(self._activating.values()):
                job.readers.discard(load)
                if not job.readers:
                    job.cancelled.set()
                    job.future.cancel()

    def _start_activation(
        self, report: ReportSpec, hosts: Optional[Sequence[str]], params: Optional[Mapping[str, Any]],
        load: Optional[ReportLoad] = None,
    ) -> List[Future]:
        """Start each node's first read once, shared by concurrent clients."""
        futures = []
        with self._lock:
            names = list(hosts) if hosts is not None else list(self._streams)
        activation = self.activation_name(report, params)
        pool = self._activation_pool if full_table_read(report, params) else self._light_activation_pool
        for name in names:
            with self._lock:
                if self._stop.is_set() or (load is not None and load.closed.is_set()):
                    break
                stream = self._streams.get(name)
                if stream is None:
                    continue
                report_key = (name, activation)
                specs = self._specs.get(report_key)
                ready = report_key in self._activated and report_key not in self._activation_errors
            # Touch the node outside the store lock. Warm reports need no
            # worker, even when every activation worker has a slow first read.
            if ready and specs is not None and stream.touch_paths(specs):
                with self._lock:
                    if self._streams.get(name) is stream:
                        continue
            with self._lock:
                if self._stop.is_set() or (load is not None and load.closed.is_set()):
                    break
                stream = self._streams.get(name)
                if stream is None:
                    continue
                key = (name, activation, stream)
                job = self._activating.get(key)
                if job is None or job.cancelled.is_set():
                    cancelled = threading.Event()
                    pending = pool.submit(self._activate_host, report, name, params, cancelled)
                    job = self._activating[key] = _Activation(pending, cancelled, {load})

                    def finished(_future: Future, key: Any = key, job: _Activation = job) -> None:
                        with self._lock:
                            if self._activating.get(key) is job:
                                self._activating.pop(key, None)

                    pending.add_done_callback(finished)
                else:
                    job.readers.add(load)
                futures.append(job.future)
        return futures

    @staticmethod
    def activation_name(report: ReportSpec, params: Optional[Mapping[str, Any]] = None) -> str:
        """What *report*, run with *params*, is activated and counted under."""
        shaping = sorted((k, str(v)) for k, v in (params or {}).items() if k in report.path_params and v)
        return report.name + ("?" + "&".join(f"{k}={v}" for k, v in shaping) if shaping else "")

    def _activate_host(
        self, report: ReportSpec, name: str, params: Optional[Mapping[str, Any]] = None,
        cancelled: Optional[threading.Event] = None,
    ) -> None:
        key = (name, self.activation_name(report, params))
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
            if cancelled is not None and cancelled.is_set():
                return
            discovered: Dict[Tuple[str, str], DiscoveryRead] = {}
            if specs is None:
                started = time.perf_counter()
                specs = self._discover(
                    report, stream, {k: v for k, v in (params or {}).items() if k in report.path_params}, discovered, cancelled
                )
                logger.debug(
                    "%s: report '%s' needs %d path(s), discovered in %.3fs: %s",
                    name,
                    report.name,
                    len(specs),
                    time.perf_counter() - started,
                    ", ".join(s.path for s in specs) or "none",
                )
                with self._lock:
                    if self._streams.get(name) is not stream:
                        return
                    self._specs[key] = specs
            if cancelled is not None and cancelled.is_set():
                return
            stream.ensure_paths(specs, discovered)
            with self._lock:
                if self._streams.get(name) is stream:
                    self._activated.add(key)
        except CancelledError:
            # Stopping a view is neither a node failure nor a completed read.
            return
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning(
                "%s: activating report '%s' failed: %s", name, report.name, exc
            )
            logger.debug(
                "%s: activating report '%s' failed", name, report.name, exc_info=exc
            )
            with self._lock:
                if self._streams.get(name) is stream:
                    self._activation_errors[key] = (time.time(), str(exc))
                    self._activated.add(key)

    #: How long the keys a node's routes are looked up by are kept: they are a
    #: Get of their own, and offering them is about what exists, not what changed.
    KEYS_TTL = 300.0

    def key_values(
        self,
        report: ReportSpec,
        param: str,
        inv_filter: Optional[Dict[str, str]] = None,
        chosen: Optional[Mapping[str, str]] = None,
        typed: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        """The values the key *param* of *report*'s rows takes, to pick one from.

        Across the (filtered) nodes, among the rows the other keys in *chosen*
        already match, and that hold *typed* - or match it, when it is a
        pattern. The most common first, at most *limit* of them; ``total``
        says how many there are. A node's keys are read once per
        :attr:`KEYS_TTL`, and by one request at a time.
        """
        if report.keys is None:
            raise KeyError(f"report '{report.name}' has no keys to offer")
        key = param.replace("_", "-")
        names = [n for n in self._targets(inv_filter) if n in self._streams]
        others = {k.replace("_", "-"): str(v) for k, v in (chosen or {}).items() if v and k != param}
        rows = [row for rows in self._pool.map(lambda n: self._node_keys(report, n), names) for row in rows]
        pattern = typed.strip()
        counts: Dict[str, int] = {}
        for row in rows:
            if key not in row or not all(key_matches(v, str(row.get(k, ""))) for k, v in others.items()):
                continue
            value = str(row[key])
            if pattern and pattern.lower() not in value.lower() and not key_matches(pattern, value):
                continue
            counts[value] = counts.get(value, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            "param": param,
            "values": [{"value": v, "routes": n} for v, n in ranked[:limit]],
            "total": len(counts),
        }

    def _node_keys(self, report: ReportSpec, name: str) -> List[Dict[str, Any]]:
        """The keys of *report*'s rows on node *name*, as recently as :attr:`KEYS_TTL`."""
        cache_key = (name, report.name)
        with self._lock:
            cached = self._keys_cache.get(cache_key)
            if cached is not None and time.time() - cached[0] < self.KEYS_TTL:
                return cached[1]
            running = self._keys_fetching.get(cache_key)
            leader = running is None
            if leader:
                running = self._keys_fetching[cache_key] = Future()
            stream = self._streams.get(name)
        if not leader:
            return running.result()
        rows: List[Dict[str, Any]] = []
        try:
            if stream is not None and report.keys is not None:
                rows = report.keys(DirectDevice(stream))
        except Exception as exc:  # noqa: BLE001 - the other nodes still offer theirs
            logger.debug("%s: reading the keys of '%s' failed: %s", name, report.name, exc)
        with self._lock:
            self._keys_cache[cache_key] = (time.time(), rows)
            del self._keys_fetching[cache_key]
        running.set_result(rows)
        return rows

    def progress(
        self,
        reports: Sequence[str],
        inv_filter: Optional[Dict[str, str]] = None,
        hosts: Optional[Sequence[str]] = None,
    ) -> Dict[str, int]:
        """How far the nodes are in answering *reports* for the first time.

        ``ready`` of ``total`` (node, report) pairs have been activated: their
        paths found and read once, or given up on. What a stream reports while
        the first render of a slow report - a BGP RIB - is still running.
        """
        names = self._targets(inv_filter, hosts)
        with self._lock:
            nodes = [n for n in names if n in self._streams]
            done = sum(1 for n in nodes for r in reports if (n, r) in self._activated)
        return {"ready": done, "total": len(nodes) * len(reports), "nodes": len(nodes)}

    def _discover(
        self, report: ReportSpec, stream: HostStream, params: Optional[Mapping[str, Any]] = None,
        discovered: Optional[Dict[Tuple[str, str], DiscoveryRead]] = None,
        cancelled: Optional[threading.Event] = None,
    ) -> List[SubscriptionSpec]:
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
        def read(path: str, datatype: str) -> List[Dict[str, Any]]:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            response = stream.discovery_get(path, datatype, discovered)
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            return response

        def lookup(paths: List[str], datatype: str, table: Optional[str]) -> List[Dict[str, Any]]:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            response = stream.lookup(paths, datatype, table)
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            return response

        recorder = RecordingDevice(stream.device, read, lookup)
        report.getter(recorder, **(params or {}), **fabric_args(report, self.nornir.inventory.hosts))
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
        hosts: Optional[Sequence[str]] = None,
        *,
        progressive: bool = False,
        load: Optional[ReportLoad] = None,
    ) -> Dict[str, Any]:
        """Render *report* across the (filtered) inventory from streamed state.

        *params* are the report's own arguments, as declared by
        :attr:`ReportSpec.params`. They only ever narrow what the getter makes
        of the state already streamed, so they cost no gNMI and never change
        which paths a node subscribes to.

        With *progressive*, return the nodes whose first reads have finished,
        with ``loading`` counts until the remaining nodes finish or fail.
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
        names = self._targets(inv_filter, hosts)
        loading = None
        if progressive:
            activation = self.activation_name(report, params)
            with self._lock:
                ready = [n for n in names if n not in self._streams or (n, activation) in self._activated]
            pending = [n for n in names if n not in ready]
            # Inspect only completed nodes: bootstrap holds a node's tree
            # lock, and a large healthy Get can exceed the reconnect grace.
            self._heal_connections(ready)
            if pending:
                self._start_activation(report, pending, params, load)
                loading = {"ready": len(ready), "total": len(names), "nodes": len(names)}
                names = ready
        else:
            self._heal_connections(names)
        if loading is None:
            self.activate(report, names, params, load=load)

        inv_key = tuple(sorted(inv_filter.items())) if inv_filter else None
        param_key = tuple(sorted((params or {}).items())) or None
        host_key = tuple(sorted(hosts)) if hosts else None
        cache_key = (report.name, inv_key, param_key, host_key)
        if loading is not None:
            cache_key += ("loading", tuple(names))
        while True:
            now = time.time()
            with self._lock:
                cached = self._table_cache.get(cache_key)
                streams = {n: self._streams.get(n) for n in names}
            # Never hold the store lock while checking a node's tree lock.
            if cached is not None and not cached.table.get("errors"):
                fresh = now < cached.expires and all(
                    streams.get(name) is stream and stream.reads_current(reads)
                    for name, (stream, reads) in cached.reads.items()
                )
                if fresh or now - cached.at < 0.5:
                    logger.debug("report '%s': serving the table cached %.2fs ago", report.name, now - cached.at)
                    return cached.table
            with self._lock:
                # A render may have finished while we checked the streams.
                if self._table_cache.get(cache_key) is not cached:
                    continue
                pending = self._rendering.get(cache_key)
                leader = pending is None
                if leader:
                    pending = self._rendering[cache_key] = Future()
                break
        if not leader:
            logger.debug("report '%s': waiting for the render already under way", report.name)
            return pending.result()
        try:
            res_table = self._render_table(report, inv_filter, params, names, cache_key, loading)
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        else:
            pending.set_result(res_table)
            return res_table
        finally:
            with self._lock:
                if self._rendering.get(cache_key) is pending:
                    del self._rendering[cache_key]

    def _render_table(
        self,
        report: ReportSpec,
        inv_filter: Optional[Dict[str, str]],
        params: Optional[Dict[str, Any]],
        names: List[str],
        cache_key: Any,
        loading: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Render *report* over *names* and cache it under *cache_key*."""
        started = time.time()
        errors: List[Dict[str, str]] = []
        extra: Dict[str, Any] = {"loading": loading} if loading is not None else {}
        dependencies: Dict[str, Tuple[HostStream, ReadDependencies]] = {}
        expires = float("inf")
        #: The rows by node, for the table to be encoded and patched by.
        row_parts: Optional[List[RowPart]] = None
        if report.name == CHECKS_REPORT:
            # Findings also depend on time windows and acknowledgements.
            expires = started + 0.5
            # Findings are about the fabric rather than about one node, so they
            # are gathered across it rather than merged per host. A node the
            # checks could not read becomes a finding, not a table-level error.
            all_columns = list(CHECKS_COLUMNS)
            state = self.fabric_state(inv_filter)
            findings = run_checks(state)
            clean_rows = [f.as_row() for f in findings]
            # Each check, passing or not, for the page to draw one card each.
            extra["checks"] = check_results(state, findings)
        else:
            param_key = tuple(sorted((params or {}).items())) or None
            reusable = report.name not in _SITE_STAMPED

            def render_host(name: str, reuse: bool = True) -> Tuple[str, _HostRender]:
                # A node whose paths did not change since its last render keeps
                # its rows: re-rendering a fabric after one node changed then
                # runs one node's getter, not every node's.
                with self._lock:
                    stream = self._streams.get(name)
                    kept = self._host_renders.get((report.name, param_key, name)) if reusable and reuse else None
                if kept is not None and kept.current(stream):
                    return name, kept
                if stream is None:
                    _, cols, rows, error, found = self._host_rows(report, name, params)
                    return name, _HostRender(None, None, cols, found, error, rows)
                with stream.track_reads() as reads:
                    _, cols, rows, error, found = self._host_rows(report, name, params)
                return name, _HostRender(stream, reads, cols, found, error, rows)

            renders = list(self._pool.map(render_host, names))
            for _attempt in range(3):
                columns: List[str] = []
                # A node with no routes at all cannot tell that 'Rib' groups rows
                # rather than holding a value, so the fabric decides it together.
                containers: Set[str] = set()
                for _name, render in renders:
                    if render.error is None:
                        merge_fields(columns, render.columns)
                        containers |= render.containers
                columns = [c for c in columns if c not in containers]
                key = tuple(columns)
                # A kept node cleaned for other columns no longer has its raw
                # rows: render it again for these.
                stale = [
                    i for i, (_name, render) in enumerate(renders)
                    if render.error is None and render.rows is None and key not in render.cleaned
                ]
                if not stale:
                    break
                for i, item in zip(stale, self._pool.map(lambda i: render_host(renders[i][0], reuse=False), stale)):
                    renders[i] = item
            all_columns = ["Node"] + clean_columns(columns)
            raw_columns = ["Node"] + columns

            def part_of(name: str, render: _HostRender) -> RowPart:
                with render.lock:
                    part = render.cleaned.get(key)
                    if part is None and render.rows is not None:
                        part = RowPart(name, [
                            {c: _cell(row.get(raw)) for c, raw in zip(all_columns, raw_columns)}
                            for row in render.rows
                        ])
                        render.cleaned[key] = part
                        # The raw rows are only kept until cleaned; two column
                        # sets at most, for a view and a filtered view of it.
                        render.rows = None
                        while len(render.cleaned) > 2:
                            render.cleaned.pop(next(iter(render.cleaned)))
                if part is None:
                    # Cleaned for other columns by a render that ran alongside.
                    _, fresh = render_host(name, reuse=False)
                    return part_of(name, fresh)
                return part

            parts: List[RowPart] = []
            for name, render in renders:
                if render.reads is not None:
                    dependencies[name] = (render.stream, render.reads)
                    if not render.reads.versions:
                        expires = started + 0.5
                if render.error:
                    errors.append({"node": name, "error": render.error})
                    continue
                parts.append(part_of(name, render))
            clean_rows = [row for part in parts for row in part.rows]
            row_parts = parts
            if not reusable:
                # The site stamped on a row depends on every node's.
                row_parts = None
                if stamp_underlay_sites(clean_rows):
                    if "Site" not in all_columns:
                        all_columns.append("Site")
            with self._lock:
                for name, render in renders:
                    if reusable and render.error is None and render.stream is not None and render.reads and render.reads.versions:
                        cache_key_host = (report.name, param_key, name)
                        self._host_renders[cache_key_host] = render
                        self._host_renders.move_to_end(cache_key_host)
                while len(self._host_renders) > _MAX_HOST_RENDERS:
                    self._host_renders.popitem(last=False)
        res_table = Table({
            "report": report.name,
            "title": report.title,
            "columns": all_columns,
            "rows": clean_rows,
            "errors": errors,
            "nodes": loading["total"] if loading is not None else len(names),
            "generated": started,
            "render_ms": round((time.time() - started) * 1000, 1),
            "oldest_update": _oldest_update(self._streams_for(names)),
            **extra,
        }, parts=row_parts)
        with self._lock:
            # Keep only the latest partial table for this query; accumulating
            # every growing copy would multiply a large fabric RIB in memory.
            for key in list(self._table_cache):
                if key != cache_key and key[:4] == cache_key[:4] and key[4:5] == ("loading",):
                    del self._table_cache[key]
            # The cache is keyed by the inventory filter and the report's own
            # parameters as well as its name, and an API client picks both, so
            # the key space has no natural bound.
            if (
                cache_key not in self._table_cache
                and len(self._table_cache) >= _MAX_CACHED_TABLES
            ):
                oldest = min(self._table_cache, key=lambda k: self._table_cache[k].at)
                del self._table_cache[oldest]
            self._table_cache[cache_key] = _CachedTable(started, res_table, dependencies, expires)
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
        recorder: Optional[Recorder] = None,
    ) -> Tuple[str, Optional[List[Any]], Optional[str]]:
        """What *report*'s getter makes of one node, before it becomes a table.

        The items are ``None`` rather than empty when there was nothing to ask:
        a node that is going away as the store shuts down has no state, which
        is not the same as having none.

        With a *recorder*, every Get the getter makes is written down, which
        is how a reading is kept to be read back later.
        """
        with self._lock:
            stream = self._streams.get(name)
            if stream is None:
                return name, None, self._connect_errors.get(name, "not connected")
            activation_error = self._activation_errors.get((name, self.activation_name(report, params)))
        if activation_error:
            return name, None, activation_error[1]
        if self._stop.is_set():
            return name, None, None
        try:
            device = (
                CachedDevice(stream)
                if recorder is None or report.name in NOT_RECORDED
                else TapDevice(stream, recorder.for_report(name, report.name, getattr(stream.device, "capabilities", None)))
            )
            result = report.getter(device, **(params or {}), **fabric_args(report, self.nornir.inventory.hosts))
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
        recorder: Optional[Recorder] = None,
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

        A *recorder* writes down every Get the reading made; see
        :mod:`nornir_srl.server.readings`.
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
                lambda n, s=spec: self._host_payload(s, n, recorder=recorder), names
            )
            for node, items, error in collected:
                if error is not None:
                    state.errors[(key, node)] = error
                elif items is not None:
                    payloads[node] = items
            state.reports[key] = payloads
        if watched:
            self._watched_routes(state, names, watched, recorder)
        if history:
            state.changes = self.timeline.recent(nodes=names)
            state.history = self.timeline.scoped(names)
        state.acknowledged = self.acks.keys()
        return state

    def _watched_routes(
        self, state: FabricState, names: List[str], prefixes: Sequence[str], recorder: Optional[Recorder] = None
    ) -> None:
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
                device: Any = DirectDevice(stream)
                if recorder is not None:
                    device = TapDirectDevice(stream, recorder.for_report(name, report))
                    recorder.bound(name, report, {"afi": afi, "prefixes": list(wanted)})
                try:
                    return name, device.get_routes(afi, wanted)["ip_rib"], None
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

    #: How long the dashboards wait for what is still being read before they
    #: answer with what there is.
    PAGE_WAIT = 2.0

    def health(self, inv_filter: Optional[Dict[str, str]] = None) -> Reading:
        """The findings and incidents of the (filtered) fabric, as recently as they are known.

        With the watcher running, its latest reading answers, however old: the
        same reading the overview shows, with no gNMI of its own. An inventory
        filter narrows that reading to its nodes and runs the checks over what
        is left. Only without a watcher, or when its first reading failed, are
        the reports read here, and the answer kept a few seconds so that a
        page polling for it does not run every check on every poll.
        """
        latest = self._watched_reading()
        if latest is not None:
            return self._narrowed(latest, inv_filter) if inv_filter else latest
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

    def _watched_reading(self) -> Optional[Reading]:
        """The watcher's latest reading, waiting for its first one if it is under way.

        ``None`` without a watcher, or once its first reading failed: a second
        reading started next to the watcher's would only compete with it for
        the same nodes.
        """
        if self.watch_interval <= 0:
            return None
        while self.timeline.latest is None:
            if self._stop.is_set() or not self.watcher.running or self.watcher.attempts:
                break
            time.sleep(0.1)
        return self.timeline.latest

    def _narrowed(self, reading: Reading, inv_filter: Dict[str, str]) -> Reading:
        """*reading* as the filtered nodes alone see it: their payloads, rechecked.

        Checks compare nodes with each other, so the findings are made again
        over what is left rather than picked out of the whole fabric's. Kept
        per filter until the watcher's next reading.
        """
        key = tuple(sorted(inv_filter.items()))
        with self._lock:
            cached = self._narrowed_cache.get(key)
        if cached is not None and cached[0] is reading:
            return cached[1]
        names = self._targets(inv_filter)
        wanted = set(names)
        state = replace(
            reading.state,
            reports={
                report: {node: payload for node, payload in payloads.items() if node in wanted}
                for report, payloads in reading.state.reports.items()
            },
            hostnames={node: host for node, host in reading.state.hostnames.items() if node in wanted},
            errors={key: error for key, error in reading.state.errors.items() if key[1] in wanted},
            containerlab=reading.state.containerlab & wanted,
            changes=self.timeline.recent(nodes=names),
            history=self.timeline.scoped(names),
            acknowledged=self.acks.keys(),
        )
        findings = run_checks(state)
        narrowed = replace(
            reading,
            state=state,
            findings=findings,
            incidents=correlate(findings, state),
            connected={node: up for node, up in reading.connected.items() if node in wanted},
        )
        with self._lock:
            if len(self._narrowed_cache) >= 16:
                self._narrowed_cache.clear()
            self._narrowed_cache[key] = (reading, narrowed)
        return narrowed

    def health_within(self, inv_filter: Optional[Dict[str, str]], wait: float) -> Optional[Reading]:
        """:meth:`health`, waiting at most *wait* seconds for it.

        What a dashboard draws while the checks are still reading the fabric:
        the latest reading there is, however old, or ``None`` before the first
        one. A fresher one is read in the background meanwhile - by the
        watcher, when it is the one keeping them.
        """
        deadline = time.time() + max(wait, 0.0)
        if self.watch_interval > 0:
            while self.timeline.latest is None and time.time() < deadline and not self._stop.is_set():
                time.sleep(0.05)
            latest = self.timeline.latest
            if latest is not None and inv_filter:
                return self._narrowed(latest, inv_filter)
            if latest is not None or not inv_filter:
                return latest
        key = tuple(sorted(inv_filter.items())) if inv_filter else None
        with self._lock:
            cached = self._health_cache.get(key)
        if cached is not None and time.time() - cached[0] < self.HEALTH_TTL:
            return cached[1]
        future = self._in_background(("health", key), lambda: self.health(inv_filter))
        try:
            return future.result(timeout=max(deadline - time.time(), 0.0))
        except TimeoutError:
            return cached[1] if cached is not None else None

    def health_progress(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, int]:
        """How far the reports the checks read are in answering for the first time."""
        return self.progress(reading_reports(REQUIRED_REPORTS), inv_filter)

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

    def set_baseline(self, name: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        """Keep the fabric as it is now as what it is compared against.

        With a history, the baseline is read afresh with its gNMI data written
        down, kept under *name* (``baseline`` if none is given) and compared
        against after a restart, until another is set or this one deleted.
        """
        if self.history is None:
            reading = self.timeline.latest
            if reading is None or self.watch_interval <= 0:
                reading = self.health()
            self.timeline.set_baseline(reading)
            return self.timeline.status()
        name = _baseline_name(name)
        recorder = Recorder()
        reading = self.watcher.capture(recorder)
        self.watcher._keep(name, reading, recorder, note=note)
        self.history.set_meta("baseline", name)
        self.timeline.set_baseline(reading, name)
        return self.timeline.status()

    def baselines(self) -> Dict[str, Any]:
        """The baselines kept, and which one the fabric is compared against."""
        kept = self.history.readings() if self.history is not None else []
        return {
            "active": self.timeline.baseline_name,
            "baseline_at": self.timeline.baseline.at if self.timeline.baseline else None,
            "persistent": self.history is not None,
            "baselines": [r.as_dict() for r in kept],
        }

    def use_baseline(self, name: Optional[str]) -> Dict[str, Any]:
        """Compare against the baseline kept under *name*; ``None`` for the latest reading, not kept."""
        if name is None:
            if self.history is not None:
                self.history.set_meta("baseline", None)
            self.timeline.set_baseline(self.timeline.latest or self.health(), None)
            return self.baselines()
        if self.history is None:
            raise KeyError("baselines are not kept: the server runs without a history")
        reading = self.watcher.load(name)
        if reading is None:
            raise KeyError(f"no baseline '{name}'")
        self.history.set_meta("baseline", name)
        self.timeline.set_baseline(reading, name)
        return self.baselines()

    def delete_baseline(self, name: str) -> Dict[str, Any]:
        """Forget the baseline kept under *name*; the fabric is compared against the latest reading if it was in use."""
        if self.history is None or not self.history.delete_reading(name):
            raise KeyError(f"no baseline '{name}'")
        if self.timeline.baseline_name == name:
            self.timeline.set_baseline(self.timeline.latest or self.health(), None)
        return self.baselines()

    # ------------------------------------------------------------------ #
    # configurations
    # ------------------------------------------------------------------ #

    def running_config(self, name: str, salt: str = "") -> Dict[str, Any]:
        """*name*'s running configuration as it is now, normalized and redacted.

        A Get of its own rather than a cached answer: what is asked for is
        the configuration a commit just made.
        """
        with self._lock:
            stream = self._streams.get(name)
        if stream is None:
            raise KeyError(f"{name} is not connected")
        return configs.normalize(stream.fresh_get("/", "config"), salt=salt)

    def config_versions(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """The configurations kept, newest commit first, for one node or every one."""
        if self.history is None:
            return []
        return [v.as_dict() for v in self.history.config_versions(name)]

    def config_text(self, name: str, commit: Optional[int] = None) -> Dict[str, Any]:
        """One kept configuration, as ``set / ...`` lines."""
        if self.history is None:
            raise KeyError("configurations are not kept: the server runs without a history")
        kept = self.history.config(name, commit)
        if kept is None:
            raise KeyError(f"no configuration of {name}" + (f" after commit {commit}" if commit is not None else ""))
        version, tree = kept
        return {**version.as_dict(), "lines": configs.flatten(tree)}

    def config_diff(self, name: str, commit: Optional[int] = None, against: Optional[int] = None) -> Dict[str, Any]:
        """What *commit* (the newest kept, if ``None``) changed in *name*'s configuration.

        Compared with the configuration kept before it, or with the one kept
        after commit *against*.
        """
        if self.history is None:
            raise KeyError("configurations are not kept: the server runs without a history")
        after = self.history.config(name, commit)
        if after is None:
            raise KeyError(f"no configuration of {name}" + (f" after commit {commit}" if commit is not None else ""))
        if against is not None:
            before = self.history.config(name, against)
            if before is None:
                raise KeyError(f"no configuration of {name} after commit {against}")
        else:
            older = self.history.latest_config(name, before=after[0].commit_id)
            before = self.history.config(name, older.commit_id) if older is not None else None
        diff = configs.diff_trees(before[1] if before else None, after[1])
        return {
            "node": name,
            "commit": after[0].as_dict(),
            "against": before[0].as_dict() if before else None,
            **diff.as_dict(),
        }

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
        # A node no report answered for is one line, not one per report - and
        # none in the incidents, where its node_unreachable card says it.
        unreachable = unreachable_nodes(state)
        errors = [
            {"node": node, "error": f"{report} not collected: {error}"}
            for (report, node), error in sorted(state.errors.items())
            if node not in unreachable
        ]
        if lens.name != "incidents":
            reasons = {node: error for (_report, node), error in sorted(state.errors.items()) if node in unreachable}
            errors += [{"node": node, "error": f"no report collected: {reason}"} for node, reason in sorted(reasons.items())]
        names = self._targets(inv_filter)
        return Table({
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
        })

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
            # Past this many notifications waiting, a node's stream is behind.
            "backlog_limit": MAX_EVICTION_BACKLOG,
            "resync_interval": self.resync_interval,
        }

    def overview(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Aggregate fabric-wide health and topology metrics for the dashboard.

        Answers within :attr:`PAGE_WAIT` however far the fabric has been read:
        the counts are of what the nodes have streamed so far, ``loading`` says
        how many have yet to, and the health card is the latest reading there
        is - ``health_loading`` saying how far the next one is, while there is
        none. A page polling it fills in as the nodes answer.
        """
        deadline = time.time() + self.PAGE_WAIT
        names = self._targets(inv_filter)
        self._heal_connections(names)
        ready = self._activate_within("overview", names, wait=self.PAGE_WAIT)

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
            snapshot = stream.snapshot_paths(_report_paths("overview"))
            itfs = snapshot.get("interface")
            _tally_interfaces(health, itfs, count_errors=stream.name not in virtual)
            _tally_network_instances(health, snapshot.get("network-instance"), itfs)

        summary = self._health_summary(inv_filter, names, wait=deadline - time.time())
        return {
            "health": summary,
            "health_loading": None if summary is not None else self.health_progress(inv_filter),
            "loading": None if ready else self.progress(["overview"], inv_filter),
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
            "telemetry": _telemetry_summary(
                [(s.name, s.status()) for s in all_streams], self.resync_interval, cached_tables
            ),
        }

    def _health_summary(
        self, inv_filter: Optional[Dict[str, str]], names: List[str], wait: float
    ) -> Optional[Dict[str, Any]]:
        """The incidents and recent changes, counted, for the dashboard.

        ``None`` while there is no reading yet to count them from.
        """
        try:
            reading = self.health_within(inv_filter, wait)
        except Exception as exc:  # noqa: BLE001 - the rest of the dashboard still renders
            logger.warning("summarizing fabric health failed: %s", exc)
            return None
        if reading is None:
            return None
        recent = self.timeline.changes(since=time.time() - 900, nodes=names)
        baseline = self.timeline.baseline
        # What is acknowledged is known, and is not what this card is for.
        incidents = mark_acknowledged(reading.incidents, self.acks.keys())
        open_ = [i for i in incidents if not i.acknowledged]
        return {
            "at": reading.at,
            # By the server's clock: a browser's may be off by more than that.
            "age": max(0.0, time.time() - reading.at),
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
            "watch_interval": self.watch_interval,
            # The next reading under way, for the card to show it is checking.
            "reading_since": self.watcher.reading_since,
        }

    def topology(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """The fabric graph: LLDP adjacencies, with a tier per node.

        Nodes that are down or have not streamed anything yet are still part of
        the answer, as unclassified ones - a topology missing the node that
        failed would be the opposite of useful.

        Like :meth:`overview`, it answers within :attr:`PAGE_WAIT`: the nodes
        drawn as far as they have streamed (``loading``), coloured by the latest
        health reading there is (``health_loading`` while there is none).
        """
        started = time.time()
        deadline = started + self.PAGE_WAIT
        names = self._targets(inv_filter)
        self._heal_connections(names)
        ready = self._activate_within("topology", names, wait=self.PAGE_WAIT)

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
                    snapshot=stream.snapshot_paths(_report_paths("topology")) if stream else None,
                    connected=bool(status.get("connected")),
                    error=status.get("error"),
                    egress=_interface_egress(stream),
                    remembered=self.timeline.cables(name),
                )
            )

        graph = build_topology(facts)
        graph["loading"] = None if ready else self.progress(["topology"], inv_filter)
        try:
            health = self.health_within(inv_filter, deadline - time.time())
        except Exception as exc:  # noqa: BLE001 - the drawing is worth having without its colours
            logger.warning("reading the fabric health for the topology failed: %s", exc)
            health = None
        graph["health_loading"] = None if health is not None else self.health_progress(inv_filter)
        try:
            if health is None:
                raise _NoReading
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
            # Fetched with Gets of their own, so in the background like the
            # rest: the drawing goes without it until they are in.
            key = tuple(sorted(inv_filter.items())) if inv_filter else None
            ribs = self._in_background(("aliasing", key), lambda: self._service_ribs(graph, health.state))
            try:
                annotate_aliasing(graph, ribs.result(timeout=max(deadline - time.time(), 0.0)))
            except TimeoutError:
                pass
        except _NoReading:
            pass
        except Exception as exc:  # noqa: BLE001 - the drawing is worth having without its colours
            logger.warning("annotating the topology with health failed: %s", exc)
            logger.debug("annotating the topology failed", exc_info=exc)
        graph["generated"] = started
        graph["render_ms"] = round((time.time() - started) * 1000, 1)
        graph["oldest_update"] = _oldest_update(list(streams.values()))
        return graph


class _NoReading(Exception):
    """No health reading yet to colour the topology with."""


def _telemetry_summary(
    statuses: List[Tuple[str, Dict[str, Any]]], resync_interval: int, cached_tables: int
) -> Dict[str, Any]:
    """What the dashboard says of the streams: paths, how they are served, and
    whether every node is being kept up with.

    ``subscriptions`` is the gNMI paths the reports in use read, on every node
    - not Subscribe RPCs, of which a node has one. Of those, ``streaming`` are
    streamed, ``pending`` were empty and are served by short-lived Gets until
    they fill, and ``polled`` were left out of a subscription for room. Paths
    another one delivers count as streaming. ``backlog`` is the most
    notifications any one node has waiting to be applied, and which node.
    """
    paths = [p for _name, status in statuses for p in status.get("paths", [])]
    worst = max(statuses, key=lambda item: item[1].get("backlog", 0) or 0, default=None)
    return {
        "subscriptions": len(paths),
        "nodes": len(statuses),
        "streaming": sum(1 for p in paths if p.get("streaming")),
        "pending": sum(1 for p in paths if p.get("pending")),
        "polled": sum(1 for p in paths if p.get("polled")),
        "backlog": (worst[1].get("backlog", 0) or 0) if worst else 0,
        "backlog_node": worst[0] if worst and worst[1].get("backlog") else None,
        "backlog_limit": MAX_EVICTION_BACKLOG,
        "resync_interval": resync_interval,
        "cached_tables": cached_tables,
    }


def _report_paths(name: str) -> Tuple[str, ...]:
    """The paths a dashboard report subscribes to: all it summarizes.

    Its own paths, not the roots they are under: those also hold every table
    the other reports stream - routes, MACs, thousands of subinterfaces'
    counters - and rendering them on every poll is what kept the dashboards
    from answering on a large fabric.
    """
    return tuple(spec.path for spec in get_report(name).subscribe)


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
