"""Tests for the per-host gNMI subscription session and the device facades."""

import threading
import time
from typing import Any, Dict, List

import pytest

from nornir_srl.server.devices import CachedDevice, RecordingDevice
from nornir_srl.reports import SubscriptionSpec
from nornir_srl.server import stream as stream_module
from nornir_srl.server.stream import HostStream, RateTracker

from .fakes import (
    ES_PATH,
    IFSTATE_PATH,
    IFSTATE_RESPONSE,
    IFSTATS_PATH,
    IFSTATS_RESPONSE,
    LAG_PATH,
    LAG_RESPONSE,
    MAC_EMPTY,
    MAC_PATH,
    MAC_RESPONSE,
    RIB_PATH,
    RIB_RESPONSE,
    LLDP_PATH,
    LLDP_RESPONSE,
    FakeDevice,
    wait_for,
)

#: The production debounce collapses a page load's worth of report activations
#: into one re-subscription; tests only need it to be non-blocking.
TEST_DEBOUNCE = 0.02


@pytest.fixture
def lldp_stream():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state", sample_interval=20)])
    yield stream, device
    stream.stop()


# --------------------------------------------------------------------------- #
# bootstrap + subscription wiring
# --------------------------------------------------------------------------- #


def test_bootstrap_seeds_the_tree_from_a_get(lldp_stream):
    stream, device = lldp_stream
    assert (LLDP_PATH, "state") in device.gets
    assert stream.snapshot(LLDP_PATH) == LLDP_RESPONSE


def test_subscription_request_uses_sample_mode_in_nanoseconds(lldp_stream):
    stream, device = lldp_stream
    assert wait_for(lambda: device.subscribe_requests)
    request = device.subscribe_requests[0]
    assert request["mode"] == "stream"
    assert request["encoding"] == "json_ietf"
    assert request["subscription"] == [
        {"path": LLDP_PATH, "mode": "sample", "sample_interval": 20_000_000_000}
    ]


def test_unsubscribed_path_is_not_snapshotted(lldp_stream):
    stream, _device = lldp_stream
    assert stream.snapshot("/system/features") is None


def test_a_failed_bootstrap_leaves_the_path_pending_with_its_error():
    """A Get that fails must not be taken as proof the path cannot be streamed.

    The path stays a candidate, so it is retried rather than written off; a node
    that is merely unreachable would otherwise never come back.
    """
    device = FakeDevice({})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec("/does/not/exist")])
    try:
        assert stream.snapshot("/does/not/exist") is None
        status = stream.status()
        assert status["paths"][0]["streaming"] is False
        assert status["paths"][0]["pending"] is True
        assert "unexpected path" in status["paths"][0]["error"]
    finally:
        stream.stop()


def test_status_reports_connection_and_paths(lldp_stream):
    stream, _device = lldp_stream
    assert wait_for(lambda: stream.status()["connected"])
    status = stream.status()
    assert status["node"] == "leaf1"
    assert status["paths"][0]["path"] == LLDP_PATH
    assert status["paths"][0]["streaming"] is True


def test_status_counts_one_session_for_the_subscription(lldp_stream):
    stream, _device = lldp_stream
    assert wait_for(lambda: stream.status()["sessions"] == 1)


# --------------------------------------------------------------------------- #
# keeping the session count down
# --------------------------------------------------------------------------- #


def test_a_burst_of_path_additions_causes_one_resubscribe():
    """Opening several reports must not cost one Subscribe RPC per report."""
    device = FakeDevice(
        {
            LLDP_PATH: LLDP_RESPONSE,
            IFSTATS_PATH: IFSTATS_RESPONSE,
            IFSTATE_PATH: IFSTATE_RESPONSE,
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=0.2)
    try:
        for path in (LLDP_PATH, IFSTATS_PATH, IFSTATE_PATH):
            stream.ensure_paths([SubscriptionSpec(path, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        time.sleep(0.5)
        assert len(device.subscribe_requests) == 1
        subscribed = {s["path"] for s in device.subscribe_requests[0]["subscription"]}
        assert subscribed == {LLDP_PATH, IFSTATS_PATH, IFSTATE_PATH}
    finally:
        stream.stop()


def test_re_asserting_known_paths_does_not_resubscribe(lldp_stream):
    stream, device = lldp_stream
    assert wait_for(lambda: device.subscribe_requests)
    for _ in range(5):
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state", sample_interval=20)])
    time.sleep(0.3)
    assert len(device.subscribe_requests) == 1


def test_idle_paths_are_retired():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream(
        "leaf1", device, restart_debounce=TEST_DEBOUNCE, idle_timeout=0.01
    )
    try:
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        assert wait_for(lambda: stream.status()["paths"] == [], timeout=5)
        assert stream.snapshot(LLDP_PATH) is None
        # the subscription carrying the retired path is torn down as well
        assert wait_for(lambda: device.subscribers[0].closed)
    finally:
        stream.stop()


def test_a_glob_path_does_not_see_what_other_paths_added():
    """The LAG report reads ``interface[name=lag*]``; ifstats reads them all.

    Both land in the same ``interface`` envelope of the shared state tree, so
    the snapshot has to re-apply the glob or the LAG report grows a row per
    ethernet interface.
    """
    device = FakeDevice(
        {
            LAG_PATH: LAG_RESPONSE,
            IFSTATS_PATH: IFSTATS_RESPONSE,
            IFSTATE_PATH: IFSTATE_RESPONSE,
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(LAG_PATH, "all")])
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]

        stream.ensure_paths(
            [SubscriptionSpec(IFSTATS_PATH), SubscriptionSpec(IFSTATE_PATH)]
        )
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]
        # the tree holds both, but each path only sees the entries that carry
        # what it asked for, exactly as its own Get would have reported them
        assert set(stream.interfaces()) == {"lag1", "ethernet-1/1"}
        streamed = {i["name"] for i in stream.snapshot(IFSTATS_PATH)[0]["interface"]}
        assert streamed == {"ethernet-1/1"}
    finally:
        stream.stop()


def test_a_glob_path_ignores_streamed_entries_it_did_not_ask_for():
    device = FakeDevice({LAG_PATH: LAG_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(LAG_PATH, "all")])
        assert wait_for(lambda: device.subscribe_requests)
        device.push("", [("interface[name=ethernet-1/5]/oper-state", "up")])
        assert wait_for(lambda: "ethernet-1/5" in stream.interfaces())
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]
    finally:
        stream.stop()


def test_paths_a_report_still_reads_are_not_retired(lldp_stream):
    stream, _device = lldp_stream
    stream.idle_timeout = 60.0
    assert stream.snapshot(LLDP_PATH) is not None
    assert stream._retire_idle_paths() is False
    assert stream.status()["paths"][0]["path"] == LLDP_PATH


# --------------------------------------------------------------------------- #
# ageing out entries the target stopped reporting
# --------------------------------------------------------------------------- #

#: An ethernet-segment whose DF election has two candidates, the second elected.
ES_DF_RESPONSE: List[Dict[str, Any]] = [
    {
        "system/network-instance/protocols/evpn/ethernet-segments": {
            "bgp-instance": [
                {
                    "id": 1,
                    "ethernet-segment": [
                        {
                            "name": "ES-01",
                            "association": {
                                "network-instance": [
                                    {
                                        "name": "subnet-1",
                                        "bgp-instance": [
                                            {
                                                "instance": 1,
                                                "computed-designated-forwarder-candidates": {
                                                    "designated-forwarder-candidate": [
                                                        {
                                                            "address": "192.168.255.1",
                                                            "designated-forwarder": False,
                                                        },
                                                        {
                                                            "address": "192.168.255.2",
                                                            "designated-forwarder": True,
                                                        },
                                                    ]
                                                },
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
]

_ES_CANDIDATE = (
    "system/network-instance/protocols/evpn/ethernet-segments"
    "/bgp-instance[id=1]/ethernet-segment[name=ES-01]"
    "/association/network-instance[name=subnet-1]/bgp-instance[instance=1]"
    "/computed-designated-forwarder-candidates"
    "/designated-forwarder-candidate[address={address}]/designated-forwarder"
)


def _df_candidates(stream) -> List[Dict[str, Any]]:
    envelope = stream.snapshot(ES_PATH)[0][
        "system/network-instance/protocols/evpn/ethernet-segments"
    ]
    segment = envelope["bgp-instance"][0]["ethernet-segment"][0]
    association = segment["association"]["network-instance"][0]["bgp-instance"][0]
    return association["computed-designated-forwarder-candidates"][
        "designated-forwarder-candidate"
    ]


@pytest.fixture
def es_stream(monkeypatch):
    """An ES subscription whose entries go stale in a fraction of a second."""
    monkeypatch.setattr(stream_module, "STALE_ENTRY_TICKS", 0)
    monkeypatch.setattr(stream_module, "MIN_STALE_TTL", 0.2)
    monkeypatch.setattr(stream_module, "PRUNE_INTERVAL", 0.0)
    device = FakeDevice({ES_PATH: ES_DF_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec(ES_PATH, "all", sample_interval=20)])
    assert wait_for(lambda: device.subscribe_requests)
    assert wait_for(lambda: stream.connected)
    # The sweep waits out one TTL of subscription uptime before trusting that a
    # missing entry is really missing; the test has no time to sit through it.
    stream._subscribed_at = time.time() - 60
    yield stream, device
    stream.stop()


def _sample(device, *addresses: str) -> None:
    """One SAMPLE tick, carrying exactly the candidates the node still has."""
    device.push(
        "", [(_ES_CANDIDATE.format(address=a), True) for a in addresses]
    )


def _sample_until(device, addresses, predicate, timeout: float = 3.0) -> bool:
    """Keep sampling *addresses* until *predicate* holds. Eviction is driven by
    arriving notifications, so a tree only ages while the node keeps talking."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _sample(device, *addresses)
        time.sleep(0.05)
        if predicate():
            return True
    return predicate()


def test_a_candidate_the_target_stopped_sending_is_dropped(es_stream):
    """A DF that moved must not leave the old winner behind as a second DF.

    SAMPLE mode re-sends every candidate each tick and never reports a delete,
    so a candidate that stops arriving is one the node no longer has. Keeping it
    rendered two designated forwarders for one segment until the next resync.
    """
    stream, device = es_stream
    assert len(_df_candidates(stream)) == 2

    assert _sample_until(
        device, ["192.168.255.1"], lambda: len(_df_candidates(stream)) == 1
    )
    assert _df_candidates(stream) == [
        {"address": "192.168.255.1", "designated-forwarder": True}
    ]


_ES_CANDIDATE_WITH_MODULES = (
    "srl_nokia-system:system/srl_nokia-system-network-instance:network-instance"
    "/protocols/srl_nokia-system-network-instance-bgp-evpn-ethernet-segments:evpn"
    "/ethernet-segments/bgp-instance[id=1]/ethernet-segment[name=ES-01]"
    "/association/network-instance[name=subnet-1]/bgp-instance[instance=1]"
    "/computed-designated-forwarder-candidates"
    "/designated-forwarder-candidate[address={address}]/designated-forwarder"
)


def test_a_candidate_is_dropped_when_the_target_names_its_modules(es_stream):
    """SR Linux names every element with its YANG module in a subscription.

    The envelope an update refreshes was matched against the path as sent, so
    an envelope named the way a Get answers - without modules - never saw
    anything arrive, and nothing under it was ever evicted: a BGP neighbour
    whose link went down stayed 'established' until the next resync.
    """
    stream, device = es_stream
    assert len(_df_candidates(stream)) == 2

    def sample(address: str) -> None:
        device.push("", [(_ES_CANDIDATE_WITH_MODULES.format(address=address), True)])

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(_df_candidates(stream)) != 1:
        sample("192.168.255.1")
        time.sleep(0.05)
    assert _df_candidates(stream) == [
        {"address": "192.168.255.1", "designated-forwarder": True}
    ]


def test_candidates_the_target_keeps_sending_survive(es_stream):
    stream, device = es_stream
    both = ["192.168.255.1", "192.168.255.2"]
    assert not _sample_until(
        device, both, lambda: len(_df_candidates(stream)) != 2, timeout=1.0
    )


def test_nothing_is_dropped_while_the_subscription_is_down(es_stream):
    """An outage is not the fabric going away, and must not empty the tree."""
    stream, device = es_stream
    stream.connected = False
    assert not _sample_until(
        device, ["192.168.255.1"], lambda: len(_df_candidates(stream)) != 2, timeout=1.0
    )


def test_an_envelope_that_has_gone_quiet_keeps_what_its_last_sample_delivered(es_stream):
    """A stream that is behind delivers its samples late but whole.

    While no newer ethernet-segment data arrives, the sweep - which every
    notification runs, whatever it is about - has nothing to say a candidate
    is gone, however long the clock runs. Once the samples resume without
    the candidate, it goes.
    """
    stream, device = es_stream
    assert len(_df_candidates(stream)) == 2

    def chatter_until(predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            device.push("system", [("name/host-name", "leaf1")])
            time.sleep(0.05)
            if predicate():
                return True
        return predicate()

    assert not chatter_until(lambda: len(_df_candidates(stream)) != 2, timeout=1.0)
    assert _sample_until(device, ["192.168.255.1"], lambda: len(_df_candidates(stream)) == 1)


def test_nothing_is_dropped_before_the_subscription_has_run_a_full_ttl(
    es_stream, monkeypatch
):
    """Entries go stale during an outage because nothing is arriving, not
    because the node dropped them, so a reconnect starts the clock over."""
    stream, device = es_stream
    monkeypatch.setattr(stream_module, "MIN_STALE_TTL", 0.6)
    applied = stream.last_update
    _sample(device, "192.168.255.1")
    assert wait_for(lambda: stream.last_update != applied)
    time.sleep(0.8)  # .2 has gone unrefreshed for longer than the TTL

    stream._subscribed_at = time.time()  # ...but the subscription just came up
    assert not _sample_until(
        device,
        ["192.168.255.1"],
        lambda: len(_df_candidates(stream)) != 2,
        timeout=0.4,
    )

    stream._subscribed_at = time.time() - 60
    assert _sample_until(
        device, ["192.168.255.1"], lambda: len(_df_candidates(stream)) == 1
    )


# --------------------------------------------------------------------------- #
# streamed updates
# --------------------------------------------------------------------------- #


def test_streamed_leaf_update_is_visible_in_the_snapshot(lldp_stream):
    stream, device = lldp_stream
    device.push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"][0][
            "neighbor"
        ][0]["system-name"]
        == "spine9"
    )
    # untouched leaves of the same entry survive the merge
    neighbor = stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"][0]["neighbor"][
        0
    ]
    assert neighbor["port-id"] == "ethernet-1/49"


def test_streamed_delete_removes_a_list_entry(lldp_stream):
    stream, device = lldp_stream
    device.push("system/lldp", deletes=["interface[name=ethernet-1/2]"])
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 1
    )


def test_streamed_delete_handles_dict_elem_paths(lldp_stream):
    stream, device = lldp_stream
    # Directly queue a gNMI delete message containing dict items {"path": ...}
    device.updates.put(
        {"update": {"prefix": "system/lldp", "delete": [{"path": "interface[name=ethernet-1/2]"}]}}
    )
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 1
    )


def test_streamed_update_reaches_the_report_getter(lldp_stream):
    stream, device = lldp_stream
    cached = CachedDevice(stream)
    assert cached.get_lldp_sum()["lldp_nbrs"][0].neighbors[0].system_name == "spine1"
    device.push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: cached.get_lldp_sum()["lldp_nbrs"][0].neighbors[0].system_name == "spine9"
    )


def test_resync_drops_state_the_target_stopped_reporting(lldp_stream):
    stream, device = lldp_stream
    device.push(
        "system/lldp/interface[name=ethernet-1/3]",
        [("neighbor[id=1]/system-name", "ghost")],
    )
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 3
    )
    stream.resync()
    assert len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 2


def test_a_report_does_not_see_the_sibling_branches_srlinux_streams():
    """Subscribing to one branch of ``rib-in-out`` gets the others streamed too.

    The BGP RIB getters recurse through their response to attach path attributes
    to every route they find, and only the ``rib-in-post`` routes carry the
    fields they read, so the siblings must not reach them.
    """
    device = FakeDevice({RIB_PATH: RIB_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(RIB_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        before = stream.status()["last_update"]
        device.push(
            "network-instance[name=default]/bgp-rib/"
            "afi-safi[afi-safi-name=evpn]/evpn/rib-in-out",
            [("rib-in-pre/mac-ip-route[path-id=0]/attr-id", 2)],
        )
        assert wait_for(lambda: stream.status()["last_update"] != before)

        rib_in_out = stream.snapshot(RIB_PATH)[0]["network-instance"][0]["bgp-rib"][
            "afi-safi"
        ][0]["evpn"]["rib-in-out"]
        assert list(rib_in_out) == ["rib-in-post"]
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# paths that are empty when the report is opened
# --------------------------------------------------------------------------- #


def test_an_empty_path_is_pending_rather_than_unstreamable():
    """A table with no entries yet says nothing about its envelope shape."""
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE, get_ttl=0)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        path = stream.status()["paths"][0]
        assert path["pending"] is True
        assert path["streaming"] is False
        assert path["error"] is None
        # there is nothing to subscribe to yet, so no RPC is spent on it
        time.sleep(0.2)
        assert device.subscribe_requests == []
        # the report still renders, off a TTL-cached Get
        assert stream.snapshot(MAC_PATH) is None
        assert stream.direct_get(MAC_PATH, "state") == MAC_EMPTY
    finally:
        stream.stop()


def test_the_bootstrap_response_serves_the_first_render():
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        after_bootstrap = len(device.gets)
        CachedDevice(stream).get(paths=[MAC_PATH], datatype="state")
        assert len(device.gets) == after_bootstrap
    finally:
        stream.stop()


def test_a_pending_path_starts_streaming_once_it_has_entries():
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE, get_ttl=0)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        assert stream.status()["paths"][0]["pending"] is True

        # the network learns a MAC; the next render's fallback Get picks it up
        device.responses[MAC_PATH] = MAC_RESPONSE
        cached = CachedDevice(stream)
        assert cached.get(paths=[MAC_PATH], datatype="state") == MAC_RESPONSE

        assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
        assert wait_for(lambda: device.subscribe_requests)
        assert device.subscribe_requests[0]["subscription"][0]["path"] == MAC_PATH
        assert stream.snapshot(MAC_PATH) == MAC_RESPONSE
    finally:
        stream.stop()


def test_a_promoted_path_then_tracks_streamed_updates():
    device = FakeDevice({MAC_PATH: MAC_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        device.push(
            "network-instance[name=vrf-1]/bridge-table/mac-table",
            [("mac[address=00:AA:BB:CC:DD:EE]/destination", "lag2")],
        )
        assert wait_for(
            lambda: len(
                stream.snapshot(MAC_PATH)[0]["network-instance"][0]["bridge-table"][
                    "mac-table"
                ]["mac"]
            )
            == 2
        )
    finally:
        stream.stop()


def test_a_response_that_cannot_be_placed_in_the_tree_is_not_streamable():
    device = FakeDevice({"/odd": [{"first": 1, "second": 2}]})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec("/odd", "state")])
        path = stream.status()["paths"][0]
        assert path["streaming"] is False
        assert path["pending"] is False
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# fallback Gets
# --------------------------------------------------------------------------- #


def test_cached_device_falls_back_to_a_get_for_unsubscribed_paths(lldp_stream):
    stream, device = lldp_stream
    device.responses["/system/features"] = [{"system/features": ["bridged"]}]
    cached = CachedDevice(stream)
    assert cached.get(paths=["/system/features"], datatype="state") == [
        {"system/features": ["bridged"]}
    ]


def test_fallback_gets_are_ttl_cached(lldp_stream):
    stream, device = lldp_stream
    device.responses["/system/features"] = [{"system/features": ["bridged"]}]
    before = len(device.gets)
    for _ in range(5):
        stream.direct_get("/system/features", "state")
    assert len(device.gets) == before + 1


def test_a_failing_fallback_get_is_not_repeated_within_the_ttl(lldp_stream):
    """A down node must not be asked again by every report on every render."""
    stream, device = lldp_stream
    device.down = True
    before = len(device.gets)
    for _ in range(5):
        with pytest.raises(Exception):
            stream.direct_get("/system/features", "state")
    assert len(device.gets) == before + 1


# --------------------------------------------------------------------------- #
# surviving a node restart
# --------------------------------------------------------------------------- #


def test_a_path_recovers_after_the_node_goes_down_and_comes_back(lldp_stream):
    """Restarting the lab must not cost the node its subscription for good.

    A resync that runs while the node is down used to write the path off as
    unstreamable, which dropped it from the subscription and excluded it from
    every later sweep, so the node stayed dead until the server was restarted.
    """
    stream, device = lldp_stream
    assert wait_for(lambda: stream.status()["paths"][0]["streaming"])

    device.down = True
    stream.resync()
    path = stream.status()["paths"][0]
    assert path["streaming"] or path["pending"], "the path was written off"

    device.down = False
    stream.resync()
    assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
    assert stream.snapshot(LLDP_PATH) == LLDP_RESPONSE


def test_a_resync_against_a_down_node_keeps_the_last_known_state(lldp_stream):
    """Half of a failed re-read must not replace a good tree."""
    stream, device = lldp_stream
    before = stream.snapshot(LLDP_PATH)
    assert before == LLDP_RESPONSE

    device.down = True
    stream.resync()
    assert stream.snapshot(LLDP_PATH) == before


def test_a_resync_subscribes_a_pending_path_that_gained_state():
    """The sweep is also what picks up a table that was empty until now."""
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec(MAC_PATH)])
    try:
        assert stream.status()["paths"][0]["pending"] is True
        device.responses[MAC_PATH] = MAC_RESPONSE
        stream.resync()
        assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
        assert stream.snapshot(MAC_PATH) == MAC_RESPONSE
    finally:
        stream.stop()


def test_a_hanging_get_counts_as_the_node_not_answering():
    """A Get that never returns leaves the node just as unusable as one that fails.

    gNMI calls carry no deadline, so one issued against an address that stopped
    being routed blocks until TCP gives up while holding the Get lock. Reporting
    the node as fine throughout would hide it from the reconnect logic. A Get
    that is merely in flight is not that: those finish inside the hang grace.
    """
    release = threading.Event()

    class Hanging(FakeDevice):
        def get(self, paths, datatype="config", strip_mod=True):
            release.wait(timeout=10)
            return super().get(paths, datatype, strip_mod)

    device = Hanging({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream(
        "leaf1", device, restart_debounce=TEST_DEBOUNCE, get_hang_grace=0.05
    )
    worker = threading.Thread(
        target=lambda: stream.direct_get(LLDP_PATH, "state"), daemon=True
    )
    try:
        assert stream.failing_since is None
        worker.start()
        assert wait_for(lambda: stream.failing_since is not None)
        release.set()
        worker.join(timeout=10)
        assert wait_for(lambda: stream.failing_since is None)
    finally:
        release.set()
        stream.stop()


def test_a_path_the_device_rejects_is_not_the_node_failing():
    """A rejection is the node answering, so the Nodes pane must stay green.

    Reports probe paths a release may not have - the l3vpn RIBs, the IPv6
    route-table the services reports read - and every one of those flipped the
    node red until some later Get happened to succeed.
    """
    class Rejecting(FakeDevice):
        def get(self, paths, datatype="config", strip_mod=True):
            raise ValueError(
                "gNMI Get failed: Path not valid - unknown element 'l3vpn-ipv4-unicast'"
            )

    device = Rejecting({})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        with pytest.raises(ValueError):
            stream.direct_get("/network-instance[name=*]/bgp-rib", "state")
        assert stream.failing_since is None
        assert stream.last_error is None
    finally:
        stream.stop()


def test_a_streamed_update_clears_an_earlier_get_failure(lldp_stream):
    """Once every path streams, no Get runs again to clear the failure itself.

    The node is demonstrably answering while its updates keep arriving, so one
    Get that failed must not leave it red for the rest of the session.
    """
    stream, device = lldp_stream
    assert wait_for(lambda: stream.status()["connected"])
    device.down = True
    with pytest.raises(Exception):
        stream.direct_get("/system/information", "state")
    assert stream.failing_since is not None

    device.down = False
    device.push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(lambda: stream.failing_since is None)
    assert stream.last_error is None


def test_an_in_flight_get_is_not_reported_as_down_until_it_hangs():
    """A resync or bootstrap Get must not flash the Nodes pane red."""
    release = threading.Event()
    started = threading.Event()

    class Slow(FakeDevice):
        def get(self, paths, datatype="config", strip_mod=True):
            started.set()
            release.wait(timeout=10)
            return super().get(paths, datatype, strip_mod)

    device = Slow({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream(
        "leaf1", device, restart_debounce=TEST_DEBOUNCE, get_hang_grace=2.0
    )
    worker = threading.Thread(
        target=lambda: stream.direct_get(LLDP_PATH, "state"), daemon=True
    )
    try:
        worker.start()
        assert wait_for(started.is_set)
        time.sleep(0.1)
        assert stream.getting
        assert stream.status()["getting"] is True
        assert stream.failing_since is None
    finally:
        release.set()
        worker.join(timeout=10)
        stream.stop()


def test_a_node_with_nothing_streamable_is_not_reported_as_connected():
    device = FakeDevice({})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec("/does/not/exist")])
    try:
        assert wait_for(lambda: stream.status()["connected"] is False)
        assert stream.status()["sessions"] == 0
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# path discovery
# --------------------------------------------------------------------------- #


def test_recording_device_captures_the_paths_a_report_needs():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    recorder = RecordingDevice(device)
    result = recorder.get_lldp_sum()
    assert result["lldp_nbrs"][0].name == "ethernet-1/1"
    assert recorder.recorded == [(LLDP_PATH, "state")]


def test_recording_device_deduplicates_repeated_paths():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    recorder = RecordingDevice(device)
    recorder.get_lldp_sum()
    recorder.get_lldp_sum()
    assert recorder.recorded == [(LLDP_PATH, "state")]


# --------------------------------------------------------------------------- #
# interface rates
# --------------------------------------------------------------------------- #


def test_rate_tracker_computes_a_rate_between_two_samples():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 1000}, 1_000_000_000)
    assert tracker.rates("e1") == {}
    tracker.observe("e1", {"in-octets": 2000}, 3_000_000_000)
    assert tracker.rates("e1")["in-octets"] == pytest.approx(500.0)
    assert tracker.all_rates()["e1"]["in-octets"] == pytest.approx(500.0)


def test_rate_tracker_ignores_counter_resets():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 5000}, 1_000_000_000)
    tracker.observe("e1", {"in-octets": 10}, 3_000_000_000)
    assert tracker.rates("e1")["in-octets"] == 0.0


def test_rate_tracker_skips_samples_that_are_too_close_together():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 1000}, 1_000_000_000)
    tracker.observe("e1", {"in-octets": 2000}, 1_100_000_000)
    assert tracker.rates("e1") == {}


def test_ifstats_report_uses_streamed_counter_samples():
    device = FakeDevice(
        {IFSTATS_PATH: IFSTATS_RESPONSE, IFSTATE_PATH: IFSTATE_RESPONSE}
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths(
        [
            SubscriptionSpec(IFSTATS_PATH, sample_interval=1),
            SubscriptionSpec(IFSTATE_PATH, sample_interval=1),
        ]
    )
    try:
        cached = CachedDevice(stream)
        records = cached.get_ifstats()["ifstats"]
        assert records[0].name == "ethernet-1/1"
        assert records[0].oper == "up"
        assert records[0].in_kbps == 0.0  # no second sample yet

        base = 10_000_000_000
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics", dict(IFSTATS_RESPONSE[0]["interface"][0]["statistics"]))],
            timestamp=base,
        )
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics/in-octets", "126000")],
            timestamp=base + 1_000_000_000,
        )
        assert wait_for(lambda: cached.get_ifstats()["ifstats"][0].in_kbps > 0)
        stats = cached.get_ifstats()["ifstats"][0]
        # 125000 octets in 1s -> 1 000 Kbps
        assert stats.in_kbps == pytest.approx(1000.0)
        assert stats.in_octets == 126000
    finally:
        stream.stop()


def test_ifstats_report_counts_errors_over_the_sample_not_since_boot():
    """An error that happened once is not one that is happening now.

    The CLI's two-sample report counts errors between its samples; the server
    counts them between the last two it streamed, so a check reading either
    sees the same thing. The totals it keeps are the packet and octet counters.
    """
    device = FakeDevice({IFSTATS_PATH: IFSTATS_RESPONSE, IFSTATE_PATH: IFSTATE_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths(
        [
            SubscriptionSpec(IFSTATS_PATH, sample_interval=1),
            SubscriptionSpec(IFSTATE_PATH, sample_interval=1),
        ]
    )
    try:
        base = 10_000_000_000
        counters = dict(IFSTATS_RESPONSE[0]["interface"][0]["statistics"])
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics", {**counters, "in-error-packets": "40"})],
            timestamp=base,
        )
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics", {**counters, "in-error-packets": "43", "in-packets": "30"})],
            timestamp=base + 1_000_000_000,
        )
        assert wait_for(lambda: cached_stats(stream).in_errors == 3)
        stats = cached_stats(stream)
        # The three new ones, not the forty-three since boot; the totals are.
        assert (stats.in_errors, stats.in_packets, stats.in_pps) == (3, 30, 20.0)
    finally:
        stream.stop()


def cached_stats(stream: HostStream):
    return CachedDevice(stream).get_ifstats()["ifstats"][0]


def test_a_path_the_node_rejects_is_asked_for_once():
    """A fixed-form chassis has no fabric modules, and says so the same way every time.

    The rejection is kept for as long as the connection lasts: the notifications
    that clear a failed Get - a node that answers again - say nothing about a
    path that is not in its schema.
    """
    path = "/platform/fabric[slot=*]"
    device = FakeDevice({})
    device.get = lambda paths, datatype="config", strip_mod=True: (  # type: ignore[method-assign]
        device.gets.append((paths[0], datatype))
        or (_ for _ in ()).throw(
            Exception("GRPC ERROR Host: n1, Error: Path not valid - unknown element 'fabric'")
        )
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        for _ in range(3):
            with pytest.raises(Exception, match="unknown element"):
                stream.direct_get(path, "state")
            stream._failed_gets.clear()  # what every arriving notification does
        assert [p for p, _dt in device.gets] == [path]
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# the path limit of one Subscribe request
# --------------------------------------------------------------------------- #


def _spec(path: str, interval: int = 10) -> SubscriptionSpec:
    return SubscriptionSpec(path, "state", "sample", interval)


def test_a_path_another_one_covers_is_merged_into_it():
    from nornir_srl.server.stream import plan_subscription

    request, covered, polled = plan_subscription(
        [
            _spec("/interface[name=*]/subinterface", 20),
            _spec("/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor", 10),
            _spec("/interface[name=irb*]/subinterface", 30),
            _spec("/interface[name=lag*]", 20),
            _spec("/platform/control[slot=*]", 60),
            _spec("/platform/control[slot=A]", 10),
        ]
    )
    assert [s.path for s in request] == [
        "/interface[name=*]/subinterface",
        "/interface[name=lag*]",
        "/platform/control[slot=*]",
    ]
    # the covering path is sampled as fast as the fastest it stands in for
    assert {s.path: s.sample_interval for s in request}["/interface[name=*]/subinterface"] == 10
    assert {s.path: s.sample_interval for s in request}["/platform/control[slot=*]"] == 10
    assert covered["/interface[name=irb*]/subinterface"] == "/interface[name=*]/subinterface"
    assert "/interface[name=lag*]" not in covered, "lag* is not everything under name=*"
    assert polled == []


def test_a_narrower_key_does_not_cover_a_wider_one():
    from nornir_srl.server.stream import _covers

    assert not _covers(_spec("/interface[name=lag*]"), _spec("/interface[name=*]/subinterface"))
    assert not _covers(_spec("/network-instance[name=default]"), _spec("/network-instance[name=*]/type"))
    assert _covers(_spec("/network-instance[name=*]"), _spec("/network-instance[name=default]/type"))


def test_what_does_not_fit_is_polled_slowest_first():
    from nornir_srl.server.stream import plan_subscription

    specs = [_spec(f"/a{i}", 10) for i in range(4)] + [_spec("/slow", 60), _spec("/slow/child", 60)]
    request, covered, polled = plan_subscription(specs, limit=4)
    assert len(request) == 4 and "/slow" not in {s.path for s in request}
    # what the polled path covered is polled with it
    assert polled == ["/slow", "/slow/child"] and covered == {}


def test_a_stream_never_asks_for_more_paths_than_a_request_may_carry(monkeypatch):
    monkeypatch.setattr(stream_module, "MAX_SUBSCRIBED_PATHS", 3)
    paths = [f"/p{i}" for i in range(5)]
    device = FakeDevice({p: [{p.strip("/"): {"x": 1}}] for p in paths})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([_spec(p, 10 + i) for i, p in enumerate(paths)])
        assert wait_for(lambda: device.subscribe_requests)
        request = device.subscribe_requests[-1]["subscription"]
        assert len(request) == 3
        status = {p["path"]: p for p in stream.status()["paths"]}
        assert [p for p in paths if status[p]["polled"]] == ["/p3", "/p4"]
        # a polled path is not served from a tree nothing keeps current
        assert stream.snapshot("/p4") is None
        assert stream.snapshot("/p0") is not None
    finally:
        stream.stop()


def test_a_node_that_allows_fewer_paths_is_believed(monkeypatch):
    """SR Linux says how many it takes when it refuses a request."""
    paths = [f"/p{i}" for i in range(4)]
    device = FakeDevice({p: [{p.strip("/"): {"x": 1}}] for p in paths})
    refused = RuntimeError(
        "<_MultiThreadedRendezvous of RPC that terminated with: status = StatusCode.OUT_OF_RANGE "
        'details = "Exceeded the maximum of 2 subscribed paths per subscribe request">'
    )
    original = device.gnmi_subscribe

    def subscribe(request):
        if len(request["subscription"]) > 2:
            device.subscribe_requests.append(request)
            raise refused
        return original(request)

    device.gnmi_subscribe = subscribe  # type: ignore[method-assign]
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE, reconnect_delay=0.05)
    try:
        stream.ensure_paths([_spec(p) for p in paths])
        assert wait_for(lambda: stream.connected, timeout=5)
        assert stream.max_paths == 2
        assert len(device.subscribe_requests[-1]["subscription"]) == 2
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# ON_CHANGE next to SAMPLE
# --------------------------------------------------------------------------- #

NI_ITF_PATH = "/network-instance[name=*]/interface"
NI_TYPE_PATH = "/network-instance[name=*]/type"
SUBIF_PATH = "/interface[name=*]/subinterface"


def _ni_interfaces(stream) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for envelope in stream.snapshot(NI_ITF_PATH) or []:
        for ni in envelope.get("network-instance", []):
            found[ni["name"]] = [itf["name"] for itf in ni.get("interface", [])]
    return found


@pytest.fixture
def mixed_stream(monkeypatch):
    """Instance interfaces on ON_CHANGE, instance types on SAMPLE, one envelope."""
    monkeypatch.setattr(stream_module, "STALE_ENTRY_TICKS", 0)
    monkeypatch.setattr(stream_module, "MIN_STALE_TTL", 0.2)
    monkeypatch.setattr(stream_module, "PRUNE_INTERVAL", 0.0)
    device = FakeDevice(
        {
            NI_ITF_PATH: [
                {
                    "network-instance": [
                        {"name": "default", "interface": [{"name": "ethernet-1/1.0"}]},
                        {"name": "ipvrf-1", "interface": [{"name": "irb0.1"}, {"name": "irb0.2"}]},
                    ]
                }
            ],
            NI_TYPE_PATH: [
                {"network-instance": [{"name": "default", "type": "default"}, {"name": "ipvrf-1", "type": "ip-vrf"}]}
            ],
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths(
        [
            SubscriptionSpec(NI_ITF_PATH, "all", mode="on_change"),
            SubscriptionSpec(NI_TYPE_PATH, "all", sample_interval=10),
        ]
    )
    assert wait_for(lambda: device.subscribe_requests)
    assert wait_for(lambda: stream.connected)
    # What SR Linux opens an ON_CHANGE subscription with: every entry, keyed.
    _initial_sync(device, {"default": ["ethernet-1/1.0"], "ipvrf-1": ["irb0.1", "irb0.2"]})
    assert wait_for(lambda: stream.synced)
    stream._subscribed_at = time.time() - 60
    yield stream, device
    stream.stop()


def _initial_sync(device, interfaces: Dict[str, List[str]]) -> None:
    for ni, names in interfaces.items():
        device.push("", [(f"network-instance[name={ni}]/interface[name={n}]/oper-state", "up") for n in names])
    device.updates.put({"sync_response": True})


def _types(device, *names: str) -> None:
    """One SAMPLE tick of the instance types."""
    device.push("", [(f"network-instance[name={n}]/type", "ip-vrf") for n in names])


def test_sample_eviction_leaves_what_on_change_streams_alone(mixed_stream):
    """ON_CHANGE sends nothing while nothing changes, which is not going away.

    The SAMPLE path feeding the same envelope used to age every list under it,
    and an instance's interfaces vanished 45 s after the subscription started.
    """
    stream, device = mixed_stream
    for _ in range(10):
        _types(device, "default", "ipvrf-1")
        time.sleep(0.05)
    assert _ni_interfaces(stream) == {"default": ["ethernet-1/1.0"], "ipvrf-1": ["irb0.1", "irb0.2"]}


def test_an_on_change_delete_removes_the_entry(mixed_stream):
    stream, device = mixed_stream
    device.push("", deletes=["network-instance[name=ipvrf-1]/interface[name=irb0.2]"])
    assert wait_for(lambda: _ni_interfaces(stream).get("ipvrf-1") == ["irb0.1"])


def test_an_instance_on_change_no_longer_holds_is_aged_out_by_sample(mixed_stream):
    """A removed instance: ON_CHANGE deletes its interfaces, SAMPLE stops sending its type."""
    stream, device = mixed_stream
    device.push(
        "",
        deletes=[
            "network-instance[name=ipvrf-1]/interface[name=irb0.1]",
            "network-instance[name=ipvrf-1]/interface[name=irb0.2]",
        ],
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "ipvrf-1" in str(stream.snapshot(NI_TYPE_PATH)):
        _types(device, "default")
        time.sleep(0.05)
    assert "ipvrf-1" not in str(stream.snapshot(NI_TYPE_PATH))
    assert _ni_interfaces(stream) == {"default": ["ethernet-1/1.0"]}


def test_a_resubscription_drops_what_its_initial_sync_did_not_resend(mixed_stream):
    """Deletes made while the subscription was being replaced are never sent.

    The initial sync of the new one is the whole state of an ON_CHANGE path, so
    what it leaves out is gone.
    """
    stream, device = mixed_stream
    stream._restart()
    assert wait_for(lambda: not stream.synced)
    _initial_sync(device, {"default": ["ethernet-1/1.0"], "ipvrf-1": ["irb0.1"]})
    assert wait_for(lambda: _ni_interfaces(stream) == {"default": ["ethernet-1/1.0"], "ipvrf-1": ["irb0.1"]})


def test_deleting_the_last_child_does_not_leave_its_parent_behind():
    """A subscription below a list entry is never told the entry itself went."""
    device = FakeDevice(
        {
            SUBIF_PATH: [
                {
                    "interface": [
                        {"name": "ethernet-1/1", "subinterface": [{"index": 0, "admin-state": "enable"}]},
                        {"name": "lo9", "subinterface": [{"index": 0, "admin-state": "enable"}]},
                    ]
                }
            ]
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(SUBIF_PATH, "all", mode="on_change")])
        assert wait_for(lambda: stream.connected)
        device.push("", [(f"interface[name={n}]/subinterface[index=0]/admin-state", "enable") for n in ("ethernet-1/1", "lo9")])
        device.updates.put({"sync_response": True})
        assert wait_for(lambda: stream.synced)
        device.push("", deletes=["srl_nokia-interfaces:interface[name=lo9]/subinterface[index=0]"])

        def names():
            # The whole root, as the topology and the overview read it: a
            # single path's view hides an entry without the branch it asked for.
            return [itf["name"] for itf in stream.snapshot_roots(("interface",)).get("interface", [])]

        assert wait_for(lambda: names() == ["ethernet-1/1"])
    finally:
        stream.stop()


def test_what_on_change_sends_during_a_resync_is_not_lost(mixed_stream):
    """A resync reads the node with Gets and swaps the result in afterwards.

    A change ON_CHANGE delivers in between lands in the tree about to be
    replaced, and is never sent again: SAMPLE would re-send it next tick.
    """
    stream, device = mixed_stream
    real_get = device.get
    read = threading.Event()
    resume = threading.Event()

    def slow_get(paths, datatype="config", strip_mod=True):
        result = real_get(paths, datatype, strip_mod)
        read.set()
        resume.wait(2)
        return result

    device.get = slow_get
    resyncing = threading.Thread(target=stream.resync)
    resyncing.start()
    assert read.wait(2)
    # Read, not yet swapped in: the node changes now.
    device.push("", deletes=["network-instance[name=ipvrf-1]/interface[name=irb0.2]"])
    device.push("", [("network-instance[name=default]/interface[name=lo0.0]/oper-state", "up")])
    time.sleep(0.2)
    resume.set()
    resyncing.join(3)
    device.get = real_get
    assert _ni_interfaces(stream) == {"default": ["ethernet-1/1.0", "lo0.0"], "ipvrf-1": ["irb0.1"]}


def test_a_resync_tells_the_store_only_once_it_has_let_go_of_the_stream(mixed_stream):
    """The store holds its lock while it reads a stream, so the stream must not
    call into the store while holding its own: the two would wait on each other."""
    stream, device = mixed_stream
    free: List[bool] = []

    def on_update():
        other = threading.Thread(target=lambda: free.append(stream._lock.acquire(timeout=0.5) and (stream._lock.release() or True)))
        other.start()
        other.join()

    stream.on_update = on_update
    real_get = device.get
    read = threading.Event()

    def slow_get(paths, datatype="config", strip_mod=True):
        result = real_get(paths, datatype, strip_mod)
        read.set()
        time.sleep(0.2)
        return result

    device.get = slow_get
    resyncing = threading.Thread(target=stream.resync)
    resyncing.start()
    assert read.wait(2)
    device.push("", [("network-instance[name=default]/interface[name=lo0.0]/oper-state", "up")])
    resyncing.join(5)
    device.get = real_get
    assert free and all(free)


def test_a_path_another_one_covers_does_not_overwrite_its_data():
    """A Get does not say which leaves key a list, so its lists are merged opaquely.

    Reading ``route/ipv4-prefix`` after ``ipv4-unicast`` then replaced every route
    with its keys alone. SAMPLE put the rest back next tick; ON_CHANGE never does.
    """
    table = "/network-instance[name=default]/route-table/ipv4-unicast"
    keys_only = table + "/route/ipv4-prefix"
    route = {"ipv4-prefix": "192.0.2.1/32", "route-type": "bgp", "id": 0}
    device = FakeDevice(
        {
            table: [{"network-instance[name=default]/route-table/ipv4-unicast": {"route": [{**route, "active": True, "metric": 0}]}}],
            keys_only: [{"network-instance[name=default]/route-table/ipv4-unicast": {"route": [dict(route)]}}],
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(table, mode="on_change"), SubscriptionSpec(keys_only, mode="on_change")])

        def routes():
            return [r for env in stream.snapshot(table) for r in env[table.lstrip("/")]["route"]]

        assert [r.get("active") for r in routes()] == [True]
        stream.resync()
        assert [r.get("active") for r in routes()] == [True]
    finally:
        stream.stop()


def test_a_subscription_with_on_change_paths_carries_a_heartbeat(mixed_stream):
    """Without it, a node that fell off the network looks like one where nothing changed."""
    stream, device = mixed_stream
    request = device.subscribe_requests[-1]
    assert stream_module.HEARTBEAT.as_gnmi() in request["subscription"]
    stream.last_update = stream._subscribed_at = time.time() - 60
    assert stream.stale_for is not None


def test_a_sampled_only_subscription_needs_no_heartbeat(lldp_stream):
    stream, device = lldp_stream
    assert wait_for(lambda: device.subscribe_requests)
    assert stream_module.HEARTBEAT.as_gnmi() not in device.subscribe_requests[-1]["subscription"]
