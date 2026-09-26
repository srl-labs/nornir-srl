"""Per-host gNMI subscription session feeding an in-memory state tree.

Each :class:`HostStream` owns one gNMI ``Subscribe`` RPC per host. The paths it
subscribes to are the very same paths the report getters in
:mod:`nornir_srl.connections` ask for, so the streamed state can be handed back
to those getters unchanged (see :class:`~nornir_srl.server.devices.CachedDevice`).

A path is bootstrapped with a regular gNMI ``Get`` before the subscription is
started: that gives both a complete starting state and the exact response
envelope key SR Linux uses for the path, which is what a ``Get`` caller expects
to find in the returned structure.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..connections.helpers import strip_modules
# One suppressor for the whole process: it swaps pygnmi's handlers out and back
# under a refcount, and two copies with a count each would restore them while
# the other still meant them gone.
from ..connections.routing import _gnmi_path_missing, _suppress_pygnmi_client_logging
from ..reports import SubscriptionSpec
from .tree import (
    ListNode,
    key_matches,
    split_path,
    strip_module,
    delete,
    get_node,
    insert,
    join_path,
    materialize,
    parse_path,
    prune,
    select_path,
    sweep,
)

logger = logging.getLogger(__name__)



# Counters used to derive interface rates from consecutive streamed samples.
IFSTATS_COUNTERS: Tuple[str, ...] = (
    "in-octets",
    "out-octets",
    "in-packets",
    "out-packets",
    "in-error-packets",
    "out-error-packets",
    "in-discarded-packets",
    "out-discarded-packets",
)

_MIN_RATE_INTERVAL = 0.5  # seconds; below this the rate is too noisy to report

#: Tree roots that reports overlap on heavily enough to be worth reading from a
#: neighbouring subscription rather than spending a Get of their own on.
_SHARED_ROOTS: Tuple[str, ...] = ("interface", "network-instance")

#: Seconds an in-flight Get may run before the node is reported as not answering.
#: Healthy Gets (resync, bootstrap) finish well inside this; a Get that hangs
#: against a dead route does not, and that is what the Nodes pane should show.
GET_HANG_GRACE = 2.0

#: How many sample intervals a list entry may go unrefreshed before it is taken
#: to have gone away on the device. More than one tick of slack, so a sample
#: that is late or lost does not blank out live state.
STALE_ENTRY_TICKS = 3

#: Floor under the above, for paths sampled so fast that a few ticks is no
#: margin at all.
MIN_STALE_TTL = 45.0

#: Seconds between eviction sweeps. The sweep walks the whole tree, so it is
#: kept well clear of the per-notification path.
PRUNE_INTERVAL = 10.0


@dataclass
class PathState:
    """Bookkeeping for one subscribed path."""

    spec: SubscriptionSpec
    envelopes: List[str] = field(default_factory=list)
    bootstrapped: bool = False
    error: Optional[str] = None
    streamable: bool = True
    #: When a report last read this path, used to retire unwatched paths.
    last_read: float = field(default_factory=time.time)


class RateTracker:
    """Derives per-interface rates from consecutive streamed counter samples.

    Alongside each rate it keeps the raw change over the same interval, which
    is what an error or discard count is reported as: how many during the
    last sample, the way the CLI's two-sample report counts them, rather than
    a total that never goes back to zero.
    """

    def __init__(self) -> None:
        self._last: Dict[str, Tuple[float, Dict[str, int]]] = {}
        self._rates: Dict[str, Dict[str, float]] = {}
        self._deltas: Dict[str, Dict[str, int]] = {}

    def observe(self, itf: str, counters: Dict[str, Any], ts_ns: int) -> None:
        now = ts_ns / 1e9 if ts_ns else time.time()
        current: Dict[str, int] = {}
        for name in IFSTATS_COUNTERS:
            try:
                current[name] = int(counters.get(name, 0))
            except (TypeError, ValueError):
                current[name] = 0
        previous = self._last.get(itf)
        if previous is not None:
            prev_ts, prev_counters = previous
            dt = now - prev_ts
            if dt >= _MIN_RATE_INTERVAL:
                rates = {}
                deltas = {}
                for name in IFSTATS_COUNTERS:
                    delta = current[name] - prev_counters.get(name, 0)
                    if delta < 0:  # counter reset/wrap
                        delta = 0
                    rates[name] = delta / dt
                    deltas[name] = delta
                self._rates[itf] = rates
                self._deltas[itf] = deltas
                self._last[itf] = (now, current)
            return
        self._last[itf] = (now, current)

    def rates(self, itf: str) -> Dict[str, float]:
        # Copied: read by report renders while the subscription thread observes.
        return dict(self._rates.get(itf, {}))

    def deltas(self, itf: str) -> Dict[str, int]:
        """How much each counter moved between the last two samples."""
        return dict(self._deltas.get(itf, {}))

    def all_rates(self) -> Dict[str, Dict[str, float]]:
        """Every interface that has a derived rate, each a copy of its counters."""
        return {itf: dict(rates) for itf, rates in self._rates.items()}

    def forget(self, itf: str) -> None:
        """Drop the counters of an interface that no longer exists."""
        self._last.pop(itf, None)
        self._rates.pop(itf, None)
        self._deltas.pop(itf, None)


def _extract_item_path(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if "path" in item and isinstance(item["path"], str):
            return item["path"]
        if "elem" in item and isinstance(item["elem"], list):
            parts = []
            for el in item["elem"]:
                if isinstance(el, dict):
                    name = el.get("name", "")
                    keys = el.get("key", {})
                    if keys:
                        k_str = "".join(f"[{k}={v}]" for k, v in keys.items())
                        parts.append(f"{name}{k_str}")
                    else:
                        parts.append(name)
            return "/".join(parts)
    return ""


#: How many paths SR Linux accepts in one Subscribe request. Beyond it the
#: whole request is refused with OUT_OF_RANGE - every path of the node stops
#: streaming - so a request is planned to stay within it; see
#: :func:`plan_subscription`. A device that says it allows fewer is believed.
MAX_SUBSCRIBED_PATHS = 36

#: Sampled alongside a subscription that has ON_CHANGE paths in it. Those say
#: nothing while nothing changes, which is also what a node that silently
#: dropped off the network says; one leaf that is due every few seconds is
#: what tells the two apart (see :attr:`HostStream.stale_for`).
HEARTBEAT = SubscriptionSpec("/system/information/current-datetime", "state", "sample", 10)

_MAX_PATHS_ERROR = re.compile(r"maximum of (\d+) subscribed paths")


def _covers(outer: SubscriptionSpec, inner: SubscriptionSpec) -> bool:
    """Whether subscribing to *outer* delivers everything *inner* would.

    It does when *outer* is a prefix of *inner* element by element, and each
    key it constrains selects at least what *inner*'s does: ``name=*`` covers
    ``name=irb*`` and ``name=default``, but ``name=lag*`` does not cover
    ``name=*``, and a key *inner* leaves open is one *outer* must too.
    """
    if outer.path == inner.path or outer.mode != inner.mode:
        return False
    outer_elems, inner_elems = parse_path(outer.path), parse_path(inner.path)
    if len(outer_elems) > len(inner_elems):
        return False
    for (o_name, o_keys), (i_name, i_keys) in zip(outer_elems, inner_elems):
        if o_name != i_name:
            return False
        for key, pattern in o_keys.items():
            value = i_keys.get(key)
            if value is None:
                if pattern != "*":
                    return False
            elif not key_matches(pattern, value) or ("*" in value and pattern != "*" and pattern != value):
                return False
    return True


def plan_subscription(
    specs: List[SubscriptionSpec], limit: int = MAX_SUBSCRIBED_PATHS
) -> Tuple[List[SubscriptionSpec], Dict[str, str], List[str]]:
    """What goes into one Subscribe request, for *specs* to all be served.

    Returns the request's specs, the paths left out because another path of
    the request delivers them (path -> the path that does), and the paths
    left out because the request would otherwise exceed *limit* - which are
    served by polling instead.

    A path another one covers is merged into it, taking the faster of the two
    sample intervals: the same updates land in the same tree either way. What
    still does not fit is decided by how fast it is sampled - the slowest are
    polled, as the ones for which a Get every so often loses the least.
    """
    specs = sorted(specs, key=lambda s: len(parse_path(s.path)))
    roots: List[SubscriptionSpec] = []
    covered: Dict[str, str] = {}
    for spec in specs:
        root = next((r for r in roots if _covers(r, spec)), None)
        if root is None:
            roots.append(spec)
            continue
        covered[spec.path] = root.path
        if spec.sample_interval < root.sample_interval:
            faster = SubscriptionSpec(root.path, root.datatype, root.mode, spec.sample_interval)
            roots[roots.index(root)] = faster
    polled: List[str] = []
    if len(roots) > limit:
        by_speed = sorted(roots, key=lambda s: (-s.sample_interval, s.path))
        for spec in by_speed[: len(roots) - limit]:
            roots.remove(spec)
            polled.append(spec.path)
            # What it covered goes with it: nothing in the request delivers it.
            polled.extend(path for path, root in covered.items() if root == spec.path)
        covered = {path: root for path, root in covered.items() if path not in polled}
    roots.sort(key=lambda s: s.path)
    return roots, covered, sorted(polled)


def _bare(path: str) -> str:
    """*path* with the YANG module prefix taken off every element name.

    SR Linux names elements with their module in a subscription update -
    ``srl_nokia-network-instance:network-instance[name=default]/protocols/
    srl_nokia-bgp:bgp/...`` - while an envelope is named as a Get answers,
    which is without. Only the names are touched: a key value keeps its
    colons, which an IPv6 address is made of.
    """
    elems = []
    for elem in split_path(path):
        bracket = elem.find("[")
        name, keys = (elem, "") if bracket == -1 else (elem[:bracket], elem[bracket:])
        elems.append(strip_module(name) + keys)
    return "/".join(elems)


def _under(path: str, envelope: str) -> bool:
    """Whether an update *path* lies under *envelope* (``network-instance``,
    ``system/lldp``); the root envelope holds everything."""
    if envelope == "":
        return True
    return path == envelope or path.startswith(envelope + "/") or path.startswith(envelope + "[")


def _stale_lists(node: Any, cutoff: float, path: str = "") -> Dict[str, int]:
    """How many entries each list under *node* is about to lose, by list path.

    What the eviction log names, so a list that keeps going stale can be
    traced to the subscription that should have been refreshing it.
    """
    found: Dict[str, int] = {}
    if isinstance(node, ListNode):
        stale = sum(1 for seen in node.seen.values() if seen < cutoff)
        if stale:
            found[path or "/"] = stale
        for _keys, child in node.entries.values():
            for key, count in _stale_lists(child, cutoff, path).items():
                found[key] = found.get(key, 0) + count
    elif isinstance(node, dict):
        for name, child in node.items():
            for key, count in _stale_lists(child, cutoff, f"{path}/{name}").items():
                found[key] = found.get(key, 0) + count
    return found


class HostStream:
    """Streaming state for a single SR Linux node.

    Every node is served by exactly one ``Subscribe`` RPC carrying the union of
    the paths the opened reports need. gNMI cannot add paths to a running
    subscription, so growing that set means replacing the RPC; restarts are
    therefore coalesced by a background reconciler instead of being done once
    per report, and paths nobody reads any more are retired again. That keeps
    the number of gRPC sessions this node spends near the one-per-node floor,
    well inside SR Linux's default ``session-limit`` of 20.
    """

    def __init__(
        self,
        name: str,
        device: Any,
        *,
        default_sample_interval: int = 10,
        get_ttl: float = 30.0,
        reconnect_delay: float = 5.0,
        restart_debounce: float = 1.0,
        idle_timeout: float = 900.0,
        get_hang_grace: float = GET_HANG_GRACE,
        on_update: Optional[Callable[[], None]] = None,
    ) -> None:
        self.name = name
        self.device = device
        self.default_sample_interval = default_sample_interval
        self.get_ttl = get_ttl
        self.reconnect_delay = reconnect_delay
        self.restart_debounce = restart_debounce
        self.idle_timeout = idle_timeout
        self.get_hang_grace = get_hang_grace
        self.on_update = on_update

        self._lock = threading.RLock()
        self._get_lock = threading.Lock()
        self._tree: Dict[str, Any] = {}
        self._paths: Dict[str, PathState] = {}
        self._direct_cache: Dict[
            Tuple[str, str], Tuple[float, List[Dict[str, Any]]]
        ] = {}
        #: Failed Gets, kept for the same TTL as successful ones.
        self._failed_gets: Dict[Tuple[str, str], Tuple[float, Exception]] = {}
        #: Paths this node rejected as not in its schema - the fabric modules
        #: of a fixed-form chassis. Unlike a failure, that does not change
        #: while the connection lasts, so they are not asked for again; a
        #: reconnect (an upgrade is one) starts a new stream and asks afresh.
        self._rejected: Dict[Tuple[str, str], Exception] = {}
        #: The most paths one Subscribe request may carry on this node.
        self.max_paths = MAX_SUBSCRIBED_PATHS
        #: How the current request was planned: paths another one delivers,
        #: and paths polled because the request had no room for them.
        self._covered: Dict[str, str] = {}
        self._polled: Set[str] = set()
        self.rates = RateTracker()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._generation = 0
        self._subscription: Any = None
        self._gets = 0
        #: When the running subscription was established, if there is one.
        self._subscribed_at: Optional[float] = None
        #: Start of the current run of consecutively failing Gets, if any.
        self._failing_since: Optional[float] = None
        #: When the Get currently in flight started, if there is one.
        self._get_started: Optional[float] = None
        #: Why the last Get failed, kept until one succeeds again.
        self._get_error: Optional[str] = None
        #: When the last stale-entry sweep ran (monotonic).
        self._last_prune = 0.0
        #: When each envelope last had an update applied (monotonic): what a
        #: sweep measures staleness against, so a stream that is merely behind
        #: is not read as the node forgetting its state.
        self._envelope_seen: Dict[str, float] = {}
        self.last_update: Optional[float] = None
        self.connected = False
        #: Whether the running subscription has sent its initial sync.
        self.synced = False
        #: When it started, on the clock list entries are aged by.
        self._subscribed_from = 0.0
        #: What arrived while a resync was reading the node, to apply again to
        #: the tree it reads once that is swapped in; ``None`` outside one.
        self._replay: Optional[List[Dict[str, Any]]] = None
        #: Whether the running subscription carries :data:`HEARTBEAT`.
        self._heartbeat = False
        #: The ON_CHANGE paths of the running subscription, parsed.
        self._on_change: List[Tuple[str, List[Tuple[str, Dict[str, str]]]]] = []
        self.error: Optional[str] = None

        self._dirty = threading.Event()
        self._reconciler = threading.Thread(
            target=self._reconcile,
            name=f"gnmi-reconcile-{self.name}",
            daemon=True,
        )
        self._reconciler.start()

    # ------------------------------------------------------------------ #
    # subscription lifecycle
    # ------------------------------------------------------------------ #

    def ensure_paths(self, specs: List[SubscriptionSpec]) -> None:
        """Make sure every spec in *specs* is subscribed.

        Newly added paths are bootstrapped with a ``Get`` right away so the
        report that asked for them can be rendered immediately, but replacing
        the ``Subscribe`` RPC is left to the reconciler, which batches the
        restarts caused by opening several reports in a row.
        """
        added = []
        now = time.time()
        with self._lock:
            for spec in specs:
                state = self._paths.get(spec.path)
                if state is not None:
                    state.last_read = now
                    continue
                self._paths[spec.path] = PathState(spec=spec, last_read=now)
                added.append(spec)
        if not added:
            return
        logger.debug(
            "%s: %d new path(s) to bootstrap: %s",
            self.name,
            len(added),
            ", ".join(s.path for s in added),
        )
        with self._lock:
            known = [s.spec for s in self._paths.values() if s.streamable]
        # Widest first, so a path is only ever read into the tree once nothing
        # already there holds more of it.
        for spec in sorted(added, key=lambda s: len(parse_path(s.path))):
            self._bootstrap(spec, self._tree_for(spec, known, self._tree))
        self._dirty.set()

    @staticmethod
    def _tree_for(spec: SubscriptionSpec, specs: List[SubscriptionSpec], tree: Dict[str, Any]) -> Dict[str, Any]:
        """Where *spec*'s bootstrap Get goes: *tree*, or nowhere if another path covers it.

        A Get does not say which leaves key a list, so what it returns is
        merged in as a plain list, and a second Get of the same list replaces
        the first. The narrower one of two paths answers with less of each entry
        - ``route/ipv4-prefix`` with a route's keys alone - and read after the
        wider one it would leave only that. SAMPLE puts the rest back on its
        next tick; ON_CHANGE never does. The wider path already holds all of
        it, so the narrower one is read only to learn its envelope.
        """
        if any(_covers(other, spec) for other in specs if other.path != spec.path):
            return {}
        return tree

    def _bootstrap(self, spec: SubscriptionSpec, tree: Dict[str, Any]) -> bool:
        """Seed *tree* with a gNMI Get and learn the response envelope keys.

        Returns whether the ``Get`` itself succeeded, which is a different
        question from whether the path came out bootstrapped: an empty response
        is a perfectly good answer that simply leaves the path pending.
        """
        with self._lock:
            state = self._paths.get(spec.path)
        if state is None:  # retired while we were getting to it
            return False
        rejected = self._rejected.get((spec.path, spec.datatype))
        if rejected is not None:
            # Discovery has just asked, and the node said it has no such path.
            with self._lock:
                state.error = str(rejected)
            return False
        # Deliberately outside the lock: this is a network round-trip, and every
        # render of every report on this node would queue up behind it.
        try:
            resp = self._raw_get(spec.path, spec.datatype)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            # A failed Get says nothing about whether the path can be streamed;
            # the node is usually just unreachable, e.g. rebooting with the lab.
            # Leaving the path's own flags alone keeps it retryable: whatever it
            # was before, it goes back to being that once the node answers.
            with self._lock:
                state.error = str(exc)
            if not _gnmi_path_missing(exc):
                logger.warning(
                    "%s: bootstrap Get failed for %s: %s", self.name, spec.path, exc
                )
            else:
                logger.debug(
                    "%s: %s does not exist on this node: %s", self.name, spec.path, exc
                )
            return False
        # Serve the first render from this response instead of repeating the Get
        # while the path is still pending.
        with self._lock:
            self._direct_cache[(spec.path, spec.datatype)] = (time.time(), resp)
        self._absorb(spec, resp, tree)
        return True

    def _absorb(
        self, spec: SubscriptionSpec, resp: List[Dict[str, Any]], tree: Dict[str, Any]
    ) -> bool:
        """Learn the envelope keys of *resp* and seed *tree* with its state.

        Returns whether the path is now bootstrapped, i.e. ready to be streamed.

        SR Linux answers a ``Get`` for a subtree that holds nothing with a
        notification carrying no updates, which says nothing about the envelope
        key a ``Get`` caller expects to find. Such a path stays *pending*: it is
        left out of the subscription and served by a TTL-cached ``Get`` until its
        first entry appears. Control-plane driven tables (MAC, ES destinations)
        routinely start out that way.
        """
        hints = _key_hints(spec.path)
        # The tree is shared with the subscription thread, so seeding it and
        # recording what was learned from the response is one atomic step.
        with self._lock:
            state = self._paths.get(spec.path)
            if state is None:
                return False
            envelopes: List[str] = []
            streamable = True
            for item in resp:
                if not isinstance(item, dict):
                    streamable = False
                    break
                if not item:  # empty subtree: nothing to learn from yet
                    continue
                if len(item) != 1:
                    # Keyless yang-list responses cannot be placed in the tree;
                    # such a path is served by a TTL-cached Get instead.
                    streamable = False
                    break
                env_key = next(iter(item))
                env_path = "" if env_key in ("/", "") else env_key
                insert(tree, env_path, item[env_key], key_hints=hints, pin=spec.mode == "on_change")
                if env_path not in envelopes:
                    envelopes.append(env_path)
            state.streamable = streamable
            if not streamable:
                logger.debug(
                    "%s: %s cannot be streamed, serving it from cached Gets",
                    self.name,
                    spec.path,
                )
                state.envelopes = []
                state.bootstrapped = False
                return False
            state.error = None
            if envelopes:
                # A path that just gained state has to join the subscription,
                # whether it is brand new, was empty until now, or is coming
                # back after the node was unreachable.
                if not state.bootstrapped:
                    self._dirty.set()
                state.envelopes = envelopes
                state.bootstrapped = True
            else:
                logger.debug(
                    "%s: %s is still empty, leaving it pending", self.name, spec.path
                )
            return state.bootstrapped

    def resync(self) -> None:
        """Rebuild the whole tree from gNMI Gets, dropping any stale state.

        SAMPLE subscriptions refresh values but rely on the target sending
        deletes for entries that disappear. A periodic full re-read keeps the
        view self-healing if one is ever missed.

        Paths that are not streaming yet are re-read too, so this doubles as the
        retry for a node that was unreachable: the first sweep whose Gets come
        back puts its paths back on the subscription.
        """
        with self._lock:
            specs = [
                state.spec for state in self._paths.values() if state.streamable
            ]
            if not specs:
                return
            # The Gets take a while, and what the subscription delivers in the
            # meantime lands in the tree about to be replaced. SAMPLE would send
            # it again next tick; ON_CHANGE never does.
            self._replay = []
        logger.debug("%s: resyncing %d path(s)", self.name, len(specs))
        started = time.time()
        fresh: Dict[str, Any] = {}
        try:
            for spec in specs:
                if not self._bootstrap(spec, self._tree_for(spec, specs, fresh)):
                    # Swapping in a half-read tree would blank the reports of a node
                    # that is merely unreachable. Keeping the old one leaves them on
                    # their last known state, which ``last_update`` dates for the UI.
                    logger.debug("%s: resync aborted at %s", self.name, spec.path)
                    return
            with self._lock:
                self._tree = fresh
                arrived, self._replay = self._replay or [], None
                # In order and under the lock, so nothing newer slips in between:
                # replaying from before the first Get converges on the latest state.
                for message in arrived:
                    self._apply(message, notify=False)
        finally:
            with self._lock:
                self._replay = None
        self.last_update = time.time()
        self._notify()
        logger.debug(
            "%s: resynced %d path(s) in %.3fs",
            self.name,
            len(specs),
            self.last_update - started,
        )

    def _reconcile(self) -> None:
        """Apply pending path-set changes, one restart per burst.

        Opening a report adds its paths and flags the set dirty. Waiting for the
        flag to stay clear for ``restart_debounce`` before re-subscribing turns
        the burst of activations that a page load produces into a single new
        ``Subscribe`` RPC, instead of one per report.
        """
        while not self._closed.is_set():
            if not self._dirty.wait(timeout=1.0):
                if self._retire_idle_paths():
                    self._restart()
                continue
            while self._dirty.is_set():
                self._dirty.clear()
                if self._closed.wait(self.restart_debounce):
                    return
            self._restart()

    def _retire_idle_paths(self) -> bool:
        """Drop paths no report has read for ``idle_timeout``.

        The streamed values stay behind in the tree, but they are unreachable
        without a :class:`PathState` and get overwritten by a fresh bootstrap if
        the path is ever asked for again. Returns whether anything was dropped.
        """
        if self.idle_timeout <= 0:
            return False
        cutoff = time.time() - self.idle_timeout
        with self._lock:
            idle = [
                path for path, state in self._paths.items() if state.last_read < cutoff
            ]
            for path in idle:
                del self._paths[path]
        if idle:
            logger.info("%s: retired %d idle path(s)", self.name, len(idle))
            logger.debug("%s: retired %s", self.name, ", ".join(idle))
        return bool(idle)

    def _restart(self) -> None:
        """(Re)start the subscription thread with the current path set."""
        if self._closed.is_set():
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            wanted = [
                s.spec for s in self._paths.values() if s.streamable and s.bootstrapped
            ]
            heartbeat = any(s.mode == "on_change" for s in wanted)
            specs, self._covered, polled = plan_subscription(wanted, self.max_paths - heartbeat)
            if heartbeat and specs:
                specs.append(HEARTBEAT)
            self._heartbeat = heartbeat and bool(specs)
            self._polled = set(polled)
            self._on_change = [(s.path, parse_path(s.path)) for s in specs if s.mode == "on_change"]
        if polled:
            logger.info(
                "%s: %d path(s) do not fit the %d a subscription may carry, polling them: %s",
                self.name,
                len(polled),
                self.max_paths,
                ", ".join(polled),
            )
        logger.debug(
            "%s: restarting subscription (generation %d) with %d path(s): %s",
            self.name,
            generation,
            len(specs),
            ", ".join(s.path for s in specs) or "none",
        )
        self._close_subscription()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if not specs or self._closed.is_set():
            # Nothing left to stream, so there is no Subscribe RPC to be
            # connected by; saying otherwise would leave a stale 'connected'
            # behind for as long as the node has no streamable path.
            self.connected = False
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(generation, specs),
            name=f"gnmi-sub-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def _close_subscription(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is None:
            return
        try:
            subscription.close()
        except Exception as exc:  # noqa: BLE001 - best effort teardown
            logger.debug("%s: closing subscription failed: %s", self.name, exc)

    def _run(self, generation: int, specs: List[SubscriptionSpec]) -> None:
        request = {
            "subscription": [s.as_gnmi() for s in specs],
            "mode": "stream",
            "encoding": "json_ietf",
        }
        while self._alive(generation):
            try:
                subscription = self.device.gnmi_subscribe(request)
                self._subscription = subscription
                self.connected = True
                self.error = None
                self.synced = False
                self._subscribed_at = time.time()
                self._subscribed_from = time.monotonic()
                logger.info("%s: subscribed to %d path(s)", self.name, len(specs))
                while self._alive(generation):
                    try:
                        message = subscription.get_update(timeout=1.0)
                    except TimeoutError:
                        if subscription.error is not None:
                            raise subscription.error
                        continue
                    if message:
                        self._apply(message)
            except Exception as exc:  # noqa: BLE001 - retried with backoff
                self.connected = False
                if self._alive(generation):
                    self.error = str(exc)
                    logger.warning("%s: subscription failed: %s", self.name, exc)
                    limit = _MAX_PATHS_ERROR.search(str(exc))
                    if limit and int(limit.group(1)) < self.max_paths:
                        # The same request would be refused again: plan a new
                        # one within what the node says it takes.
                        self.max_paths = int(limit.group(1))
                        self._dirty.set()
                # Otherwise this is the RPC we cancelled ourselves to re-subscribe
                # with a changed path set, which says nothing about the node.
            finally:
                self._close_subscription()
            if not self._alive(generation):
                break
            self._stop.wait(self.reconnect_delay)
        self.connected = False

    def _alive(self, generation: int) -> bool:
        """Whether the subscription of *generation* should still be running.

        ``_stop`` is cleared again by every restart, so a subscription that
        raced with :meth:`stop` also has to check ``_closed`` - otherwise it
        would reconnect and hold a session on the target until the process ends.
        """
        return (
            not self._stop.is_set()
            and not self._closed.is_set()
            and generation == self._generation
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Tear the subscription down and wait for its threads to notice.

        Both threads block on waits of their own - up to a second on the next
        update, up to ``restart_debounce`` plus a thread join on a pending
        restart - so returning before they have run out would leave the node's
        ``Subscribe`` RPC open past the shutdown that was supposed to close it.
        """
        self._closed.set()
        self._stop.set()
        self._dirty.set()
        with self._lock:
            self._generation += 1
        self._close_subscription()
        for thread in (self._thread, self._reconciler):
            if thread is None or not thread.is_alive():
                continue
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "%s: %s did not stop within %.1fs", self.name, thread.name, timeout
                )

    # ------------------------------------------------------------------ #
    # update handling
    # ------------------------------------------------------------------ #

    def _apply(self, message: Dict[str, Any], notify: bool = True) -> None:
        """Merge one notification into the tree.

        *notify* runs :attr:`on_update` afterwards, which the store answers by
        taking its own lock. It is only ever called without this stream's lock
        held: the store takes the two the other way round.
        """
        if message.get("sync_response"):
            self._synced()
            return
        update = message.get("update")
        if not update:
            return
        with self._lock:
            if self._replay is not None:
                self._replay.append(message)
        prefix = update.get("prefix") or ""
        timestamp = update.get("timestamp") or 0
        touched_itfs = set()
        logger.debug(
            "%s: notification on %s with %d update(s) and %d delete(s)",
            self.name,
            prefix or "/",
            len(update.get("update") or []),
            len(update.get("delete") or []),
        )
        with self._lock:
            self._direct_cache.clear()
            self._failed_gets.clear()
            envelopes = self._envelopes()
            arrived = time.monotonic()
            for item in update.get("update", []) or []:
                item_path = _extract_item_path(item)
                path = join_path(prefix, item_path)
                val = item.get("val") if isinstance(item, dict) else None
                bare = _bare(path)
                insert(self._tree, path, val, pin=self._pinned(bare))
                for env in envelopes:
                    if _under(bare, env):
                        self._envelope_seen[env] = arrived
                itf = _touched_interface(path)
                if itf:
                    touched_itfs.add(itf)
            for item in update.get("delete", []) or []:
                item_path = _extract_item_path(item)
                path = join_path(prefix, item_path)
                delete(self._tree, path, [p for p, _elems in self._on_change])
                gone = _deleted_interface(path)
                if gone:
                    self.rates.forget(gone)
            for itf in touched_itfs:
                stats = get_node(self._tree, f"interface[name={itf}]/statistics")
                if isinstance(stats, dict):
                    self.rates.observe(itf, materialize(stats), timestamp)
            # State arriving is the node answering, which a Get that failed
            # earlier no longer has anything to say about. Nothing else clears
            # that: once every path of a report streams, renders stop issuing
            # Gets, so one failure would have kept the node red indefinitely.
            self._failing_since = None
            self._get_error = None
            self._evict_stale()
        self.last_update = time.time()
        if notify:
            self._notify()

    def _notify(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update()
            except Exception:  # noqa: BLE001
                pass

    def _pinned(self, path: str) -> bool:
        """Whether an update to *path* comes from one of the ON_CHANGE paths."""
        if not self._on_change:
            return False
        elems = parse_path(path)
        for _path, pattern in self._on_change:
            if all(
                name == p_name and all(k not in keys or key_matches(v, keys[k]) for k, v in p_keys.items())
                for (name, keys), (p_name, p_keys) in zip(elems, pattern)
            ):
                return True
        return False

    def _synced(self) -> None:
        """The initial sync is in: what it did not re-send of an ON_CHANGE path is gone.

        Deletes made while the previous subscription was being replaced were
        sent to nobody, and an ON_CHANGE path has no later tick to notice by.
        """
        with self._lock:
            dropped = sum(sweep(self._tree, path, self._subscribed_from) for path, _elems in self._on_change)
            self.synced = True
        if dropped:
            logger.debug("%s: dropped %d entr%s the initial sync did not re-send", self.name, dropped, "y" if dropped == 1 else "ies")

    def _envelopes(self) -> List[str]:
        """Every envelope a streaming path of this node feeds."""
        found: List[str] = []
        for state in self._paths.values():
            for env in state.envelopes:
                if env not in found:
                    found.append(env)
        return found

    def _evict_stale(self) -> None:
        """Drop list entries the subscription has stopped refreshing.

        SAMPLE mode re-sends every leaf of a subtree on each tick and never
        reports a delete, while the tree merges rather than replaces so that
        two subscriptions sharing an envelope cannot erase each other. An entry
        that goes away on the device therefore lingers, and until this ran the
        only thing that removed it was the resync sweep, minutes later: a
        designated forwarder that moved rendered as two.

        Staleness is measured against the envelope's own progress where that
        lags the clock: an entry is stale when the envelope has had a TTL's
        worth of newer data that did not include it. A stream that is merely
        behind - a spine's RPC catching up on a large subtree - delivers its
        samples late but whole, and evicting what the last sample refreshed
        because the next one is overdue would blank out live state.

        Caller holds ``self._lock``.
        """
        now = time.monotonic()
        if now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        # Nothing is refreshing the tree while the RPC is down, and a sweep then
        # would read an outage as the whole fabric going away. The same applies
        # right after reconnecting, until the subscription has had time to
        # deliver a full tick of every path.
        if not self.connected or self._subscribed_at is None:
            return
        uptime = time.time() - self._subscribed_at

        # An envelope is what the tree merges into, so what survives under one
        # is decided by the slowest path feeding it, not by whichever path the
        # entry originally arrived on.
        horizons: Dict[str, float] = {}
        for state in self._paths.values():
            if not (state.streamable and state.bootstrapped):
                continue
            if state.spec.mode != "sample":
                continue
            ttl = max(STALE_ENTRY_TICKS * state.spec.sample_interval, MIN_STALE_TTL)
            for env in state.envelopes:
                horizons[env] = max(horizons.get(env, 0.0), ttl)
        if "" in horizons:  # the root envelope overlaps every other one
            horizons[""] = max(horizons.values())

        for env, ttl in horizons.items():
            if uptime < ttl:
                continue
            node = self._tree if env == "" else get_node(self._tree, env)
            if node is None:
                continue
            # The TTL counts from the envelope's last update where that is
            # older than the clock: only data newer than an entry can say it
            # is gone, and none has arrived while the stream is behind.
            latest = self._envelope_seen.get(env)
            if latest is None:
                continue
            cutoff = min(now, latest) - ttl
            stale = _stale_lists(node, cutoff) if logger.isEnabledFor(logging.DEBUG) else {}
            dropped = prune(node, cutoff)
            if dropped:
                logger.debug(
                    "%s: dropped %d stale entr%s under %s (unrefreshed for %.0fs): %s",
                    self.name,
                    dropped,
                    "y" if dropped == 1 else "ies",
                    env or "/",
                    ttl,
                    ", ".join(
                        f"{path} x{count}"
                        for path, count in sorted(stale.items(), key=lambda kv: -kv[1])[:6]
                    ),
                )

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def snapshot(self, path: str) -> Optional[List[Dict[str, Any]]]:
        """Return the streamed state for *path* shaped like a gNMI Get response."""
        with self._lock:
            state = self._paths.get(path)
            if path in self._polled:
                # Not in the subscription, so the tree is not kept current for
                # it: the caller Gets it instead, at its sample interval.
                return None
            if state is None or not state.bootstrapped or not state.streamable:
                return self._borrowed_snapshot(path)
            state.last_read = time.time()
            result: List[Dict[str, Any]] = []
            for env in state.envelopes:
                node = self._tree if env == "" else get_node(self._tree, env)
                key = env if env else "/"
                if node is None:
                    result.append({key: {}})
                    continue
                # The tree is shared by every subscription of this node, so the
                # envelope can hold entries this path never asked for.
                result.append({key: select_path(materialize(node), path, env)})
            return result

    def _borrowed_snapshot(self, path: str) -> Optional[List[Dict[str, Any]]]:
        """Serve *path* from state a neighbouring subscription already streams.

        Reports overlap so much on ``/interface`` and ``/network-instance`` that a
        path without a subscription of its own is often covered anyway. Borrowing
        that state still has to go through :func:`select_path`, exactly like a
        registered path does, or the caller would be handed every entry the other
        subscriptions put under the root rather than the ones it asked for.

        Returns ``None`` when nothing survives the narrowing, so the caller falls
        back to a ``Get`` instead of rendering an empty table forever. Must be
        called with ``_lock`` held.
        """
        elems = parse_path(path)
        if not elems:
            return None
        root = elems[0][0]
        if root not in _SHARED_ROOTS:
            return None
        node = self._tree.get(root)
        if node is None:
            return None
        selected = select_path(materialize(node), path, root)
        if not selected:
            return None
        return [{root: selected}]

    def snapshot_roots(self, roots: Tuple[str, ...]) -> Dict[str, Any]:
        """Materialize whole tree roots, for summaries that span reports.

        Unlike :meth:`snapshot` this returns everything below each root instead
        of what one path selects, because the caller is summarizing the node
        rather than standing in for a ``Get``. Roots the node has no state for
        are left out.
        """
        with self._lock:
            # Materializing walks the live tree, so it cannot be moved out of
            # the lock - the subscription thread would be mutating it midway.
            return {
                root: materialize(node)
                for root, node in ((r, self._tree.get(r)) for r in roots)
                if node is not None
            }

    def interfaces(self) -> List[str]:
        """Names of the interfaces currently present in the streamed state."""
        with self._lock:
            node = get_node(self._tree, "interface")
            entries = materialize(node) if node is not None else []
        if not isinstance(entries, list):
            return []
        return sorted(str(e.get("name", "")) for e in entries if isinstance(e, dict))

    def interface_state(self, name: str) -> Dict[str, Any]:
        with self._lock:
            node = get_node(self._tree, f"interface[name={name}]")
            return materialize(node) if isinstance(node, dict) else {}

    def direct_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        """gNMI Get with a short TTL cache, for paths that are not subscribed.

        Failures are cached alongside the successes: an unreachable node would
        otherwise be asked again by every report on every render, which buries
        the log in gRPC errors and spends the node's session budget on calls
        that are already known to fail.
        """
        cache_key = (path, datatype)
        now = time.time()
        with self._lock:
            state = self._paths.get(path)
            # A path polled in place of streaming is as fresh as its sample
            # interval asks for, not as the Get cache would otherwise keep it.
            ttl = state.spec.sample_interval if path in self._polled and state else self.get_ttl
            cached = self._direct_cache.get(cache_key)
            if cached and now - cached[0] < ttl:
                logger.debug(
                    "%s: serving %s from the %.0fs Get cache (%.1fs old)",
                    self.name,
                    path,
                    ttl,
                    now - cached[0],
                )
                return cached[1]
            rejected = self._rejected.get(cache_key)
            if rejected is not None:
                raise rejected
            failed = self._failed_gets.get(cache_key)
            if failed and now - failed[0] < self.get_ttl:
                logger.debug(
                    "%s: %s failed %.1fs ago, not asking again yet",
                    self.name,
                    path,
                    now - failed[0],
                )
                raise failed[1]
        try:
            resp = self._raw_get(path, datatype)
        except Exception as exc:  # noqa: BLE001 - re-raised to the caller
            with self._lock:
                if _gnmi_path_missing(exc):
                    self._rejected[cache_key] = exc
                else:
                    self._failed_gets[cache_key] = (now, exc)
            raise
        with self._lock:
            self._failed_gets.pop(cache_key, None)
            self._direct_cache[cache_key] = (now, resp)
        self._promote(path, datatype, resp)
        return resp

    def _promote(self, path: str, datatype: str, resp: List[Dict[str, Any]]) -> None:
        """Start streaming a pending path once its first data shows up.

        A path that was empty when the report was opened is served by these
        TTL-cached Gets, so the response that finally carries data is also the
        one that reveals the envelope key. Learning it here means the path joins
        the subscription without spending a Get of its own on probing.
        """
        with self._lock:
            state = self._paths.get(path)
            if (
                state is None
                or state.bootstrapped
                or not state.streamable
                or state.spec.datatype != datatype
            ):
                return
            spec = state.spec
            known = [s.spec for s in self._paths.values() if s.streamable]
        if self._absorb(spec, resp, self._tree_for(spec, known, self._tree)):
            logger.info("%s: %s now has state, subscribing to it", self.name, path)

    def _raw_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        # Serialized on purpose: an in-flight Get holds a gRPC session on the
        # target just like the subscription does, and the node's budget is
        # shared with every other gRPC client.
        with self._get_lock:
            self._gets += 1
            self._get_started = time.time()
            try:
                with _suppress_pygnmi_client_logging():
                    resp = self.device.get(paths=[path], datatype=datatype)
            except Exception as exc:
                # A path the device does not have is not a node that stopped
                # answering: it answered, with a rejection. Reports probe optional
                # paths all the time - the l3vpn RIBs, the IPv6 route-table the
                # services reports read - and counting those as failures is what
                # left the Nodes pane flashing red on a healthy fabric.
                if _gnmi_path_missing(exc):
                    logger.debug(
                        "%s: %s rejected as unknown, not counted against the node: %s",
                        self.name,
                        path,
                        exc,
                    )
                    self._rejected[(path, datatype)] = exc
                    self._failing_since = None
                    self._get_error = None
                    raise
                # How long the node has been failing decides whether waiting for
                # it is still worthwhile or the connection itself has to go; see
                # FabricStore._heal_connections.
                if self._failing_since is None:
                    self._failing_since = time.time()
                self._get_error = str(exc)
                raise
            finally:
                self._get_started = None
            self._failing_since = None
            self._get_error = None
        return [strip_modules(d) for d in resp] if resp else []

    def discovery_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        """A Get made while discovering which paths a report needs.

        Uncached on purpose - discovery is what decides the shape of everything
        that follows - but otherwise accounted for like any other Get.
        """
        return self._raw_get(path, datatype)

    @property
    def getting(self) -> bool:
        """True while a gNMI Get is in flight against this node."""
        return self._get_started is not None

    @property
    def gets(self) -> int:
        """Gets issued to this node so far, counting the failed ones.

        Monotonic on purpose: a caller sampling it periodically sees gNMI
        activity it would otherwise miss, since a healthy Get is over long
        before the next sample.
        """
        return self._gets

    @property
    def failing_since(self) -> Optional[float]:
        """When this node stopped answering, or ``None`` while it answers.

        A Get that hangs counts as much as one that fails, but only after it has
        been in flight for ``get_hang_grace`` seconds. Healthy Gets (a resync,
        bootstrapping a new report) finish well inside that window; treating
        them as down is what made Nodes dots flash red. A Get against an
        address that stopped being routed blocks until TCP gives up, and that
        is what this reports.

        A gRPC call carries no deadline of its own, so the elapsed time is what
        tells a slow Get from a dead one.
        """
        stamps = []
        if self._failing_since is not None:
            stamps.append(self._failing_since)
        started = self._get_started
        if (
            started is not None
            and (time.time() - started) >= self.get_hang_grace
        ):
            stamps.append(started)
        return min(stamps) if stamps else None

    @property
    def stale_for(self) -> Optional[float]:
        """Seconds by which this node's streamed updates are overdue, if they are.

        A subscription can die without the transport ever reporting it. If the
        route to the node goes away rather than the node refusing connections,
        the TCP connection simply falls silent, and there is no keepalive on it
        to find that out - so gRPC keeps considering the call healthy and the
        updates just stop.

        What gives it away is the cadence. A SAMPLE path is reported on a known
        interval whether anything changed or not, and the fastest of those
        intervals is the one that has to keep being met. ON_CHANGE paths have
        no cadence, which is what the heartbeat is for.
        """
        with self._lock:
            intervals = [
                state.spec.sample_interval
                for state in self._paths.values()
                if state.streamable and state.bootstrapped
                and state.spec.mode == "sample"
            ]
            if self._heartbeat:
                intervals.append(HEARTBEAT.sample_interval)
        if not intervals or not self.connected:
            return None  # nothing is streaming, so nothing is due
        reference = max(self.last_update or 0.0, self._subscribed_at or 0.0)
        if reference == 0.0:
            return None
        overdue = time.time() - reference - (min(intervals) * 3 + 5)
        return overdue if overdue > 0 else None

    @property
    def last_error(self) -> Optional[str]:
        """The most recent reason this node did not answer, if any."""
        return self._get_error or self.error

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        with self._lock:
            paths = [
                {
                    "path": state.spec.path,
                    "mode": state.spec.mode,
                    "sample_interval": state.spec.sample_interval,
                    "streaming": state.streamable
                    and state.bootstrapped
                    and state.spec.path not in self._polled,
                    # Empty when the report was opened, so the envelope shape is
                    # not known yet; served by TTL-cached Gets until it fills up.
                    "pending": state.streamable and not state.bootstrapped,
                    "error": state.error,
                    # Delivered by another path of the subscription, or left
                    # out of it for room and polled instead.
                    "covered_by": self._covered.get(state.spec.path),
                    "polled": state.spec.path in self._polled,
                }
                for state in self._paths.values()
            ]
        return {
            "node": self.name,
            "connected": self.connected,
            "error": self.error,
            "last_update": self.last_update,
            # gRPC sessions this node currently spends on us: the Subscribe RPC
            # plus at most one in-flight Get.
            "sessions": (1 if self.connected else 0)
            + (1 if self._get_lock.locked() else 0),
            "gets": self.gets,
            "getting": self.getting,
            "failing_since": self.failing_since,
            "paths": paths,
        }


def _key_hints(path: str) -> Dict[str, List[str]]:
    """Map YANG list name -> key leaves, as far as the requested path reveals.

    A gNMI ``Get`` response returns lists inline without marking which leaves
    are the keys. The requested path does name them (``interface[name=*]``), so
    those hints let the bootstrap store the list as an addressable list node
    instead of an opaque blob.
    """
    hints: Dict[str, List[str]] = {}
    for name, keys in parse_path(path):
        if keys:
            hints[name] = list(keys)
    return hints


def _deleted_interface(path: str) -> Optional[str]:
    """Return the interface name when *path* removes a whole interface entry."""
    elems = parse_path(path)
    if len(elems) != 1 or elems[0][0] != "interface":
        return None
    return elems[0][1].get("name") or None


def _touched_interface(path: str) -> Optional[str]:
    """Return the interface name when *path* points into interface statistics."""
    elems = parse_path(path)
    if len(elems) < 2 or elems[0][0] != "interface":
        return None
    name = elems[0][1].get("name")
    if not name:
        return None
    if not any(elem[0] == "statistics" for elem in elems[1:]):
        return None
    return name
