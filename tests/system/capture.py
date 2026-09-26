"""Record the gNMI exchange of every report against a live SR Linux node.

Run once per release, against a configured fabric::

    python -m tests.system.capture --release 25.3.2 \\
        --node clab-4l2s-l1=leaf --node clab-4l2s-s1=spine

Each node yields one fixture under ``tests/fixtures/releases/<release>/``, which
``tests/test_release_matrix.py`` then replays with no lab present. The point is
to catch the case a unit test cannot see: a path or a YANG structure that a
getter assumes, which a given release does not actually have.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nornir_srl.connections.srlinux import SrLinux
from nornir_srl.reports import REPORTS, ReportSpec

from .replay import (
    FIXTURE_ROOT,
    GetCall,
    Recording,
    ReportRecording,
    deterministic_clock,
    flatten_report,
)

logger = logging.getLogger("capture")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "NokiaSrl1!"
DEFAULT_PORT = 57400

#: Reports with no gNMI exchange of their own to record. ``overview`` and
#: ``topology`` are computed by the server from streamed state; ``checks``
#: correlates the reports it declares, and is recorded through those;
#: ``bgp_received_routes`` makes exactly the gets of the ``bgp_rib_*``
#: reports, and is replayed from theirs.
SKIP = {"overview", "topology", "checks", "bgp_received_routes"}


class RecordingDevice(SrLinux):
    """A real connection that keeps every get it is asked to make."""

    def __init__(self) -> None:
        self.calls: List[GetCall] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        try:
            response = super().get(paths=paths, datatype=datatype, strip_mod=strip_mod)
        except BaseException as exc:
            for path in paths:
                self.calls.append(
                    GetCall(
                        path=path,
                        datatype=datatype or "config",
                        error=str(exc),
                        grpc_code=_grpc_code(exc),
                    )
                )
            raise
        # Recorded as a copy, because several getters enrich the response in
        # place - and not always idempotently. Keeping the reference would
        # record the enriched payload as though the device had sent it, and
        # replaying that would enrich it a second time.
        #
        # The getters ask for one path per get. Should that ever change, the
        # response is the concatenation over the paths, so attributing all of it
        # to the first path and nothing to the rest still replays correctly.
        for index, path in enumerate(paths):
            self.calls.append(
                GetCall(
                    path=path,
                    datatype=datatype or "config",
                    response=copy.deepcopy(response) if index == 0 else [],
                )
            )
        return response

    def take(self) -> List[GetCall]:
        calls, self.calls = self.calls, []
        return calls


def _grpc_code(exc: BaseException) -> Optional[str]:
    """The gRPC status code name behind *exc*, if there is one."""
    candidates: List[Any] = [exc, getattr(exc, "orig_exc", None), exc.__cause__]
    for candidate in candidates:
        code_fn = getattr(candidate, "code", None)
        if callable(code_fn):
            try:
                code = code_fn()
            except Exception:
                continue
            name = getattr(code, "name", None)
            if name:
                return str(name)
    return None


def capture_report(
    device: RecordingDevice,
    spec: ReportSpec,
    now: datetime.datetime,
    ifstats_interval: int,
) -> ReportRecording:
    """Run one report against the device and record what it asked for."""
    device.take()
    params: Dict[str, Any] = {}
    if spec.name == "ifstats":
        params["interval"] = ifstats_interval

    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    getter_error: Optional[str] = None
    try:
        # Under the same pinned clock the replay will use, so that the recorded
        # expiries and rates are reproducible. The sleep between the two ifstats
        # samples is real here - the counters have to actually move.
        with deterministic_clock(now, ifstats_interval, skip_sleep=False):
            result = spec.getter(device, **params) or {}
        columns, rows = flatten_report(spec, result)
    except BaseException as exc:  # noqa: BLE001 - a failure is the finding
        getter_error = f"{type(exc).__name__}: {exc}"
        logger.warning("  %-18s getter raised: %s", spec.name, getter_error)

    recording = ReportRecording(
        report=spec.name,
        resource=spec.resource,
        calls=device.take(),
        columns=columns,
        rows=rows,
        getter_error=getter_error,
    )
    failed = recording.failed_paths
    status = f"{len(rows) if getter_error is None else '-':>5} rows"
    if failed:
        status += f", {len(failed)} path(s) rejected"
    logger.info("  %-18s %s", spec.name, status)
    return recording


def capture_node(
    node: str,
    role: str,
    release: str,
    *,
    port: int,
    username: str,
    password: str,
    ifstats_interval: int,
) -> Recording:
    device = RecordingDevice()
    logger.info("%s (%s) on %s", node, role, release)
    device.open(
        hostname=node,
        username=username,
        password=password,
        port=port,
        platform="srlinux",
    )
    try:
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        recording = Recording(
            release=release,
            node=node,
            role=role,
            captured=now.isoformat(),
            capabilities={
                "supported_models": (device.capabilities or {}).get(
                    "supported_models", []
                )
            },
            ifstats_interval=ifstats_interval,
        )
        for spec in REPORTS:
            if spec.name in SKIP:
                continue
            recording.reports[spec.name] = capture_report(
                device, spec, now, ifstats_interval
            )
        return recording
    finally:
        device.close()


def _parse_node(value: str) -> Tuple[str, str]:
    if "=" in value:
        name, role = value.split("=", 1)
        return name.strip(), role.strip()
    return value.strip(), "unknown"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="SR Linux release captured")
    parser.add_argument(
        "--node",
        required=True,
        action="append",
        metavar="NAME[=ROLE]",
        help="Node to capture; repeat for several",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--ifstats-interval",
        type=int,
        default=2,
        help="Seconds between the two counter samples ifstats needs",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=FIXTURE_ROOT,
        help="Fixture root to write recordings under",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # pygnmi narrates every Get at INFO, which buries the per-report summary.
    logging.getLogger("pygnmi").setLevel(logging.WARNING)

    failures = 0
    for spec in args.node:
        node, role = _parse_node(spec)
        recording = capture_node(
            node,
            role,
            args.release,
            port=args.port,
            username=args.username,
            password=args.password,
            ifstats_interval=args.ifstats_interval,
        )
        path = args.out / args.release / f"{node}.json.gz"
        recording.save(path)
        logger.info("wrote %s", path)
        failures += sum(
            1 for r in recording.reports.values() if r.failed_paths or r.getter_error
        )
    if failures:
        logger.warning("%d report(s) had a rejected path or a getter error", failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
