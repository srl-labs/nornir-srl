"""The fabric's recent past, as the server watched it happen.

The server already holds the fabric's state; what it did not hold until now is
what that state *was*. :class:`Timeline` keeps three things:

* the changes between consecutive readings, newest first, in a bounded buffer;
* the latest reading - the fabric state, the findings of the checks over it
  and the incidents they group into - so a surface that wants the fabric's
  health does not have to run every check again to get it;
* a *baseline*: one reading kept aside as "what good looks like", taken once
  the server has settled after starting, or whenever someone asks for one.
  The drift from it is not a timeline but a comparison, computed when asked.

With a :class:`~nornir_srl.history.HistoryStore`, all of it outlives the
process: changes are written to disk as they are recorded and read back on
start, a baseline someone set is kept and compared against after a restart,
fcli stopping and starting is on the timeline itself, and what changed while
it was not running is reported as soon as it is running again.

:class:`Watcher` is the thread that takes the readings.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .. import configs
from ..changes import (
    FLAP_WINDOW,
    INFO,
    WARNING,
    WATCH_REPORTS,
    Change,
    change_order,
    diff_fabric,
    diff_findings,
    node_change,
    normalize_prefix,
    settled_findings,
)
from ..checks import CHECKS_BY_NAME, Finding, run_checks
from ..fabric import FabricState, as_list
from ..history import DEFAULT_RETENTION_DAYS, LAST_READING, HistoryStore
from ..incidents import Incident, correlate
from ..reports import reading_reports
from .readings import NOT_RECORDED, Recorder, payload as reading_payload, replay_reading

if TYPE_CHECKING:  # pragma: no cover - types only
    from .store import FabricStore

logger = logging.getLogger(__name__)


#: Changes kept, oldest dropped first.
DEFAULT_CAPACITY = 5000

#: Readings taken before the timeline starts recording. The first readings
#: after start-up are paths still being bootstrapped, and comparing them
#: would log the whole fabric as having just appeared.
WARMUP_READINGS = 2

#: How often, in readings, the running server keeps the reading it just took,
#: for the next start to compare against. A reading kept is one written in
#: full; every 20 at the default interval is every five minutes.
PERSIST_EVERY = 20

#: How often, in readings, changes older than the retention are pruned.
PRUNE_EVERY = 240

#: The node and kind fcli's own comings and goings are recorded under.
SERVER_NODE = "fcli"
SERVER_KIND = "server"

#: What a change found by comparing with the reading kept before a restart
#: says about when it happened.
WHILE_DOWN = "while fcli was not running"

#: Checks whose reports a kept reading cannot hold. Comparing their findings
#: with a reading read back would report each of them raised or cleared.
UNRECORDED_CHECKS = frozenset(
    name for name, check in CHECKS_BY_NAME.items() if check.requires and set(check.requires) <= NOT_RECORDED
)


def _duration(seconds: float) -> str:
    """``3h 12m``, ``4d 2h``, ``45s``: how long, to the unit that matters."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def comparable_findings(findings: Iterable[Finding], replayed: bool) -> List[Finding]:
    """*findings*, less those a reading read back from disk could never have had."""
    if not replayed:
        return list(findings)
    return [f for f in findings if f.check not in UNRECORDED_CHECKS]


@dataclass
class Reading:
    """The fabric at one moment: its state and what the checks made of it."""

    at: float
    state: FabricState
    findings: List[Finding] = field(default_factory=list)
    incidents: List[Incident] = field(default_factory=list)
    #: Which nodes were answering, by name.
    connected: Dict[str, bool] = field(default_factory=dict)
    #: Read back from a kept recording rather than taken from the streams:
    #: it holds no interface rates, so no findings made of them.
    replayed: bool = False


class Timeline:
    """Changes between readings, the latest reading, and a baseline."""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        history: Optional[HistoryStore] = None,
        retention_days: float = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._lock = threading.Lock()
        self._changes: Deque[Change] = deque(maxlen=capacity)
        self.latest: Optional[Reading] = None
        self.baseline: Optional[Reading] = None
        #: The name the baseline is kept under, when it is kept at all.
        self.baseline_name: Optional[str] = None
        self.started = time.time()
        #: Where the timeline is kept across restarts, if anywhere.
        self.history = history
        self.retention_days = retention_days
        #: Prefixes whose changes are reported one by one, beyond the defaults
        #: and the system addresses every table has; see
        #: :func:`nornir_srl.changes.diff_fabric`.
        self._watched: Set[str] = set()
        #: Every cable LLDP has shown, by node and local port, as the
        #: (advertised system name, port) on the far end. Kept when LLDP loses
        #: it, so a link that went down is still drawn, down; replaced when
        #: another neighbour shows up on the same port.
        self.cabling: Dict[str, Dict[str, Tuple[str, str]]] = {}
        #: When each finding still raised was raised, by (check, node,
        #: subject), and whether that is only when watching began: a finding
        #: already there then has been there since before it.
        self._raised_at: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}
        if history is not None:
            self._load_history(capacity)

    def _load_history(self, capacity: int) -> None:
        """The watched prefixes and the newest changes a previous run kept."""
        history = self.history
        assert history is not None
        try:
            kept = history.changes(limit=capacity)
            watched = history.watched()
        except Exception as exc:  # noqa: BLE001 - a history that cannot be read is no history
            logger.warning("could not read the history in %s: %s", history.path, exc)
            return
        with self._lock:
            for change in reversed(kept):
                self._changes.append(change)
                self._note_finding(change)
            for prefix in watched:
                try:
                    self._watched.add(normalize_prefix(prefix))
                except ValueError:
                    continue
        logger.info("history: %d change(s) and %d watched prefix(es) from %s", len(kept), len(watched), history.path)

    def _persist(self, what: str, call: Callable[[], Any]) -> Any:
        """Run *call* on the history; a disk that fails is a warning, not a stop."""
        if self.history is None:
            return None
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - the timeline in memory goes on
            logger.warning("could not keep %s in %s: %s", what, self.history.path, exc)
            return None

    def watch(self, prefix: str) -> str:
        """Report changes to *prefix* one by one; returns it as tables spell it."""
        normalized = normalize_prefix(prefix)
        with self._lock:
            self._watched.add(normalized)
            watched = set(self._watched)
        self._persist("the watched prefixes", lambda: self.history.set_watched(watched))
        return normalized

    def unwatch(self, prefix: str) -> bool:
        normalized = normalize_prefix(prefix)
        with self._lock:
            if normalized not in self._watched:
                return False
            self._watched.discard(normalized)
            watched = set(self._watched)
        self._persist("the watched prefixes", lambda: self.history.set_watched(watched))
        return True

    def watched(self) -> List[str]:
        with self._lock:
            return sorted(self._watched)

    def learn_cabling(self, state: FabricState) -> bool:
        """Add what LLDP shows now to the cables known; whether anything was new."""
        changed = False
        with self._lock:
            for node, itf, neighbor in state.sub_items("lldp", "neighbors"):
                if neighbor.system_name and neighbor.port_id:
                    cable = (neighbor.system_name, neighbor.port_id)
                    ports = self.cabling.setdefault(node, {})
                    if ports.get(itf.name) != cable:
                        ports[itf.name] = cable
                        changed = True
        return changed

    def load_cabling(self, path: Path) -> None:
        """The cables a previous run of the server learned.

        A server started while a link is down has never seen that link's
        LLDP, and would otherwise draw it nowhere and correlate its two ends
        as two unrelated ports.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            for node, ports in (raw.items() if isinstance(raw, dict) else ()):
                if not isinstance(ports, dict):
                    continue
                for port, cable in ports.items():
                    if isinstance(cable, list) and len(cable) == 2:
                        self.cabling.setdefault(str(node), {}).setdefault(str(port), (str(cable[0]), str(cable[1])))

    def save_cabling(self, path: Path) -> None:
        with self._lock:
            payload = {node: {port: list(cable) for port, cable in ports.items()} for node, ports in self.cabling.items()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("could not keep the learned cabling in %s: %s", path, exc)

    def cables(self, node: str) -> Dict[str, Tuple[str, str]]:
        with self._lock:
            return dict(self.cabling.get(node, {}))

    def _note_finding(self, change: Change) -> None:
        """Follow a finding raised or cleared into :attr:`_raised_at`. Call with the lock held."""
        if change.kind != "finding":
            return
        check, _, subject = change.subject.partition(" ")
        key = (check, change.node, subject)
        if change.after:
            self._raised_at[key] = (change.at, False)
        else:
            self._raised_at.pop(key, None)

    def mark_present(self, findings: Iterable[Finding], since: float) -> None:
        """*findings*, there when watching began at *since*: raised before it, not at it."""
        with self._lock:
            for f in findings:
                self._raised_at.setdefault((f.check, f.node, f.subject), (since, True))

    def raised_at(self) -> Dict[Tuple[str, str, str], Tuple[float, bool]]:
        """When each finding still raised was raised, and whether only 'before' that is known."""
        with self._lock:
            return dict(self._raised_at)

    def record(self, changes: Iterable[Change]) -> None:
        ordered = sorted(changes, key=lambda c: c.at)
        with self._lock:
            for change in ordered:
                self._changes.append(change)
                self._note_finding(change)
        if ordered:
            self._persist("the changes", lambda: self.history.add_changes(ordered))

    def changes(self, since: Optional[float] = None, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """Changes newer than *since* (all kept, if ``None``), newest first.

        fcli's own stopping and starting is about every node, and is listed
        whichever nodes are asked for.
        """
        wanted = set(nodes) if nodes is not None else None
        with self._lock:
            found = [
                c
                for c in self._changes
                if (since is None or c.at >= since)
                and (wanted is None or c.node in wanted or c.kind == SERVER_KIND)
            ]
        found.sort(key=change_order)
        return found

    def recent(self, window: float = FLAP_WINDOW, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """The last *window* seconds of changes, oldest first: what a flap is counted in."""
        found = self.changes(since=time.time() - window, nodes=nodes)
        found.reverse()
        return found

    def set_baseline(self, reading: Optional[Reading] = None, name: Optional[str] = None) -> Optional[Reading]:
        """Keep *reading*, or the latest one, as what the fabric is compared against.

        *name* is what it is kept under on disk, if it is; ``None`` for a
        baseline that lasts as long as the server.
        """
        with self._lock:
            chosen = reading or self.latest
            if chosen is not None:
                self.baseline = chosen
                self.baseline_name = name
        return chosen

    # ------------------------------------------------------------------ #
    # fcli's own comings and goings
    # ------------------------------------------------------------------ #

    def mark_started(self, now: Optional[float] = None) -> None:
        """Put the server starting on the timeline, and a stop it never recorded.

        A server killed rather than stopped leaves no stop behind; the last
        time it was seen running is when it stopped watching, as far as
        anyone can tell, and that is where the gap starts.
        """
        history = self.history
        if history is None:
            return
        now = time.time() if now is None else now
        try:
            seen = history.get_meta("heartbeat")
            clean = history.get_meta("stopped")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read the history in %s: %s", history.path, exc)
            return
        last = float(seen) if seen else None
        changes = []
        if last is not None and not clean:
            changes.append(
                Change(
                    at=last,
                    node=SERVER_NODE,
                    kind=SERVER_KIND,
                    subject="fcli server",
                    before="running",
                    after="stopped",
                    severity=WARNING,
                    detail="last seen running here; it stopped without shutting down, and nothing was watched until it started again",
                )
            )
        gone_since = float(clean) if clean else last
        changes.append(
            Change(
                at=now,
                node=SERVER_NODE,
                kind=SERVER_KIND,
                subject="fcli server",
                before="stopped" if gone_since is not None else "",
                after="running",
                severity=INFO,
                detail=(
                    f"started; not running for {_duration(now - gone_since)}"
                    if gone_since is not None
                    else "started; the history begins here"
                ),
            )
        )
        self.record(changes)
        self._persist("the heartbeat", lambda: (history.set_meta("stopped", None), history.set_meta("heartbeat", str(now))))

    def heartbeat(self, now: Optional[float] = None) -> None:
        """Note that the server is still running, for a start after a crash to know since when it was not."""
        now = time.time() if now is None else now
        self._persist("the heartbeat", lambda: self.history.set_meta("heartbeat", str(now)))

    def mark_stopped(self, now: Optional[float] = None) -> None:
        """Put the server stopping on the timeline."""
        if self.history is None:
            return
        now = time.time() if now is None else now
        self.record(
            [
                Change(
                    at=now,
                    node=SERVER_NODE,
                    kind=SERVER_KIND,
                    subject="fcli server",
                    before="running",
                    after="stopped",
                    severity=INFO,
                    detail="stopped; nothing is watched until it starts again",
                )
            ]
        )
        self._persist("the heartbeat", lambda: self.history.set_meta("stopped", str(now)))

    def prune(self, now: Optional[float] = None) -> int:
        """Drop changes older than the retention from the history on disk."""
        removed = self._persist("the pruning", lambda: self.history.prune(self.retention_days, now=now)) or 0
        if removed:
            logger.info("history: pruned %d change(s) older than %g day(s)", removed, self.retention_days)
        return removed

    def drift(self, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """How the latest reading differs from the baseline, worst first.

        A comparison of two readings says what differs but not since when, so
        each difference is dated by the timeline: the last change to the same
        thing after the baseline was taken, which is when it came to read as it
        does now. One the timeline has no record of - aged out, or summarized
        under another subject - keeps the time of the latest reading.
        """
        baseline, latest = self.baseline, self.latest
        if baseline is None or latest is None or baseline is latest:
            return []
        replayed = baseline.replayed or latest.replayed
        changes = diff_fabric(baseline.state, latest.state, at=latest.at, watched=self.watched()) + diff_findings(
            comparable_findings(baseline.findings, replayed), comparable_findings(latest.findings, replayed), at=latest.at
        )
        for node in sorted(set(baseline.connected) | set(latest.connected)):
            was, now = baseline.connected.get(node), latest.connected.get(node)
            if was is not None and now is not None and was != now:
                changes.append(node_change(node, now, at=latest.at))
        if nodes is not None:
            wanted = set(nodes)
            changes = [c for c in changes if c.node in wanted]
        last: Dict[Tuple[str, str, str], float] = {}
        with self._lock:
            for change in self._changes:
                if change.at >= baseline.at:
                    key = (change.node, change.kind, change.subject)
                    last[key] = max(last.get(key, change.at), change.at)
        changes = [
            replace(c, at=last.get((c.node, c.kind, c.subject), c.at)) for c in changes
        ]
        changes.sort(key=change_order)
        return changes

    def status(self) -> Dict[str, Any]:
        with self._lock:
            count = len(self._changes)
            oldest = self._changes[0].at if self._changes else None
        latest, baseline = self.latest, self.baseline
        status: Dict[str, Any] = {
            "changes": count,
            "oldest": oldest,
            "latest_at": latest.at if latest else None,
            "baseline_at": baseline.at if baseline else None,
            "baseline_name": self.baseline_name,
            "findings": len(latest.findings) if latest else None,
            "incidents": len(latest.incidents) if latest else None,
            "history": None,
        }
        history = self.history
        if history is not None:
            kept = self._persist("the status", history.change_count) or (0, None)
            status["history"] = {
                "path": str(history.path),
                "retention_days": self.retention_days,
                "changes": kept[0],
                "oldest": kept[1],
            }
        return status

    def scoped(self, nodes: Sequence[str]) -> "TimelineView":
        return TimelineView(self, nodes)


class TimelineView:
    """A timeline seen through an inventory filter: only its nodes' changes."""

    def __init__(self, timeline: Timeline, nodes: Sequence[str]) -> None:
        self._timeline = timeline
        self._nodes = list(nodes)

    def changes(self, since: Optional[float] = None) -> List[Change]:
        return self._timeline.changes(since=since, nodes=self._nodes)

    def drift(self) -> List[Change]:
        return self._timeline.drift(nodes=self._nodes)

    def cabling(self) -> Dict[str, Dict[str, Tuple[str, str]]]:
        """Every cable ever seen on the nodes in view, as (far system name, port)."""
        return {node: self._timeline.cables(node) for node in self._nodes}

    def raised_at(self) -> Dict[Tuple[str, str, str], Tuple[float, bool]]:
        """When each finding on the nodes in view was raised; see :meth:`Timeline.raised_at`."""
        wanted = set(self._nodes)
        return {key: when for key, when in self._timeline.raised_at().items() if key[1] in wanted}

    def config_diff(self, node: str, commit: Optional[int] = None, against: Optional[int] = None) -> List[Any]:
        """What *commit* changed in *node*'s configuration, as :class:`~nornir_srl.lenses.ConfigLine` records.

        Raises :class:`ValueError` for a node not in view or a configuration
        that was not kept.
        """
        from ..lenses import ConfigLine  # noqa: PLC0415 - lenses import the timeline's records

        history = self._timeline.history
        if history is None:
            raise ValueError("configurations are only kept when the server runs with a history")
        name = resolve_node(node, self._nodes)
        after = history.config(name, commit)
        if after is None:
            raise ValueError(f"no configuration of {name} kept" + (f" after commit {commit}" if commit is not None else ""))
        if against is not None:
            before = history.config(name, against)
            if before is None:
                raise ValueError(f"no configuration of {name} kept after commit {against}")
        else:
            older = history.latest_config(name, before=after[0].commit_id)
            before = history.config(name, older.commit_id) if older else None
        diff = configs.diff_trees(before[1] if before else None, after[1])
        return [
            ConfigLine(
                node=name,
                commit=after[0].commit_id,
                against=before[0].commit_id if before else None,
                op=op,
                line=line,
                username=after[0].username,
                comment=after[0].comment,
            )
            for op, line in diff.lines
        ]


def resolve_node(name: str, nodes: Sequence[str]) -> str:
    """*name* as one of *nodes*: exactly, or as the end of one - ``leaf1`` for ``clab-dc1-leaf1``."""
    if name in nodes:
        return name
    tails = [n for n in nodes if n.endswith("-" + name)]
    if len(tails) == 1:
        return tails[0]
    raise ValueError(f"no node '{name}' in view" + (f": did you mean {', '.join(tails)}?" if tails else ""))


class Watcher:
    """Reads the fabric every *interval* seconds and keeps the timeline.

    One reading is the reports the checks and the timeline need, rendered
    from the streams the store already holds - no gNMI of its own beyond
    keeping those paths subscribed - then the checks over it and the
    incidents they group into.

    With a history, the watcher also keeps a reading every *persist_every*
    readings for the next start to compare against, reads back the baseline
    someone set before a restart, and keeps every node's configuration as
    it stood after each commit.
    """

    def __init__(
        self,
        store: "FabricStore",
        timeline: Timeline,
        interval: float = 15.0,
        warmup: int = WARMUP_READINGS,
        clock: Callable[[], float] = time.time,
        persist_every: int = PERSIST_EVERY,
    ) -> None:
        self.store = store
        self.timeline = timeline
        self.interval = interval
        self.warmup = warmup
        self.clock = clock
        self.persist_every = max(1, persist_every)
        self.readings = 0
        #: Whether the first reading after the warm-up has been taken.
        self._settled = False
        #: Findings the timeline has reported raised and not yet cleared.
        self._raised: Dict[Any, Finding] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: The latest reading with the gNMI data it was made of, not yet
        #: written: what a clean stop keeps for the next start.
        self._unkept: Optional[Tuple[Reading, Recorder]] = None

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self.timeline.mark_started(self.clock())
        self.timeline.prune(self.clock())
        self._thread = threading.Thread(target=self._run, name="fcli-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            # The reading the next start compares against is the last one
            # taken, or what happened between it and the stop would be
            # reported again as having happened while stopped.
            unkept, self._unkept = self._unkept, None
            if unkept is not None:
                self._keep(LAST_READING, *unkept)
            self.timeline.mark_stopped(self.clock())
            self._thread = None

    #: How long the first reading waits for the dashboards to be read.
    WARM_UP_WAIT = 60.0

    def _run(self) -> None:
        # A node answers one Get at a time: the first reading goes after the
        # dashboards' reports, or its far larger tables keep them empty.
        warmed = getattr(self.store, "warmed", None)
        if warmed is not None:
            deadline = time.monotonic() + self.WARM_UP_WAIT
            while not warmed.wait(0.2) and time.monotonic() < deadline:
                if self._stop.is_set():
                    return
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - one bad reading is not the end of it
                logger.warning("fabric reading failed: %s", exc)
                logger.debug("fabric reading failed", exc_info=exc)
            logger.debug("fabric reading took %.2fs", time.perf_counter() - started)
            if self._stop.wait(self.interval):
                return

    def capture(self, recorder: Optional[Recorder] = None, history: bool = False) -> Reading:
        """Read the fabric once, without recording anything on the timeline.

        A *recorder* writes down the gNMI data the reading is made of, which
        is what keeping it takes.
        """
        store = self.store
        if store.stopping:
            raise RuntimeError("the store is stopping")
        state = store.fabric_state(
            None,
            reading_reports(WATCH_REPORTS),
            history=False,
            watched=self.timeline.watched(),
            recorder=recorder,
        )
        # What the flap check counts in, and the cables the correlation
        # remembers from before a link went down.
        state.changes = self.timeline.recent()
        state.history = self.timeline.scoped(list(state.hostnames))
        findings = run_checks(state)
        return Reading(
            at=self.clock(),
            state=state,
            findings=findings,
            incidents=correlate(findings, state),
            connected={host["name"]: bool(host["connected"]) for host in store.inventory()},
        )

    def tick(self) -> Reading:
        """Take one reading, record what changed since the last, and keep it."""
        store = self.store
        history = self.timeline.history
        settling = not self._settled and self.readings + 1 >= self.warmup
        # Every reading is taken down - the answers are in memory already -
        # and written every persist_every readings, and on a clean stop.
        recorder = Recorder() if history is not None else None
        # Not before settling: until this run has compared with the reading
        # the previous run kept, that reading is not to be replaced.
        keep = settling or (self._settled and (self.readings + 1) % self.persist_every == 0)
        reading = self.capture(recorder)
        state, findings, now = reading.state, reading.findings, reading.at
        if self.timeline.learn_cabling(state) and store.cabling_file is not None:
            self.timeline.save_cabling(store.cabling_file)
        previous = self.timeline.latest
        self.readings += 1
        if previous is not None and self.readings > self.warmup:
            settled, self._raised = settled_findings(self._raised, previous.findings, findings, at=now)
            # An acknowledgement lasts as long as its finding: gone from two
            # readings in a row, the fault is over, and its return is news.
            present = {(f.check, f.node, f.subject) for f in list(previous.findings) + list(findings)}
            for ack in store.acks.expire(store.acks.keys() - present):
                logger.debug("acknowledgement of %s on %s ended: the finding cleared", ack.check, ack.node)
            changes = diff_fabric(previous.state, state, at=now, watched=self.timeline.watched()) + settled
            for node, connected in reading.connected.items():
                was = previous.connected.get(node)
                if was is not None and was != connected:
                    changes.append(node_change(node, connected, at=now))
            changes = self._keep_configs(changes, state, now)
            self.timeline.record(changes)
            if changes:
                logger.debug("fabric reading: %d change(s)", len(changes))
        self.timeline.latest = reading
        if settling:
            self._settled = True
            # What is wrong when the timeline starts is where it starts from,
            # not something that happened.
            self._raised = {(f.check, f.node, f.subject): f for f in findings}
            # Nor does anyone know since when: since before watching began.
            # A finding the history saw raised keeps the time it was raised.
            self.timeline.mark_present(findings, self._watching_since())
            self._settle(reading)
        if recorder is not None and self._settled:
            if keep:
                self._keep(LAST_READING, reading, recorder)
                self._unkept = None
            else:
                self._unkept = (reading, recorder)
        if history is not None:
            self.timeline.heartbeat(now)
            if self.readings % PRUNE_EVERY == 0:
                self.timeline.prune(now)
        return reading

    # ------------------------------------------------------------------ #
    # what survives a restart
    # ------------------------------------------------------------------ #

    def _keep(self, name: str, reading: Reading, recorder: Recorder, note: str = "") -> None:
        """Write *reading*, as *recorder* took it down, to the history under *name*."""
        history = self.timeline.history
        if history is None:
            return
        kept = reading_payload(
            recorder, at=reading.at, state=reading.state, connected=reading.connected, findings=len(reading.findings)
        )
        self.timeline._persist(
            f"the reading '{name}'",
            lambda: history.save_reading(
                name, reading.at, kept, nodes=len(reading.state.hostnames), findings=len(reading.findings), note=note
            ),
        )

    def _watching_since(self) -> float:
        """When this fabric was first watched: the history's oldest change, or this start."""
        with self.timeline._lock:
            oldest = self.timeline._changes[0].at if self.timeline._changes else self.timeline.started
        return min(oldest, self.timeline.started)

    def _settle(self, reading: Reading) -> None:
        """The first reading after the warm-up: catch up with what happened while stopped.

        The reading kept before the server last stopped is compared with this
        one, and what differs is recorded as having happened in between. The
        baseline someone set before the restart is read back; without one,
        this reading is the baseline, as it has always been.
        """
        history = self.timeline.history
        if history is not None:
            self._catch_up(reading)
            self._keep_configs([], reading.state, reading.at, initial=True)
        if self.timeline.baseline is not None:
            return
        name = self.timeline._persist("the baseline name", lambda: history.get_meta("baseline")) if history else None
        if name:
            kept = self.load(name)
            if kept is not None:
                self.timeline.set_baseline(kept, name)
                logger.info("baseline '%s' from %s read back", name, time.strftime("%Y-%m-%d %H:%M", time.localtime(kept.at)))
                return
            logger.warning("baseline '%s' could not be read back; taking a new one", name)
        self.timeline.set_baseline(reading)
        logger.info("baseline taken: %d finding(s) in %d incident(s)", len(reading.findings), len(reading.incidents))

    def load(self, name: str) -> Optional[Reading]:
        """The reading kept under *name*, read back; ``None`` if it is not there or unreadable."""
        history = self.timeline.history
        if history is None:
            return None
        loaded = self.timeline._persist(f"the reading '{name}'", lambda: history.load_reading(name))
        if loaded is None:
            return None
        try:
            kept = replay_reading(loaded[1])
        except Exception as exc:  # noqa: BLE001 - a reading of another format, or damaged
            logger.warning("reading '%s' could not be read back: %s", name, exc)
            return None
        return replace(kept, replayed=True)

    def _catch_up(self, reading: Reading) -> None:
        """Record what differs from the reading kept before this server started."""
        history = self.timeline.history
        assert history is not None
        loaded = self.timeline._persist("the last reading", lambda: history.load_reading(LAST_READING))
        if loaded is None or loaded[0].saved >= self.timeline.started:
            return
        try:
            before = replace(replay_reading(loaded[1]), replayed=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("the reading kept before the restart could not be read back: %s", exc)
            return
        now = reading.at
        changes = diff_fabric(before.state, reading.state, at=now, watched=self.timeline.watched()) + diff_findings(
            comparable_findings(before.findings, True), comparable_findings(reading.findings, True), at=now
        )
        for node, connected in reading.connected.items():
            was = before.connected.get(node)
            if was is not None and was != connected:
                changes.append(node_change(node, connected, at=now))
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(before.at))
        changes = [
            replace(c, detail=f"{WHILE_DOWN} (since {since})" + (f"; {c.detail}" if c.detail else ""))
            for c in changes
        ]
        changes = self._keep_configs(changes, reading.state, now)
        self.timeline.record(changes)
        logger.info("%d change(s) while fcli was not running, since %s", len(changes), since)

    # ------------------------------------------------------------------ #
    # configurations
    # ------------------------------------------------------------------ #

    def _keep_configs(
        self, changes: List[Change], state: FabricState, now: float, initial: bool = False
    ) -> List[Change]:
        """Keep the configuration of every node that committed, and say what each commit did.

        The configuration is read once per node per reading, after the newest
        commit: two commits in one reading are kept as one configuration, the
        newest commit's. *initial* keeps the configuration of every node the
        history has none of yet, or an older one than its newest commit.

        Returns *changes*, with the newest commit of each node told what it
        changed: ``+3 -1 lines``.
        """
        history = self.timeline.history
        if history is None:
            return changes
        newest: Dict[str, Tuple[int, Any]] = {}
        for node, commit in ((n, c) for n, payload in state.reports.get("config_commits", {}).items() for c in as_list(payload)):
            if node not in newest or commit.id > newest[node][0]:
                newest[node] = (commit.id, commit)
        committed = {c.node for c in changes if c.kind == "config"}
        wanted = set()
        for node, (commit_id, _commit) in newest.items():
            if node in committed:
                wanted.add(node)
            elif initial:
                kept = self.timeline._persist("the configurations", lambda n=node: history.latest_config(n))
                if kept is None or kept.commit_id < commit_id:
                    wanted.add(node)
        if not wanted:
            return changes
        summaries: Dict[str, str] = {}
        salt = self.timeline._persist("the salt", history.salt) or ""
        for node in sorted(wanted):
            commit_id, commit = newest[node]
            try:
                tree = self.store.running_config(node, salt=salt)
            except Exception as exc:  # noqa: BLE001 - one node's configuration
                logger.warning("%s: could not read the configuration after commit %s: %s", node, commit_id, exc)
                continue
            previous = self.timeline._persist("the configurations", lambda n=node: history.config(n))
            self.timeline._persist(
                "the configuration",
                lambda n=node, t=tree, i=commit_id, c=commit: history.save_config(
                    n, i, t, at=now, username=c.username, comment=c.comment
                ),
            )
            if previous is not None and previous[0].commit_id < commit_id:
                summaries[node] = configs.diff_trees(previous[1], tree).summary
        out = []
        for change in changes:
            if change.kind == "config" and change.node in summaries and change.subject == f"commit {newest[change.node][0]}":
                change = replace(change, detail=f"{change.detail}; {summaries[change.node]}" if change.detail else summaries[change.node])
            out.append(change)
        return out


__all__ = ["Reading", "Timeline", "TimelineView", "WATCH_REPORTS", "Watcher"]
