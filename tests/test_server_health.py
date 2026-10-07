"""The incidents and the overview answer from the watcher's reading, not their own."""

import time

import pytest

from nornir_srl.checks import run_checks
from nornir_srl.incidents import correlate
from nornir_srl.server.store import FabricStore
from nornir_srl.server.timeline import Reading

from .test_checks import _ni, _vxlan, fabric as fabric_state
from .test_server_app import fabric  # noqa: F401 - the fixture


def _reading(at: float) -> Reading:
    # leaf1 and spine1 disagree about the VNI of one service.
    state = fabric_state(
        ni={**_ni("leaf1"), **_ni("spine1")},
        vxlan={**_vxlan("leaf1", vni=100), **_vxlan("spine1", vni=200)},
    )
    findings = run_checks(state)
    return Reading(at=at, state=state, findings=findings, incidents=correlate(findings, state))


@pytest.fixture
def store(fabric, monkeypatch):  # noqa: F811 - the fixture
    nornir, _devices = fabric
    store = FabricStore(nornir, resync_interval=0, watch_interval=15)

    def no_reading(*_args, **_kwargs):
        raise AssertionError("health read the fabric next to the watcher")

    monkeypatch.setattr(store, "fabric_state", no_reading)
    return store


def test_an_old_watcher_reading_answers_rather_than_a_reading_of_its_own(store):
    reading = _reading(time.time() - 3600)
    store.timeline.latest = reading
    assert store.health() is reading
    assert store.health_within(None, 0) is reading


def test_a_filter_narrows_the_watchers_reading_and_checks_it_again(store):
    reading = _reading(time.time())
    store.timeline.latest = reading
    assert {f.node for f in reading.findings if f.check == "evpn_service_mismatch"} == {"leaf1", "spine1"}

    narrowed = store.health({"role": "leaf"})
    # leaf1 alone has nobody to disagree with.
    assert set(narrowed.state.hostnames) == {"leaf1"}
    assert set(narrowed.state.reports["vxlan"]) == {"leaf1"}
    assert not [f for f in narrowed.findings if f.check == "evpn_service_mismatch"]
    assert store.health_within({"role": "leaf"}, 0) is narrowed
    # The watcher's own reading is left as it was.
    assert store.health() is reading

    # Narrowed again once the watcher reads anew, not before.
    assert store.health({"role": "leaf"}) is narrowed
    store.timeline.latest = _reading(time.time())
    assert store.health({"role": "leaf"}) is not narrowed


def test_the_watchers_first_reading_is_waited_for(store, monkeypatch):
    first = _reading(time.time())
    monkeypatch.setattr(type(store.watcher), "running", property(lambda _self: True))
    sleeps = []

    def sleep(_seconds):
        # The watcher finishes its first reading while health waits.
        sleeps.append(_seconds)
        store.timeline.latest = first

    monkeypatch.setattr("nornir_srl.server.store.time.sleep", sleep)
    assert store.health() is first
    assert sleeps


def test_without_a_watcher_reading_the_checks_read_the_fabric(store, monkeypatch):
    """A first reading that failed is not waited on for ever."""
    store.watcher.attempts = 1
    monkeypatch.setattr(type(store.watcher), "running", property(lambda _self: True))
    with pytest.raises(AssertionError, match="next to the watcher"):
        store.health()


def _down(state):
    """*state* with spine1 answering no report, and leaf1 missing just one."""
    for report in ("lldp", "ni", "vxlan"):
        state.reports.get(report, {}).pop("spine1", None)
        state.errors[(report, "spine1")] = "not reachable: no route to spine1"
    state.errors[("lldp", "leaf1")] = "timed out"
    return state


def test_incidents_leave_an_unreachable_node_to_its_card(store):
    from nornir_srl.lenses import LENSES_BY_NAME

    reading = _reading(time.time())
    _down(reading.state)
    store.timeline.latest = reading
    errors = store.lens_table(LENSES_BY_NAME["incidents"])["errors"]
    # One report missing on a node that answered the others is still said.
    assert errors == [{"node": "leaf1", "error": "lldp not collected: timed out"}]


def test_other_lenses_say_an_unreachable_node_once(store, monkeypatch):
    from nornir_srl.lenses import LENSES_BY_NAME

    state = _down(_reading(time.time()).state)
    monkeypatch.setattr(store, "fabric_state", lambda *_args, **_kwargs: state)
    errors = store.lens_table(LENSES_BY_NAME["service"], params={"name": "mac-vrf-100"})["errors"]
    assert [e for e in errors if e["node"] == "spine1"] == [
        {"node": "spine1", "error": "no report collected: not reachable: no route to spine1"}
    ]
    assert {"node": "leaf1", "error": "lldp not collected: timed out"} in errors


def test_the_health_card_says_how_old_its_reading_is_and_whether_one_runs(store):
    store.timeline.latest = _reading(time.time() - 40)
    summary = store._health_summary(None, ["leaf1", "spine1"], 0)
    assert 39 <= summary["age"] <= 45
    assert summary["reading_since"] is None
    assert summary["watch_interval"] == 15

    store.watcher.reading_since = time.time() - 2
    assert store._health_summary(None, ["leaf1", "spine1"], 0)["reading_since"] == store.watcher.reading_since
