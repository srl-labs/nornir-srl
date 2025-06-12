"""Basic tests for the nornir_srl package."""

from __future__ import annotations

import pathlib
import tomllib

import nornir_srl


def test_version() -> None:
    """Ensure package version matches ``pyproject.toml``."""

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)

    expected_version = data["project"]["version"]

    assert nornir_srl.__version__ == expected_version
