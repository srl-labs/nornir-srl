"""A node that cannot be connected to says why, not that its certificate failed."""

import errno
import socket
import ssl

import pytest
from pygnmi.client import gNMIException

from nornir_srl.checks import Finding
from nornir_srl.connections import srlinux
from nornir_srl.connections.srlinux import NodeUnreachable, SrLinux, _connect_failure, _probe, _unreachable
from nornir_srl.fabric import FabricState
from nornir_srl.incidents import correlate


def _certificate_error(cause: BaseException) -> gNMIException:
    """What pygnmi raises when it cannot fetch a certificate, whatever the reason."""
    return gNMIException("The SSL certificate cannot be retrieved from ('leaf1', 57400)", cause)


@pytest.mark.parametrize(
    "cause,expected",
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "not reachable: leaf1 on gNMI port 57400 refused the connection"),
        (socket.timeout("timed out"), "not responding: no answer from leaf1 on gNMI port 57400"),
        (TimeoutError("timed out"), "not responding: no answer from leaf1 on gNMI port 57400"),
        (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "not reachable: leaf1 does not resolve (Name or service not known)"),
        (OSError(errno.EHOSTUNREACH, "No route to host"), "not reachable: no route to leaf1 on gNMI port 57400 (No route to host)"),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), "not reachable: no route to leaf1 on gNMI port 57400 (Network is unreachable)"),
        (ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), "not responding: leaf1 on gNMI port 57400 closed the connection"),
    ],
)
def test_a_certificate_that_could_not_be_fetched_is_said_as_what_went_wrong(cause, expected):
    clearer = _connect_failure(_certificate_error(cause), "leaf1", 57400)
    assert isinstance(clearer, NodeUnreachable)
    assert str(clearer).startswith(expected)


def test_a_real_tls_failure_is_still_one():
    clearer = _connect_failure(_certificate_error(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")), "leaf1", 57400)
    assert not isinstance(clearer, NodeUnreachable)
    assert str(clearer).startswith("TLS handshake with leaf1 on gNMI port 57400 failed")


def test_any_other_failure_is_left_as_it_came():
    assert _connect_failure(RuntimeError("GRPC ERROR: unauthenticated"), "leaf1", 57400) is None


def test_the_probe_says_a_closed_port_refused():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Nothing listens on it any more.
    with pytest.raises(NodeUnreachable, match="refused the connection"):
        _probe("127.0.0.1", port, timeout=2)


def test_the_probe_lets_a_listening_port_through():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _probe("127.0.0.1", server.getsockname()[1], timeout=2)


class _RefusingClient:
    """pygnmi, for a node that refuses: it reports a certificate problem."""

    def __init__(self, **kwargs):
        pass

    def connect(self):
        raise _certificate_error(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))


def _open(monkeypatch, client, probe=lambda *a, **k: None):
    monkeypatch.setattr(srlinux, "gNMIclient", client)
    monkeypatch.setattr(srlinux, "_probe", probe)
    SrLinux().open(hostname="leaf1", username="admin", password="x", port=57400, platform="srlinux", extras={})


def test_opening_a_connection_says_why_it_failed(monkeypatch):
    with pytest.raises(NodeUnreachable, match="refused the connection"):
        _open(monkeypatch, _RefusingClient)


def test_a_node_the_probe_cannot_reach_is_never_handed_to_pygnmi(monkeypatch):
    def unreachable(host, port, timeout=None):
        raise _unreachable(socket.timeout("timed out"), host, port)

    class Untouched:
        def __init__(self, **kwargs):
            raise AssertionError("pygnmi was asked to connect to a node that does not answer")

    with pytest.raises(NodeUnreachable, match="not responding: no answer from leaf1"):
        _open(monkeypatch, Untouched, probe=unreachable)


def test_an_unreachable_node_s_incident_says_why():
    state = FabricState()
    state.errors[("lldp", "spine1")] = "not reachable: no route to spine1 on gNMI port 57400 (No route to host)"
    findings = [
        Finding("collection", "warning", "spine1", "lldp", "not checked: not reachable: no route to spine1 on gNMI port 57400 (No route to host)")
    ]
    (incident,) = correlate(findings, state)
    assert incident.root.check == "node_unreachable"
    assert incident.root.detail == "no report could be collected: not reachable: no route to spine1 on gNMI port 57400 (No route to host)"
