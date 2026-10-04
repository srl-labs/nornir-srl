"""The server's timeline, its baseline, and the fabric's health on the topology."""

from typing import Any, Dict

import pytest
from starlette.testclient import TestClient

from nornir_srl.changes import ERROR, OK
from nornir_srl.checks import Finding
from nornir_srl.incidents import correlate, locate
from nornir_srl.fabric import FabricState
from nornir_srl.lenses import get_lens
from nornir_srl.server.app import create_app
from nornir_srl.server.store import FabricStore
from nornir_srl.server.timeline import Timeline, Watcher
from nornir_srl.server.topology import annotate_health, build_topology, node_facts, summarize

from .fakes import wait_for
from .test_server_app import fabric  # noqa: F401 - the fixture


BGP_NEIGHBOR = "network-instance[name=default]/protocols/bgp/neighbor[peer-address=192.168.1.1]"


@pytest.fixture
def watched(fabric):  # noqa: F811 - the fixture imported above
    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    watcher = Watcher(store, store.timeline, interval=0)
    yield store, watcher, devices
    store.stop()


def _session_state(store: FabricStore, node: str) -> str:
    state = store.fabric_state(None, ("bgp_peers",), history=False)
    return next((peer.state for n, _entry, peer in state.sub_items("bgp_peers", "neighbors") if n == node), "")


def test_the_timeline_starts_after_the_warmup_with_a_baseline(watched):
    store, watcher, _devices = watched
    watcher.tick()
    assert store.timeline.baseline is None, "the first reading is still bootstrapping"
    watcher.tick()
    assert store.timeline.baseline is store.timeline.latest
    assert store.timeline.changes() == []


def test_a_session_going_down_is_on_the_timeline_and_in_the_drift(watched):
    store, watcher, devices = watched
    watcher.tick()
    watcher.tick()
    devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "active")])
    assert wait_for(lambda: _session_state(store, "leaf1") == "active")
    watcher.tick()

    (change,) = [c for c in store.timeline.changes() if c.kind == "bgp"]
    assert (change.node, change.subject, change.before, change.after, change.severity) == (
        "leaf1", "default/192.168.1.1", "established", "active", ERROR
    )
    assert any(c.kind == "bgp" and c.node == "leaf1" for c in store.timeline.drift())

    # The finding waits for a second reading, so a blip never reaches the timeline.
    assert not [c for c in store.timeline.changes() if c.kind == "finding" and "bgp_down" in c.subject]
    watcher.tick()
    assert [c for c in store.timeline.changes() if c.kind == "finding" and "bgp_down" in c.subject]

    devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "established")])
    assert wait_for(lambda: _session_state(store, "leaf1") == "established")
    watcher.tick()
    assert [c.severity for c in store.timeline.changes() if c.kind == "bgp"][0] == OK


def test_the_drift_is_dated_by_when_the_timeline_saw_it(fabric):  # noqa: F811
    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    now = [1000.0]
    watcher = Watcher(store, store.timeline, interval=0, clock=lambda: now[0])
    try:
        watcher.tick()
        watcher.tick()
        now[0] = 1100.0
        devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "active")])
        assert wait_for(lambda: _session_state(store, "leaf1") == "active")
        watcher.tick()
        now[0] = 1900.0
        watcher.tick()

        (drifted,) = [c for c in store.timeline.drift() if c.kind == "bgp"]
        assert drifted.at == 1100.0, "when it went down, not the latest reading"
    finally:
        store.stop()


def test_the_changes_lens_answers_from_the_timeline(watched):
    store, watcher, devices = watched
    watcher.tick()
    watcher.tick()
    devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "idle")])
    assert wait_for(lambda: _session_state(store, "leaf1") == "idle")
    watcher.tick()

    lens = get_lens("changes")
    recent = store.lens_table(lens, None, {"since": "15m"})
    assert any(row["Kind"] == "bgp" and row["Change"] == "established -> idle" for row in recent["rows"])
    drift = store.lens_table(lens, None, {"since": "baseline"})
    assert any(row["Kind"] == "bgp" for row in drift["rows"])
    # Filtered to another node, the change on leaf1 is not shown.
    filtered = store.lens_table(lens, {"hostname": "spine1"}, {"since": "15m"})
    assert all(row["Node"] == "spine1" for row in filtered["rows"])


def test_the_baseline_can_be_taken_again(watched):
    store, watcher, _devices = watched
    watcher.tick()
    watcher.tick()
    first = store.timeline.baseline
    watcher.tick()
    store.set_baseline()
    assert store.timeline.baseline is not first
    assert store.timeline.drift() == []


def test_the_timeline_remembers_the_cables_lldp_showed(watched):
    store, watcher, _devices = watched
    watcher.tick()
    assert store.timeline.cables("leaf1")


def test_a_timeline_keeps_only_what_it_has_room_for():
    from nornir_srl.changes import Change

    timeline = Timeline(capacity=3)
    timeline.record(Change(float(i), "leaf1", "bgp", str(i), "a", "b", ERROR) for i in range(5))
    assert [c.subject for c in timeline.changes()] == ["4", "3", "2"]
    assert [c.subject for c in timeline.changes(since=3.5)] == ["4"]


# --------------------------------------------------------------------------- #
# the topology
# --------------------------------------------------------------------------- #


def _node(name: str, peer: str, port: str = "ethernet-1/1", remembered=None, oper: str = "up"):
    lldp = {"interface": [{"name": port, "neighbor": [{"system-name": peer, "port-id": port}]}]} if peer else {"interface": []}
    return node_facts(
        name,
        snapshot={
            "system": {"name": {"host-name": name}, "lldp": lldp},
            "interface": [{"name": port, "oper-state": oper}],
        },
        remembered=remembered,
    )


def test_a_cable_lldp_lost_is_still_drawn_down():
    remembered = {"ethernet-1/1": ("spine1", "ethernet-1/1")}
    graph = build_topology(
        [
            _node("leaf1", "", remembered=remembered, oper="down"),
            _node("spine1", "", remembered={"ethernet-1/1": ("leaf1", "ethernet-1/1")}, oper="down"),
        ]
    )
    (link,) = graph["links"]
    assert link["lost"] is True
    assert link["state"] == "down"


def test_a_cable_one_end_still_hears_is_not_lost():
    graph = build_topology(
        [
            _node("leaf1", "spine1"),
            _node("spine1", "", remembered={"ethernet-1/1": ("leaf1", "ethernet-1/1")}),
        ]
    )
    (link,) = graph["links"]
    assert link["lost"] is False


def test_findings_are_drawn_on_the_node_and_the_cable_they_are_about():
    graph = build_topology([_node("leaf1", "spine1"), _node("spine1", "leaf1")])
    findings = [
        Finding("itf_down", "error", "leaf1", "ethernet-1/1.0", "oper down"),
        Finding("mtu_outlier", "warning", "spine1", "ip-mtu", "1500"),
    ]
    state = FabricState()
    annotate_health(graph, locate(findings, state), correlate(findings, state))
    nodes = {n["name"]: n for n in graph["nodes"]}
    assert nodes["leaf1"]["findings"] == {"error": 1, "warning": 0} and nodes["leaf1"]["health"] == "error"
    assert nodes["spine1"]["health"] == "warning"
    (link,) = graph["links"]
    assert link["health"] == "error" and [f["check"] for f in link["findings"]] == ["itf_down"]
    assert [i["severity"] for i in graph["incidents"]] == ["error", "warning"]
    assert graph["summary"][-1].startswith("1 error incident, 1 warning")


def test_a_healthy_fabric_says_so():
    graph = build_topology([_node("leaf1", "spine1"), _node("spine1", "leaf1")])
    annotate_health(graph, [], [])
    assert graph["summary"][-1] == "No findings: every check passes"
    assert summarize({"nodes": [], "roles": {}})[0] == "No nodes have reported yet"


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(fabric, tmp_path):  # noqa: F811 - the fixture imported above
    nornir, devices = fabric
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02, snapshot_dir=tmp_path)
    with TestClient(app) as test_client:
        yield test_client, devices


def test_the_timeline_and_baseline_endpoints(client):
    test_client, _devices = client
    status = test_client.get("/api/timeline").json()
    assert status["baseline_at"] is None and status["watch_interval"] == 0
    taken = test_client.post("/api/baseline").json()
    assert taken["baseline_at"] is not None
    assert test_client.get("/api/timeline").json()["baseline_at"] == taken["baseline_at"]


def test_the_overview_and_topology_carry_the_fabric_s_health(client):
    test_client, _devices = client
    health: Dict[str, Any] = test_client.get("/api/overview").json()["health"]
    assert {"incidents", "errors", "warnings", "findings", "worst", "changes_15m", "baseline_at"} <= set(health)
    graph = test_client.get("/api/topology").json()
    assert "summary" in graph and "incidents" in graph
    assert all("health" in node for node in graph["nodes"])


def test_the_incidents_lens_is_a_report_like_any_other(client):
    test_client, _devices = client
    table = test_client.get("/api/report/incidents").json()
    assert table["columns"][:3] == ["Node", "Severity", "Incident"]
    assert isinstance(table["tree"], list)


def test_the_cables_learned_survive_a_restart(tmp_path):
    """A server started during an outage has never seen the down link's LLDP."""
    from nornir_srl.records import LldpInterface, LldpNeighbor

    path = tmp_path / "cabling" / "fabric.json"
    first = Timeline()
    state = FabricState(reports={"lldp": {"leaf1": [LldpInterface("ethernet-1/1", (LldpNeighbor("spine1", "ethernet-1/1"),))]}})
    assert first.learn_cabling(state)
    assert not first.learn_cabling(state), "nothing new the second time"
    first.save_cabling(path)

    second = Timeline()
    second.load_cabling(path)
    assert second.cables("leaf1") == {"ethernet-1/1": ("spine1", "ethernet-1/1")}
    assert second.scoped(["leaf1"]).cabling() == {"leaf1": {"ethernet-1/1": ("spine1", "ethernet-1/1")}}


def test_a_missing_or_broken_cabling_file_is_no_cabling(tmp_path):
    timeline = Timeline()
    timeline.load_cabling(tmp_path / "absent.json")
    (tmp_path / "broken.json").write_text("{not json")
    timeline.load_cabling(tmp_path / "broken.json")
    assert timeline.cabling == {}


def test_the_correlation_uses_the_remembered_cabling():
    from nornir_srl.checks import Finding as F

    timeline = Timeline()
    timeline.cabling = {"leaf1": {"ethernet-1/1": ("spine1", "ethernet-1/1")}}
    state = FabricState(reports={"lldp": {"leaf1": [], "spine1": []}})
    state.hostnames = {"leaf1": "leaf1", "spine1": "spine1"}
    state.history = timeline.scoped(["leaf1", "spine1"])
    findings = [F("itf_down", "error", "leaf1", "ethernet-1/1.0", "down"), F("itf_down", "error", "spine1", "ethernet-1/1.0", "down")]
    (incident,) = correlate(findings, state)
    assert incident.kind == "link"


def test_watched_prefixes_over_the_api(fabric, tmp_path):  # noqa: F811 - the fixture
    nornir, _devices = fabric
    app = create_app(nornir, resync_interval=0, snapshot_dir=tmp_path / "snapshots", watch_prefixes=["10.1.4.16"])
    with TestClient(app) as client:
        assert client.get("/api/watch").json() == {"watched": ["10.1.4.16/32"]}
        assert client.post("/api/watch", json={"prefix": "6.6.6.0/24"}).json()["watched"] == ["10.1.4.16/32", "6.6.6.0/24"]
        assert client.post("/api/watch", json={"prefix": "not-a-prefix"}).status_code == 400
        assert client.post("/api/unwatch", json={"prefix": "10.1.4.16"}).json()["watched"] == ["6.6.6.0/24"]
        assert client.post("/api/unwatch", json={"prefix": "10.1.4.16"}).status_code == 404


def test_the_changes_lens_narrows_down_to_kinds_and_severities(watched):
    store, watcher, devices = watched
    watcher.tick()
    watcher.tick()
    devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "idle")])
    assert wait_for(lambda: _session_state(store, "leaf1") == "idle")
    watcher.tick()
    watcher.tick()

    lens = get_lens("changes")
    everything = store.lens_table(lens, None, {"since": "15m"})
    kinds = {row["Kind"] for row in everything["rows"]}
    assert {"bgp", "finding"} <= kinds

    bgp = store.lens_table(lens, None, {"since": "15m", "kind": "bgp"})
    assert bgp["rows"] and {row["Kind"] for row in bgp["rows"]} == {"bgp"}
    # The cards are drawn from the same narrowed changes as the rows.
    titles = [item["title"] for card in bgp["tree"] for entry in card["entries"] for item in entry["items"]]
    assert titles and all(title.startswith("bgp ") for title in titles)

    # 'all' is everything the timeline holds, as an empty since is.
    assert store.lens_table(lens, None, {"since": "all"})["rows"] == store.lens_table(lens, None, {})["rows"]

    errors = store.lens_table(lens, None, {"since": "15m", "kind": "bgp,finding", "severity": "error"})
    assert errors["rows"] and {row["Severity"] for row in errors["rows"]} == {"error"}
    assert {row["Kind"] for row in errors["rows"]} <= {"bgp", "finding"}


def test_a_kind_of_change_that_does_not_exist_is_refused():
    from nornir_srl.lenses import coerce_lens_params

    lens = get_lens("changes")
    assert coerce_lens_params(lens, {"kind": "BGP, config,bgp"}) == {"kind": "bgp,config"}
    with pytest.raises(ValueError, match="bogus is not one of"):
        coerce_lens_params(lens, {"kind": "bgp,bogus"})
    assert coerce_lens_params(lens, {"kind": " , "}) == {}
