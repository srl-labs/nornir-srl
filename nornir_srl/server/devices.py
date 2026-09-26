"""Device facades that feed the existing report getters from streamed state.

The getters in :mod:`nornir_srl.connections` only ever touch two things on the
device object: ``self.get(paths=..., datatype=...)`` and ``self.capabilities``.
That makes it possible to run every existing report against a different data
source simply by swapping the ``get`` implementation:

* :class:`RecordingDevice` proxies to the real gNMI connection and records the
  paths a report asks for. That is how the server learns which paths to
  subscribe to, without duplicating the path definitions.
* :class:`CachedDevice` answers from the streamed state tree, falling back to a
  TTL-cached gNMI ``Get`` for paths that could not be subscribed.
* :class:`DirectDevice` answers with those Gets only, for paths asked for by
  key that nothing subscribes to.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..connections.down_reason import clean_leaf
from ..connections.health import HealthMixin
from ..connections.ifstats import InterfaceStatsMixin
from ..connections.interfaces import NetworkInstanceMixin
from ..connections.layer2 import Layer2Mixin
from ..connections.neighbor_discovery import NeighborDiscoveryMixin
from ..connections.routing import RoutingMixin
from ..connections.system import SystemMixin
from ..records import InterfaceStats
from ..connections.routing import _suppress_pygnmi_client_logging
from .stream import HostStream

logger = logging.getLogger(__name__)


class MixinDevice(
    NetworkInstanceMixin,
    RoutingMixin,
    Layer2Mixin,
    NeighborDiscoveryMixin,
    SystemMixin,
    InterfaceStatsMixin,
    HealthMixin,
):
    """All report getters, with ``get``/``capabilities`` supplied by subclasses."""

    capabilities: Optional[Dict[str, Any]] = None

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class RecordingDevice(MixinDevice):
    """Real device proxy that records every path a report getter requests.

    *getter* redirects the Gets themselves, which is how the server runs
    discovery through a :class:`~nornir_srl.server.stream.HostStream`: they then
    share the one-Get-at-a-time lock and the failure accounting of every other
    Get made against the node, instead of going straight to the connection.
    """

    def __init__(
        self,
        device: Any,
        getter: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
    ) -> None:
        self._device = device
        self._getter = getter
        self.capabilities = getattr(device, "capabilities", None)
        self.recorded: List[Tuple[str, str]] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        for path in paths:
            entry = (path, datatype or "config")
            if entry not in self.recorded:
                self.recorded.append(entry)
        if self._getter is None:
            with _suppress_pygnmi_client_logging():
                return self._device.get(
                    paths=paths, datatype=datatype, strip_mod=strip_mod
                )
        result: List[Dict[str, Any]] = []
        for path in paths:
            result.extend(self._getter(path, datatype or "config"))
        return result


class DirectDevice(MixinDevice):
    """Report getters answered by gNMI Gets alone, never from the streamed state.

    For what a reading asks for by key - one prefix, one next-hop-group, one
    VRF's route table - and does not subscribe to. Served from the streamed
    tree, such a path would be borrowed from whatever else is streamed under
    the same root, and answer with that rather than with what the node has.
    """

    def __init__(self, stream: HostStream) -> None:
        self.stream = stream
        self.capabilities = getattr(stream.device, "capabilities", None)

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for path in paths:
            result.extend(self.stream.direct_get(path, datatype or "config"))
        return result


class CachedDevice(MixinDevice):
    """Report getters served from a :class:`~nornir_srl.server.stream.HostStream`."""

    def __init__(self, stream: HostStream) -> None:
        self.stream = stream
        self.capabilities = getattr(stream.device, "capabilities", None)

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for path in paths:
            snapshot = self.stream.snapshot(path)
            if snapshot is None:
                snapshot = self.stream.direct_get(path, datatype or "config")
            result.extend(snapshot)
        return result

    def get_ifstats(self, interface: str = "*", interval: int = 5) -> Dict[str, Any]:
        """Interface rates derived from the streamed counter samples.

        Unlike the CLI implementation this does not take two samples of its own:
        the subscription already delivers a fresh sample every interval, and
        :class:`~nornir_srl.server.stream.RateTracker` keeps the derived rates
        and the per-sample counts up to date. The port state streams alongside
        the counters, so the records carry it too.
        """
        records: List[InterfaceStats] = []
        for name in self.stream.interfaces():
            if interface not in ("*", name):
                continue
            itf = self.stream.interface_state(name)
            stats = itf.get("statistics") or {}
            if not stats:
                continue
            rates = self.stream.rates.rates(name)
            deltas = self.stream.rates.deltas(name)

            def _counter(key: str) -> int:
                try:
                    return int(stats.get(key, 0))
                except (TypeError, ValueError):
                    return 0

            records.append(
                InterfaceStats(
                    name=name,
                    oper=str(itf.get("oper-state", "-")),
                    # Why a port is down is what tells an idle interface that is
                    # meant to be idle - a standby ethernet-segment member -
                    # from one that is not.
                    down_reason=clean_leaf(itf.get("oper-down-reason")),
                    in_kbps=round(rates.get("in-octets", 0.0) * 8 / 1000, 1),
                    out_kbps=round(rates.get("out-octets", 0.0) * 8 / 1000, 1),
                    in_pps=round(rates.get("in-packets", 0.0), 1),
                    out_pps=round(rates.get("out-packets", 0.0), 1),
                    in_errors=deltas.get("in-error-packets", 0),
                    out_errors=deltas.get("out-error-packets", 0),
                    in_discards=deltas.get("in-discarded-packets", 0),
                    out_discards=deltas.get("out-discarded-packets", 0),
                    in_packets=_counter("in-packets"),
                    out_packets=_counter("out-packets"),
                    in_octets=_counter("in-octets"),
                    out_octets=_counter("out-octets"),
                )
            )
        return {"ifstats": records}
