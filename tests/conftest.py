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
