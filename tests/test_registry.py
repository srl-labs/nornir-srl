"""Tests for the shared report registry and the row flattening it feeds.

The registry exists so a report is declared once and every surface renders it
the same way. These tests fail if a surface starts drifting from it again.
"""

import inspect

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

from nornir_srl import cli, mcp_server
from nornir_srl.lenses import lenses_for
from nornir_srl.reports import (
    ALL_SURFACES,
    CLI,
    MCP,
    SERVER,
    REPORTS,
    REPORTS_BY_NAME,
    coerce_params,
    get_report,
    reports_for,
)
from nornir_srl.rows import extract, pass_filter


# --------------------------------------------------------------------------- #
# Registry consistency
# --------------------------------------------------------------------------- #


def test_report_names_and_tool_names_are_unique():
    names = [r.name for r in REPORTS]
    assert len(names) == len(set(names))
    tool_names = [r.tool_name for r in reports_for(MCP)]
    assert len(tool_names) == len(set(tool_names))


def test_every_report_is_offered_somewhere():
    for report in REPORTS:
        assert report.surfaces, f"{report.name} is on no surface"
        assert report.surfaces <= ALL_SURFACES


def test_every_getter_is_callable_with_only_a_device():
    """A surface with nothing to pass - the server - must still be able to call."""
    for report in REPORTS:
        params = list(inspect.signature(report.getter).parameters.values())
        assert params, f"{report.name} getter takes no device"
        for param in params[1:]:
            assert (
                param.default is not inspect.Parameter.empty
            ), f"{report.name} getter requires '{param.name}'"


def test_a_declared_parameter_is_one_the_getter_takes():
    """Parameters are handed on as keywords, so a typo would fail every node."""
    for report in REPORTS:
        accepted = set(inspect.signature(report.getter).parameters)
        for spec in report.params:
            assert spec.name in accepted, (
                f"{report.name} declares the parameter '{spec.name}', which its "
                f"getter does not take"
            )


def test_coerce_params_takes_the_declared_ones_and_leaves_the_rest():
    """The query string of a live table is not all the getter's business."""
    params = coerce_params(
        get_report("ipv4_rib"),
        {"address": " 10.1.1.55 ", "refresh": "2", "inv_filter": "role=leaf"},
    )
    assert params == {"address": "10.1.1.55"}


def test_coerce_params_reads_an_unset_parameter_as_absent():
    """An empty box in the browser means the report in full, not a lookup."""
    assert coerce_params(get_report("ipv4_rib"), {"address": "   "}) == {}
    assert coerce_params(get_report("ipv4_rib"), {}) == {}


def test_coerce_params_normalizes_an_address():
    assert coerce_params(get_report("ipv6_rib"), {"address": "2001:0DB8:0000::1"}) == {
        "address": "2001:db8::1"
    }


def test_coerce_params_rejects_what_is_not_an_address():
    """Including a prefix: the RIB reports look up a host, as the CLI does."""
    for value in ("no-such-thing", "10.0.0.0/8", "10.0.0.256"):
        with pytest.raises(ValueError, match="not an IP address"):
            coerce_params(get_report("ipv4_rib"), {"address": value})


def test_a_report_that_declares_no_parameters_is_handed_none():
    assert coerce_params(get_report("lldp"), {"address": "10.0.0.1"}) == {}


def test_coerce_params_detail_for_bgp_rib_reports():
    spec = get_report("bgp_rib_evpn_2")
    assert coerce_params(spec, {"detail": "1"}) == {"detail": True}
    assert coerce_params(spec, {"detail": "true"}) == {"detail": True}
    assert coerce_params(spec, {}) == {}
    assert coerce_params(get_report("ipv4_rib"), {"detail": "1"}) == {}


#: Single leaves SR Linux defines as config rather than state.
CONFIG_LEAVES = frozenset(
    {
        "/interface[name=*]/admin-state",
        "/interface[name=*]/description",
        "/network-instance[name=*]/type",
        "/system/name/host-name",
    }
)


def test_config_leaves_are_not_subscribed_as_state():
    """A 'state' Get on a config leaf answers nothing, and answers it silently.

    The path is then never bootstrapped, so it stays pending forever and the
    report reading it renders as though the node had no such data - which is how
    the topology once put every node of a fabric on one tier.
    """
    for report in REPORTS:
        for spec in report.subscribe:
            if spec.path in CONFIG_LEAVES:
                assert spec.datatype != "state", (
                    f"{report.name} subscribes to the config leaf {spec.path} "
                    "with datatype 'state', which returns nothing"
                )


def _all_subscriptions():
    from nornir_srl.reports import _READING_ONLY

    return [spec for report in list(REPORTS) + list(_READING_ONLY) for spec in report.subscribe]


def test_a_path_is_streamed_one_way_on_every_report():
    """One node streams a path once, whichever report asked for it first."""
    from nornir_srl.reports import ON_CHANGE_PATHS

    specs = _all_subscriptions()
    for spec in specs:
        assert spec.mode == ("on_change" if spec.path in ON_CHANGE_PATHS else "sample"), spec.path
    assert ON_CHANGE_PATHS <= {spec.path for spec in specs}, "an ON_CHANGE path no report reads"


def test_no_on_change_path_overlaps_a_sampled_one():
    """What a sampled path delivers is aged out; what ON_CHANGE delivers is not.

    Two paths of different modes under one another would feed the same entries
    both ways, and which one holds them would depend on which arrived last.
    """
    from nornir_srl.reports import SubscriptionSpec
    from nornir_srl.server.stream import _covers

    specs = {spec.path: spec.mode for spec in _all_subscriptions()}
    for outer, outer_mode in specs.items():
        for inner, inner_mode in specs.items():
            if outer_mode != inner_mode and _covers(SubscriptionSpec(outer), SubscriptionSpec(inner)):
                pytest.fail(f"{outer} ({outer_mode}) covers {inner} ({inner_mode})")


def test_non_tabular_reports_are_not_streamed():
    """The browser only shows tables, so a report without columns cannot stream."""
    for report in REPORTS:
        if not report.tabular:
            assert not report.on(SERVER)


# --------------------------------------------------------------------------- #
# Surfaces match the registry
# --------------------------------------------------------------------------- #


def _cli_commands():
    return {c.callback.__name__: c for c in cli.app.registered_commands}


#: Commands that are neither a report nor a lens: they run the fabric, or run
#: over a report, rather than reading one.
NON_REPORT_COMMANDS = {"server", "diff", "summary"}


def test_cli_exposes_exactly_the_cli_reports():
    commands = _cli_commands()
    expected = {r.name for r in reports_for(CLI)} | {
        lens.name for lens in lenses_for(CLI)
    }
    assert expected <= set(commands)
    assert set(commands) - expected == NON_REPORT_COMMANDS


def test_cli_exposes_every_lens():
    commands = _cli_commands()
    for lens in lenses_for(CLI):
        assert lens.name in commands, f"no CLI command for lens '{lens.name}'"


def test_mcp_exposes_every_lens():
    for lens in lenses_for(MCP):
        tool = getattr(mcp_server, lens.tool_name, None)
        assert tool is not None, f"no MCP tool for lens '{lens.name}'"


def test_every_lens_reads_reports_that_exist():
    for lens in lenses_for(CLI):
        for report in lens.requires:
            assert report in REPORTS_BY_NAME, (
                f"lens '{lens.name}' reads unknown report '{report}'"
            )


@pytest.mark.parametrize("runner", [cli.run_lens, mcp_server._run_lens])
def test_a_lens_parameter_cannot_shadow_the_surface_plumbing(runner):
    """A lens's arguments are passed through as ``**params``.

    ``service`` takes a ``name``; a surface whose own parameter was called
    ``name`` got two of them and crashed on the one command that used it.
    """
    own = {
        p.name
        for p in inspect.signature(runner).parameters.values()
        if p.kind is not p.VAR_KEYWORD
    }
    for lens in lenses_for(CLI) + lenses_for(MCP):
        clash = own & {param.name for param in lens.params}
        assert not clash, (
            f"lens '{lens.name}' takes {sorted(clash)}, which "
            f"{runner.__qualname__} uses for itself"
        )


def test_mcp_exposes_exactly_the_mcp_reports():
    for report in reports_for(MCP):
        tool = getattr(mcp_server, report.tool_name, None)
        assert tool is not None, f"no MCP tool for report '{report.name}'"
        assert tool.__doc__, f"MCP tool '{report.tool_name}' has no description"


def test_server_reports_all_declare_a_resource():
    for report in reports_for(SERVER):
        assert report.resource
        assert report.sample_interval > 0


def test_get_report_rejects_unknown_names():
    with pytest.raises(KeyError):
        get_report("no-such-report")


def test_subscriptions_convert_to_gnmi_sample_intervals():
    spec = next(s for s in get_report("ifstats").subscribe if s.path.endswith("/statistics"))
    assert spec.as_gnmi() == {
        "path": spec.path,
        "mode": "sample",
        "sample_interval": spec.sample_interval * 1_000_000_000,
    }


def test_an_on_change_subscription_carries_no_interval():
    spec = next(s for s in get_report("bgp_peers").subscribe if s.path.endswith("/neighbor"))
    assert spec.as_gnmi() == {"path": spec.path, "mode": "on_change"}


# --------------------------------------------------------------------------- #
# Shared row extraction
# --------------------------------------------------------------------------- #


def _aggregated(resource, per_host, failed=()):
    """Build an AggregatedResult like a Nornir run would."""
    aggregated = AggregatedResult(resource)
    for name, items in per_host.items():
        result = Result(host=Host(name=name, hostname=name), result={resource: items})
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    for name, exception in failed:
        result = Result(host=Host(name=name, hostname=name), exception=exception)
        result.failed = True
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    return aggregated


def test_extract_takes_columns_from_the_item_fields():
    results = _aggregated("ni", {"leaf1": [{"ni": "default", "oper-state": "up"}]})
    columns, per_node = extract("ni", results)
    assert columns == ["ni", "oper-state"]
    assert [r.values for r in per_node[0].rows] == [
        {"ni": "default", "oper-state": "up"}
    ]


def test_extract_unions_the_columns_of_every_host():
    """A field the first host has no row for is still a column.

    Nodes differ in what they carry - only some leaves have IPv6 routes, or a
    leaked route - and the table has to hold the fields of all of them.
    """
    results = _aggregated(
        "ip_rib",
        {
            "leaf1": [{"NI": "default", "Rib": None}],
            "leaf2": [{"NI": "all-rails", "Rib": [{"Prefix": "fd00::/64"}]}],
        },
    )
    columns, per_node = extract("ip_rib", results)
    # 'Rib' names the list of routes on the host that has some, so it groups
    # rows rather than being a column, even though leaf1 holds it as a null.
    assert columns == ["NI", "Prefix"]
    assert [r.values for node in per_node for r in node.rows] == [
        {"NI": "default"},
        {"NI": "all-rails", "Prefix": "fd00::/64"},
    ]


def test_extract_expands_sub_items_into_rows_inheriting_the_parent():
    items = [
        {
            "ni": "default",
            "itfs": [{"itf": "ethernet-1/1"}, {"itf": "ethernet-1/2"}],
        }
    ]
    columns, per_node = extract("ni", _aggregated("ni", {"leaf1": items}))
    assert columns == ["ni", "itf"]
    rows = per_node[0].rows
    assert [r.values for r in rows] == [
        {"ni": "default", "itf": "ethernet-1/1"},
        {"ni": "default", "itf": "ethernet-1/2"},
    ]
    # A table shows the parent once and blanks it on the rows that continue it,
    # while structured output repeats it on every row.
    assert rows[0].cells(group=True) == {"ni": "default", "itf": "ethernet-1/1"}
    assert rows[1].cells(group=True) == {"itf": "ethernet-1/2"}
    assert rows[1].cells() == {"ni": "default", "itf": "ethernet-1/2"}


def test_a_sub_item_keeps_a_field_the_parent_also_has():
    """Reports reuse names like 'oper-state' at both levels; the sub-item wins."""
    items = [
        {
            "ni": "default",
            "oper-state": "up",
            "itfs": [
                {"itf": "ethernet-1/1", "oper-state": "up"},
                {"itf": "ethernet-1/2", "oper-state": "down"},
            ],
        }
    ]
    _columns, per_node = extract("ni", _aggregated("ni", {"leaf1": items}))
    continued = per_node[0].rows[1].cells(group=True)
    assert continued == {"itf": "ethernet-1/2", "oper-state": "down"}
    assert "ni" not in continued


def test_extract_applies_the_field_filter_to_parent_and_sub_item_fields():
    items = [
        {
            "ni": "default",
            "itfs": [
                {"itf": "ethernet-1/1", "oper-state": "up"},
                {"itf": "ethernet-1/2", "oper-state": "down"},
            ],
        }
    ]
    _columns, per_node = extract(
        "ni", _aggregated("ni", {"leaf1": items}), field_filter={"oper-state": "down"}
    )
    assert [r.values["itf"] for r in per_node[0].rows] == ["ethernet-1/2"]


def test_extract_hands_failed_hosts_to_the_callback():
    results = _aggregated(
        "ni", {"leaf1": [{"ni": "default"}]}, failed=[("leaf2", RuntimeError("boom"))]
    )
    seen = []

    def on_error(node, exception):
        seen.append((node, str(exception)))
        return {"_error": str(exception)}

    _columns, per_node = extract("ni", results, on_error=on_error)
    assert seen == [("leaf2", "boom")]
    assert per_node[1].rows[0].values == {"_error": "boom"}


def test_extract_leaves_failed_hosts_out_when_the_callback_returns_nothing():
    results = _aggregated("ni", {}, failed=[("leaf2", RuntimeError("boom"))])
    _columns, per_node = extract("ni", results, on_error=lambda node, exc: None)
    assert per_node == []


def test_pass_filter_matches_case_insensitive_regexes():
    row = {"oper-state": "Down", "itf": "ethernet-1/1"}
    assert pass_filter(row, None)
    assert pass_filter(row, {"Oper-State": "down"})
    assert pass_filter(row, {"itf": "ethernet-1/."})
    assert not pass_filter(row, {"oper-state": "up"})
    # Every filter key has to match something.
    assert not pass_filter(row, {"oper-state": "down", "missing": "x"})


def test_cli_table_omits_bgp_rib_communities_column():
    columns = cli._cli_table_columns(
        "bgp_rib", ["NI", "peer", "communities", "RT", "next-hop"]
    )
    assert columns == ["NI", "peer", "RT", "next-hop"]
    assert cli._cli_table_columns("bgp_peers", ["peer", "communities"]) == [
        "peer",
        "communities",
    ]


def test_cli_table_omits_bgp_peers_local_endpoint_columns():
    columns = cli._cli_table_columns(
        "bgp_peers",
        ["peer", "local-address", "local-port", "state", "peer-as"],
    )
    assert columns == ["peer", "state", "peer-as"]


def test_cli_table_omits_the_port_state_the_cli_ifstats_never_reads():
    columns = cli._cli_table_columns(
        "ifstats", ["interface", "oper-state", "down-reason", "in-Kbps", "in-pps"]
    )
    assert columns == ["interface", "in-Kbps", "in-pps"]
