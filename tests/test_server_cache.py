"""Cache reuse must not hide changes, or turn unrelated telemetry into work."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest

from nornir_srl.connections.routing import ATTR_SETS_TABLE
from nornir_srl.reports import SubscriptionSpec, get_report
from nornir_srl.server.app import table_events
from nornir_srl.server.stream import HostStream
from nornir_srl.server.table import Table, serialize_table, table_digest
from nornir_srl.server.versions import PathVersions, shape

from .fakes import FakeDevice, LLDP_PATH, LLDP_RESPONSE, RIB_PATH, RIB_RESPONSE
from .test_server_app import anyio_backend, fabric, store  # noqa: F401 - shared fixtures


def apply(stream, path, value=None, *, delete=False):
    stream._apply({"update": {"delete" if delete else "update": [{"path": path, "val": value}]}})


def age_tables(store):
    # Bypass only the short render debounce, not dependency or TTL checks.
    for cached in store._table_cache.values():
        cached.at -= 1


def test_unrelated_nodes_and_paths_preserve_a_cached_table(store):
    fabric, _devices = store
    report = get_report("lldp")
    table = fabric.table(report, hosts=["leaf1"])
    age_tables(fabric)
    apply(fabric._streams["leaf1"], "/system/information/current-datetime", "later")
    apply(fabric._streams["leaf1"], "/interface[name=ethernet-1/1]/statistics/in-octets", 123)
    apply(fabric._streams["spine1"], "/system/lldp/interface[name=ethernet-1/1]/neighbor[id=1]/system-name", "changed")
    assert fabric.table(report, hosts=["leaf1"]) is table

    apply(fabric._streams["leaf1"], "/system/lldp/interface[name=ethernet-1/1]/neighbor[id=1]/system-name", "new-peer")
    changed = fabric.table(report, hosts=["leaf1"])
    assert changed is not table
    assert any(row["Nbr-System"] == "new-peer" for row in changed["rows"])
    age_tables(fabric)
    apply(fabric._streams["leaf1"], "/system/lldp/interface[name=ethernet-1/1]", delete=True)
    deleted = fabric.table(report, hosts=["leaf1"])
    assert len(deleted["rows"]) < len(changed["rows"])


def test_updates_during_a_render_are_not_marked_as_already_read(store, monkeypatch):
    fabric, _devices = store
    report = get_report("lldp")
    real = fabric._host_rows
    changed = False

    def rows(spec, name, params):
        nonlocal changed
        result = real(spec, name, params)
        if name == "leaf1" and not changed:
            changed = True
            apply(fabric._streams[name], "/system/lldp/interface[name=ethernet-1/1]/neighbor[id=1]/system-name", "during-render")
        return result

    monkeypatch.setattr(fabric, "_host_rows", rows)
    before = fabric.table(report)
    age_tables(fabric)
    after = fabric.table(report)
    assert after is not before
    assert any(row["Nbr-System"] == "during-render" for row in after["rows"])


def test_resync_and_replaced_stream_invalidate_rendered_data(store):
    fabric, devices = store
    report = get_report("lldp")
    before = fabric.table(report, hosts=["leaf1"])
    fabric._streams["leaf1"].resync()
    age_tables(fabric)
    after = fabric.table(report, hosts=["leaf1"])
    assert after is not before
    old = fabric._streams["leaf1"]
    replacement = HostStream("leaf1", devices["leaf1"], restart_debounce=60)
    replacement.ensure_paths(list(report.subscribe))
    fabric._streams["leaf1"] = replacement
    old.stop()
    age_tables(fabric)
    assert fabric.table(report, hosts=["leaf1"]) is not after


def test_fallback_ttl_is_not_extended_by_rendering_a_table(store, monkeypatch):
    fabric, devices = store
    stream = fabric._streams["leaf1"]
    path = "/system/lldp/interface[name=ethernet-1/1]/neighbor"
    devices["leaf1"].responses[path] = LLDP_RESPONSE
    clock = time.time()
    monkeypatch.setattr("nornir_srl.server.stream.time.time", lambda: clock)
    stream.direct_get(path, "state")
    clock += stream.get_ttl - 1
    report = replace(get_report("lldp"), name="fallback-probe", subscribe=(),
                     getter=lambda d: d.get_lldp_sum(interface="ethernet-1/1"))
    # No subscription: exercise the report's fallback Get on every render.
    monkeypatch.setattr(fabric, "activate", lambda *args, **kwargs: None)
    before = fabric.table(report, hosts=["leaf1"])
    assert devices["leaf1"].gets.count((path, "state")) == 1
    clock += 2
    after = fabric.table(report, hosts=["leaf1"])
    assert after is not before
    assert devices["leaf1"].gets.count((path, "state")) == 2


@pytest.mark.parametrize("available", [True, False])
def test_evpn_attributes_missing_from_the_stream_are_retried_and_expire(store, monkeypatch, available):
    fabric, devices = store
    stream, device = fabric._streams["leaf1"], devices["leaf1"]
    rib = deepcopy(RIB_RESPONSE)
    post = rib[0]["network-instance"][0]["bgp-rib"]["afi-safi"][0]["evpn"]["rib-in-out"]["rib-in-post"]
    post["ip-prefix-route"] = post.pop("mac-ip-route")
    post["ip-prefix-route"][0].update({"ip-prefix": "10.0.1.0/24", "route-distinguisher": "10.0.1.1:9001"})
    device.responses[RIB_PATH.replace("mac-ip-route", "ip-prefix-route")] = rib
    attrs = {
        "index": 1,
        "origin": "igp",
        "next-hop": "10.0.1.1",
        "as-path": {"segment": [{"member": [65500, 65001]}]},
        "communities": {"ext-community": ["target:9001:9001", "bgp-tunnel-encap:VXLAN"]},
    }
    device.responses[ATTR_SETS_TABLE] = [
        {"network-instance": [{"name": "default", "bgp-rib": {"attr-sets": {"attr-set": [attrs]}}}]}
    ]
    path = "/network-instance[name=default]/bgp-rib/attr-sets/attr-set[index=1]"
    report = get_report("bgp_rib_evpn_5")
    # Another, large RIB view can already be streaming the shared table.
    stream.ensure_paths([SubscriptionSpec(ATTR_SETS_TABLE, "state", mode="on_change")])

    def render():
        return fabric.table(report, hosts=["leaf1"], params={"scope": "all"})

    first = render()
    assert not first["errors"]
    assert len(first["rows"]) == 1
    row = first["rows"][0]
    assert row["RT"] == "9001:9001"
    assert row["as-path"] == "65500, 65001"
    assert "target:9001:9001" in row["communities"]

    # Routes and attributes arrive independently. A referenced attribute can
    # be absent locally even though a direct Get still finds it on the node.
    device.responses[path] = [{path.lstrip("/"): attrs}] if available else []
    apply(stream, path, delete=True)
    now = time.time() + 1
    monkeypatch.setattr("nornir_srl.server.stream.time.time", lambda: now)
    recovered = render()
    assert device.gets.count((path, "state")) == 1
    assert recovered["rows"][0]["RT"] == ("9001:9001" if available else "")
    if available:
        assert recovered["rows"] == first["rows"]

    # Positive and negative answers share the original Get's TTL. Unrelated
    # telemetry does not trigger more Gets; an empty answer cannot live forever.
    device.responses[path] = [{path.lstrip("/"): attrs}]
    now += stream.get_ttl - 1
    apply(stream, "/system/information/current-datetime", "later")
    assert render() is recovered
    assert device.gets.count((path, "state")) == 1
    now += 2
    refreshed = render()
    assert refreshed["rows"] == first["rows"]
    assert device.gets.count((path, "state")) == 2

    # Streamed state takes precedence as soon as it catches up, even while a
    # fallback answer is cached, and goes back to needing no device reads.
    changed = {**attrs, "communities": {"ext-community": ["target:9002:9002"]}}
    apply(stream, path, changed)
    age_tables(fabric)
    assert render()["rows"][0]["RT"] == "9002:9002"
    assert device.gets.count((path, "state")) == 2


def test_fallback_cache_survives_unrelated_updates_but_not_ancestor_delete():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device)
    try:
        first = stream.direct_get(LLDP_PATH, "state")
        apply(stream, "/system/information/current-datetime", "later")
        assert stream.direct_get(LLDP_PATH, "state") is first
        assert len(device.gets) == 1
        apply(stream, "/system/lldp", delete=True)
        stream.direct_get(LLDP_PATH, "state")
        assert len(device.gets) == 2
    finally:
        stream.stop()


def test_path_counters_ignore_route_keys_but_respect_schema_branches():
    versions = PathVersions()
    route = shape("/network-instance[name=*]/bgp-rib")
    other = shape("/network-instance[name=*]/protocols/bgp")
    for i in range(1000):
        versions.touch(f"/network-instance[name=vrf{i}]/bgp-rib/route[prefix=10.0.0.{i}/32]/valid")
    assert versions.version(route) > 0
    assert versions.version(other) == 0
    assert len(versions._exact) == 1
    versions.touch("/network-instance")
    assert versions.version(route) == versions.version(other)


@pytest.mark.parametrize("method", ["query", "lookup"])
def test_narrow_reads_keep_the_original_get_expiry(method, monkeypatch):
    path = "/network-instance[name=default]/route-table/next-hop[index=1]"
    device = FakeDevice({path: [{path.lstrip("/"): {"index": 1}}]})
    stream = HostStream("leaf1", device)
    now = time.time()
    monkeypatch.setattr("nornir_srl.server.stream.time.time", lambda: now)
    try:
        read = getattr(stream, method)
        read([path], "state")
        now += stream.get_ttl - 1
        with stream.track_reads() as dependencies:
            read([path], "state")
        assert stream.reads_current(dependencies)
        assert len(device.gets) == 1
        now += 2
        assert not stream.reads_current(dependencies)
        read([path], "state")
        assert len(device.gets) == 2
    finally:
        stream.stop()


def test_failed_fallback_only_retries_for_a_related_update():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device)
    try:
        device.down = True
        with pytest.raises(RuntimeError):
            stream.direct_get(LLDP_PATH, "state")
        device.down = False
        apply(stream, "/system/information/current-datetime", "later")
        with pytest.raises(RuntimeError):
            stream.direct_get(LLDP_PATH, "state")
        assert len(device.gets) == 1
        apply(stream, "/system/lldp", delete=True)
        assert stream.direct_get(LLDP_PATH, "state") == LLDP_RESPONSE
        assert len(device.gets) == 2
    finally:
        stream.stop()


def test_concurrent_clients_share_one_encoding(monkeypatch):
    from nornir_srl.server import table as module

    original = module._encode
    calls = []
    started, release = threading.Event(), threading.Event()

    def encode(table):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return original(table)

    monkeypatch.setattr(module, "_encode", encode)
    table = Table(columns=["Node"], rows=[{"Node": "leaf1"}], errors=[])
    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(serialize_table, table) for _ in range(4)]
        assert started.wait(5)
        release.set()
        results = [future.result(5) for future in futures]
    assert len(calls) == 1
    assert all(result is results[0] for result in results)
    assert json.loads(results[0].body) == table


@pytest.mark.parametrize("field", ["checks", "tree", "graph", "records"])
def test_visual_changes_are_sent_even_when_rows_are_unchanged(field):
    base = {"columns": ["Node"], "rows": [], "errors": [], field: []}
    assert table_digest(base) != table_digest({**base, field: [{"state": "down"}]})
    assert table_digest(base) == table_digest({**base, "generated": 1, "render_ms": 2, "oldest_update": 3})


@pytest.mark.anyio
async def test_encoding_does_not_block_the_event_loop(store, monkeypatch):
    from nornir_srl.server import table as module

    fabric, _devices = store
    original = module._encode
    started, release = threading.Event(), threading.Event()
    worker_threads = []

    def encode(table):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2), "the event loop could not release the encoding worker"
        return original(table)

    monkeypatch.setattr(module, "_encode", encode)

    async def connected():
        return False

    stream = table_events(fabric, "probe", lambda: Table(rows=[]), 0.01, connected)
    pending = asyncio.create_task(anext(stream))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        release.set()
        assert (await pending).startswith(b"event: table")
        assert len(worker_threads) == 1
        assert worker_threads[0] != threading.get_ident()
    finally:
        release.set()
        await stream.aclose()
