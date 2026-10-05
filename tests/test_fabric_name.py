"""The fabric the state on disk is kept under, whatever the inventory came from."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nornir_srl.cli import app

TOPO = """
name: mylab
topology:
  nodes:
    leaf1:
      kind: nokia_srlinux
"""


def _nornir_config(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hosts.yaml").write_text("leaf1:\n  hostname: 192.0.2.1\n", encoding="utf-8")
    cfg = directory / "nornir_config.yaml"
    cfg.write_text(
        "inventory:\n"
        "  plugin: SimpleInventory\n"
        "  options:\n"
        f"    host_file: {directory / 'hosts.yaml'}\n",
        encoding="utf-8",
    )
    return cfg


def _served(args, env=None):
    """The fabric name and its source, as handed to the server."""
    with patch("nornir_srl.server.app.serve") as serve:
        result = CliRunner().invoke(app, args + ["server"], env=env)
    assert result.exit_code == 0, result.output
    kwargs = serve.call_args.kwargs
    return kwargs["topo_name"], kwargs["fabric_source"]


@pytest.fixture(autouse=True)
def _no_fabric_env(monkeypatch, tmp_path):
    for var in ("FCLI_FABRIC", "FCLI_TOPO", "CLAB_TOPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def test_nornir_inventory_is_named_after_its_directory(tmp_path):
    cfg = _nornir_config(tmp_path / "dc7")
    assert _served(["-c", str(cfg)]) == ("dc7", "nornir")


def test_two_nornir_inventories_do_not_share_a_name(tmp_path):
    one = _served(["-c", str(_nornir_config(tmp_path / "dc1"))])
    two = _served(["-c", str(_nornir_config(tmp_path / "dc2"))])
    assert one[0] != two[0]


def test_clab_topology_is_named_after_the_lab(tmp_path):
    topo = tmp_path / "mylab.clab.yml"
    topo.write_text(TOPO, encoding="utf-8")
    assert _served(["-t", str(topo)]) == ("mylab", "clab")


def test_fabric_option_wins(tmp_path):
    cfg = _nornir_config(tmp_path / "dc7")
    assert _served(["--fabric", "prod", "-c", str(cfg)]) == ("prod", "option")
    topo = tmp_path / "mylab.clab.yml"
    topo.write_text(TOPO, encoding="utf-8")
    assert _served(["--fabric", "prod", "-t", str(topo)]) == ("prod", "option")


def test_fabric_from_the_environment(tmp_path):
    cfg = _nornir_config(tmp_path / "dc7")
    assert _served(["-c", str(cfg)], env={"FCLI_FABRIC": "lab9"}) == ("lab9", "option")
