"""What the server keeps on disk: the history, configurations, kept readings."""

import sqlite3
import time
from typing import Any, Dict

import pytest

from nornir_srl import configs
from nornir_srl.changes import INFO, WARNING, Change, diff_fabric
from nornir_srl.history import LAST_READING, MAX_BASELINES, HistoryError, HistoryStore
from nornir_srl.records import as_dict
from nornir_srl.reports import reading_reports
from nornir_srl.changes import WATCH_REPORTS
from nornir_srl.server.readings import Recorder, replay, replay_reading
from nornir_srl.server.store import FabricStore
from nornir_srl.server.timeline import SERVER_KIND, WHILE_DOWN, Timeline, Watcher

from .fakes import wait_for
from .test_server_app import fabric  # noqa: F401 - the fixture


def _change(at: float, node: str = "leaf1", kind: str = "bgp", subject: str = "default/10.0.0.1", **kw: Any) -> Change:
    return Change(
        at=at,
        node=node,
        kind=kind,
        subject=subject,
        before=kw.get("before", "established"),
        after=kw.get("after", "idle"),
        severity=kw.get("severity", "error"),
        detail=kw.get("detail", ""),
    )


@pytest.fixture
def history(tmp_path):
    store = HistoryStore(tmp_path / "history" / "dc1.sqlite")
    yield store
    store.close()


# --------------------------------------------------------------------------- #
# the history file
# --------------------------------------------------------------------------- #


def test_changes_are_kept_and_read_back_newest_first(history):
    history.add_changes([_change(100.0), _change(200.0, node="spine1", detail="d")])
    kept = history.changes()
    assert [c.at for c in kept] == [200.0, 100.0]
    assert kept[0] == _change(200.0, node="spine1", detail="d")
    assert history.change_count() == (2, 100.0)


def test_a_node_filter_keeps_the_server_s_own_changes(history):
    history.add_changes(
        [_change(1.0), _change(2.0, node="spine1"), _change(3.0, node="fcli", kind=SERVER_KIND, subject="fcli server")]
    )
    assert [c.node for c in history.changes(nodes=["leaf1"])] == ["fcli", "leaf1"]
    assert [c.node for c in history.changes(kinds=["bgp"], since=1.5)] == ["spine1"]
    assert history.changes(nodes=[]) == [c for c in history.changes() if c.kind == SERVER_KIND]


def test_old_changes_are_pruned_and_nothing_else(history):
    now = time.time()
    history.add_changes([_change(now - 40 * 86400), _change(now - 86400)])
    history.save_reading("before", now - 40 * 86400, {"format": 1})
    assert history.prune(30, now=now) == 1
    assert len(history.changes()) == 1
    assert [r.name for r in history.readings()] == ["before"]
    assert history.prune(0, now=now) == 0


def test_readings_are_kept_by_name_and_the_oldest_baselines_dropped(history):
    history.save_reading(LAST_READING, 1.0, {"format": 1, "x": 1})
    for n in range(MAX_BASELINES + 2):
        history.save_reading(f"b{n}", float(n), {"format": 1, "n": n})
    names = {r.name for r in history.readings()}
    assert len(names) == MAX_BASELINES and "b0" not in names and "b1" not in names
    # The server's own last reading is never one of them, and never dropped.
    assert LAST_READING not in names
    assert LAST_READING in {r.name for r in history.readings(include_last=True)}
    meta, payload = history.load_reading("b5")
    assert payload == {"format": 1, "n": 5} and meta.at == 5.0


def test_deleting_the_active_baseline_forgets_it_is_active(history):
    history.save_reading("golden", 1.0, {"format": 1})
    history.set_meta("baseline", "golden")
    assert history.delete_reading("golden")
    assert history.get_meta("baseline") is None
    assert not history.delete_reading("golden")


def test_a_configuration_is_stored_once_however_many_commits_it_stood_after(history):
    tree = {"interface": [{"name": "ethernet-1/1", "admin-state": "enable"}]}
    history.save_config("leaf1", 4, tree, at=10.0, username="admin", comment="first")
    history.save_config("leaf1", 5, tree, at=20.0)
    history.save_config("leaf1", 6, {"interface": []}, at=30.0)
    with sqlite3.connect(history.path) as db:
        assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 2
    assert [v.commit_id for v in history.config_versions("leaf1")] == [6, 5, 4]
    assert history.latest_config("leaf1").commit_id == 6
    assert history.latest_config("leaf1", before=6).commit_id == 5
    version, kept = history.config("leaf1", 4)
    assert kept == tree and version.comment == "first"
    assert history.config("leaf2") is None


def test_the_salt_and_watched_prefixes_outlive_the_connection(tmp_path):
    first = HistoryStore(tmp_path / "dc1.sqlite")
    salt = first.salt()
    first.set_watched(["10.1.4.16/32", "10.1.4.16/32", "6.6.6.0/24"])
    first.close()
    again = HistoryStore(tmp_path / "dc1.sqlite")
    assert again.salt() == salt
    assert again.watched() == ["10.1.4.16/32", "6.6.6.0/24"]
    again.close()


def test_a_history_written_by_a_newer_fcli_is_refused(tmp_path):
    path = tmp_path / "dc1.sqlite"
    HistoryStore(path).set_meta("schema", "99")
    with pytest.raises(HistoryError, match="newer fcli"):
        HistoryStore(path)


def test_a_file_that_is_not_a_history_is_refused(tmp_path):
    path = tmp_path / "dc1.sqlite"
    path.write_text("not a database at all, just text that is long enough" * 40)
    with pytest.raises(HistoryError):
        HistoryStore(path)


def test_the_history_directory_is_its_owner_s_alone(tmp_path):
    store = HistoryStore(tmp_path / "private" / "dc1.sqlite")
    assert (tmp_path / "private").stat().st_mode & 0o077 == 0
    assert store.path.stat().st_mode & 0o077 == 0
    store.close()


# --------------------------------------------------------------------------- #
# configurations
# --------------------------------------------------------------------------- #

RAW_CONFIG = [
    {
        "/": {
            "_annotate": "a note a tool left",
            "srl_nokia-system:system": {
                "srl_nokia-aaa:aaa": {
                    "authentication": {
                        "linuxadmin-user": {"password": "$y$j9T$hash", "ssh-key": ["ssh-ed25519 AAAA"]},
                    },
                    "server-group": [{"name": "local", "type": "srl_nokia-aaa-types:local"}],
                },
                "srl_nokia-tls:tls": {"profile": [{"name": "clab", "key": "$aes1$secret"}]},
                "srl_nokia-dns:dns-instance": [{"name": "default", "server-list": ["10.0.0.1", "10.0.0.2"]}],
            },
            "srl_nokia-interfaces:interface": [
                {"name": "ethernet-1/10", "admin-state": "enable"},
                {"name": "ethernet-1/2", "description": "to spine1", "admin-state": "enable",
                 "subinterface": [{"index": 0, "ipv4": {"admin-state": "enable", "dhcp-client": {}}}]},
            ],
            "srl_nokia-acl:acl": {
                "acl-filter": [
                    {"name": "cpm", "type": "ipv4", "entry": [{"sequence-id": 10, "action": {"accept": {}}}]},
                    {"name": "cpm", "type": "ipv6", "entry": [{"sequence-id": 10, "action": {"accept": {}}}]},
                ]
            },
        }
    }
]


def test_a_configuration_loses_its_modules_and_annotations_and_keeps_no_secret():
    tree = configs.normalize(RAW_CONFIG, salt="s")
    assert set(tree) == {"system", "interface", "acl"}
    aaa = tree["system"]["aaa"]
    assert aaa["server-group"][0]["type"] == "local"
    password = aaa["authentication"]["linuxadmin-user"]["password"]
    assert password.startswith("<redacted ") and "hash" not in password
    # A public key is not a secret; a private one is.
    assert aaa["authentication"]["linuxadmin-user"]["ssh-key"] == ["ssh-ed25519 AAAA"]
    assert "secret" not in configs.canonical(tree)


def test_a_redacted_secret_still_reads_as_changed_when_it_changed():
    one = configs.redact({"password": "a"}, salt="s")["password"]
    assert configs.redact({"password": "a"}, salt="s")["password"] == one
    assert configs.redact({"password": "b"}, salt="s")["password"] != one
    # Another store's digests say nothing about this one's secrets.
    assert configs.redact({"password": "a"}, salt="t")["password"] != one


def test_a_configuration_reads_as_the_set_lines_sr_linux_prints():
    lines = configs.flatten(configs.normalize(RAW_CONFIG))
    assert "set / interface ethernet-1/2 description \"to spine1\"" in lines
    # Natural order: ethernet-1/2 before ethernet-1/10.
    names = [line.split()[3] for line in lines if line.startswith("set / interface ")]
    assert names.index("ethernet-1/2") < names.index("ethernet-1/10")
    # A list keyed by two leaves names the second one.
    assert "set / acl acl-filter cpm type ipv6 entry 10 action accept" in lines
    assert "set / system dns-instance default server-list [ 10.0.0.1 10.0.0.2 ]" in lines
    # A container configured with nothing in it is still a line.
    assert "set / interface ethernet-1/2 subinterface 0 ipv4 dhcp-client" in lines


def test_a_list_s_keys_are_extended_until_they_tell_its_entries_apart():
    trees = [{"route": [{"prefix": "10.0.0.0/8", "owner": "a", "metric": 1}, {"prefix": "10.0.0.0/8", "owner": "b", "metric": 1}]}]
    assert configs.list_keys(trees)[("route",)] == ("prefix", "metric", "owner")
    assert configs.list_keys([{"interface": [{"name": "e1", "mtu": 9000}]}])[("interface",)] == ("name",)


def test_a_change_of_value_reads_as_its_old_line_then_its_new_one():
    before = {"interface": [{"name": "ethernet-1/1", "description": "old", "admin-state": "enable"}]}
    after = {"interface": [{"name": "ethernet-1/1", "description": "new", "admin-state": "enable"},
                           {"name": "ethernet-1/2", "admin-state": "enable"}]}
    diff = configs.diff_trees(before, after)
    assert diff.lines == (
        ("-", "set / interface ethernet-1/1 description old"),
        ("+", "set / interface ethernet-1/1 description new"),
        ("+", "set / interface ethernet-1/2 admin-state enable"),
    )
    assert diff.summary == "+2 -1 lines"
    assert configs.diff_trees(before, before).summary == "no change"
    assert configs.diff_trees(None, before).added == 2


def test_values_that_need_it_are_quoted():
    lines = configs.flatten({"system": {"banner": {"login-banner": 'say "hi" there'}, "flag": True}})
    assert 'set / system banner login-banner "say \\"hi\\" there"' in lines
    assert "set / system flag true" in lines


# --------------------------------------------------------------------------- #
# readings kept and read back
# --------------------------------------------------------------------------- #


@pytest.fixture
def served(fabric):  # noqa: F811 - the fixture imported above
    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    yield store, devices
    store.stop()


def test_a_reading_read_back_is_the_reading_it_was_taken_as(served):
    store, _devices = served
    recorder = Recorder()
    state = store.fabric_state(None, reading_reports(WATCH_REPORTS), history=False, recorder=recorder)
    from nornir_srl.server.readings import payload

    kept = payload(recorder, at=123.0, state=state, connected={"leaf1": True})
    replayed, connected, at = replay(kept)
    assert at == 123.0 and connected == {"leaf1": True}
    assert replayed.hostnames == state.hostnames
    for report, nodes in state.reports.items():
        if report == "ifstats":
            continue
        assert {n: [as_dict(r) if hasattr(r, "__dataclass_fields__") else r for r in items] for n, items in replayed.reports.get(report, {}).items()} == {
            n: [as_dict(r) if hasattr(r, "__dataclass_fields__") else r for r in items] for n, items in nodes.items()
        }, report
    assert diff_fabric(state, replayed) == []
    # The interface rates are derived from samples, not read: not kept.
    assert "ifstats" not in replayed.reports


def test_a_reading_of_another_format_is_not_read():
    with pytest.raises(ValueError):
        replay({"format": 99})


def test_a_report_missing_from_a_kept_reading_is_an_error_of_that_node():
    kept = {"format": 1, "at": 1.0, "nodes": {"leaf1": {"lldp": [{"paths": ["/elsewhere"], "datatype": "state", "response": []}]}}}
    state, _connected, _at = replay(kept)
    assert "not in the kept reading" in state.errors[("lldp", "leaf1")]
    assert replay_reading(kept).findings  # the collection finding


# --------------------------------------------------------------------------- #
# the timeline across restarts
# --------------------------------------------------------------------------- #


def test_the_timeline_reads_back_what_a_previous_run_kept(history):
    first = Timeline(history=history)
    first.watch("10.1.4.16")
    first.record([_change(1.0), _change(2.0, node="spine1")])
    again = Timeline(history=history)
    assert [c.at for c in again.changes()] == [2.0, 1.0]
    assert again.watched() == ["10.1.4.16/32"]
    again.unwatch("10.1.4.16")
    assert Timeline(history=history).watched() == []


def test_a_server_that_was_killed_leaves_a_warning_where_it_was_last_seen(history):
    first = Timeline(history=history)
    first.mark_started(now=100.0)
    first.heartbeat(now=160.0)
    # No mark_stopped: the process was killed.
    Timeline(history=history).mark_started(now=1000.0)
    server = [c for c in history.changes() if c.kind == SERVER_KIND]
    assert [(c.at, c.after, c.severity) for c in server] == [
        (1000.0, "running", INFO),
        (160.0, "stopped", WARNING),
        (100.0, "running", INFO),
    ]
    assert "not running for 14m" in server[0].detail


def test_a_server_that_stopped_cleanly_says_how_long_it_was_stopped(history):
    first = Timeline(history=history)
    first.mark_started(now=100.0)
    first.mark_stopped(now=200.0)
    Timeline(history=history).mark_started(now=200.0 + 3 * 3600 + 120)
    server = [c for c in history.changes() if c.kind == SERVER_KIND]
    assert [c.after for c in server] == ["running", "stopped", "running"]
    assert server[0].detail == "started; not running for 3h 2m"
    assert all(c.severity == INFO for c in server)


def _persistent(store: FabricStore, history: HistoryStore) -> Watcher:
    """*store*'s timeline and watcher, kept in *history*."""
    store.history = history
    store.timeline = Timeline(history=history)
    store.watcher = Watcher(store, store.timeline, interval=0, persist_every=1)
    return store.watcher


def test_what_changed_while_stopped_is_recorded_on_the_next_start(served, history):
    store, devices = served
    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    assert history.load_reading(LAST_READING) is not None

    # The server restarts; meanwhile a session went down.
    devices["leaf1"].push(
        "network-instance[name=default]/protocols/bgp/neighbor[peer-address=192.168.1.1]",
        [("session-state", "idle")],
    )
    assert wait_for(
        lambda: any(
            p.state == "idle"
            for _n, _e, p in store.fabric_state(None, ("bgp_peers",), history=False).sub_items("bgp_peers", "neighbors")
        )
    )
    time.sleep(0.01)
    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    caught_up = [c for c in store.timeline.changes() if c.detail.startswith(WHILE_DOWN)]
    assert any(c.kind == "bgp" and c.after == "idle" for c in caught_up), caught_up


def test_a_baseline_set_before_a_restart_is_compared_against_after_it(served, history):
    store, _devices = served
    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    status = store.set_baseline("before-upgrade", note="maintenance 42")
    assert status["baseline_name"] == "before-upgrade"
    assert [b["name"] for b in store.baselines()["baselines"]] == ["before-upgrade"]

    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    assert store.timeline.baseline_name == "before-upgrade"
    assert store.timeline.baseline.replayed
    # Nothing changed, and a reading read back holds no interface rates:
    # that alone must not read as drift.
    assert store.timeline.drift() == []

    store.use_baseline(None)
    assert store.timeline.baseline_name is None and history.get_meta("baseline") is None
    store.use_baseline("before-upgrade")
    assert store.timeline.baseline_name == "before-upgrade"
    with pytest.raises(KeyError):
        store.use_baseline("nope")
    store.delete_baseline("before-upgrade")
    assert store.baselines()["baselines"] == []
    with pytest.raises(ValueError):
        store.set_baseline("__last__")


CONFIG_ONE: Dict[str, Any] = {"srl_nokia-interfaces:interface": [{"name": "ethernet-1/1", "description": "one"}]}
CONFIG_TWO: Dict[str, Any] = {"srl_nokia-interfaces:interface": [{"name": "ethernet-1/1", "description": "two"}]}


def test_each_commit_keeps_the_configuration_and_says_what_it_changed(served, history):
    store, devices = served
    for device in devices.values():
        device.responses["/"] = [{"/": CONFIG_ONE}]
    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    # Settling kept every node's configuration as of its newest commit.
    assert {v.node: v.commit_id for v in history.config_versions()} == {"leaf1": 1, "spine1": 1}

    devices["leaf1"].responses["/"] = [{"/": CONFIG_TWO}]
    devices["leaf1"].push(
        "system/configuration/commit[id=2]",
        [("status", "complete"), ("username", "bob"), ("comment", "rename"), ("name", "default")],
    )
    assert wait_for(lambda: len(store.fabric_state(None, ("config_commits",), history=False).reports["config_commits"]["leaf1"]) == 2)
    watcher.tick()
    commit = next(c for c in store.timeline.changes() if c.kind == "config")
    assert (commit.node, commit.subject) == ("leaf1", "commit 2")
    assert commit.detail.startswith("by bob, 'rename'") and commit.detail.endswith("+1 -1 lines")
    diff = store.config_diff("leaf1")
    assert [line["line"] for line in diff["lines"]] == [
        "set / interface ethernet-1/1 description one",
        "set / interface ethernet-1/1 description two",
    ]
    assert diff["against"]["commit"] == 1
    assert store.config_text("leaf1", 1)["lines"] == ["set / interface ethernet-1/1 description one"]
    with pytest.raises(KeyError):
        store.config_text("leaf1", 99)


def test_the_config_diff_lens_answers_from_the_kept_configurations(served, history):
    from nornir_srl.lenses import get_lens

    store, devices = served
    for device in devices.values():
        device.responses["/"] = [{"/": CONFIG_ONE}]
    watcher = _persistent(store, history)
    watcher.tick()
    watcher.tick()
    devices["leaf1"].responses["/"] = [{"/": CONFIG_TWO}]
    devices["leaf1"].push("system/configuration/commit[id=2]", [("status", "complete"), ("username", "bob")])
    assert wait_for(lambda: len(store.fabric_state(None, ("config_commits",), history=False).reports["config_commits"]["leaf1"]) == 2)
    watcher.tick()

    lens = get_lens("config_diff")
    table = store.lens_table(lens, None, {"node": "leaf1", "commit": "2"})
    assert [(row["Op"], row["Line"]) for row in table["rows"]] == [
        ("-", "set / interface ethernet-1/1 description one"),
        ("+", "set / interface ethernet-1/1 description two"),
    ]
    # The commit's change on the timeline links to it.
    changes = store.lens_table(get_lens("changes"), None, {"since": "15m"})
    links = [link for card in changes["tree"] for entry in card["entries"] for item in entry["items"] for link in item["links"]]
    assert {"report": "config_diff", "node": "leaf1"}.items() <= links[0].items()
    assert dict(links[0]["params"]) == {"node": "leaf1", "commit": "2"}
