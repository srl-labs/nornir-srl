"""Tests for the MCP server module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from nornir_srl.mcp_server import (
    _build_server,
    _parse_filter,
    _run_task_json,
)


# ---------------------------------------------------------------------------
# _parse_filter tests
# ---------------------------------------------------------------------------


class TestParseFilter:
    def test_empty_string(self):
        assert _parse_filter("") is None

    def test_none(self):
        assert _parse_filter(None) is None

    def test_single_pair(self):
        assert _parse_filter("site=dc1") == {"site": "dc1"}

    def test_multiple_pairs(self):
        result = _parse_filter("site=dc1,role=leaf")
        assert result == {"site": "dc1", "role": "leaf"}

    def test_whitespace_handling(self):
        result = _parse_filter(" site = dc1 , role = leaf ")
        assert result == {"site": "dc1", "role": "leaf"}

    def test_value_with_equals(self):
        result = _parse_filter("key=val=ue")
        assert result == {"key": "val=ue"}

    def test_no_equals_ignored(self):
        result = _parse_filter("nopair")
        assert result is None


# ---------------------------------------------------------------------------
# _build_server tool registration tests
# ---------------------------------------------------------------------------


class TestBuildServer:
    """Verify that _build_server registers all expected MCP tools."""

    @pytest.fixture()
    def server(self):
        nr = MagicMock()
        return _build_server(nr)

    def test_server_name(self, server):
        assert server.name == "fcli"

    def test_all_tools_registered(self, server):
        expected_tools = {
            "bgp_peers",
            "sys_info",
            "subinterfaces",
            "lag",
            "ipv4_rib",
            "ipv6_rib",
            "bgp_rib",
            "mac_table",
            "network_instances",
            "lldp_neighbors",
            "irb_interfaces",
            "ethernet_segments",
            "es_destinations",
            "vxlan_tunnels",
            "arp_table",
            "ipv6_neighbors",
        }
        registered = set(server._tool_manager._tools.keys())
        assert expected_tools == registered

    def test_tool_count(self, server):
        assert len(server._tool_manager._tools) == 16

    def test_default_host_port(self, server):
        assert server.settings.host == "127.0.0.1"
        assert server.settings.port == 8000

    def test_custom_host_port(self):
        nr = MagicMock()
        server = _build_server(nr, host="0.0.0.0", port=9090)
        assert server.settings.host == "0.0.0.0"
        assert server.settings.port == 9090


# ---------------------------------------------------------------------------
# _run_task_json tests
# ---------------------------------------------------------------------------


class TestRunTaskJson:
    """Test the _run_task_json helper using mocked Nornir objects."""

    def _make_aggregated_result(self, host_name, resource, data):
        """Create a minimal AggregatedResult-like dict for testing."""
        host = MagicMock()
        host.hostname = host_name
        host.name = host_name

        result = MagicMock()
        result.host = host
        result.failed = False
        result.result = {resource: data}

        host_result = [result]

        agg = MagicMock()
        agg.name = resource
        agg.items.return_value = [(host_name, host_result)]
        agg.failed_hosts = []
        return agg

    def test_returns_valid_json(self):
        nr = MagicMock()
        agg = self._make_aggregated_result(
            "leaf1", "bgp_peers", [{"Peer": "10.0.0.1", "State": "established"}]
        )
        nr.filter.return_value.run.return_value = agg
        nr.run.return_value = agg

        def dummy_task(task):
            pass

        output = _run_task_json(nr, "bgp_peers", dummy_task)
        parsed = json.loads(output)
        assert isinstance(parsed, list)

    def test_with_inv_filter(self):
        nr = MagicMock()
        agg = self._make_aggregated_result(
            "leaf1", "sys_info", [{"Version": "24.3.1"}]
        )
        nr.filter.return_value.run.return_value = agg

        def dummy_task(task):
            pass

        output = _run_task_json(
            nr, "sys_info", dummy_task, inv_filter={"role": "leaf"}
        )
        nr.filter.assert_called_once_with(role="leaf")
        parsed = json.loads(output)
        assert isinstance(parsed, list)

    def test_with_field_filter(self):
        nr = MagicMock()
        agg = self._make_aggregated_result(
            "leaf1",
            "bgp_peers",
            [
                {"Peer": "10.0.0.1", "State": "established"},
                {"Peer": "10.0.0.2", "State": "active"},
            ],
        )
        nr.run.return_value = agg

        def dummy_task(task):
            pass

        output = _run_task_json(
            nr,
            "bgp_peers",
            dummy_task,
            field_filter={"State": "established"},
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        # Field filter should keep only matching rows
        for row in parsed:
            assert row.get("State") == "established"

    def test_empty_result(self):
        nr = MagicMock()
        agg = self._make_aggregated_result("leaf1", "arp", [])
        nr.run.return_value = agg

        def dummy_task(task):
            pass

        output = _run_task_json(nr, "arp", dummy_task)
        parsed = json.loads(output)
        assert parsed == []
