"""Unit tests for report helpers and getters added for DCI use-cases.

These tests use small in-memory fixtures and a fake gNMI ``get`` so they run
without a live device.
"""

from typing import Any, Dict, List, Optional

from nornir_srl.connections.helpers import clean_structured_key
from nornir_srl.connections.routing import RoutingMixin

# --------------------------------------------------------------------------- #
# clean_structured_key
# --------------------------------------------------------------------------- #


def test_clean_structured_key_strips_order_prefix():
    assert clean_structured_key("0_st") == "st"
    assert clean_structured_key("1_peer") == "peer"
    assert clean_structured_key("10_foo") == "foo"


def test_clean_structured_key_collapses_newlines():
    assert clean_structured_key("AF: EVPN\nRx/Act/Tx") == "AF: EVPN Rx/Act/Tx"
    assert clean_structured_key("U4 R/A/T") == "U4 R/A/T"


def test_clean_structured_key_leaves_plain_keys():
    assert clean_structured_key("Node") == "Node"
    assert clean_structured_key("next-hop") == "next-hop"


def test_clean_structured_key_passthrough_non_str():
    assert clean_structured_key(5) == 5
    assert clean_structured_key(None) is None


# --------------------------------------------------------------------------- #
# Fake device wiring
# --------------------------------------------------------------------------- #


class _FakeRouting(RoutingMixin):
    """RoutingMixin with a scripted ``get`` keyed on path substrings."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        self.capabilities = {
            "supported_models": [{"name": "bgp-rib", "version": "2024-10-31"}]
        }

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        path = paths[0]
        for key, resp in self._responses.items():
            if key in path:
                return resp
        raise KeyError(f"no scripted response for path {path}")


# --------------------------------------------------------------------------- #
# get_bgp_rib path attributes (detail=True)
# --------------------------------------------------------------------------- #


def test_get_bgp_rib_evpn_detail_attributes():
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "attr-sets": {
                            "attr-set": [
                                {
                                    "index": 1,
                                    "origin": "igp",
                                    "as-path": {"segment": [{"member": [65000]}]},
                                    "communities": {
                                        "community": ["65000:1"],
                                        "ext-community": [
                                            "target:65000:100",
                                            "origin:65000:1",
                                            "bgp-tunnel-encap:MPLS",
                                        ],
                                    },
                                    "domain-path": {
                                        "domain-segment": [
                                            {"domain": {"domain-id": ["65000:1"]}}
                                        ]
                                    },
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "evpn": {
                                    "rib-in-out": {
                                        "rib-in-post": {
                                            "mac-ip-route": [
                                                {
                                                    "attr-id": 1,
                                                    "used-route": True,
                                                    "valid-route": True,
                                                    "best-route": True,
                                                    "neighbor": "192.0.2.2",
                                                    "neighbor-as": 65002,
                                                    "tie-break-reason": "none",
                                                    "internal-tags": [
                                                        "tag-value = 0x1"
                                                    ],
                                                    "route-distinguisher": "192.0.2.2:100",
                                                    "esi": "00:00:00:00:00:00:00:00:00:00",
                                                    "mac-address": "1A:DC:0E:FF:00:41",
                                                    "ip-address": "10.0.0.1",
                                                    "next-hop": "192.0.2.2",
                                                    "label": {"value": 100},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            ]
        }
    ]

    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "mac-ip-route": routes})
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2", detail=True)
    rib = out["bgp_rib"]
    assert len(rib) == 1
    route = rib[0]["Rib"][0]
    assert route["RT"] == "65000:100"
    assert route["soo"] == "65000:1"
    assert route["tunnel-encap"] == "MPLS"
    assert route["dpath"] == "65000:1"
    assert route["communities"] == "65000:1"
    assert route["valid"] is True and route["best"] is True and route["used"] is True
    assert route["tie-break"] == "none"
    assert route["internal-tags"] == ["tag-value = 0x1"]
    assert route["neighbor-as"] == 65002


def test_get_bgp_rib_evpn_lean_has_no_extra_attrs():
    """Without detail, the lean projection must not include the extra fields."""
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": [{"index": 1}]}},
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "evpn": {
                                    "rib-in-out": {
                                        "rib-in-post": {
                                            "mac-ip-route": [
                                                {
                                                    "attr-id": 1,
                                                    "used-route": True,
                                                    "valid-route": True,
                                                    "best-route": True,
                                                    "neighbor": "192.0.2.2",
                                                    "route-distinguisher": "192.0.2.2:100",
                                                    "esi": "00:00:00:00:00:00:00:00:00:00",
                                                    "mac-address": "1A:DC:0E:FF:00:41",
                                                    "ip-address": "10.0.0.1",
                                                    "next-hop": "192.0.2.2",
                                                    "label": {"value": 100},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            ]
        }
    ]
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "mac-ip-route": routes})
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2", detail=False)
    route = out["bgp_rib"][0]["Rib"][0]
    assert "soo" not in route
    assert "dpath" not in route
    assert "communities" not in route


def test_get_bgp_rib_l3vpn_ipv4_alias_and_columns():
    """L3VPN IPv4 RIB uses RD + Pfx; ``l3vpn-v4`` is an accepted alias."""
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "attr-sets": {
                            "attr-set": [
                                {
                                    "index": 1,
                                    "as-path": {"segment": [{"member": [65002, "i"]}]},
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "afi-safi-name": "l3vpn-ipv4-unicast",
                                "l3vpn-ipv4-unicast": {
                                    "local-rib": {
                                        "route": [
                                            {
                                                "attr-id": 1,
                                                "used-route": True,
                                                "valid-route": True,
                                                "best-route": True,
                                                "neighbor": "10.0.0.6",
                                                "route-distinguisher": "65000:1",
                                                "ipv4-prefix": "172.16.1.0/24",
                                                "next-hop": "10.0.0.6",
                                                "local-pref": 100,
                                                "med": 0,
                                                "communities": {
                                                    "community": [],
                                                    "large-community": [],
                                                },
                                            }
                                        ]
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ]
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "local-rib/route": routes})
    out = dev.get_bgp_rib(route_fam="l3vpn-v4", detail=False)
    route = out["bgp_rib"][0]["Rib"][0]
    assert route["RD"] == "65000:1"
    assert route["Pfx"] == "172.16.1.0/24"
    assert route["neighbor"] == "10.0.0.6"
    assert route["0_st"] == "u*>"


# --------------------------------------------------------------------------- #
# get_tunnel_table next-hop resolution
# --------------------------------------------------------------------------- #


def test_get_tunnel_table_resolves_egress_and_label():
    next_hops = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "route-table": {
                        "next-hop": [
                            {
                                "index": "10",
                                "type": "mpls",
                                "ip-address": "10.255.0.1",
                                "subinterface": "ethernet-1/5.0",
                                "mpls-encapsulation": {
                                    "pushed-mpls-label-stack": [20000]
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ]
    next_hop_groups = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "route-table": {
                        "next-hop-group": [
                            {"index": "77", "next-hop": [{"next-hop": "10"}]}
                        ]
                    },
                }
            ]
        }
    ]
    tunnel_table = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "tunnel-table": {
                        "ipv4": {
                            "tunnel": [
                                {
                                    "ipv4-prefix": "192.0.2.152/32",
                                    "type": "ldp",
                                    "owner": "ldp_mgr",
                                    "id": 65537,
                                    "next-hop-group": "77",
                                    "metric": 10,
                                    "preference": 9,
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]

    dev = _FakeRouting(
        {
            "next-hop-group[index=*]": next_hop_groups,
            "next-hop[index=*]": next_hops,
            "tunnel-table": tunnel_table,
        }
    )
    out = dev.get_tunnel_table()
    rows = out["tunnel_table"]
    assert len(rows) == 1
    row = rows[0]
    assert row["Prefix"] == "192.0.2.152/32"
    assert row["type"] == "ldp"
    assert row["pref"] == 9
    assert row["metric"] == 10
    assert row["next-hop"] == ["10.255.0.1"]
    assert row["egress-itf"] == ["ethernet-1/5.0"]
    assert row["label"] == ["20000"]
