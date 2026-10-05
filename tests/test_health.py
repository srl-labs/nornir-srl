"""The BFD, IGP, platform and optics reports, and the checks that read them.

The payloads are shaped the way SR Linux answers a Get once
:meth:`~nornir_srl.connections.srlinux.SrLinux.get` has unwrapped it: keyed by
the path the device echoes, modules stripped. The BFD, IS-IS and platform ones
were read off a 26.3.1 node (the release recordings replay them too); OSPF is
written from the model, as no recorded fabric runs it.
"""

from typing import Any, Dict, List

import pytest

from nornir_srl.checks import CHECKS_BY_NAME, ERROR, WARNING, FabricState
from nornir_srl.connections.health import HealthMixin
from nornir_srl.records import (
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
)


class Device(HealthMixin):
    """Answers each path from a table, and rejects the ones it does not know."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]) -> None:
        self.responses = responses

    def get(self, paths, datatype="config", strip_mod=True):
        (path,) = paths
        if path == "/platform/fabric[slot=*]" and "/system/features" in self.responses:
            features = self.responses["/system/features"][0]["system/features"]
            assert "chassis" in features, "fabric modules asked of a fixed-form node"
        if path not in self.responses:
            raise Exception(
                f"GRPC ERROR Host: n1, Error: Path not valid - unknown element '{path}'"
            )
        return self.responses[path]


BFD_PATH = "/bfd/network-instance[name=*]/peer"
ISIS_PATH = "/network-instance[name=*]/protocols/isis/instance[name=*]/interface[interface-name=*]"
OSPF_PATH = (
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]"
    "/interface[interface-name=*]"
)


def test_bfd_sessions_are_read_per_instance():
    device = Device(
        {
            BFD_PATH: [
                {
                    "bfd": {
                        "network-instance": [
                            {
                                "name": "default",
                                "peer": [
                                    {
                                        "local-discriminator": 16385,
                                        "ipv6-link-local-interface": "ethernet-1/1.0",
                                        "local-address": "fe80::1",
                                        "remote-address": "fe80::2",
                                        "remote-discriminator": 0,
                                        "subscribed-protocols": "BGP",
                                        "session-state": "DOWN",
                                        "remote-session-state": "DOWN",
                                        "last-state-transition": "2026-09-24T10:26:45.652Z",
                                        "failure-transitions": "0",
                                        "local-diagnostic-code": "NO_DIAGNOSTIC",
                                        "active-transmit-interval": 1000000,
                                    }
                                ],
                            }
                        ]
                    }
                }
            ]
        }
    )
    (instance,) = device.get_bfd()["bfd"]
    (session,) = instance.sessions
    assert instance.ni == "default"
    assert session.state == "down"
    assert session.interface == "ethernet-1/1.0"
    assert session.protocols == ("BGP",)
    assert session.remote_discriminator == 0
    assert session.failures == 0
    assert session.tx_interval == 1000000


def test_isis_adjacencies_hang_off_their_interface():
    device = Device(
        {
            ISIS_PATH: [
                {
                    "network-instance": [
                        {
                            "name": "default",
                            "protocols": {
                                "isis": {
                                    "instance": [
                                        {
                                            "name": "main",
                                            "interface": [
                                                {
                                                    "interface-name": "ethernet-1/3.0",
                                                    "oper-state": "up",
                                                    "circuit-type": "point-to-point",
                                                    "adjacency": [
                                                        {
                                                            "neighbor-system-id": "1920.0003.0153",
                                                            "adjacency-level": "L2",
                                                            "neighbor-hostname": "dcgw3",
                                                            "state": "up",
                                                            "neighbor-ipv4": "10.255.0.3",
                                                            "neighbor-ipv6": "::",
                                                            "up-down-transitions": 2,
                                                        }
                                                    ],
                                                },
                                                {
                                                    "interface-name": "lo1.0",
                                                    "oper-state": "up",
                                                    "passive": True,
                                                    "circuit-type": "broadcast",
                                                },
                                            ],
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            ]
        }
    )
    uplink, loopback = device.get_isis()["isis"]
    (adjacency,) = uplink.adjacencies
    assert (uplink.ni, uplink.instance, uplink.name) == ("default", "main", "ethernet-1/3.0")
    assert adjacency.hostname == "dcgw3" and adjacency.level == "L2"
    # The device fills an address it does not have with the unspecified one.
    assert adjacency.ipv4 == "10.255.0.3" and adjacency.ipv6 == ""
    assert adjacency.transitions == 2
    assert loopback.passive and not loopback.adjacencies


def test_ospf_neighbors_hang_off_their_interface():
    device = Device(
        {
            OSPF_PATH: [
                {
                    "network-instance": [
                        {
                            "name": "default",
                            "protocols": {
                                "ospf": {
                                    "instance": [
                                        {
                                            "name": "main",
                                            "area": [
                                                {
                                                    "area-id": "0.0.0.0",
                                                    "interface": [
                                                        {
                                                            "interface-name": "ethernet-1/1.0",
                                                            "oper-state": "up",
                                                            "interface-type": "point-to-point",
                                                            "neighbor": [
                                                                {
                                                                    "router-id": "10.0.0.2",
                                                                    "address": "10.1.0.2",
                                                                    "adjacency-state": "exstart",
                                                                    "state-changes": 7,
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            ]
        }
    )
    (itf,) = device.get_ospf()["ospf"]
    (neighbor,) = itf.neighbors
    assert (itf.area, itf.interface_type) == ("0.0.0.0", "point-to-point")
    assert (neighbor.router_id, neighbor.state, neighbor.state_changes) == ("10.0.0.2", "exstart", 7)


def test_an_igp_that_is_not_configured_is_an_empty_report():
    """A node without IS-IS answers the Get with nothing; one whose release
    does not know the path rejects it. Neither is a failure."""
    assert Device({ISIS_PATH: []}).get_isis() == {"isis": []}
    assert Device({}).get_ospf() == {"ospf": []}


def test_resources_read_cpu_memory_and_every_datapath_engine():
    device = Device(
        {
            "/platform/control[slot=*]/cpu[index=all]/total": [
                {"platform": {"control": [{"slot": "A", "cpu": [{"index": "all", "total": {"instant": 40, "average-5": 12}}]}]}}
            ],
            "/platform/control[slot=*]/memory": [
                {"platform": {"control": [{"slot": "A", "memory": {"physical": "1000", "free": "250", "utilization": 75}}]}}
            ],
            "/platform/linecard[slot=*]/forwarding-complex[name=*]/datapath": [
                {
                    "platform": {
                        "linecard": [
                            {
                                "slot": 1,
                                "forwarding-complex": [
                                    {
                                        "name": "0",
                                        "datapath": {
                                            "asic": {
                                                "resource": [
                                                    {"name": "ip-lpm-routes", "used-percent": 97, "used-entries": 97, "free-entries": 3},
                                                    # a table this datapath does not have
                                                    {"name": "lag-groups"},
                                                ]
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            ],
        }
    )
    cpu, memory, lpm = device.get_resources()["resources"]
    assert cpu == Resource("control A", "cpu", used_percent=12)
    assert memory == Resource("control A", "memory", used_percent=75, used=750, free=250)
    assert lpm == Resource("linecard 1/0", "ip-lpm-routes", used_percent=97, used=97, free=3)


DATAPATH_PATH = "/platform/linecard[slot=*]/forwarding-complex[name=*]/datapath"
THRESHOLD_PATH = "/platform/resource-monitoring/datapath"

#: The tables an ASIC counts, some of which a container image's virtual
#: datapath lists without counters.
ASIC_TABLES = (
    "arp-nd-entries",
    "direct-ip-next-hops",
    "ecmp-groups",
    "ecmp-members",
    "indirect-ip-next-hops",
    "ip-hosts",
    "mac-addresses",
    "ip-lpm-ipv4-routes",
    "ip-lpm-ipv6-routes",
    "dyn-load-balancing-ecmp-groups",
)


def _datapath(engine: str, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "platform": {
                "linecard": [
                    {"slot": 1, "forwarding-complex": [{"name": "0", "datapath": {engine: {"resource": resources}}}]}
                ]
            }
        }
    ]


def _thresholds(engine: str, values: Dict[str, int], leaf: str = "upper-threshold-set") -> List[Dict[str, Any]]:
    return [
        {
            "platform/resource-monitoring/datapath": {
                engine: {
                    "resource": [
                        {"name": f"srl_nokia-platform-datapath-resources:{name}", leaf: value}
                        for name, value in values.items()
                    ]
                }
            }
        }
    ]


def test_every_table_an_asic_counts_is_a_resource_with_the_node_s_threshold():
    device = Device(
        {
            DATAPATH_PATH: _datapath(
                "asic",
                [{"name": name, "used-percent": 10, "used-entries": 10, "free-entries": 90} for name in ASIC_TABLES],
            ),
            THRESHOLD_PATH: _thresholds("asic", {name: 90 for name in ASIC_TABLES} | {"ip-hosts": 75}),
        }
    )
    resources = device.get_resources()["resources"]
    assert [r.name for r in resources] == list(ASIC_TABLES)
    assert {r.name: r.threshold for r in resources}["ip-hosts"] == 75
    assert all(r.threshold == 90 for r in resources if r.name != "ip-hosts")


def test_a_table_that_reports_only_its_entries_has_its_utilization_worked_out():
    device = Device(
        {
            DATAPATH_PATH: _datapath(
                "asic",
                [
                    {"name": "ecmp-members", "used-entries": 900, "free-entries": 100},
                    {"name": "ip-lpm-ipv4-routes", "used-entries": 0, "free-entries": 0},
                    # listed, not counted: what a container image's datapath does
                    {"name": "dyn-load-balancing-ecmp-groups"},
                ],
            )
        }
    )
    members, routes = device.get_resources()["resources"]
    assert (members.name, members.used_percent) == ("ecmp-members", 90)
    assert (routes.name, routes.used_percent) == ("ip-lpm-ipv4-routes", None)


def test_resource_thresholds_are_read_under_their_older_name_or_not_at_all():
    older = Device(
        {
            DATAPATH_PATH: _datapath("xdp", [{"name": "mac-addresses", "used-percent": 1, "used-entries": 1, "free-entries": 99}]),
            THRESHOLD_PATH: _thresholds("xdp", {"mac-addresses": 85}, leaf="rising-threshold-log"),
        }
    )
    assert older.get_resources()["resources"][0].threshold == 85
    # A node that does not answer for its thresholds still has its tables.
    unknown = Device({DATAPATH_PATH: older.responses[DATAPATH_PATH]})
    assert unknown.get_resources()["resources"][0].threshold is None


def test_fabric_modules_are_only_asked_of_a_modular_chassis():
    """SR Linux gates /platform/fabric on the 'chassis' feature; a fixed-form
    node rejects the path, so it is not asked there at all."""
    fixed = Device({"/system/features": [{"system/features": ["bridged"]}]})
    fixed.get_components()
    modular = Device(
        {
            "/system/features": [{"system/features": ["bridged", "chassis"]}],
            "/platform/fabric[slot=*]": [{"platform": {"fabric": [{"slot": 1, "oper-state": "up"}]}}],
        }
    )
    kinds = [(c.kind, c.id) for c in modular.get_components()["components"]]
    assert ("fabric", "1") in kinds


def test_components_survive_a_chassis_without_fabric_modules():
    device = Device(
        {
            "/system/features": [{"system/features": ["bridged"]}],
            "/platform/control[slot=*]": [{"platform": {"control": [{"slot": "A", "oper-state": "up", "healthz": {"status": "healthy"}}]}}],
            "/platform/linecard[slot=*]": [{"platform": {"linecard": [{"slot": 1, "oper-state": "up", "type": "imm36"}]}}],
            "/platform/fan-tray[id=*]": [{"platform": {"fan-tray": [{"id": 1, "oper-state": "failed"}]}}],
            "/platform/power-supply[id=*]": [{"platform": {"power-supply": [{"id": 1, "oper-state": "empty"}]}}],
        }
    )
    kinds = [(c.kind, c.id, c.oper) for c in device.get_components()["components"]]
    assert kinds == [
        ("control", "A", "up"),
        ("linecard", "1", "up"),
        ("fan-tray", "1", "failed"),
        ("power-supply", "1", "empty"),
    ]


def test_transceivers_leave_out_empty_cages_and_read_their_thresholds():
    device = Device(
        {
            "/interface[name=*]/transceiver": [
                {
                    "interface": [
                        {"name": "ethernet-1/1", "transceiver": {"oper-state": "down", "oper-down-reason": "not-present"}},
                        {
                            "name": "ethernet-1/2",
                            "transceiver": {
                                "oper-state": "up",
                                "form-factor": "QSFP28",
                                "vendor": "NOKIA",
                                "temperature": {"latest-value": 41.5, "high-warning-condition": True},
                                "channel": [
                                    {
                                        "index": 1,
                                        "input-power": {"latest-value": "-17.20", "low-alarm-condition": True},
                                        "output-power": {"latest-value": "-1.10"},
                                    },
                                    {"index": 2, "input-power": {"latest-value": "-2.00"}},
                                ],
                            },
                        },
                    ]
                }
            ]
        }
    )
    (optic,) = device.get_transceivers()["transceivers"]
    assert optic.interface == "ethernet-1/2"
    assert optic.temperature == 41.5
    assert optic.lowest_input_power == -17.2
    assert optic.alarms == ("input-power (lane 1) low",)
    assert optic.warnings == ("temperature high",)


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #


def fabric(**reports: Dict[str, Any]) -> FabricState:
    return FabricState(reports=dict(reports))


def run(check: str, state: FabricState) -> List[Dict[str, Any]]:
    return [f.as_row() for f in CHECKS_BY_NAME[check].run(state)]


def test_a_bfd_session_that_is_not_up_is_an_error_and_says_when_the_far_end_is_silent():
    silent = BfdSession("fe80::1", "fe80::2", "down", interface="ethernet-1/1.0", protocols=("BGP",), remote_discriminator=0)
    flapped = BfdSession("10.0.0.1", "10.0.0.2", "down", protocols=("ISIS",), remote_discriminator=77, failures=3)
    fine = BfdSession("10.0.0.1", "10.0.0.3", "up")
    parked = BfdSession("10.0.0.1", "10.0.0.4", "admin-down")
    rows = run("bfd_down", fabric(bfd={"leaf1": [BfdInstance("default", (silent, flapped, fine, parked))]}))
    assert [(r["Severity"], r["Subject"]) for r in rows] == [
        (ERROR, "default/fe80::2"),
        (ERROR, "default/10.0.0.2"),
    ]
    assert "never answered" in rows[0]["Detail"]
    assert "never answered" not in rows[1]["Detail"]


def test_igp_adjacencies_that_are_not_up_are_errors():
    isis = IsisInterface("default", "main", "ethernet-1/3.0", "up", adjacencies=(
        IsisAdjacency("1920.0003.0153", "dcgw3", "L2", "up"),
        IsisAdjacency("1920.0003.0154", "dcgw4", "L2", "initializing", down_reason="area-mismatch"),
    ))
    ospf = OspfInterface("default", "main", "0.0.0.0", "ethernet-1/1.0", "up", neighbors=(
        OspfNeighbor("10.0.0.2", state="full"),
        OspfNeighbor("10.0.0.3", state="two-way"),
        OspfNeighbor("10.0.0.4", state="exstart"),
    ))
    rows = run("igp_adjacency_down", fabric(isis={"dcgw1": [isis]}, ospf={"leaf1": [ospf]}))
    assert [(r["Node"], r["Subject"]) for r in rows] == [("dcgw1", "ethernet-1/3.0"), ("leaf1", "ethernet-1/1.0")]
    assert "dcgw4" in rows[0]["Detail"] and "area-mismatch" in rows[0]["Detail"]
    assert "10.0.0.4" in rows[1]["Detail"]


def test_an_igp_interface_up_with_no_adjacency_is_a_warning_unless_it_is_meant_to_be():
    lonely = IsisInterface("default", "main", "ethernet-1/5.0", "up")
    passive = IsisInterface("default", "main", "ethernet-1/6.0", "up", passive=True)
    loopback = IsisInterface("default", "main", "system0.0", "up")
    down = IsisInterface("default", "main", "ethernet-1/7.0", "down")
    rows = run("igp_no_adjacency", fabric(isis={"dcgw1": [lonely, passive, loopback, down]}))
    assert [(r["Severity"], r["Subject"]) for r in rows] == [(WARNING, "ethernet-1/5.0")]


def test_resources_warn_at_80_and_error_at_95_percent():
    resources = [
        Resource("control A", "cpu", used_percent=50),
        Resource("control A", "memory", used_percent=85),
        Resource("linecard 1/0", "ip-lpm-routes", used_percent=97, used=97, free=3),
        Resource("linecard 1/0", "mac-addresses"),
    ]
    rows = run("resource_high", fabric(resources={"leaf1": resources}))
    assert [(r["Severity"], r["Subject"]) for r in rows] == [
        (WARNING, "control A memory"),
        (ERROR, "linecard 1/0 ip-lpm-routes"),
    ]


def test_a_forwarding_table_is_warned_about_at_the_node_s_own_threshold():
    resources = [
        # past the node's lowered threshold, short of fcli's own
        Resource("linecard 1/0", "ip-hosts", used_percent=75, used=750, free=250, threshold=70),
        # past fcli's own, short of the node's
        Resource("linecard 1/0", "mac-addresses", used_percent=85, threshold=90),
        # a threshold above fcli's error level moves the error with it
        Resource("linecard 1/0", "ecmp-groups", used_percent=97, threshold=98),
        Resource("linecard 1/0", "ecmp-members", used_percent=99, threshold=98),
        Resource("linecard 1/0", "arp-nd-entries", used_percent=96, threshold=90),
    ]
    rows = run("resource_high", fabric(resources={"leaf1": resources}))
    assert [(r["Severity"], r["Subject"]) for r in rows] == [
        (WARNING, "linecard 1/0 ip-hosts"),
        (ERROR, "linecard 1/0 ecmp-members"),
        (ERROR, "linecard 1/0 arp-nd-entries"),
    ]
    hosts = next(r for r in rows if r["Subject"].endswith("ip-hosts"))
    assert hosts["Detail"] == "75% in use (750 used, 250 free), past the node's 70% threshold"


def test_hardware_that_is_fitted_and_not_working_is_an_error():
    components = [
        Component("control", "A", "up", "healthy"),
        Component("fan-tray", "1", "empty", "unspecified"),
        Component("fan-tray", "2", "failed"),
        Component("power-supply", "1", "up", "unhealthy"),
    ]
    rows = run("hardware_fault", fabric(components={"leaf1": components}))
    assert [r["Subject"] for r in rows] == ["fan-tray 2", "power-supply 1"]


def test_an_optic_reporting_a_crossed_threshold_is_a_finding():
    fading = Transceiver("ethernet-1/2", "up", channels=(TransceiverChannel(1, input_power=-17.2),), alarms=("input-power (lane 1) low",))
    warm = Transceiver("ethernet-1/3", "up", temperature=71.0, warnings=("temperature high",))
    fine = Transceiver("ethernet-1/4", "up")
    rows = run("optic_dom", fabric(transceivers={"leaf1": [fading, warm, fine]}))
    assert [(r["Severity"], r["Subject"]) for r in rows] == [(ERROR, "ethernet-1/2"), (WARNING, "ethernet-1/3")]
    assert "-17.20 dBm" in rows[0]["Detail"]


@pytest.mark.parametrize("check", ["bfd_down", "igp_adjacency_down", "igp_no_adjacency", "resource_high", "hardware_fault", "optic_dom"])
def test_a_fabric_without_the_data_has_nothing_to_report(check):
    assert run(check, fabric()) == []
