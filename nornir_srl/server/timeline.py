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

:class:`Watcher` is the thread that takes the readings.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..changes import (
    FLAP_WINDOW,
    WATCH_REPORTS,
    Change,
    change_order,
    diff_fabric,
    diff_findings,
    node_change,
    normalize_prefix,
    settled_findings,
)
from ..checks import Finding, run_checks
from ..fabric import FabricState
from ..incidents import Incident, correlate
from ..reports import reading_reports

if TYPE_CHECKING:  # pragma: no cover - types only
    from .store import FabricStore

logger = logging.getLogger(__name__)


#: Changes kept, oldest dropped first.
DEFAULT_CAPACITY = 5000

#: Readings taken before the timeline starts recording. The first readings
#: after start-up are paths still being bootstrapped, and comparing them
#: would log the whole fabric as having just appeared.
WARMUP_READINGS = 2


@dataclass
class Reading:
    """The fabric at one moment: its state and what the checks made of it."""

    at: float
    state: FabricState
    findings: List[Finding] = field(default_factory=list)
    incidents: List[Incident] = field(default_factory=list)
    #: Which nodes were answering, by name.
    connected: Dict[str, bool] = field(default_factory=dict)


class Timeline:
    """Changes between readings, the latest reading, and a baseline."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._lock = threading.Lock()
        self._changes: Deque[Change] = deque(maxlen=capacity)
        self.latest: Optional[Reading] = None
        self.baseline: Optional[Reading] = None
        self.started = time.time()
        #: Prefixes whose changes are reported one by one, beyond the defaults
        #: and the system addresses every table has; see
        #: :func:`nornir_srl.changes.diff_fabric`.
        self._watched: Set[str] = set()
        #: Every cable LLDP has shown, by node and local port, as the
        #: (advertised system name, port) on the far end. Kept when LLDP loses
        #: it, so a link that went down is still drawn, down; replaced when
        #: another neighbour shows up on the same port.
        self.cabling: Dict[str, Dict[str, Tuple[str, str]]] = {}

    def watch(self, prefix: str) -> str:
        """Report changes to *prefix* one by one; returns it as tables spell it."""
        normalized = normalize_prefix(prefix)
        with self._lock:
            self._watched.add(normalized)
        return normalized

    def unwatch(self, prefix: str) -> bool:
        normalized = normalize_prefix(prefix)
        with self._lock:
            if normalized not in self._watched:
                return False
            self._watched.discard(normalized)
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

    def record(self, changes: Iterable[Change]) -> None:
        with self._lock:
            for change in sorted(changes, key=lambda c: c.at):
                self._changes.append(change)

    def changes(self, since: Optional[float] = None, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """Changes newer than *since* (all kept, if ``None``), newest first."""
        wanted = set(nodes) if nodes is not None else None
        with self._lock:
            found = [
                c
                for c in self._changes
                if (since is None or c.at >= since) and (wanted is None or c.node in wanted)
            ]
        found.sort(key=change_order)
        return found

    def recent(self, window: float = FLAP_WINDOW, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """The last *window* seconds of changes, oldest first: what a flap is counted in."""
        found = self.changes(since=time.time() - window, nodes=nodes)
        found.reverse()
        return found

    def set_baseline(self, reading: Optional[Reading] = None) -> Optional[Reading]:
        """Keep *reading*, or the latest one, as what the fabric is compared against."""
        with self._lock:
            chosen = reading or self.latest
            if chosen is not None:
                self.baseline = chosen
        return chosen

    def drift(self, nodes: Optional[Iterable[str]] = None) -> List[Change]:
        """How the latest reading differs from the baseline, worst first."""
        baseline, latest = self.baseline, self.latest
        if baseline is None or latest is None or baseline is latest:
            return []
        changes = diff_fabric(baseline.state, latest.state, at=latest.at, watched=self.watched()) + diff_findings(
            baseline.findings, latest.findings, at=latest.at
        )
        for node in sorted(set(baseline.connected) | set(latest.connected)):
            was, now = baseline.connected.get(node), latest.connected.get(node)
            if was is not None and now is not None and was != now:
                changes.append(node_change(node, now, at=latest.at))
        if nodes is not None:
            wanted = set(nodes)
            changes = [c for c in changes if c.node in wanted]
        changes.sort(key=change_order)
        return changes

    def status(self) -> Dict[str, Any]:
        with self._lock:
            count = len(self._changes)
            oldest = self._changes[0].at if self._changes else None
        latest, baseline = self.latest, self.baseline
        return {
            "changes": count,
            "oldest": oldest,
            "latest_at": latest.at if latest else None,
            "baseline_at": baseline.at if baseline else None,
            "findings": len(latest.findings) if latest else None,
            "incidents": len(latest.incidents) if latest else None,
        }

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


class Watcher:
    """Reads the fabric every *interval* seconds and keeps the timeline.

    One reading is the reports the checks and the timeline need, rendered
    from the streams the store already holds - no gNMI of its own beyond
    keeping those paths subscribed - then the checks over it and the
    incidents they group into.
    """

    def __init__(
        self,
        store: "FabricStore",
        timeline: Timeline,
        interval: float = 15.0,
        warmup: int = WARMUP_READINGS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.timeline = timeline
        self.interval = interval
        self.warmup = warmup
        self.clock = clock
        self.readings = 0
        #: Findings the timeline has reported raised and not yet cleared.
        self._raised: Dict[Any, Finding] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="fcli-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
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

    def tick(self) -> Reading:
        """Take one reading, record what changed since the last, and keep it."""
        store = self.store
        if store.stopping:
            raise RuntimeError("the store is stopping")
        state = store.fabric_state(
            None, reading_reports(WATCH_REPORTS), history=False, watched=self.timeline.watched()
        )
        # What the flap check counts in, and the cables the correlation
        # remembers from before a link went down.
        state.changes = self.timeline.recent()
        state.history = self.timeline.scoped(list(state.hostnames))
        if self.timeline.learn_cabling(state) and self.store.cabling_file is not None:
            self.timeline.save_cabling(self.store.cabling_file)
        findings = run_checks(state)
        now = self.clock()
        reading = Reading(
            at=now,
            state=state,
            findings=findings,
            incidents=correlate(findings, state),
            connected={host["name"]: bool(host["connected"]) for host in store.inventory()},
        )
        previous = self.timeline.latest
        self.readings += 1
        if previous is not None and self.readings > self.warmup:
            settled, self._raised = settled_findings(self._raised, previous.findings, findings, at=now)
            # An acknowledgement lasts as long as its finding: gone from two
            # readings in a row, the fault is over, and its return is news.
            present = {(f.check, f.node, f.subject) for f in list(previous.findings) + list(findings)}
            for ack in self.store.acks.expire(self.store.acks.keys() - present):
                logger.debug("acknowledgement of %s on %s ended: the finding cleared", ack.check, ack.node)
            changes = diff_fabric(previous.state, state, at=now, watched=self.timeline.watched()) + settled
            for node, connected in reading.connected.items():
                was = previous.connected.get(node)
                if was is not None and was != connected:
                    changes.append(node_change(node, connected, at=now))
            self.timeline.record(changes)
            if changes:
                logger.debug("fabric reading: %d change(s)", len(changes))
        self.timeline.latest = reading
        if self.timeline.baseline is None and self.readings >= self.warmup:
            # What is wrong when the timeline starts is where it starts from,
            # not something that happened.
            self._raised = {(f.check, f.node, f.subject): f for f in findings}
            self.timeline.set_baseline(reading)
            logger.info("baseline taken: %d finding(s) in %d incident(s)", len(findings), len(reading.incidents))
        return reading


__all__ = ["Reading", "Timeline", "TimelineView", "WATCH_REPORTS", "Watcher"]
