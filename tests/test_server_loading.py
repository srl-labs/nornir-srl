"""First reads are shared, and slow nodes do not hide completed nodes' routes."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from nornir_srl.connections.routing import ATTR_SETS_TABLE
from nornir_srl.reports import SubscriptionSpec, get_report
from nornir_srl.server.app import table_events
from nornir_srl.server.stream import HostStream
from nornir_srl.server.store import ReportLoad

from .fakes import BGP_ATTR_RESPONSE, FakeDevice, LLDP_PATH, LLDP_RESPONSE, RIB_PATH, wait_for
from .test_server_app import anyio_backend, client, fabric, store  # noqa: F401 - shared fixtures


ATTR_ONE = "/network-instance[name=default]/bgp-rib/attr-sets/attr-set[index=1]"


def prepare_rib(devices):
    for device in devices.values():
        device.responses[ATTR_SETS_TABLE] = BGP_ATTR_RESPONSE
        attrs = BGP_ATTR_RESPONSE[0]["network-instance"][0]["bgp-rib"]["attr-sets"]["attr-set"][0]
        device.responses[ATTR_ONE] = [{ATTR_ONE.lstrip("/"): attrs}]


@pytest.mark.parametrize("large", [False, True])
def test_discovery_reuses_small_attribute_lookups_or_bootstraps_a_large_table(store, monkeypatch, large):
    fabric_store, devices = store
    prepare_rib(devices)
    if large:
        monkeypatch.setattr("nornir_srl.server.devices.LOOKUP_BY_KEY_LIMIT", 0)
    table = fabric_store.table(get_report("bgp_rib_evpn_2"), params={"scope": "all"})
    assert table["rows"] and table["errors"] == []
    assert "65001" in json.dumps(table["rows"])
    for name, device in devices.items():
        assert device.gets == [(RIB_PATH, "state"), (ATTR_SETS_TABLE if large else ATTR_ONE, "state")]
        paths = fabric_store._streams[name]._paths
        assert (ATTR_SETS_TABLE in paths) is large
        assert ATTR_ONE not in paths


def test_small_ipv4_rib_does_not_load_attributes_from_other_families(store, monkeypatch):
    fabric_store, devices = store
    stream, device = fabric_store._streams["leaf1"], devices["leaf1"]
    path = "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=ipv4-unicast]/ipv4-unicast/local-rib/route"
    instances = [{"name": f"empty-{i}"} for i in range(300)]
    attr_paths = []
    for name, prefix, asn in (("default", "10.0.0.1/32", 65001), ("tenant1", "192.0.2.0/24", 65002)):
        instances.append({"name": name, "bgp-rib": {"afi-safi": [{
            "afi-safi-name": "ipv4-unicast",
            "ipv4-unicast": {"local-rib": {"route": [{"prefix": prefix, "used-route": True, "attr-id": 1}]}},
        }]}})
        attr = f"/network-instance[name={name}]/bgp-rib/attr-sets/attr-set[index=1]"
        device.responses[attr] = [{attr.lstrip("/"): {"as-path": {"segment": [{"member": [asn]}]}}}]
        attr_paths.append(attr)
    device.responses[path] = [{"network-instance": instances}]
    original = device.get

    def narrow_get(*args, **kwargs):
        assert ATTR_SETS_TABLE not in kwargs["paths"], "IPv4 must not download every EVPN attribute set"
        return original(*args, **kwargs)

    monkeypatch.setattr(device, "get", narrow_get)
    now = time.time()
    monkeypatch.setattr("nornir_srl.server.stream.time.time", lambda: now)
    report = get_report("bgp_rib_ipv4")
    first = fabric_store.table(report, hosts=["leaf1"], params={"scope": "all"})
    assert not first["errors"]
    assert {(r["NI"], r["as-path"]) for r in first["rows"]} == {("default", "65001"), ("tenant1", "65002")}
    assert stream.gets == 2, "one route Get and one batched attribute Get, reused by the first render"
    assert device.gets == [(p, "state") for p in [path, *attr_paths]]
    assert list(stream._paths) == [path]

    # Without a broad attribute subscription, attribute-only changes still
    # expire the rendered table; route notifications are not required.
    device.responses[attr_paths[0]] = [{attr_paths[0].lstrip("/"): {"as-path": {"segment": [{"member": [65100]}]}}}]
    now += stream.get_ttl + 1
    refreshed = fabric_store.table(report, hosts=["leaf1"], params={"scope": "all"})
    assert {(r["NI"], r["as-path"]) for r in refreshed["rows"]} == {("default", "65100"), ("tenant1", "65002")}
    assert stream.gets == 3


@pytest.mark.parametrize("related", [False, True])
def test_discovery_reuse_does_not_overwrite_an_update(related):
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=60)
    try:
        discovered = {}
        stream.discovery_get(LLDP_PATH, "state", discovered)
        path = "/system/lldp" if related else "/system/information/current-datetime"
        stream._apply({"update": {"delete": [path]}})
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state")], discovered)
        assert device.gets.count((LLDP_PATH, "state")) == (2 if related else 1)
        assert stream.snapshot(LLDP_PATH) == LLDP_RESPONSE
        # Discovery responses never replace a later resync's fresh read.
        before = len(device.gets)
        stream.resync()
        assert len(device.gets) == before + 1
    finally:
        stream.stop()


def test_empty_discovery_keeps_its_original_fallback_expiry(monkeypatch):
    device = FakeDevice({LLDP_PATH: [{}]})
    stream = HostStream("leaf1", device, restart_debounce=60)
    clock = time.time()
    monkeypatch.setattr("nornir_srl.server.stream.time.time", lambda: clock)
    try:
        discovered = {}
        stream.discovery_get(LLDP_PATH, "state", discovered)
        clock += stream.get_ttl - 1
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state")], discovered)
        assert stream.direct_get(LLDP_PATH, "state") == [{}]
        assert len(device.gets) == 1
        clock += 2
        stream.direct_get(LLDP_PATH, "state")
        assert len(device.gets) == 2
    finally:
        stream.stop()


def test_concurrent_cold_activations_share_the_discovery_get(store, monkeypatch):
    fabric_store, devices = store
    entered, release = threading.Event(), threading.Event()
    device = devices["leaf1"]
    original = device.get

    def slow_get(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(device, "get", slow_get)
    report = replace(get_report("lldp"), subscribe=())
    try:
        first, = fabric_store._start_activation(report, ["leaf1"], None)
        assert entered.wait(5)
        second, = fabric_store._start_activation(report, ["leaf1"], None)
        assert first is second
    finally:
        release.set()
    first.result(5)
    assert device.gets == [(LLDP_PATH, "state")]
    assert wait_for(lambda: not fabric_store._activating)


@pytest.mark.parametrize("fail", [False, True])
def test_partial_rib_appears_while_another_node_holds_its_tree_lock(store, monkeypatch, fail):
    fabric_store, devices = store
    prepare_rib(devices)
    # Even one activation worker must leave completed nodes free to render.
    fabric_store._activation_pool.shutdown()
    fabric_store._activation_pool = ThreadPoolExecutor(max_workers=1)
    warm = fabric_store.table(get_report("lldp"), hosts=["leaf1"])
    entered, release = threading.Event(), threading.Event()
    original = devices["spine1"].get

    def slow_get(*args, **kwargs):
        with fabric_store._streams["spine1"]._lock:
            entered.set()
            assert release.wait(10)
        if fail:
            raise RuntimeError("slow node failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(devices["spine1"], "get", slow_get)
    report, params = get_report("bgp_rib_evpn_2"), {"scope": "all"}
    try:
        fabric_store.table(report, params=params, progressive=True)
        assert entered.wait(5)
        # A timeout catches accidentally touching the pending node's tree.
        with ThreadPoolExecutor(1) as reader:
            pending = reader.submit(fabric_store.table, report, params=params, progressive=True)
            try:
                partial = pending.result(2)
                assert {row["Node"] for row in partial["rows"]} == {"leaf1"}
                assert partial["loading"] == {"ready": 1, "total": 2, "nodes": 2}
                assert partial["nodes"] == 2
                assert fabric_store.table(report, params=params, progressive=True) is partial
                # A subset containing only the completed node is complete.
                subset = reader.submit(
                    fabric_store.table, report, params=params, hosts=["leaf1"], progressive=True
                ).result(2)
                assert "loading" not in subset
                assert reader.submit(fabric_store.table, get_report("lldp"), hosts=["leaf1"]).result(2) is warm
            finally:
                release.set()
    finally:
        release.set()
    complete = fabric_store.table(report, params=params)
    assert "loading" not in complete
    refreshed = fabric_store.table(report, params=params, progressive=True)
    assert "loading" not in refreshed
    if not fail:
        assert refreshed is complete
    assert {row["Node"] for row in complete["rows"]} == ({"leaf1"} if fail else {"leaf1", "spine1"})
    assert bool(complete["errors"]) == fail
    assert not any(key[4:5] == ("loading",) for key in fabric_store._table_cache)


@pytest.mark.anyio
async def test_sse_keeps_loading_between_partial_and_complete_results(store, monkeypatch):
    import nornir_srl.server.app as app_module

    fabric_store, devices = store
    prepare_rib(devices)
    release = threading.Event()
    original = devices["spine1"].get

    def slow_get(*args, **kwargs):
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(devices["spine1"], "get", slow_get)
    monkeypatch.setattr(app_module, "PROGRESS_INTERVAL", 0.01)
    tables = []
    report = get_report("bgp_rib_evpn_2")

    async def disconnected():
        return bool(tables and not tables[-1].get("loading"))

    async def collect():
        async for chunk in table_events(
            fabric_store, report.name,
            lambda: fabric_store.table(report, params={"scope": "all"}, progressive=True),
            60, disconnected,
        ):
            if not chunk.startswith(b"event: table"):
                continue
            table = json.loads(chunk.split(b"data: ", 1)[1])
            tables.append(table)
            if table.get("loading", {}).get("ready") == 1:
                assert {row["Node"] for row in table["rows"]} == {"leaf1"}
                release.set()

    try:
        # Loading uses short ticks even if normal refresh is one minute.
        await asyncio.wait_for(collect(), 3)
    finally:
        release.set()
    assert any(t.get("loading", {}).get("ready") == 1 for t in tables)
    assert "loading" not in tables[-1]
    assert {row["Node"] for row in tables[-1]["rows"]} == {"leaf1", "spine1"}


def test_only_full_rib_streams_opt_into_partial_answers(client, monkeypatch):
    import nornir_srl.server.app as app_module

    test_client, _devices = client
    fabric_store = test_client.app.store
    progressive = []

    def render(*args, **kwargs):
        progressive.append(kwargs.get("progressive", False))
        return {"rows": []}

    async def once(_store, _name, render, *_args):
        yield b"event: table\ndata: " + json.dumps(render()).encode() + b"\n\n"

    monkeypatch.setattr(fabric_store, "table", render)
    monkeypatch.setattr(app_module, "table_events", once)
    assert test_client.get("/api/stream/bgp_rib_evpn_2?scope=all").status_code == 200
    assert test_client.get("/api/report/bgp_rib_evpn_2?scope=all").status_code == 200
    assert test_client.get("/api/stream/bgp_rib_evpn_2?mac_address=00:00:00:00:00:01").status_code == 200
    assert test_client.get("/api/stream/lldp").status_code == 200
    assert progressive == [True, False, False, False]


def test_stopping_the_last_viewer_cancels_queued_nodes_and_the_next_read(store, monkeypatch):
    fabric_store, devices = store
    prepare_rib(devices)
    fabric_store._activation_pool.shutdown()
    fabric_store._activation_pool = ThreadPoolExecutor(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    original = devices["leaf1"].get

    def slow_get(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(devices["leaf1"], "get", slow_get)
    report, params, load = get_report("bgp_rib_evpn_2"), {"scope": "all"}, ReportLoad()
    try:
        first, queued = fabric_store._start_activation(report, ["leaf1", "spine1"], params, load)
        assert entered.wait(5)
        fabric_store.cancel_load(load)
        assert queued.cancelled()
        assert fabric_store._start_activation(report, ["spine1"], params, load) == []
    finally:
        release.set()
    first.result(5)
    assert devices["leaf1"].gets == [(RIB_PATH, "state")]
    assert devices["spine1"].gets == []
    assert not fabric_store._activated
    assert not fabric_store._activation_errors
    assert not any(stream._paths for stream in fabric_store._streams.values())
    # Resume must discover complete data, without a cached cancellation error.
    table = fabric_store.table(report, params=params)
    assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    assert table["errors"] == []


@pytest.mark.parametrize("other_browser", [True, False])
def test_stopping_one_viewer_preserves_other_readers(store, monkeypatch, other_browser):
    fabric_store, devices = store
    prepare_rib(devices)
    entered, release = threading.Event(), threading.Event()
    original = devices["leaf1"].get

    def slow_get(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(devices["leaf1"], "get", slow_get)
    report, params = get_report("bgp_rib_evpn_2"), {"scope": "all"}
    first_load = ReportLoad()
    other_load = ReportLoad() if other_browser else None
    try:
        first, = fabric_store._start_activation(report, ["leaf1"], params, first_load)
        assert entered.wait(5)
        other, = fabric_store._start_activation(report, ["leaf1"], params, other_load)
        assert first is other
        fabric_store.cancel_load(first_load)
    finally:
        release.set()
    other.result(5)
    assert devices["leaf1"].gets == [(RIB_PATH, "state"), (ATTR_ONE, "state")]
    assert ("leaf1", fabric_store.activation_name(report, params)) in fabric_store._activated


def test_resume_during_a_cancelled_get_keeps_the_new_job_registered(store, monkeypatch):
    fabric_store, devices = store
    prepare_rib(devices)
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    original = devices["leaf1"].get
    calls = 0

    def slow_get(*args, **kwargs):
        nonlocal calls
        if kwargs["paths"] == [RIB_PATH]:
            index = calls
            calls += 1
            entered[index].set()
            assert release[index].wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(devices["leaf1"], "get", slow_get)
    report, params, load = get_report("bgp_rib_evpn_2"), {"scope": "all"}, ReportLoad()
    try:
        first, = fabric_store._start_activation(report, ["leaf1"], params, load)
        assert entered[0].wait(5)
        fabric_store.cancel_load(load)
        resumed, = fabric_store._start_activation(report, ["leaf1"], params, ReportLoad())
        assert first is not resumed
        release[0].set()
        first.result(5)
        assert entered[1].wait(5)
        key = ("leaf1", fabric_store.activation_name(report, params), fabric_store._streams["leaf1"])
        assert fabric_store._activating[key].future is resumed
    finally:
        for event in release:
            event.set()
    resumed.result(5)
    assert not fabric_store._activation_errors


def test_stream_cleanup_releases_its_load(client, monkeypatch):
    import nornir_srl.server.app as app_module

    test_client, _devices = client
    loads = []

    def render(*args, **kwargs):
        loads.append(kwargs["load"])
        return {"rows": []}

    async def once(_store, _name, render, *_args):
        yield b"event: table\ndata: " + json.dumps(render()).encode() + b"\n\n"

    monkeypatch.setattr(test_client.app.store, "table", render)
    monkeypatch.setattr(app_module, "table_events", once)
    assert test_client.get("/api/stream/bgp_rib_evpn_2?scope=all").status_code == 200
    assert len(loads) == 1 and loads[0].closed.is_set()


def test_a_small_report_does_not_queue_behind_a_full_rib_read(store):
    """Full-table RIB reads have bounded workers; everything else has its own."""
    fabric_store, _devices = store
    fabric_store._activation_pool.shutdown()
    fabric_store._activation_pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    # Every full-table worker is busy with a read that takes minutes.
    blocked = fabric_store._activation_pool.submit(release.wait, 5)
    try:
        report = get_report("lldp")
        futures = fabric_store._start_activation(report, ["leaf1", "spine1"], None)
        for future in futures:
            future.result(timeout=2)
        assert not blocked.done()
        rib = fabric_store._start_activation(get_report("bgp_rib_evpn_2"), ["leaf1"], {"scope": "all"})
        assert rib and not rib[0].done()
    finally:
        release.set()
