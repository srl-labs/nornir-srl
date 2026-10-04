"""The history, from the surfaces that read the fabric once and exit.

The live server keeps the history as it watches. The CLI and the MCP server
have no timeline of their own, but they share the server's history file -
the same file, by the same fabric name - so a baseline marked by an agent
or a script is one the server can compare against, and the configurations
the server kept are there to read without it running.

Everything here takes a Nornir inventory and a
:class:`~nornir_srl.history.HistoryStore`; the surfaces only parse their
arguments and print.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from . import configs
from .changes import WATCH_REPORTS, Change, diff_fabric, diff_findings, node_change
from .checks import run_checks
from .fabric import FabricState, collect_fabric_state
from .history import DEFAULT_RETENTION_DAYS, HistoryStore, SavedReading, default_directory
from .server.readings import Recorder, payload, replay_reading
from .server.timeline import comparable_findings

if TYPE_CHECKING:  # pragma: no cover
    from nornir.core import Nornir


def open_history(fabric: Optional[str], directory: Optional[Any] = None) -> HistoryStore:
    """The history file of the fabric called *fabric*, as the server names it."""
    return HistoryStore.for_fabric(fabric or "fabric", directory or default_directory())


def capture(target: "Nornir") -> Tuple[FabricState, Recorder]:
    """Read the fabric once, with every Get written down."""
    recorder = Recorder()
    state = collect_fabric_state(target, WATCH_REPORTS, recorder=recorder)
    return state, recorder


def keep_baseline(history: HistoryStore, target: "Nornir", name: str = "baseline", note: str = "", activate: bool = True) -> Tuple[SavedReading, FabricState, List[Any]]:
    """Read the fabric and keep the reading under *name*.

    With *activate*, it becomes the baseline the server compares against,
    from its next start or as soon as someone selects it.
    """
    from .server.store import _baseline_name  # noqa: PLC0415 - one rule for names

    name = _baseline_name(name)
    state, recorder = capture(target)
    findings = run_checks(state)
    at = time.time()
    connected = {node: node not in _unreachable(state) for node in target.inventory.hosts}
    kept = history.save_reading(
        name,
        at,
        payload(recorder, at=at, state=state, connected=connected, findings=len(findings)),
        nodes=len(target.inventory.hosts),
        findings=len(findings),
        note=note,
    )
    if activate:
        history.set_meta("baseline", name)
    return kept, state, findings


def drift(
    history: HistoryStore, target: "Nornir", name: Optional[str] = None, watched: Sequence[str] = ()
) -> Tuple[SavedReading, List[Change]]:
    """How the fabric now differs from the baseline kept under *name* (the active one if ``None``).

    Raises :class:`KeyError` when there is no such baseline.
    """
    name = name or history.get_meta("baseline")
    if not name:
        raise KeyError("no baseline is kept: mark one first")
    loaded = history.load_reading(name)
    if loaded is None:
        raise KeyError(f"no baseline '{name}'")
    meta, recorded = loaded
    before = replay_reading(recorded)
    state = collect_fabric_state(target, WATCH_REPORTS)
    now = time.time()
    changes = diff_fabric(before.state, state, at=now, watched=watched) + diff_findings(
        comparable_findings(before.findings, True), comparable_findings(run_checks(state), True), at=now
    )
    unreachable = _unreachable(state)
    for node, was in before.connected.items():
        answering = node not in unreachable
        if node in target.inventory.hosts and was != answering:
            changes.append(node_change(node, answering, at=now))
    return meta, changes


def _unreachable(state: FabricState) -> set:
    from .incidents import unreachable_nodes  # noqa: PLC0415

    return unreachable_nodes(state)


def running_configs(target: "Nornir", salt: str = "") -> Dict[str, Any]:
    """Each node's running configuration now, normalized and redacted; an error string where it failed."""
    from nornir.core.task import Result, Task  # noqa: PLC0415

    from .connections.srlinux import CONNECTION_NAME  # noqa: PLC0415

    def task_func(task: "Task") -> "Result":
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=configs.normalize(device.get(paths=["/"], datatype="config"), salt=salt))

    result = target.run(task=task_func, name="running_config", raise_on_error=False)
    return {
        node: (str(multi[0].exception) if multi.failed else multi[0].result)
        for node, multi in sorted(result.items())
    }


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "capture",
    "drift",
    "keep_baseline",
    "open_history",
    "running_configs",
]
