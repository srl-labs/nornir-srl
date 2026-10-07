"""Getters for what keeps a fabric alive underneath the services.

BFD sessions, IGP adjacencies, the platform's own resources and hardware, and
the optics in its ports: the layer a working overlay quietly depends on, and
the one a fault usually starts in. Each getter returns the records of
:mod:`nornir_srl.records`, like the others.

Most of this state is only there where something is configured - IS-IS on a
node that runs eBGP, transceiver diagnostics in a containerlab port - so an
absent subtree, or one SR Linux does not know the path of, is an empty report
rather than a failure.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..records import (
    BfdInstance,
    BfdSession,
    Component,
    IsisAdjacency,
    IsisInterface,
    OspfInterface,
    OspfNeighbor,
    Resource,
    Transceiver,
    TransceiverChannel,
    as_int,
)
from .helpers import as_list, first_payload
from .routing import _gnmi_path_missing, _suppress_pygnmi_client_logging

logger = logging.getLogger(__name__)


def _float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _enum(value: Any) -> str:
    """A YANG identity or enum as its bare lower-case name."""
    return _text(value).split(":")[-1].lower()


def _branch(node: Any, *names: str) -> Dict[str, Any]:
    """Descend through nested containers, yielding ``{}`` at the first miss."""
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _container(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> List[Dict[str, Any]]:
    return [item for item in as_list(value) if isinstance(item, dict)]


def _payload(resp: Any, *roots: str) -> Any:
    """The part of a Get response under the first of *roots* it has.

    SR Linux echoes the path it answered for as the key, and how much of it
    depends on how the path was asked: a wildcard on ``name`` is dropped
    before the Get, one on any other key is not. The payload is taken under
    whichever spelling came back.
    """
    payload = first_payload(resp)
    for root in roots:
        if root in payload:
            return payload[root]
    if len(payload) == 1:
        return next(iter(payload.values()))
    return {}


class HealthMixin:
    """Getters for BFD, the IGPs, the platform and the optics."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def _get_optional(self, path: str, datatype: str = "state") -> List[Dict[str, Any]]:
        """A Get that answers nothing, rather than raising, where the path is unknown."""
        # pygnmi logs every failed Get as an error before raising it, which
        # for a container this platform does not have is noise.
        with _suppress_pygnmi_client_logging():
            try:
                return self.get(paths=[path], datatype=datatype)
            except BaseException as exc:  # noqa: BLE001 - pygnmi raises bare Exceptions
                if _gnmi_path_missing(exc):
                    return []
                raise

    def _features(self) -> List[str]:
        """What the node advertises under ``/system/features``."""
        payload = first_payload(self.get(paths=["/system/features"], datatype="state"))
        return list(payload.get("system/features") or [])

    # ------------------------------------------------------------------ #
    # bfd
    # ------------------------------------------------------------------ #

    def get_bfd(self) -> Dict[str, Any]:
        resp = self._get_optional("/bfd/network-instance[name=*]/peer")
        root = _payload(resp, "bfd")
        instances = []
        for ni in _dicts(_container(root).get("network-instance")):
            sessions = tuple(_bfd_session(peer) for peer in _dicts(ni.get("peer")))
            instances.append(BfdInstance(ni=_text(ni.get("name")), sessions=sessions))
        return {"bfd": instances}

    # ------------------------------------------------------------------ #
    # igp
    # ------------------------------------------------------------------ #

    def get_isis(self) -> Dict[str, Any]:
        # 'all', so the interface's own config - passive, circuit-type - comes
        # along with the adjacencies it formed.
        resp = self._get_optional(
            "/network-instance[name=*]/protocols/isis/instance[name=*]/interface[interface-name=*]",
            datatype="all",
        )
        interfaces = []
        for ni in _network_instances(_payload(resp, "network-instance")):
            for instance in _dicts(_branch(ni, "protocols", "isis").get("instance")):
                for itf in _dicts(instance.get("interface")):
                    interfaces.append(
                        IsisInterface(
                            ni=_text(ni.get("name")),
                            instance=_text(instance.get("name")),
                            name=_text(itf.get("interface-name")),
                            oper=_enum(itf.get("oper-state")),
                            passive=bool(itf.get("passive")),
                            circuit_type=_enum(itf.get("circuit-type")),
                            adjacencies=tuple(
                                _isis_adjacency(adj) for adj in _dicts(itf.get("adjacency"))
                            ),
                        )
                    )
        return {"isis": interfaces}

    def get_ospf(self) -> Dict[str, Any]:
        resp = self._get_optional(
            "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/interface[interface-name=*]",
            datatype="all",
        )
        interfaces = []
        for ni in _network_instances(_payload(resp, "network-instance")):
            for instance in _dicts(_branch(ni, "protocols", "ospf").get("instance")):
                for area in _dicts(instance.get("area")):
                    for itf in _dicts(area.get("interface")):
                        interfaces.append(
                            OspfInterface(
                                ni=_text(ni.get("name")),
                                instance=_text(instance.get("name")),
                                area=_text(area.get("area-id")),
                                name=_text(itf.get("interface-name")),
                                oper=_enum(itf.get("oper-state")),
                                passive=bool(itf.get("passive")),
                                interface_type=_enum(itf.get("interface-type")),
                                neighbors=tuple(
                                    _ospf_neighbor(nbr) for nbr in _dicts(itf.get("neighbor"))
                                ),
                            )
                        )
        return {"ospf": interfaces}

    # ------------------------------------------------------------------ #
    # platform
    # ------------------------------------------------------------------ #

    def get_resources(self) -> Dict[str, Any]:
        """CPU, memory and datapath table usage, one record per resource."""
        resources: List[Resource] = []
        cpu = _payload(self._get_optional("/platform/control[slot=*]/cpu[index=all]/total"), "platform")
        for control in _dicts(_container(cpu).get("control")):
            slot = _text(control.get("slot"))
            for entry in _dicts(control.get("cpu")):
                total = _branch(entry, "total")
                # The five-minute average: the instant value is too noisy to
                # hold a threshold against.
                used = as_int(total.get("average-5", total.get("instant")))
                resources.append(Resource(f"control {slot}", "cpu", used_percent=used))
        memory = _payload(self._get_optional("/platform/control[slot=*]/memory"), "platform")
        for control in _dicts(_container(memory).get("control")):
            mem = _branch(control, "memory")
            if not mem:
                continue
            physical, free = as_int(mem.get("physical")), as_int(mem.get("free"))
            resources.append(
                Resource(
                    f"control {_text(control.get('slot'))}",
                    "memory",
                    used_percent=as_int(mem.get("utilization")),
                    used=(physical - free) if physical is not None and free is not None else None,
                    free=free,
                )
            )
        datapath = _payload(
            self._get_optional("/platform/linecard[slot=*]/forwarding-complex[name=*]/datapath"),
            "platform",
        )
        thresholds = self._resource_thresholds()
        for card in _dicts(_container(datapath).get("linecard")):
            for complex_ in _dicts(card.get("forwarding-complex")):
                where = f"linecard {_text(card.get('slot'))}/{_text(complex_.get('name'))}"
                # 'asic' on hardware, 'xdp' on the virtual datapath: the same
                # resource list under a different container either way.
                for engine in _branch(complex_, "datapath").values():
                    for item in _dicts(_container(engine).get("resource")):
                        used = as_int(item.get("used-percent"))
                        entries, free = as_int(item.get("used-entries")), as_int(item.get("free-entries"))
                        if used is None and entries is None:
                            continue  # a table this datapath does not count
                        if used is None and free is not None and entries + free > 0:
                            # Some tables report their entries and no
                            # percentage; a table without one could never
                            # be found close to full.
                            used = (entries * 100) // (entries + free)
                        name = _enum(item.get("name"))
                        resources.append(
                            Resource(
                                where,
                                name,
                                used_percent=used,
                                used=entries,
                                free=free,
                                threshold=thresholds.get(name),
                            )
                        )
        return {"resources": resources}

    def _resource_thresholds(self) -> Dict[str, int]:
        """Datapath table -> the utilization the node raises its own alarm at.

        Configuration with its defaults, so asked as ``all``: a ``config``
        Get answers only what someone configured. Releases before the
        ``upper-threshold-set`` leaf called it ``rising-threshold-log``.
        What the thresholds add is where to warn; a node that does not
        answer for them still has its tables reported, warned about at
        fcli's own threshold.
        """
        try:
            payload = _payload(self._get_optional("/platform/resource-monitoring/datapath", "all"), "platform")
        except Exception as exc:  # noqa: BLE001 - only an enrichment of the tables
            logger.debug("resource thresholds not read: %s", exc)
            return {}
        thresholds: Dict[str, int] = {}
        for engine in _container(payload).values():
            for item in _dicts(_container(engine).get("resource")):
                value = as_int(item.get("upper-threshold-set", item.get("rising-threshold-log")))
                if value is not None:
                    thresholds[_enum(item.get("name"))] = value
        return thresholds

    def get_components(self) -> Dict[str, Any]:
        """Control and line cards, fabric modules, fans and power supplies.

        Fabric modules exist only on a modular chassis: SR Linux gates the
        whole ``/platform/fabric`` list on the ``chassis`` feature, and a
        fixed-form node rejects the path. So it is only asked where the node
        says it is one.
        """
        components: List[Component] = []
        paths = [p for p in _COMPONENT_PATHS if p[0] != "fabric" or "chassis" in self._features()]
        for kind, path, key in paths:
            if kind in _CARD_LEAVES_ONLY:
                items = self._card_leaves(kind, path, key)
            else:
                items = _dicts(_container(_payload(self._get_optional(path), "platform")).get(kind))
            for item in items:
                components.append(
                    Component(
                        kind=kind,
                        id=_text(item.get(key)),
                        oper=_enum(item.get("oper-state")),
                        health=_enum(_branch(item, "healthz").get("status")),
                        type=_text(item.get("type")),
                        serial_number=_text(item.get("serial-number")),
                    )
                )
        return {"components": components}

    def _card_leaves(self, kind: str, path: str, key: str) -> List[Dict[str, Any]]:
        """The leaves of each card a component is made of, card by card.

        Not the card's subtree: a line card's holds the forwarding tables,
        a hundred thousand leaves on an EVPN fabric of a thousand services,
        which the server would otherwise be sent again every sample.
        """
        cards: Dict[str, Dict[str, Any]] = {}
        for leaf in _CARD_LEAVES:
            payload = _payload(self._get_optional(f"{path}/{leaf}", datatype="all"), "platform")
            for item in _dicts(_container(payload).get(kind)):
                cards.setdefault(_text(item.get(key)), {}).update(item)
        return list(cards.values())

    def get_transceivers(self) -> Dict[str, Any]:
        resp = self._get_optional("/interface[name=*]/transceiver")
        found = []
        for itf in _dicts(_payload(resp, "interface")):
            optic = _branch(itf, "transceiver")
            reason = _enum(optic.get("oper-down-reason"))
            if not optic or reason == "not-present":
                continue
            found.append(_transceiver(_text(itf.get("name")), optic))
        return {"transceivers": found}


#: What a component is read as: whether it is up and healthy, and what it is.
_CARD_LEAVES: Tuple[str, ...] = ("oper-state", "healthz", "type", "serial-number")

#: The components read leaf by leaf rather than whole; see _card_leaves.
_CARD_LEAVES_ONLY = frozenset({"linecard"})

#: Hardware lists, with the key each is indexed by. ``fabric`` is asked only
#: of a modular chassis; see :meth:`HealthMixin.get_components`.
_COMPONENT_PATHS: Tuple[Tuple[str, str, str], ...] = (
    ("control", "/platform/control[slot=*]", "slot"),
    ("linecard", "/platform/linecard[slot=*]", "slot"),
    ("fabric", "/platform/fabric[slot=*]", "slot"),
    ("fan-tray", "/platform/fan-tray[id=*]", "id"),
    ("power-supply", "/platform/power-supply[id=*]", "id"),
)

#: The DOM measurements a transceiver and its channels carry thresholds for.
_DOM_MEASURES = ("temperature", "voltage", "input-power", "output-power", "laser-bias-current")


def _network_instances(payload: Any) -> List[Dict[str, Any]]:
    return _dicts(payload)


def _bfd_session(peer: Dict[str, Any]) -> BfdSession:
    return BfdSession(
        local_address=_text(peer.get("local-address")),
        remote_address=_text(peer.get("remote-address")),
        state=_enum(peer.get("session-state")),
        remote_state=_enum(peer.get("remote-session-state")),
        remote_discriminator=as_int(peer.get("remote-discriminator")),
        interface=_text(peer.get("ipv6-link-local-interface")),
        protocols=tuple(
            p for p in (_text(v).upper() for v in _protocols(peer.get("subscribed-protocols"))) if p
        ),
        last_transition=_text(peer.get("last-state-transition")),
        failures=as_int(peer.get("failure-transitions")) or 0,
        local_diagnostic=_enum(peer.get("local-diagnostic-code")),
        remote_diagnostic=_enum(peer.get("remote-diagnostic-code")),
        tx_interval=as_int(peer.get("active-transmit-interval")),
        rx_interval=as_int(peer.get("active-receive-interval")),
        multiplier=as_int(peer.get("remote-multiplier")),
    )


def _protocols(value: Any) -> Iterable[str]:
    """``subscribed-protocols`` is a space-separated string or a list of them."""
    for item in as_list(value):
        yield from str(item).replace(",", " ").split()


def _isis_adjacency(adj: Dict[str, Any]) -> IsisAdjacency:
    return IsisAdjacency(
        system_id=_text(adj.get("neighbor-system-id")),
        hostname=_text(adj.get("neighbor-hostname")),
        level=_enum(adj.get("adjacency-level")).upper(),
        state=_enum(adj.get("state")),
        down_reason=_enum(adj.get("down-reason")),
        ipv4=_address(adj.get("neighbor-ipv4")),
        ipv6=_address(adj.get("neighbor-ipv6")),
        last_transition=_text(adj.get("last-up-down-transition")),
        transitions=as_int(adj.get("up-down-transitions")) or 0,
    )


def _address(value: Any) -> str:
    """An address leaf, empty where the device fills it with the unspecified one."""
    text = _text(value)
    return "" if text in ("::", "0.0.0.0") else text


def _ospf_neighbor(nbr: Dict[str, Any]) -> OspfNeighbor:
    return OspfNeighbor(
        router_id=_text(nbr.get("router-id")),
        address=_text(nbr.get("address")),
        state=_enum(nbr.get("adjacency-state")),
        priority=as_int(nbr.get("priority")),
        last_established=_text(nbr.get("last-established-time")),
        state_changes=as_int(nbr.get("state-changes")) or 0,
    )


def _crossed(container: Dict[str, Any], measure: str) -> Tuple[List[str], List[str]]:
    """The alarm and warning thresholds a DOM measurement reports as crossed."""
    alarms, warnings = [], []
    for edge in ("high", "low"):
        if container.get(f"{edge}-alarm-condition") is True:
            alarms.append(f"{measure} {edge}")
        elif container.get(f"{edge}-warning-condition") is True:
            warnings.append(f"{measure} {edge}")
    return alarms, warnings


def _transceiver(name: str, optic: Dict[str, Any]) -> Transceiver:
    alarms: List[str] = []
    warnings: List[str] = []
    for measure in _DOM_MEASURES:
        found = _crossed(_branch(optic, measure), measure)
        alarms += found[0]
        warnings += found[1]
    channels = []
    for channel in _dicts(optic.get("channel")):
        index = as_int(channel.get("index")) or 0
        for measure in ("input-power", "output-power", "laser-bias-current"):
            found = _crossed(_branch(channel, measure), f"{measure} (lane {index})")
            alarms += found[0]
            warnings += found[1]
        channels.append(
            TransceiverChannel(
                index=index,
                input_power=_float(_branch(channel, "input-power").get("latest-value")),
                output_power=_float(_branch(channel, "output-power").get("latest-value")),
                laser_bias=_float(_branch(channel, "laser-bias-current").get("latest-value")),
            )
        )
    return Transceiver(
        interface=name,
        oper=_enum(optic.get("oper-state")),
        down_reason=_enum(optic.get("oper-down-reason")),
        form_factor=_enum(optic.get("form-factor")),
        pmd=_enum(optic.get("ethernet-pmd")),
        vendor=_text(optic.get("vendor")),
        part_number=_text(optic.get("vendor-part-number")),
        serial_number=_text(optic.get("serial-number")),
        temperature=_float(_branch(optic, "temperature").get("latest-value")),
        voltage=_float(_branch(optic, "voltage").get("latest-value")),
        channels=tuple(channels),
        alarms=tuple(dict.fromkeys(alarms)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
