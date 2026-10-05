"""Tests for how a gNMI connection decides to verify its target."""

from typing import Any, Dict

import pytest

from nornir_srl import clab
from nornir_srl.connections import srlinux
from nornir_srl.connections.srlinux import SrLinux


class FakeGnmiClient:
    """Stands in for pygnmi, recording what it was constructed with."""

    last_kwargs: Dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        FakeGnmiClient.last_kwargs = kwargs

    def connect(self) -> None:
        pass

    def capabilities(self) -> Dict[str, Any]:
        return {"gnmi_version": "0.10.0"}


@pytest.fixture(autouse=True)
def fake_gnmi(monkeypatch):
    FakeGnmiClient.last_kwargs = {}
    monkeypatch.setattr(srlinux, "gNMIclient", FakeGnmiClient)
    # Nothing to connect to: the reachability probe would try a real socket.
    monkeypatch.setattr(srlinux, "_probe", lambda *args, **kwargs: None)
    # Every test wants to see the warning decided fresh.
    monkeypatch.setattr(srlinux, "_warned_unverified", False)
    return FakeGnmiClient


def connect(extras=None):
    """Open a connection and return the kwargs pygnmi was handed."""
    SrLinux().open(
        hostname="leaf1",
        username="admin",
        password="NokiaSrl1!",
        port=57400,
        platform="srlinux",
        extras=extras,
    )
    return FakeGnmiClient.last_kwargs


# --------------------------------------------------------------------------- #
# what reaches pygnmi
# --------------------------------------------------------------------------- #


def test_no_trust_anchor_skips_verification():
    """A containerlab node offers a self-signed certificate and no CA."""
    assert connect()["skip_verify"] is True


def test_a_trust_anchor_is_used_to_verify():
    kwargs = connect({"path_cert": "/tmp/root-ca.pem"})
    assert kwargs["skip_verify"] is False
    assert kwargs["path_cert"] == "/tmp/root-ca.pem"


def test_skip_verify_in_the_inventory_wins_over_the_trust_anchor():
    """Pinning a CA and still asking to skip is a choice, not a contradiction."""
    kwargs = connect({"path_cert": "/tmp/root-ca.pem", "skip_verify": True})
    assert kwargs["skip_verify"] is True


def test_verification_can_be_forced_without_a_trust_anchor():
    """pygnmi then verifies against the system trust store."""
    assert connect({"skip_verify": False})["skip_verify"] is False


def test_skip_verify_is_never_passed_twice():
    """It is consumed from extras, which is splatted into the same call."""
    connect({"skip_verify": True})  # would raise TypeError on a duplicate


def test_the_other_tls_settings_are_passed_through():
    kwargs = connect(
        {
            "path_cert": "/tmp/client.pem",
            "path_key": "/tmp/client.key",
            "path_root": "/tmp/root-ca.pem",
            "override": "leaf1.lab",
        }
    )
    assert kwargs["path_key"] == "/tmp/client.key"
    assert kwargs["path_root"] == "/tmp/root-ca.pem"
    assert kwargs["override"] == "leaf1.lab"


def test_the_caller_s_extras_are_left_alone():
    """open() pops from extras, and an inventory hands out the same dict twice."""
    extras = {"skip_verify": True}
    connect(extras)
    assert extras == {"skip_verify": True}


# --------------------------------------------------------------------------- #
# warning about an unverified fabric
# --------------------------------------------------------------------------- #


def test_an_unverified_connection_warns_once(caplog):
    with caplog.at_level("WARNING", logger="nornir_srl.connections.srlinux"):
        connect()
        connect()
    warnings = [r for r in caplog.records if "not verified" in r.message]
    assert len(warnings) == 1


def test_a_verified_connection_does_not_warn(caplog):
    with caplog.at_level("WARNING", logger="nornir_srl.connections.srlinux"):
        connect({"path_cert": "/tmp/root-ca.pem"})
    assert not [r for r in caplog.records if "not verified" in r.message]


# --------------------------------------------------------------------------- #
# the containerlab inventory
# --------------------------------------------------------------------------- #


def srl_extras(**kwargs) -> Dict[str, Any]:
    groups = clab.srl_groups(**kwargs)
    return groups["srl"]["connection_options"]["srlinux"]["extras"]


def test_clab_groups_carry_no_tls_settings_by_default():
    assert srl_extras() == {}


def test_clab_groups_carry_the_tls_settings_they_are_given():
    assert srl_extras(
        cert_file="/tmp/root-ca.pem",
        skip_verify=False,
        tls_server_name="leaf1.lab",
    ) == {
        "path_cert": "/tmp/root-ca.pem",
        "skip_verify": False,
        "override": "leaf1.lab",
    }


def test_clab_groups_leave_skip_verify_unset_when_it_was_not_asked_for():
    """Unset and False mean different things: one defers, the other verifies."""
    assert "skip_verify" not in srl_extras(cert_file="/tmp/root-ca.pem")
