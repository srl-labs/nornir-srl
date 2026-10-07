"""Fixtures every test gets."""

import pytest


@pytest.fixture(autouse=True)
def _private_state_home(tmp_path_factory, monkeypatch):
    """Keep whatever a test writes under ~/.local/state in a directory of its own.

    The server keeps snapshots, cabling, acknowledgements and its history
    there by default; a test that forgets to point it elsewhere must not
    write into the home directory of whoever runs the suite.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def _no_warm_up(monkeypatch):
    """A store reads only what a test asks of it, not the dashboards as well.

    Warming up at start-up subscribes the overview's and the topology's paths
    before anything asks, which every test counting paths or Gets would have
    to allow for. The tests of the warm-up itself put it back.
    """
    from nornir_srl.server.store import FabricStore

    monkeypatch.setattr(FabricStore, "WARM_UP_REPORTS", ())
