"""Tests for the fcli server store and its HTTP API."""

import json
import os
import re
import tempfile
import threading
import time

import pytest
import yaml
from nornir import InitNornir
from starlette.testclient import TestClient

from nornir_srl.checks import CHECKS_COLUMNS, REQUIRED_REPORTS
from nornir_srl.lenses import get_lens, lenses_for
from nornir_srl.reports import SERVER, get_report, reports_for
from nornir_srl.rows import flatten, get_fields, is_scalar
from nornir_srl.server.app import (
    ASSET_TOKEN,
    STATIC_DIR,
    asset_version,
    create_app,
    parse_kv,
    table_digest,
    table_events,
)
from nornir_srl.server.store import FabricStore

from .fakes import (
    BGP_ATTR_PATH,
    BGP_ATTR_RESPONSE,
    ES_PATH,
    HOSTNAME_PATH,
    IFADMIN_PATH,
    IFADMIN_RESPONSE,
    IFSTATE_PATH,
    IFSTATE_RESPONSE,
    IFSTATS_PATH,
    IFSTATS_RESPONSE,
    IPV4_RIB_PATH,
    IPV4_RIB_RESPONSE,
    RIB_PATH,
    RIB_RESPONSE,
    LLDP_PATH,
    LLDP_RESPONSE,
    NH_PATH,
    NH_RESPONSE,
    NHGROUP_PATH,
    NHGROUP_RESPONSE,
    HEALTH_RESPONSES,
    SYS_INFO_RESPONSES,
    FakeDevice,
    es_response,
    hostname_response,
    wait_for,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


HOSTS = {
    "leaf1": {"hostname": "leaf1", "platform": "srlinux", "data": {"role": "leaf"}},
    "spine1": {"hostname": "spine1", "platform": "srlinux", "data": {"role": "spine"}},
}

#: The segment both fake nodes report on lag1, as a multi-homing pair does.
ES_ESI = "00:00:00:00:01:01:01:01:01:01"


def _responses(name="leaf1"):
    return {
        LLDP_PATH: LLDP_RESPONSE,
        # Reports gate the bridged, EVPN and VXLAN paths on this, as a node
        # that does none of them does not implement them.
        "/system/features": [{"system/features": ["bridged", "evpn", "vxlan"]}],
        "/tunnel-interface[name=*]/vxlan-interface": [{"tunnel-interface": []}],
        HOSTNAME_PATH: hostname_response(name),
        ES_PATH: es_response("mh-1", ES_ESI, "lag1"),
        IFSTATS_PATH: IFSTATS_RESPONSE,
        IFSTATE_PATH: IFSTATE_RESPONSE,
        IFADMIN_PATH: IFADMIN_RESPONSE,
        RIB_PATH: RIB_RESPONSE,
        BGP_ATTR_PATH: BGP_ATTR_RESPONSE,
        IPV4_RIB_PATH: IPV4_RIB_RESPONSE,
        NHGROUP_PATH: NHGROUP_RESPONSE,
        NH_PATH: NH_RESPONSE,
        "/network-instance[name=*]/protocols/bgp/neighbor": [
            {
                "network-instance": [
                    {
                        "name": "default",
                        "protocols": {
                            "bgp": {
                                "neighbor": [
                                    {
                                        "peer-address": "192.168.1.1",
                                        "session-state": "established",
                                    }
                                ]
                            }
                        },
                    }
                ]
            }
        ],
        **SYS_INFO_RESPONSES,
        **HEALTH_RESPONSES,
    }


@pytest.fixture
def fabric(monkeypatch):
    """A two-node Nornir inventory whose connections are FakeDevices."""
    with tempfile.TemporaryDirectory() as tmp:
        host_file = os.path.join(tmp, "hosts.yml")
        group_file = os.path.join(tmp, "groups.yml")
        with open(host_file, "w") as handle:
            yaml.safe_dump(HOSTS, handle)
        with open(group_file, "w") as handle:
            yaml.safe_dump({}, handle)
        nornir = InitNornir(
            inventory={
                "plugin": "SimpleInventory",
                "options": {"host_file": host_file, "group_file": group_file},
            },
            runner={"plugin": "serial"},
            logging={"enabled": False},
        )
        devices = {name: FakeDevice(_responses(name)) for name in HOSTS}
        monkeypatch.setattr(
            "nornir.core.inventory.Host.get_connection",
            lambda self, name, config: devices[self.name],
        )
        yield nornir, devices


@pytest.fixture
def store(fabric):
    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    yield store, devices
    store.stop()


@pytest.fixture
def client(fabric, tmp_path):
    nornir, devices = fabric
    app = create_app(
        nornir,
        resync_interval=0,
        refresh=0.5,
        restart_debounce=0.02,
        snapshot_dir=tmp_path / "snapshots",
    )
    with TestClient(app) as test_client:
        yield test_client, devices


# --------------------------------------------------------------------------- #
# row flattening
# --------------------------------------------------------------------------- #


def test_is_scalar():
    assert is_scalar("up")
    assert is_scalar(3)
    assert is_scalar(["a", "b"])
    assert not is_scalar([{"a": 1}])
    assert not is_scalar({"a": 1})


def test_get_fields_sorts_nested_fields():
    item = {"NI": "default", "Rib": [{"b": 1, "a": 2}]}
    assert get_fields(item) == ["NI", "a", "b"]


def test_flatten_expands_nested_lists_into_rows():
    items = [
        {"interface": "ethernet-1/1", "Neighbors": [{"Nbr-System": "spine1"}]},
        {"interface": "ethernet-1/2", "Neighbors": [{"Nbr-System": "spine2"}]},
    ]
    columns, rows = flatten("leaf1", items)
    assert columns == ["interface", "Nbr-System"]
    assert rows == [
        {"Node": "leaf1", "interface": "ethernet-1/1", "Nbr-System": "spine1"},
        {"Node": "leaf1", "interface": "ethernet-1/2", "Nbr-System": "spine2"},
    ]


def test_get_fields_unions_the_fields_of_every_sub_item():
    """A route carries ``orig-vrf`` only when it is leaked, so the first one
    cannot be trusted to name the columns of the rest."""
    item = {"NI": "default", "Rib": [{"Prefix": "10.0.0.0/8"}, {"orig-vrf": "blue"}]}
    assert get_fields(item) == ["NI", "Prefix", "orig-vrf"]


def test_flatten_takes_columns_from_every_item():
    """An item whose nested list is empty must not decide the columns alone.

    A node with an IPv6 route table only in one network-instance used to lose
    every route column, because the first network-instance had no routes to
    name them after. ``Rib`` names that list, so it groups the rows rather than
    being a column of its own.
    """
    items = [
        {"NI": "default", "Rib": None},
        {"NI": "all-rails", "Rib": [{"Prefix": "fd00::/64", "type": "bgp"}]},
    ]
    columns, rows = flatten("leaf1", items)
    assert columns == ["NI", "Prefix", "type"]
    assert rows == [
        {"Node": "leaf1", "NI": "default"},
        {"Node": "leaf1", "NI": "all-rails", "Prefix": "fd00::/64", "type": "bgp"},
    ]


def test_flatten_keeps_a_sub_item_key_no_item_fills():
    """With no routes anywhere, nothing shows that ``Rib`` holds a list.

    The server drops it once another node reports routes; on its own a node
    cannot tell this apart from a field it has no value for.
    """
    columns, rows = flatten("leaf1", [{"NI": "default", "Rib": None}])
    assert columns == ["NI", "Rib"]
    assert rows == [{"Node": "leaf1", "NI": "default"}]


def test_flatten_handles_flat_items():
    columns, rows = flatten("leaf1", [{"type": "7220 IXR-D2L", "uptime": 10}])
    assert columns == ["type", "uptime"]
    assert rows == [{"Node": "leaf1", "type": "7220 IXR-D2L", "uptime": 10}]


def test_flatten_of_empty_result():
    assert flatten("leaf1", []) == ([], [])


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_store_connects_to_every_node(store):
    fabric_store, _devices = store
    inventory = fabric_store.inventory()
    assert {h["name"] for h in inventory} == set(HOSTS)


def test_store_stop_closes_host_connections(fabric):
    nornir, devices = fabric
    fabric_store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    fabric_store.start()
    fabric_store.stop()
    assert fabric_store._stop.is_set()
    fabric_store.stop()  # a second Ctrl+C / lifespan must not raise
    table = fabric_store.table(get_report("lldp"))
    assert table["rows"] == []
    assert table["errors"] == []


def test_table_renders_rows_for_all_nodes(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("lldp"))
    assert table["columns"] == [
        "Node",
        "interface",
        "Nbr-System",
        "Nbr-port",
        "Nbr-port-desc",
    ]
    assert table["nodes"] == 2
    assert len(table["rows"]) == 4
    assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    assert table["errors"] == []


def test_table_subscribes_to_the_paths_the_report_needs(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(d.subscribe_requests for d in devices.values()))
    for device in devices.values():
        paths = [s["path"] for s in device.subscribe_requests[-1]["subscription"]]
        # LLDP streams ON_CHANGE, so the heartbeat comes with it.
        assert paths == [LLDP_PATH, "/system/information/current-datetime"]


def test_bgp_rib_reflects_streamed_best_route_changes(store):
    fabric_store, devices = store
    fabric_store.table(get_report("bgp_rib_evpn_2"))
    assert wait_for(lambda: devices["leaf1"].subscribe_requests)

    def route_state():
        rows = fabric_store.table(get_report("bgp_rib_evpn_2"))["rows"]
        leaf_rows = [r for r in rows if r["Node"] == "leaf1"]
        return leaf_rows[0]["st"] if leaf_rows else None

    assert route_state() == "u*"
    devices["leaf1"].push(
        "",
        [
            (
                "network-instance[name=default]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/"
                "rib-in-out/rib-in-post/mac-ip-route[path-id=0]/best-route",
                True,
            )
        ],
    )
    assert wait_for(lambda: route_state() == "u*>")


def test_paths_are_discovered_once_per_node_and_report(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    after_first = len(devices["leaf1"].subscribe_requests)
    for _ in range(3):
        fabric_store.table(get_report("lldp"))
    assert len(devices["leaf1"].subscribe_requests) == after_first


def test_table_reflects_streamed_updates(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )

    def updated():
        rows = fabric_store.table(get_report("lldp"))["rows"]
        return any(row["Nbr-System"] == "spine9" for row in rows)

    assert wait_for(updated)


def test_inventory_filter_narrows_the_nodes(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("lldp"), {"role": "leaf"})
    assert table["nodes"] == 1
    assert {row["Node"] for row in table["rows"]} == {"leaf1"}


def test_report_failure_on_one_node_is_reported_not_raised(store):
    fabric_store, devices = store
    devices["spine1"].responses.pop(LLDP_PATH)
    table = fabric_store.table(get_report("lldp"))
    assert [e["node"] for e in table["errors"]] == ["spine1"]
    assert {row["Node"] for row in table["rows"]} == {"leaf1"}


def test_sys_info_report_uses_multiple_paths(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("sys_info"))
    assert len(table["rows"]) == 2
    row = table["rows"][0]
    assert row["type"] == "7220 IXR-D2L"
    assert row["software-version"] == "24.10.1"


def test_the_rib_report_is_the_whole_route_table_without_an_address(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("ipv4_rib"))
    prefixes = {row["Prefix"] for row in table["rows"]}
    assert prefixes == {"10.0.0.0/8", "10.1.0.0/16", "10.1.1.0/24"}


def test_the_rib_report_narrows_to_the_prefix_an_address_falls_into(store):
    """The server's answer to the CLI's ``fcli ipv4-rib -a``.

    Of the three nested prefixes the node carries, a lookup keeps the one it
    would actually forward on.
    """
    fabric_store, _devices = store
    table = fabric_store.table(
        get_report("ipv4_rib"), None, {"address": "10.1.1.55"}
    )
    assert {row["Prefix"] for row in table["rows"]} == {"10.1.1.0/24"}
    assert table["errors"] == []
    # Narrowed, not shortened: the route still comes with its next-hop.
    assert table["rows"][0]["next-hop"] == "10.10.10.1"


def test_an_address_in_no_prefix_narrows_the_rib_report_to_nothing(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("ipv4_rib"), None, {"address": "9.9.9.9"})
    assert table["rows"] == []
    assert table["errors"] == []


def test_narrowing_the_rib_report_leaves_the_next_render_the_whole_table(store):
    """One report rendered two ways at once: the lookups must not interfere.

    Every render reads the same streamed state, and two browsers can be
    watching this report with different addresses, or none.
    """
    fabric_store, _devices = store
    fabric_store.table(get_report("ipv4_rib"), None, {"address": "10.1.1.55"})
    table = fabric_store.table(get_report("ipv4_rib"))
    assert {row["Prefix"] for row in table["rows"]} == {
        "10.0.0.0/8",
        "10.1.0.0/16",
        "10.1.1.0/24",
    }


def test_status_lists_subscriptions(store):
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    status = fabric_store.status()
    assert status["subscriptions"] == 2
    assert {node["node"] for node in status["nodes"]} == set(HOSTS)


# --------------------------------------------------------------------------- #
# gNMI session budget
# --------------------------------------------------------------------------- #


def test_repeated_renders_do_not_hit_the_node_again(store):
    """Renders come out of the streamed state, not out of fresh Gets."""
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    settled = len(devices["leaf1"].gets)
    for _ in range(3):
        fabric_store.table(get_report("lldp"))
    assert len(devices["leaf1"].gets) == settled


def test_table_caching_and_invalidation(store):
    """Consecutive calls to table() return cached dict unless state updates."""
    fabric_store, devices = store
    t1 = fabric_store.table(get_report("lldp"))
    t2 = fabric_store.table(get_report("lldp"))
    assert t1 is t2  # Cache hit returns exact same dict object

    # Simulating a state update invalidates the cache
    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine99")],
    )
    assert wait_for(
        lambda: any(
            row["Nbr-System"] == "spine99"
            for row in fabric_store.table(get_report("lldp"))["rows"]
        )
    )
    t3 = fabric_store.table(get_report("lldp"))
    assert t3 is not t1


def test_all_static_reports_have_predeclared_subscriptions():
    """Verify that core static reports have explicit subscribe specs defined."""
    reports_with_subscribe = [r for r in reports_for(SERVER) if r.subscribe]
    assert len(reports_with_subscribe) >= 10


def test_opening_every_report_costs_one_session_per_node(store):
    """The whole report catalogue shares a single Subscribe RPC per node.

    SR Linux allows 20 concurrent gRPC sessions per server by default, shared
    with every other client of the node, so the path set growing must not grow
    the session count with it.
    """
    fabric_store, devices = store
    for report in reports_for(SERVER):
        fabric_store.table(report)
    # Per node rather than max_sessions_per_node: a maximum is already 1 once
    # the first node is subscribed, so waiting on it lets a slower node be
    # asserted on while it still sits between two Subscribe RPCs.
    assert wait_for(
        lambda: all(
            node["connected"] and node["sessions"] == 1
            for node in fabric_store.status()["nodes"]
        ),
        timeout=10,
    )
    for device in devices.values():
        live = [s for s in device.subscribers if not s.closed]
        assert len(live) == 1


def test_rendering_a_report_keeps_its_paths_subscribed(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: devices["leaf1"].subscribe_requests)
    stream = fabric_store._streams["leaf1"]
    stream.idle_timeout = 60.0
    fabric_store.table(get_report("lldp"))
    assert stream._retire_idle_paths() is False
    assert stream.status()["paths"][0]["path"] == LLDP_PATH


# --------------------------------------------------------------------------- #
# connecting
# --------------------------------------------------------------------------- #


def test_a_node_that_was_down_at_startup_is_picked_up_later(fabric, monkeypatch):
    """A node still booting when the server starts must not stay unreachable.

    Opening a gNMI connection reaches the node to fetch its certificate, so a
    node that is not up yet fails at startup. Nothing else would retry it: the
    resync sweep only walks nodes that already connected.
    """
    nornir, devices = fabric
    down = {"leaf1"}

    def get_connection(self, name, config):
        if self.name in down:
            raise RuntimeError("The SSL certificate cannot be retrieved")
        return devices[self.name]

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        table = fabric_store.table(get_report("lldp"))
        assert [e["node"] for e in table["errors"]] == ["leaf1"]
        assert [n["node"] for n in fabric_store.status()["unreachable"]] == ["leaf1"]

        down.clear()  # the node finishes booting
        # The render that schedules the retry still reports the node as down,
        # since reconnecting happens in the background.
        fabric_store.table(get_report("lldp"))
        assert wait_for(lambda: "leaf1" in fabric_store._streams, timeout=10)

        table = fabric_store.table(get_report("lldp"))
        assert table["errors"] == []
        assert fabric_store.status()["unreachable"] == []
        assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    finally:
        fabric_store.stop()


def test_a_node_that_stopped_answering_is_not_counted_as_up(store):
    """'connected' has to mean the node answers, not that a stream object exists.

    The gRPC channel behind the stream outlives the node it was opened to, so
    the node pane counted every node as up while the whole fabric was down.
    """
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert all(host["connected"] for host in fabric_store.inventory())

    devices["leaf1"].down = True
    fabric_store._streams["leaf1"].resync()

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert "GRPC ERROR" in hosts["leaf1"]["error"]
    assert hosts["spine1"]["connected"] is True
    assert hosts["spine1"]["error"] is None


def test_a_node_whose_subscription_dropped_is_not_counted_as_up(store):
    """A dropped Subscribe RPC is the fastest evidence a node went away."""
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(h["streaming"] for h in fabric_store.inventory()))

    stream = fabric_store._streams["leaf1"]
    stream.connected = False
    stream.error = "GRPC ERROR: Stream removed"

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert hosts["spine1"]["connected"] is True


def test_a_node_whose_updates_stopped_arriving_is_not_counted_as_up(store):
    """A subscription can fall silent without the transport reporting anything.

    That is what a vanished route looks like: the connection stays open, gRPC
    keeps considering the call healthy, and only the missing SAMPLE updates say
    the node is gone.
    """
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(h["streaming"] for h in fabric_store.inventory()))
    assert all(host["connected"] for host in fabric_store.inventory())

    stream = fabric_store._streams["leaf1"]
    interval = min(s.spec.sample_interval for s in stream._paths.values())
    # Nothing has arrived for well past the interval the node reports on.
    stream.last_update = time.time() - (interval * 3 + 30)
    stream._subscribed_at = stream.last_update

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert hosts["spine1"]["connected"] is True


def test_a_node_whose_channel_died_is_given_a_new_connection(fabric, monkeypatch):
    """A redeployed node needs a new connection, not a longer wait.

    Its gRPC channel belongs to the container that went away and keeps failing
    every call from its own state, so the server has to replace it. Waiting was
    what left the whole fabric dead in the UI after a lab restart.
    """
    nornir, devices = fabric
    handed: dict = {}

    def get_connection(self, name, config):
        # The first connection each node gets is one whose calls all fail; any
        # reconnection after that gets a working one.
        if handed.setdefault(self.name, 0) == 0:
            handed[self.name] += 1
            broken = FakeDevice(_responses())
            broken.down = True
            return broken
        return devices[self.name]

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        table = fabric_store.table(get_report("lldp"))
        assert len(table["errors"]) == 2
        assert table["rows"] == []

        # The render that notices schedules the reconnect; a later one benefits.
        assert wait_for(
            lambda: fabric_store.table(get_report("lldp"))["errors"] == [], timeout=10
        )
        table = fabric_store.table(get_report("lldp"))
        assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    finally:
        fabric_store.stop()


def test_a_report_a_node_could_not_serve_is_probed_again(fabric, monkeypatch):
    """Discovery failures must not be a permanent verdict either.

    Discovery runs the report's getter against the device, so it fails while the
    node is down - and the report has to recover once the node is back, without
    the node itself having to be reconnected.
    """
    nornir, devices = fabric
    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection",
        lambda self, name, config: devices[self.name],
        raising=True,
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        for device in devices.values():
            device.down = True
        assert len(fabric_store.table(get_report("lldp"))["errors"]) == 2

        for device in devices.values():
            device.down = False
        assert wait_for(
            lambda: fabric_store.table(get_report("lldp"))["errors"] == [], timeout=10
        )
    finally:
        fabric_store.stop()


def test_an_unreachable_node_is_not_retried_on_every_render(fabric, monkeypatch):
    """Retries are rate-limited, so a down node does not slow every render."""
    nornir, _devices = fabric
    attempts = []

    def get_connection(self, name, config):
        attempts.append(self.name)
        raise RuntimeError("unreachable")

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=300.0,
    )
    fabric_store.start()
    try:
        settled = len(attempts)
        for _ in range(3):
            fabric_store.table(get_report("lldp"))
        assert len(attempts) == settled
    finally:
        fabric_store.stop()


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def test_fabric_state_collects_what_the_checks_read(store):
    fabric_store, _devices = store
    state = fabric_store.fabric_state()
    # The route tables arrive under the names the checks read, with the size
    # of every table alongside.
    assert set(state.reports) == set(REQUIRED_REPORTS) | {"rib_summary"}
    assert state.reports["lldp"]["leaf1"], "the LLDP payload is what the getter returned"
    assert state.hostnames == {"leaf1": "leaf1", "spine1": "spine1"}
    assert state.errors == {}


def test_a_reading_streams_the_underlay_rib_and_not_every_vrf(store):
    """The whole RIB of every VRF is what a large fabric's server drowns in."""
    fabric_store, _devices = store
    state = fabric_store.fabric_state()
    assert "ipv4_rib" in state.reports and "ipv4_rib_underlay" not in state.reports
    wanted = [entry["path"] for entry in fabric_store._streams["leaf1"].status()["paths"]]
    route_tables = [p for p in wanted if "route-table" in p]
    assert route_tables, "the underlay table and the sizes are streamed"
    assert not [p for p in route_tables if "[name=*]" in p and not p.endswith("/statistics/active-routes")]


def test_a_reading_looks_watched_prefixes_up_one_by_one(store):
    fabric_store, devices = store
    route = "/network-instance[name=*]/route-table/ipv4-unicast/route[ipv4-prefix=6.6.6.1/32]"
    for device in devices.values():
        device.responses[route] = [
            {
                "network-instance": [
                    {
                        "name": "ipvrf-1",
                        "route-table": {"ipv4-unicast": {"route": [{"ipv4-prefix": "6.6.6.1/32", "active": True}]}},
                    }
                ]
            }
        ]
    state = fabric_store.fabric_state(watched=["6.6.6.1/32"])
    (table,) = state.reports["ipv4_watched"]["leaf1"]
    assert (table.ni, [r.prefix for r in table.routes]) == ("ipvrf-1", ["6.6.6.1/32"])
    assert "ipv6_watched" not in state.reports, "no IPv6 prefix is watched"
    assert (route, "state") in devices["leaf1"].gets


def test_fabric_state_honours_the_inventory_filter(store):
    fabric_store, _devices = store
    state = fabric_store.fabric_state({"role": "leaf"})
    assert list(state.hostnames) == ["leaf1"]
    assert list(state.reports["lldp"]) == ["leaf1"]


def test_fabric_state_records_a_node_it_could_not_read(store, monkeypatch):
    fabric_store, devices = store
    devices["spine1"].down = True
    state = fabric_store.fabric_state(reports=("lldp",))
    assert list(state.reports["lldp"]) == ["leaf1"]
    assert [node for _report, node in state.errors] == ["spine1"]


def test_the_checks_report_renders_as_an_ordinary_table(store):
    """It is a table like any other, so the browser needs nothing new for it."""
    fabric_store, _devices = store
    table = fabric_store.table(get_report("checks"))
    assert table["columns"] == list(CHECKS_COLUMNS)
    assert table["report"] == "checks"
    assert table["errors"] == []


def test_the_checks_report_finds_the_fault_in_the_fake_fabric(store):
    """Both fake nodes see spine1 and spine2, so nothing sees leaf1 back."""
    fabric_store, _devices = store
    rows = fabric_store.table(get_report("checks"))["rows"]
    assert [(r["Check"], r["Node"], r["Subject"]) for r in rows] == [
        ("lldp_one_sided", "leaf1", "ethernet-1/1")
    ]


def test_the_checks_report_is_served_over_http(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/checks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns"] == list(CHECKS_COLUMNS)
    assert all(set(row) == set(CHECKS_COLUMNS) for row in body["rows"])


def test_a_node_the_checks_could_not_read_is_a_finding(store):
    fabric_store, devices = store
    devices["spine1"].down = True
    rows = fabric_store.table(get_report("checks"))["rows"]
    collection = [r for r in rows if r["Check"] == "collection"]
    assert collection, "an unread node is reported rather than passed over"
    assert {r["Node"] for r in collection} == {"spine1"}


# --------------------------------------------------------------------------- #
# snapshots and comparison over HTTP
# --------------------------------------------------------------------------- #


def save_snapshot(test_client, report="lldp", label="before", **params):
    query = {"label": label, **params}
    return test_client.post(f"/api/snapshot/{report}", params=query)


def test_no_snapshots_to_begin_with(client):
    test_client, _devices = client
    assert test_client.get("/api/snapshots").json() == {"snapshots": []}


def test_saving_a_snapshot_renders_the_report_and_keeps_it(client):
    test_client, _devices = client
    resp = save_snapshot(test_client)
    assert resp.status_code == 201
    saved = resp.json()
    assert saved["report"] == "lldp"
    assert saved["label"] == "before"
    assert saved["rows"] == 4
    assert saved["nodes"] == ["leaf1", "spine1"]


def test_a_saved_snapshot_is_listed(client):
    test_client, _devices = client
    saved = save_snapshot(test_client).json()
    listed = test_client.get("/api/snapshots").json()["snapshots"]
    assert [s["id"] for s in listed] == [saved["id"]]


def test_the_list_can_be_narrowed_to_one_report(client):
    test_client, _devices = client
    save_snapshot(test_client, report="lldp")
    save_snapshot(test_client, report="bgp_peers")
    listed = test_client.get("/api/snapshots?report=bgp_peers").json()["snapshots"]
    assert [s["report"] for s in listed] == ["bgp_peers"]


def test_a_snapshot_can_be_deleted(client):
    test_client, _devices = client
    saved = save_snapshot(test_client).json()
    assert test_client.delete(f"/api/snapshot/{saved['id']}").status_code == 200
    assert test_client.get("/api/snapshots").json()["snapshots"] == []


def test_deleting_a_snapshot_that_is_not_there_is_a_404(client):
    test_client, _devices = client
    assert test_client.delete("/api/snapshot/nope").status_code == 404


def test_snapshotting_a_report_that_cannot_be_streamed_is_a_404(client):
    test_client, _devices = client
    assert test_client.post("/api/snapshot/routing_pol").status_code == 404


def test_an_unchanged_fabric_differs_from_its_snapshot_in_nothing(client):
    test_client, _devices = client
    saved = save_snapshot(test_client).json()
    body = test_client.get(f"/api/diff/lldp?against={saved['id']}").json()
    assert body["rows"] == []
    assert body["diff"]["counts"]["same"] == 4
    assert body["diff"]["labels"] == ["before", "now"]


def test_a_neighbour_that_went_away_since_the_snapshot(client):
    test_client, devices = client
    saved = save_snapshot(test_client).json()
    devices["leaf1"].push("system/lldp", deletes=["interface[name=ethernet-1/2]"])
    assert wait_for(
        lambda: len(test_client.get("/api/report/lldp").json()["rows"]) == 3
    )
    body = test_client.get(f"/api/diff/lldp?against={saved['id']}").json()
    assert body["diff"]["counts"] == {"added": 0, "removed": 1, "changed": 0, "same": 3}
    removed = [row for row in body["rows"] if row["\u00b1"] == "removed"]
    assert [(r["Node"], r["interface"]) for r in removed] == [("leaf1", "ethernet-1/2")]


def test_a_neighbour_that_changed_since_the_snapshot(client):
    """The row is the same row - same node, same port - saying something else."""
    test_client, devices = client
    saved = save_snapshot(test_client).json()
    devices["leaf1"].push(
        "system/lldp",
        updates=[("interface[name=ethernet-1/1]/neighbor[id=1]/port-description", "to-spine1")],
    )
    assert wait_for(
        lambda: any(
            row["Nbr-port-desc"] == "to-spine1"
            for row in test_client.get("/api/report/lldp").json()["rows"]
        )
    )
    body = test_client.get(f"/api/diff/lldp?against={saved['id']}").json()
    assert body["diff"]["counts"]["changed"] == 1
    changed = [row for row in body["rows"] if row["\u00b1"] == "changed"][0]
    assert changed["Nbr-port-desc"] == "to-leaf1 \u2192 to-spine1"
    assert changed["_changes"] == {"Nbr-port-desc": ["to-leaf1", "to-spine1"]}


def test_the_unchanged_rows_can_be_asked_for(client):
    test_client, _devices = client
    saved = save_snapshot(test_client).json()
    body = test_client.get(f"/api/diff/lldp?against={saved['id']}&same=1").json()
    assert len(body["rows"]) == 4
    assert all(row["\u00b1"] == "same" for row in body["rows"])


def test_two_nodes_are_compared_without_their_names(client):
    test_client, _devices = client
    body = test_client.get("/api/diff/lldp?nodes=leaf1,spine1").json()
    assert body["columns"][0] == "\u00b1"
    assert "Node" not in body["columns"]
    # Both fake nodes report the same two neighbours, so they agree.
    assert body["rows"] == []
    assert body["diff"]["labels"] == ["leaf1", "spine1"]


def test_a_node_the_report_has_no_rows_for_is_said_so(client):
    test_client, _devices = client
    body = test_client.get("/api/diff/lldp?nodes=leaf1,leaf9").json()
    assert [e["node"] for e in body["errors"]] == ["leaf9"]


def test_a_diff_needs_something_to_compare_against(client):
    test_client, _devices = client
    assert test_client.get("/api/diff/lldp").status_code == 400
    assert test_client.get("/api/diff/lldp?against=x&nodes=a,b").status_code == 400


def test_comparing_nodes_takes_exactly_two(client):
    test_client, _devices = client
    assert test_client.get("/api/diff/lldp?nodes=leaf1").status_code == 400


def test_a_diff_against_a_snapshot_that_is_not_there_is_a_404(client):
    test_client, _devices = client
    assert test_client.get("/api/diff/lldp?against=nope").status_code == 404


def test_a_snapshot_of_another_report_is_refused(client):
    test_client, _devices = client
    saved = save_snapshot(test_client, report="bgp_peers").json()
    resp = test_client.get(f"/api/diff/lldp?against={saved['id']}")
    assert resp.status_code == 400
    assert "bgp_peers" in resp.json()["error"]


def test_a_snapshot_of_a_different_slice_of_the_fabric_is_refused(client):
    """Every node the two do not share would read as one that came or went."""
    test_client, _devices = client
    saved = save_snapshot(test_client, inv_filter="role=leaf").json()
    resp = test_client.get(f"/api/diff/lldp?against={saved['id']}")
    assert resp.status_code == 409
    assert "inventory filter" in resp.json()["error"]


def test_a_snapshot_of_the_same_slice_is_compared(client):
    test_client, _devices = client
    saved = save_snapshot(test_client, inv_filter="role=leaf").json()
    resp = test_client.get(f"/api/diff/lldp?against={saved['id']}&inv_filter=role=leaf")
    assert resp.status_code == 200
    assert resp.json()["diff"]["counts"]["same"] == 2


def test_a_diff_of_a_report_that_cannot_be_streamed_is_a_404(client):
    test_client, _devices = client
    assert test_client.get("/api/diff/routing_pol?nodes=a,b").status_code == 404


def test_a_snapshot_records_the_fabric_and_the_whole_inventory(fabric, tmp_path):
    nornir, _devices = fabric
    app = create_app(
        nornir, resync_interval=0, topo_name="dc1", snapshot_dir=tmp_path / "snap"
    )
    with TestClient(app) as test_client:
        saved = save_snapshot(test_client).json()
    assert saved["fabric"] == "dc1"
    assert saved["inventory"] == sorted(HOSTS)


def test_the_recorded_inventory_follows_the_filter(fabric, tmp_path):
    nornir, _devices = fabric
    app = create_app(
        nornir, resync_interval=0, topo_name="dc1", snapshot_dir=tmp_path / "snap"
    )
    with TestClient(app) as test_client:
        saved = save_snapshot(test_client, inv_filter="role=leaf").json()
    assert saved["inventory"] == ["leaf1"]


def test_a_snapshot_of_another_fabric_is_refused(fabric, tmp_path):
    """One snapshot directory serves every lab, so the diff has to say no."""
    nornir, _devices = fabric
    shared = tmp_path / "snapshots"

    dc1 = create_app(nornir, resync_interval=0, topo_name="dc1", snapshot_dir=shared)
    with TestClient(dc1) as test_client:
        saved = save_snapshot(test_client).json()

    dc2 = create_app(nornir, resync_interval=0, topo_name="dc2", snapshot_dir=shared)
    with TestClient(dc2) as test_client:
        resp = test_client.get(f"/api/diff/lldp?against={saved['id']}")
    assert resp.status_code == 409
    assert "dc1" in resp.json()["error"] and "dc2" in resp.json()["error"]


def test_the_same_fabric_under_another_name_is_still_compared(fabric, tmp_path):
    nornir, _devices = fabric
    shared = tmp_path / "snapshots"
    app = create_app(nornir, resync_interval=0, topo_name="dc1", snapshot_dir=shared)
    with TestClient(app) as test_client:
        saved = save_snapshot(test_client).json()
        resp = test_client.get(f"/api/diff/lldp?against={saved['id']}")
    assert resp.status_code == 200


def test_a_snapshot_taken_before_a_fabric_was_named_still_compares(client, tmp_path):
    """The server without a topology name records none, and judges none."""
    test_client, _devices = client
    saved = save_snapshot(test_client).json()
    assert saved["fabric"] == ""
    assert test_client.get(f"/api/diff/lldp?against={saved['id']}").status_code == 200


def test_a_report_says_what_identifies_its_rows(client):
    test_client, _devices = client
    reports = {r["name"]: r for r in test_client.get("/api/reports").json()["reports"]}
    assert reports["bgp_peers"]["key_columns"] == ["Node", "NI", "peer"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_parse_kv():
    assert parse_kv("role=leaf, site=ams") == {"role": "leaf", "site": "ams"}
    assert parse_kv("") is None
    assert parse_kv(None) is None
    assert parse_kv("nonsense") is None


def test_table_digest_ignores_render_timings():
    base = {"columns": ["Node"], "rows": [{"Node": "leaf1"}], "errors": []}
    assert table_digest({**base, "render_ms": 1}) == table_digest(
        {**base, "render_ms": 99}
    )
    assert table_digest(base) != table_digest({**base, "rows": []})


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_index_is_served(client):
    test_client, _devices = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "fcli" in response.text


def test_static_assets_are_served(client):
    test_client, _devices = client
    assert test_client.get("/static/app.js").status_code == 200
    assert test_client.get("/static/style.css").status_code == 200


def test_the_page_is_never_cached_and_its_assets_are_fingerprinted(client):
    test_client, _devices = client
    response = test_client.get("/")
    assert response.headers["cache-control"] == "no-cache"
    assert ASSET_TOKEN not in response.text
    assert f"/static/app.js?v={asset_version()}" in response.text
    assert f"/static/style.css?v={asset_version()}" in response.text


def test_every_chat_bubble_field_is_assigned_somewhere():
    """A read of a field nothing assigns is a silent no-op, not an error.

    That is how a finished answer went missing: the body element was built and
    appended, but the reference was never stored on the row, so every guarded
    `if (bubble._body) render(...)` skipped quietly and the drawer stayed empty
    while the tool chips looked healthy.
    """
    source = (STATIC_DIR / "app.js").read_text()
    read = set(re.findall(r"(?:bubble|row)\.(_[A-Za-z]+)", source))
    assigned = set(re.findall(r"(?:bubble|row)\.(_[A-Za-z]+)\s*=[^=]", source))
    assert read, "expected the chat bubble to carry fields"
    assert read - assigned == set()


def test_the_asset_version_follows_the_assets(tmp_path, monkeypatch):
    monkeypatch.setattr("nornir_srl.server.app.STATIC_DIR", tmp_path)
    (tmp_path / "app.js").write_text("//")
    (tmp_path / "style.css").write_text("/**/")
    before = asset_version()

    os.utime(tmp_path / "app.js", ns=(0, 2_000_000_000_000_000_000))
    assert asset_version() != before


def test_reports_endpoint_lists_every_report(client):
    test_client, _devices = client
    payload = test_client.get("/api/reports").json()
    assert {r["name"] for r in payload["reports"] if r.get("kind") != "lens"} == {
        r.name for r in reports_for(SERVER)
    }
    assert all(r["title"] and r["description"] for r in payload["reports"])


def test_reports_endpoint_lists_the_lenses_as_what_they_are(client):
    """A lens is offered next to the reports, marked as a question with arguments."""
    test_client, _devices = client
    payload = test_client.get("/api/reports").json()
    lenses = {r["name"]: r for r in payload["reports"] if r.get("kind") == "lens"}
    assert set(lenses) == {lens.name for lens in lenses_for(SERVER)} == {
        "incidents",
        "changes",
        "where",
        "path",
        "service",
    }
    assert all(r["category"] == "Lenses" for r in lenses.values())
    required = {name: [p["name"] for p in r["params"] if p["required"]] for name, r in lenses.items()}
    assert required == {
        "incidents": [],
        "changes": [],
        "where": ["target"],
        "path": ["source", "destination"],
        "service": ["name"],
    }


def test_reports_endpoint_returns_topo_name(fabric):
    nornir, _devices = fabric
    app = create_app(nornir, resync_interval=0, topo_name="dc1")
    with TestClient(app) as test_client:
        payload = test_client.get("/api/reports").json()
        assert payload["topo_name"] == "dc1"


def test_inventory_endpoint(client):
    test_client, _devices = client
    hosts = test_client.get("/api/inventory").json()["hosts"]
    assert {h["name"] for h in hosts} == set(HOSTS)
    assert {h["labels"]["role"] for h in hosts} == {"leaf", "spine"}
    # the gNMI session is open, but nothing streams until a report is opened
    assert all(h["connected"] and not h["streaming"] and not h["getting"] for h in hosts)


def _instances_response(*instances):
    """What a node answers for its network-instances: name and type of each."""
    return [{"network-instance": [{"name": name, "type": kind, "oper-state": "up"} for name, kind in instances]}]


def test_network_instances_endpoint_lists_the_fabric_s_instances(client):
    """One entry per name across the nodes, typed and counted, the default
    instance first and the routed ones before the bridged - what a field
    choosing an instance to look a route up in offers."""
    test_client, devices = client
    devices["leaf1"].responses["/network-instance[name=*]"] = _instances_response(
        ("macvrf-101", "mac-vrf"), ("ipvrf-1", "ip-vrf"), ("default", "default"), ("mgmt", "ip-vrf")
    )
    devices["spine1"].responses["/network-instance[name=*]"] = _instances_response(
        ("default", "default"), ("mgmt", "ip-vrf")
    )
    found = test_client.get("/api/network-instances").json()["network_instances"]
    assert found == [
        {"name": "default", "type": "default", "nodes": 2},
        {"name": "ipvrf-1", "type": "ip-vrf", "nodes": 1},
        {"name": "mgmt", "type": "ip-vrf", "nodes": 2},
        {"name": "macvrf-101", "type": "mac-vrf", "nodes": 1},
    ]
    # The inventory filter narrows whose instances are counted.
    narrowed = test_client.get("/api/network-instances", params={"inv_filter": "role=spine"}).json()
    assert [(e["name"], e["nodes"]) for e in narrowed["network_instances"]] == [("default", 1), ("mgmt", 1)]


def test_the_path_lens_chooses_its_instance_from_the_fabric(client):
    """The browser draws the path lens's instance as a choice, not a text field."""
    test_client, _devices = client
    payload = test_client.get("/api/reports").json()
    path = next(r for r in payload["reports"] if r["name"] == "path")
    ni = next(p for p in path["params"] if p["name"] == "ni")
    assert (ni["kind"], ni["placeholder"]) == ("ni", "default")


def test_inventory_counts_the_gets_a_node_was_asked_for(store):
    """The Nodes pane watches this counter move: a Get is over in milliseconds.

    Sampling 'a Get is in flight' would practically never catch one, so the
    transfer mark keys off the count having changed between two polls.
    """
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    before = {h["name"]: h["gets"] for h in fabric_store.inventory()}
    assert all(count > 0 for count in before.values())

    fabric_store._streams["leaf1"].direct_get(IFSTATE_PATH, "state")
    after = {h["name"]: h["gets"] for h in fabric_store.inventory()}
    assert after["leaf1"] > before["leaf1"]
    assert after["spine1"] == before["spine1"]


def test_inventory_marks_a_node_while_a_get_is_in_flight(store):
    """The Nodes pane uses this to show a transfer mark without flipping the dot red."""
    fabric_store, devices = store
    release = threading.Event()
    started = threading.Event()
    orig = devices["leaf1"].get

    def slow(paths, datatype="config", strip_mod=True):
        started.set()
        release.wait(timeout=10)
        return orig(paths, datatype, strip_mod)

    devices["leaf1"].get = slow
    stream = fabric_store._streams["leaf1"]
    worker = threading.Thread(
        target=lambda: stream.direct_get(LLDP_PATH, "state"), daemon=True
    )
    try:
        worker.start()
        assert wait_for(started.is_set)
        hosts = {h["name"]: h for h in fabric_store.inventory()}
        assert hosts["leaf1"]["getting"] is True
        assert hosts["leaf1"]["connected"] is True
        assert hosts["spine1"]["getting"] is False
    finally:
        release.set()
        worker.join(timeout=10)


def test_inventory_reports_streaming_once_a_report_is_open(client):
    test_client, _devices = client
    test_client.get("/api/report/lldp")
    assert wait_for(
        lambda: all(
            h["streaming"] for h in test_client.get("/api/inventory").json()["hosts"]
        )
    )


def test_report_endpoint_returns_a_table(client):
    test_client, _devices = client
    table = test_client.get("/api/report/lldp").json()
    assert table["report"] == "lldp"
    assert len(table["rows"]) == 4


def test_report_endpoint_honours_the_inventory_filter(client):
    test_client, _devices = client
    table = test_client.get("/api/report/lldp?inv_filter=role%3Dspine").json()
    assert {row["Node"] for row in table["rows"]} == {"spine1"}


def test_report_endpoint_looks_an_address_up_in_the_rib(client):
    test_client, _devices = client
    table = test_client.get("/api/report/ipv4_rib?address=10.1.1.55").json()
    assert {row["Prefix"] for row in table["rows"]} == {"10.1.1.0/24"}


def test_report_endpoint_says_why_it_will_not_look_up_a_bad_address(client):
    """An EventSource cannot read a body, so the browser checks this too - but
    an API client asking for nonsense gets told, rather than an empty table."""
    test_client, _devices = client
    resp = test_client.get("/api/report/ipv4_rib?address=10.1.1.999")
    assert resp.status_code == 400
    assert "not an IP address" in resp.json()["error"]
    assert test_client.get("/api/stream/ipv4_rib?address=nope").status_code == 400


def test_reports_endpoint_describes_the_arguments_a_report_takes(client):
    """All the browser knows about a report's own arguments comes from here."""
    test_client, _devices = client
    payload = test_client.get("/api/reports").json()
    by_name = {r["name"]: r for r in payload["reports"]}
    assert by_name["ipv4_rib"]["params"] == [
        {
            "name": "address",
            "label": "LPM",
            "placeholder": "LPM lookup, e.g. 10.0.0.1",
            "help": "Longest prefix matching this address, per node and route table",
            "kind": "address",
            "required": False,
        }
    ]
    assert by_name["lldp"]["params"] == []


def test_unknown_report_is_a_404(client):
    test_client, _devices = client
    assert test_client.get("/api/report/nope").status_code == 404
    assert test_client.get("/api/stream/nope").status_code == 404


def test_status_endpoint(client):
    test_client, _devices = client
    test_client.get("/api/report/lldp")
    status = test_client.get("/api/status").json()
    assert len(status["nodes"]) == 2


def test_overview_endpoint(client):
    test_client, _devices = client
    resp = test_client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"]["total"] == 2
    assert "connected" in data["nodes"]
    assert "established" in data["bgp"]
    assert "total" in data["interfaces"]
    assert "subscriptions" in data["telemetry"]
    assert "bridge_domains" in data
    assert "routers" in data
    assert "total" in data["bridge_domains"]
    assert "total" in data["routers"]


def test_store_overview_method(store):
    fabric_store, _devices = store
    data = fabric_store.overview()
    assert data["nodes"]["total"] == 2
    assert data["nodes"]["connected"] == 2
    assert isinstance(data["bgp"]["established"], int)
    assert isinstance(data["interfaces"]["total"], int)
    assert isinstance(data["telemetry"]["subscriptions"], int)
    assert isinstance(data["bridge_domains"]["total"], int)
    assert isinstance(data["routers"]["total"], int)


def _push_services(stream, *instances):
    """Give a node the network-instance state the topology classifies it on."""
    stream._tree["network-instance"] = list(instances)


def _mac_vrf(*route_targets):
    return {
        "name": "mac-vrf-100",
        "type": "mac-vrf",
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {"id": index, "route-target": {"export-rt": rt, "import-rt": rt}}
                    for index, rt in enumerate(route_targets, start=1)
                ]
            }
        },
    }


def test_topology_endpoint(client):
    test_client, _devices = client
    resp = test_client.get("/api/topology")
    assert resp.status_code == 200
    graph = resp.json()
    assert {n["name"] for n in graph["nodes"]} >= set(HOSTS)
    assert any({link["a"], link["b"]} == {"leaf1", "spine1"} for link in graph["links"])
    # Both nodes advertise a neighbour the inventory does not have.
    assert graph["unresolved"] == [{"peer": "spine2", "seen_by": ["leaf1", "spine1"]}]


def test_topology_endpoint_honours_the_inventory_filter(client):
    test_client, _devices = client
    graph = test_client.get("/api/topology?inv_filter=role%3Dspine").json()
    assert {n["name"] for n in graph["nodes"] if not n["external"]} == {"spine1"}


def test_topology_subscribes_to_lldp_and_the_services(store):
    fabric_store, devices = store
    fabric_store.topology()
    assert wait_for(lambda: devices["leaf1"].subscribe_requests)
    streamed = [s["path"] for s in devices["leaf1"].subscribe_requests[-1]["subscription"]]
    assert LLDP_PATH in streamed
    assert HOSTNAME_PATH in streamed
    assert IFSTATS_PATH in streamed
    # The service paths are registered as well; this fake node answers nothing
    # for them, which leaves them pending rather than streaming.
    registered = {p["path"] for p in fabric_store._streams["leaf1"].status()["paths"]}
    assert "/network-instance[name=*]/type" in registered
    assert "/network-instance[name=*]/protocols/bgp-vpn" in registered
    assert "/network-instance[name=*]/interface" in registered
    assert "/system/network-instance/protocols/evpn/ethernet-segments" in registered
    assert "/platform/chassis" in registered


def test_topology_infers_the_tier_from_the_services_of_a_node(store):
    fabric_store, _devices = store
    fabric_store.topology()
    _push_services(
        fabric_store._streams["leaf1"],
        {"name": "default", "type": "default"},
        _mac_vrf("target:100:100"),
    )
    _push_services(
        fabric_store._streams["spine1"], {"name": "default", "type": "default"}
    )
    graph = fabric_store.topology()
    roles = {n["name"]: n["role"] for n in graph["nodes"]}
    assert roles["leaf1"] == "leaf"
    # spine1 runs no services and leaf1 reports it as a neighbour.
    assert roles["spine1"] == "spine"
    assert [layer["index"] for layer in graph["layers"]] == [4, 3, 2]


def test_topology_marks_a_stitched_service_as_a_gateway(store):
    fabric_store, _devices = store
    fabric_store.topology()
    _push_services(
        fabric_store._streams["leaf1"],
        {"name": "default", "type": "default"},
        _mac_vrf("target:100:100", "target:64500:100"),
    )
    graph = fabric_store.topology()
    leaf1 = next(n for n in graph["nodes"] if n["name"] == "leaf1")
    assert leaf1["role"] == "dcgw"
    assert leaf1["stitched"] == 1


def test_topology_hangs_a_client_off_the_port_a_service_is_bound_to(store):
    fabric_store, _devices = store
    fabric_store.topology()
    mac_vrf = _mac_vrf("target:100:100")
    mac_vrf["interface"] = [{"name": "irb0.100"}, {"name": "ethernet-1/10.100"}]
    _push_services(
        fabric_store._streams["leaf1"], {"name": "default", "type": "default"}, mac_vrf
    )
    graph = fabric_store.topology()
    client = next(n for n in graph["nodes"] if n["role"] == "client")
    assert (client["name"], client["layer"]) == ("leaf1:ethernet-1/10", 0)
    assert client["peers"] == ["leaf1"]
    assert client["services"] == ["mac-vrf-100"]
    assert graph["layers"][-1]["label"] == "Clients"
    leaf1 = next(n for n in graph["nodes"] if n["name"] == "leaf1")
    assert leaf1["clients"] == 1


def test_topology_merges_a_multi_homed_client_by_its_ethernet_segment(store):
    """The ESI has to survive the trip from the Get through the streamed tree."""
    fabric_store, _devices = store
    fabric_store.topology()
    for name in HOSTS:
        mac_vrf = _mac_vrf("target:100:100")
        mac_vrf["interface"] = [{"name": "lag1.100"}]
        _push_services(
            fabric_store._streams[name], {"name": "default", "type": "default"}, mac_vrf
        )
    graph = fabric_store.topology()
    clients = [n for n in graph["nodes"] if n["role"] == "client"]
    assert len(clients) == 1
    assert clients[0]["name"] == ES_ESI
    # It hangs off the one bundle, which is what spans the two nodes.
    segment = next(n for n in graph["nodes"] if n["role"] == "segment")
    assert (segment["label"], segment["esi"]) == ("ES 01:01", ES_ESI)
    assert segment["names"] == ["mh-1"]
    assert segment["peers"] == [ES_ESI, "leaf1", "spine1"]
    assert clients[0]["peers"] == [segment["name"]]


def test_topology_attaches_egress_rates_to_each_end_of_a_link(store):
    fabric_store, _devices = store
    fabric_store.topology()
    stream = fabric_store._streams["leaf1"]
    stream.rates.observe("ethernet-1/1", {"out-octets": 0}, 1_000_000_000)
    stream.rates.observe("ethernet-1/1", {"out-octets": 1_000_000}, 2_000_000_000)
    graph = fabric_store.topology()
    link = next(cable for cable in graph["links"] if {cable["a"], cable["b"]} == {"leaf1", "spine1"})
    assert link["a"] == "leaf1"
    # 1_000_000 octets in 1s is 8 Mbps leaving leaf1 toward spine1.
    assert link["a_out_bps"] == 8_000_000
    assert "b_out_bps" not in link


def test_topology_keeps_a_node_that_has_streamed_nothing(store):
    fabric_store, _devices = store
    graph = fabric_store.topology()
    assert {n["role"] for n in graph["nodes"] if n["name"] in HOSTS} == {"unknown"}
    assert all(n["connected"] for n in graph["nodes"] if n["name"] in HOSTS)


def test_topology_exposes_the_chassis_type(store):
    fabric_store, _devices = store
    graph = fabric_store.topology()
    leaf1 = next(n for n in graph["nodes"] if n["name"] == "leaf1")
    assert leaf1["platform"] == "7220 IXR-D2L"


@pytest.mark.anyio
async def test_stream_pushes_a_table_event(store):
    fabric_store, _devices = store
    events = await _collect(fabric_store, get_report("lldp"), stop_after=1)
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "table"
    assert payload["report"] == "lldp"
    assert len(payload["rows"]) == 4


@pytest.mark.anyio
async def test_stream_only_pushes_when_the_table_changed(store):
    fabric_store, devices = store
    # Three ticks over unchanged state must produce exactly one table event.
    events = await _collect(fabric_store, get_report("lldp"), stop_after=3)
    assert [kind for kind, _ in events] == ["table"]

    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: any(
            row["Nbr-System"] == "spine9"
            for row in fabric_store.table(get_report("lldp"))["rows"]
        )
    )
    events = await _collect(fabric_store, get_report("lldp"), stop_after=2)
    assert [kind for kind, _ in events] == ["table"]
    assert any(row["Nbr-System"] == "spine9" for row in events[0][1]["rows"])


@pytest.mark.anyio
async def test_stream_stops_when_the_store_is_stopping(store):
    fabric_store, _devices = store
    fabric_store.stop()

    async def never_disconnects():
        return False

    chunks = [
        chunk
        async for chunk in table_events(
            fabric_store, "lldp", lambda: fabric_store.table(get_report("lldp")), 0.01, never_disconnects
        )
    ]
    assert chunks == []


@pytest.mark.anyio
async def test_stream_reports_a_render_failure_as_an_error_event(store, monkeypatch):
    fabric_store, _devices = store

    def boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(fabric_store, "table", boom)
    events = await _collect(fabric_store, get_report("lldp"), stop_after=1)
    assert events[0][0] == "error"
    assert events[0][1]["error"] == "render exploded"


async def _collect(fabric_store, report, stop_after, render=None):
    """Drive table_events for *stop_after* ticks and parse what it yielded."""
    ticks = {"n": 0}

    async def is_disconnected():
        ticks["n"] += 1
        return ticks["n"] > stop_after

    events = []
    if render is None:
        render = lambda: fabric_store.table(report)  # noqa: E731 - what the route builds
    async for chunk in table_events(fabric_store, report.name, render, 0.01, is_disconnected):
        text = chunk.decode()
        if text.startswith(":"):
            events.append(("keep-alive", None))
            continue
        kind = text.split("\n", 1)[0].removeprefix("event: ")
        data = text.split("data: ", 1)[1].strip()
        events.append((kind, json.loads(data)))
    return events


def test_bridge_domains_report_endpoint(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/bridge_domains")
    assert resp.status_code == 200
    table = resp.json()
    assert table["report"] == "bridge_domains"
    assert "columns" in table


def test_overview_ignores_admin_disabled_interfaces(store):
    fabric_store, _devices = store
    # Inject stream tree with admin-disabled interface and admin-enabled oper-down configured interface
    stream = list(fabric_store._streams.values())[0]
    stream._tree["interface"] = [
        {"name": "ethernet-1/1", "admin-state": "enable", "oper-state": "up"},
        {"name": "ethernet-1/2", "admin-state": "disable", "oper-state": "down"},
        {"name": "ethernet-1/3", "admin-state": "enable", "oper-state": "down", "subinterface": [{"index": 0}]},
    ]
    data = fabric_store.overview()
    # ethernet-1/2 (admin disabled) and unconfigured ports are ignored from faults
    # ethernet-1/3 (admin enable, configured, oper down) is counted as down
    assert data["interfaces"]["down"] == 1
    assert data["interfaces"]["total"] == 3


def test_overview_does_not_count_a_standby_port_as_down(store):
    """An ethernet-segment holding a port in standby is intent, not a fault.

    Counting it put a permanent red 'oper down' number on a healthy
    multi-homed fabric.
    """
    fabric_store, _devices = store
    stream = list(fabric_store._streams.values())[0]

    def down_count(reason):
        stream._tree["interface"] = [
            {"name": "lag1", "admin-state": "enable", "oper-state": "up"},
            {
                "name": "lag2",
                "admin-state": "enable",
                "oper-state": "down",
                "oper-down-reason": reason,
            },
        ]
        return fabric_store.overview()["interfaces"]["down"]

    # The same port, down for the same length of time; only the reason differs.
    assert down_count("standby-signaling") == 0
    assert down_count("min-links-not-met") == 1



# --------------------------------------------------------------------------- #
# lenses
# --------------------------------------------------------------------------- #


def test_a_lens_answers_once_with_rows_records_and_a_tree(client):
    """A lens is asked the way a report is, and answers in the same shape plus
    the records and the hierarchy the browser draws them as."""
    test_client, _devices = client
    resp = test_client.get("/api/report/path", params={"source": "leaf1", "destination": "10.1.1.5"})
    assert resp.status_code == 200, resp.json()
    answer = resp.json()
    assert answer["report"] == "path" and answer["title"] == "Path"
    assert answer["columns"][:3] == ["Node", "Hop", "NI"]
    assert [row["Type"] for row in answer["rows"]] == ["local", "no-neighbor"]
    assert [r["outcome"] for r in answer["records"]] == ["delivered", "no-neighbor"]
    assert answer["nodes"] == 2 and answer["errors"] == []
    # Hierarchically: one card per hop, the node reached at it, one item per
    # lookup - the delivery, then the last mile that found no neighbour.
    delivered, last_mile = answer["tree"]
    assert (delivered["title"], delivered["badge"], delivered["state"]) == ("Hop 1", "1 node", "up")
    (leaf,) = delivered["entries"]
    assert (leaf["title"], leaf["badge"], leaf["state"]) == ("leaf1", "1 lookup", "up")
    assert leaf["items"][0]["title"] == "default: 10.1.1.0/24"
    assert leaf["items"][0]["details"][0] == {"label": "Outcome", "value": "delivered", "state": "up"}
    assert (last_mile["title"], last_mile["state"]) == ("Hop 2", "down")
    assert last_mile["entries"][0]["items"][0]["title"] == "default: 10.1.1.5"
    # And as a graph: the delivery box leads to the host box.
    graph = answer["graph"]
    assert [n["title"] for n in graph["nodes"]] == ["leaf1", "10.1.1.5"]
    assert [(e["label"], e["state"]) for e in graph["edges"]] == [("ethernet-1/1.0", "up")]


def test_a_lens_without_a_graph_answers_none_for_it(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/where", params={"target": "00:00:00:00:00:01"})
    assert resp.json()["graph"] is None


def test_a_lens_missing_the_argument_it_needs_is_a_bad_request(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/where")
    assert resp.status_code == 400
    assert "needs address" in resp.json()["error"]


def test_a_lens_that_cannot_answer_is_a_bad_request_rather_than_a_failure(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/service", params={"name": "no-such-service"})
    assert resp.status_code == 400
    assert "no network-instance matching" in resp.json()["error"]


def test_a_lens_reports_nothing_found_as_an_answer(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/where", params={"target": "00:00:00:00:00:01"})
    assert resp.status_code == 200
    (card,) = resp.json()["tree"]
    assert (card["badge"], card["state"], card["entries"]) == ("not found", "down", [])
    assert "2 searched" in card["subtitle"]


@pytest.mark.anyio
async def test_a_lens_streams_like_a_report(store):
    fabric_store, _devices = store
    lens = get_lens("where")
    render = lambda: fabric_store.lens_table(lens, None, {"target": "00:00:00:00:00:01"})  # noqa: E731
    events = await _collect(fabric_store, lens, stop_after=1, render=render)
    assert events[0][0] == "table"
    assert events[0][1]["tree"][0]["badge"] == "not found"


@pytest.mark.anyio
async def test_a_stream_passes_a_question_it_cannot_answer_on_as_an_error(store):
    """A ValueError is the lens saying so, which the browser shows as such."""
    fabric_store, _devices = store
    lens = get_lens("service")
    render = lambda: fabric_store.lens_table(lens, None, {"name": "no-such-service"})  # noqa: E731
    events = await _collect(fabric_store, lens, stop_after=1, render=render)
    assert events[0][0] == "error"
    assert "no network-instance matching" in events[0][1]["error"]
