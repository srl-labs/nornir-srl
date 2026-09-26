"""Unit tests for report helpers and getters added for DCI use-cases.

These tests use small in-memory fixtures and a fake gNMI ``get`` so they run
without a live device.
"""

from typing import Any, Dict, List, Optional

import pytest

from nornir_srl.connections.helpers import clean_structured_key
from nornir_srl.connections.interfaces import NetworkInstanceMixin
from nornir_srl.connections.routing import RoutingMixin
from nornir_srl.records import BgpRib, BgpRoute, Egress
from nornir_srl.reports import (
    ES_TABLE,
    IP_RIB_TABLE,
    LAG_TABLE,
    TUNNEL_TABLE,
    bgp_rib_table,
    coerce_params,
    get_report,
)

# --------------------------------------------------------------------------- #
# clean_structured_key
# --------------------------------------------------------------------------- #


def test_clean_structured_key_strips_order_prefix():
    assert clean_structured_key("0_st") == "st"
    assert clean_structured_key("1_peer") == "peer"
    assert clean_structured_key("10_foo") == "foo"


def test_clean_structured_key_collapses_newlines():
    assert clean_structured_key("AF: EVPN\nRx/Act/Tx") == "AF: EVPN Rx/Act/Tx"
    assert clean_structured_key("ipv4-unicast Rx/Act/Tx") == "ipv4-unicast Rx/Act/Tx"
    assert clean_structured_key("ipv4-unicast\nRx/Act/Tx") == "ipv4-unicast Rx/Act/Tx"
    assert clean_structured_key("EVPN\nR/A/T") == "EVPN R/A/T"


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


class _FakeInterfaces(NetworkInstanceMixin):
    """NetworkInstanceMixin with a scripted ``get`` keyed on path substrings."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        #: Every path asked for, so a test can assert one was *not* needed.
        self.requested: List[str] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        path = paths[0]
        self.requested.append(path)
        for key, resp in self._responses.items():
            if key in path:
                return resp
        raise KeyError(f"no scripted response for path {path}")


# --------------------------------------------------------------------------- #
# get_sum_bgp local endpoint fields
# --------------------------------------------------------------------------- #


class _FakeBgpPeers(RoutingMixin):
    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        self.capabilities = {
            "supported_models": [
                {"name": "urn:srl_nokia/bgp:srl_nokia-bgp", "version": "2023-3-1"},
            ]
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


def test_get_sum_bgp_includes_local_address_and_port():
    neighbors = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "protocols": {
                        "bgp": {
                            "neighbor": [
                                {
                                    "peer-address": "10.0.0.2",
                                    "transport": {
                                        "local-address": "10.0.0.1",
                                        "local-port": 179,
                                    },
                                    "peer-as": 65002,
                                    "session-state": "established",
                                    "local-as": {"as-number": 65001},
                                    "afi-safi": [
                                        {
                                            "afi-safi-name": "ipv4-unicast",
                                            "admin-state": "enable",
                                            "received-routes": 10,
                                            "active-routes": 8,
                                            "sent-routes": 5,
                                            "oper-state": "up",
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    dev = _FakeBgpPeers({"protocols/bgp/neighbor": neighbors})
    peer = dev.get_sum_bgp()["bgp_peers"][0].neighbors[0]
    assert peer.peer == "10.0.0.2"
    assert peer.local_address == "10.0.0.1"
    assert peer.local_port == 179
    ipv4 = peer.family("ipv4-unicast")
    assert (ipv4.enabled, ipv4.oper, ipv4.received, ipv4.active, ipv4.sent) == (
        True, "up", 10, 8, 5,
    )
    assert peer.family("evpn") is None, "a family the session is not configured for"


# --------------------------------------------------------------------------- #
# get_bgp_rib path attributes (detail=True)
# --------------------------------------------------------------------------- #


def _bgp_row(out: Dict[str, Any], detail: bool = False) -> Dict[str, Any]:
    """The first route of the first RIB returned, as the table shows it."""
    rib = out["bgp_rib"][0]
    return bgp_rib_table(rib.family, rib.route_type, detail).rows(rib)[0].values


def _rib_row(out: Dict[str, Any]) -> Dict[str, Any]:
    """The first route of the first route table returned, as the table shows it."""
    return IP_RIB_TABLE.rows(out["ip_rib"][0])[0].values


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
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2")
    assert len(out["bgp_rib"]) == 1
    rib = out["bgp_rib"][0]
    assert (rib.ni, rib.family, rib.route_type) == ("default", "evpn", "2")
    route = rib.routes[0]
    # The record carries every attribute, read apart from the communities.
    assert route.route_targets == ("65000:100",)
    assert route.soo == ("65000:1",)
    assert route.tunnel_encap == ("MPLS",)
    assert route.domain_path == ("65000:1",)
    assert route.communities == ("65000:1",)
    assert route.ext_communities == ("target:65000:100", "origin:65000:1", "bgp-tunnel-encap:MPLS")
    assert (route.valid, route.best, route.used) == (True, True, True)
    assert route.tie_break == "none"
    assert route.internal_tags == ("tag-value = 0x1",)
    assert route.neighbor_as == 65002
    assert (route.mac, route.ip, route.vni, route.as_path) == ("1A:DC:0E:FF:00:41", "10.0.0.1", 100, (65000,))
    # The detail table shows them.
    row = _bgp_row(out, detail=True)
    assert row["RT"] == "65000:100"
    assert row["soo"] == "65000:1"
    assert row["tunnel-encap"] == "MPLS"
    assert row["dpath"] == "65000:1"
    assert row["communities"] == (
        "65000:1, target:65000:100, origin:65000:1, bgp-tunnel-encap:MPLS"
    )
    assert row["valid"] is True and row["best"] is True and row["used"] is True
    assert row["tie-break"] == "none"
    assert row["internal-tags"] == ["tag-value = 0x1"]
    assert row["neighbor-as"] == 65002


def test_get_bgp_rib_evpn_lean_has_communities_not_detail_attrs():
    """The lean table carries communities; the detail-only columns stay out."""
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
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2")
    row = _bgp_row(out)
    assert row["communities"] == ""
    assert "soo" not in row
    assert "dpath" not in row


def test_get_bgp_rib_evpn_type5_reports_the_esi():
    """An RT-5 prefix carries an ESI, so the type 5 report has to show it.

    RFC 9136 pairs the ESI with the gateway address as the overlay index of the
    prefix, which is what says whether a route came in over a multi-homed segment.
    """
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
                                            "ip-prefix-route": [
                                                {
                                                    "attr-id": 1,
                                                    "used-route": True,
                                                    "valid-route": True,
                                                    "best-route": True,
                                                    "neighbor": "192.0.2.2",
                                                    "route-distinguisher": "192.0.2.2:100",
                                                    "esi": "01:24:00:00:00:00:00:00:00:01",
                                                    "ip-prefix": "10.0.1.0/24",
                                                    "gateway-ip": "0.0.0.0",
                                                    "next-hop": "192.0.2.2",
                                                    "vni": 2001,
                                                    "local-pref": 100,
                                                    "origin": "igp",
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
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "ip-prefix-route": routes})
    out = dev.get_bgp_rib(route_fam="evpn", route_type="5")
    route = out["bgp_rib"][0].routes[0]
    assert (route.esi, route.prefix, route.gateway) == (
        "01:24:00:00:00:00:00:00:00:01", "10.0.1.0/24", "0.0.0.0",
    )
    row = _bgp_row(out)
    assert row["ESI"] == "01:24:00:00:00:00:00:00:00:01"
    assert row["IP-Pfx"] == "10.0.1.0/24"
    assert row["GW"] == "0.0.0.0"


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
    out = dev.get_bgp_rib(route_fam="l3vpn-v4")
    assert out["bgp_rib"][0].family == "l3vpn-ipv4-unicast"
    row = _bgp_row(out)
    assert row["RD"] == "65000:1"
    assert row["Pfx"] == "172.16.1.0/24"
    assert row["neighbor"] == "10.0.0.6"
    assert row["st"] == "u*>"
    assert row["communities"] == ""


def test_get_bgp_rib_l3vpn_detail_includes_communities():
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
                                    "communities": {
                                        "community": ["65000:100"],
                                        "large-community": ["65000:1:2"],
                                        "ext-community": ["target:65000:200"],
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
    row = _bgp_row(dev.get_bgp_rib(route_fam="l3vpn-v4"), detail=True)
    assert row["communities"] == "65000:100, 65000:1:2, target:65000:200"


def _ip_rib_payloads(afi: str, prefix: str, communities: Dict[str, Any]):
    """An ``attr-sets`` and a ``local-rib`` response for one IP-family route."""
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
                                    "communities": communities,
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
                                "afi-safi-name": afi,
                                afi: {
                                    "local-rib": {
                                        "route": [
                                            {
                                                "attr-id": 1,
                                                "used-route": True,
                                                "valid-route": True,
                                                "best-route": True,
                                                "neighbor": "10.0.0.6",
                                                "prefix": prefix,
                                                "next-hop": "10.0.0.6",
                                                "local-pref": 100,
                                                "med": 0,
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
    return attr_sets, routes


@pytest.mark.parametrize(
    "route_fam,afi,prefix",
    [
        ("ipv4", "ipv4-unicast", "10.10.0.0/24"),
        ("ipv6", "ipv6-unicast", "2001:db8:10::/64"),
    ],
)
def test_get_bgp_rib_ip_lean_has_communities(route_fam, afi, prefix):
    """The IP-family lean table carries communities, as EVPN and L3VPN do."""
    attr_sets, routes = _ip_rib_payloads(
        afi,
        prefix,
        {
            "community": ["65000:100"],
            "large-community": ["65000:1:2"],
            "ext-community": ["target:65000:200"],
        },
    )
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "local-rib/route": routes})
    row = _bgp_row(dev.get_bgp_rib(route_fam=route_fam))
    assert row["Prefix"] == prefix
    assert row["communities"] == "65000:100, 65000:1:2, target:65000:200"
    # Lean means lean: the detail-only path attributes stay out of the table.
    assert "valid" not in row
    assert "soo" not in row


def test_get_bgp_rib_ip_detail_keeps_communities_and_adds_attrs():
    """The detail table extends the IP columns without displacing communities."""
    attr_sets, routes = _ip_rib_payloads(
        "ipv4-unicast", "10.10.0.0/24", {"community": ["65000:100"]}
    )
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "local-rib/route": routes})
    row = _bgp_row(dev.get_bgp_rib(route_fam="ipv4"), detail=True)
    assert row["communities"] == "65000:100"
    assert row["valid"] is True
    assert row["best"] is True


def test_get_bgp_rib_l3vpn_returns_empty_when_rib_path_absent():
    """Nodes without an L3VPN RIB path (e.g. EVPN-only leaves) return an empty RIB."""

    import grpc

    class _RpcNotFound(Exception):
        def code(self) -> grpc.StatusCode:
            return grpc.StatusCode.NOT_FOUND

    class _LeafNoL3vpn(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise _RpcNotFound()
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafNoL3vpn({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


def test_get_bgp_rib_l3vpn_empty_on_pygnmi_path_invalid_message():
    """pygnmi surfaces SR Linux path errors as a string (no grpc __cause__ chain)."""

    class _LeafNoL3vpnPygnmi(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise RuntimeError(
                    "GRPC ERROR Host: leaf2:57400, Error: Path not valid - unknown element "
                    "'l3vpn-ipv4-unicast'. Options are [ipv4-unicast, ipv6-unicast, evpn, "
                    "ipv4-flowspec-v1, ipv6-flowspec-v1, route-target, afi-safi-name]"
                )
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafNoL3vpnPygnmi({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


def test_get_bgp_rib_l3vpn_empty_when_orig_exc_has_grpc_code():
    """pygnmi ``gNMIException`` stores RpcError in ``orig_exc``, not ``__cause__``."""

    import grpc

    class _InactiveLike:
        def code(self) -> grpc.StatusCode:
            return grpc.StatusCode.INVALID_ARGUMENT

    class _GnmiExcWrapper(Exception):
        def __init__(self) -> None:
            super().__init__("GRPC ERROR Host: leaf:57400, Error: Path not valid")
            self.orig_exc = _InactiveLike()

    class _LeafOrig(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise _GnmiExcWrapper()
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafOrig({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


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
    (table,) = dev.get_tunnel_table()["tunnel_table"]
    assert table.ni == "default"
    (tunnel,) = table.tunnels
    assert (tunnel.prefix, tunnel.type, tunnel.preference, tunnel.metric) == ("192.0.2.152/32", "ldp", 9, 10)
    (hop,) = tunnel.next_hops
    assert (hop.address, hop.subinterface, hop.type, hop.labels) == ("10.255.0.1", "ethernet-1/5.0", "mpls", ("20000",))
    # The table lists what each next-hop resolved to, one column per part.
    row = TUNNEL_TABLE.rows(table)[0].values
    assert (row["next-hop"], row["egress-itf"], row["label"]) == (["10.255.0.1"], ["ethernet-1/5.0"], ["20000"])


def test_get_bridge_domains():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "type": "default",
                    "oper-state": "up",
                },
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:999"}],
                                    }
                                }
                            ]
                        }
                    },
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:100"}],
                                        "export-rt": [{"target": "target:65000:100"}],
                                    }
                                }
                            ]
                        }
                    },
                    "interface": [
                        {"name": "ethernet-1/1.100"},
                        {"name": "ethernet-1/2.100"},
                        {
                            "name": "irb1.100",
                            "ipv4": {
                                "address": [
                                    {"ip-prefix": "10.1.100.1/24", "anycast-gw": True}
                                ]
                            },
                        },
                    ],
                    "vxlan-interface": [{"name": "vxlan1.100"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_bridge_domains()
    bds = out["bridge_domains"]
    assert len(bds) == 1
    bd = bds[0]
    assert bd["Bridge Domain"] == "target:65000:100"
    assert bd["MAC-VRF"] == "mac-vrf-100"
    assert bd["Oper State"] == "up"
    assert bd["Subnets"] == "10.1.100.0/24"
    assert bd["IRB Interface"] == "irb1.100 [up]: 10.1.100.1/24 (anycast-gw: true) -> ip-vrf-1"
    assert bd["Sub-Interfaces"] == "ethernet-1/1.100 [up] (VLAN: 100); ethernet-1/2.100 [up] (VLAN: 100)"
    assert bd["VXLAN Interface"] == "vxlan1.100"
    assert bd["Gateway"] == ""
    assert bd["BGP Instance"] == ""
    assert bd["System IPv4"] == ""
    assert bd["System IPv6"] == ""


def test_get_bridge_domains_lists_a_shared_subnet_once():
    """Two IRB addresses in one subnet are one subnet of the service.

    An EVPN gateway is commonly addressed twice on the same IRB - a per-node
    address next to the anycast one - and both resolve to the same network, which
    the MAC-VRF heading of a node card used to name twice.
    """
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-104",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [
                        {
                            "name": "irb1.104",
                            "ipv4": {
                                "address": [
                                    {"ip-prefix": "10.0.4.1/24"},
                                    {"ip-prefix": "10.0.4.254/24", "anycast-gw": True},
                                ]
                            },
                        },
                    ],
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    bd = dev.get_bridge_domains()["bridge_domains"][0]
    assert bd["Subnets"] == "10.0.4.0/24"
    # Both addresses still belong on the interface itself.
    assert bd["IRB Interface"] == (
        "irb1.104 [up]: 10.0.4.1/24, 10.0.4.254/24 (anycast-gw: true)"
    )


def test_system0_addresses_skips_link_local_and_reads_system0():
    from nornir_srl.connections.layer2 import _system0_addresses

    assert _system0_addresses({}) == ("", "")
    ipv4, ipv6 = _system0_addresses(
        {
            "system0.0": {
                "ipv4": {"address": [{"ip-prefix": "192.0.2.11/32"}]},
                "ipv6": {
                    "address": [
                        {"ip-prefix": "fe80::1/64"},
                        {"ip-prefix": "2001:db8::11/128"},
                    ]
                },
            }
        }
    )
    assert ipv4 == "192.0.2.11"
    assert ipv6 == "2001:db8::11"


def test_get_bridge_domains_includes_system0_addresses():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "ethernet-1/1.100"}],
                }
            ]
        }
    ]
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "system0",
                    "subinterface": [
                        {
                            "index": 0,
                            "ipv4": {"address": [{"ip-prefix": "10.0.0.1/32"}]},
                            "ipv6": {"address": [{"ip-prefix": "2001:db8::1/128"}]},
                        }
                    ],
                },
                {
                    "name": "ethernet-1/1",
                    "subinterface": [{"index": 100, "oper-state": "up"}],
                },
            ]
        }
    ]
    dev = _FakeLayer2(
        {"network-instance": network_instances, "subinterface": subinterfaces}
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]
    assert bd["System IPv4"] == "10.0.0.1"
    assert bd["System IPv6"] == "2001:db8::1"


def test_get_bridge_domains_reports_a_disabled_mac_vrf_from_its_own_view():
    """An IRB held down by its mac-vrf must not read as up.

    SR Linux answers the same question two ways: the subinterface is up under
    ``/interface`` - nothing is wrong with it - while the disabled mac-vrf holding
    it reports it down with ``net-inst-down``. The service listing has to show the
    mac-vrf's view, or the row contradicts itself by pairing a down bridge domain
    with an up IRB.
    """
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-v20",
                    "type": "mac-vrf",
                    "admin-state": "disable",
                    "oper-state": "down",
                    "oper-down-reason": "admin-down",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {"route-target": {"import-rt": [{"target": "target:1:20"}]}}
                            ]
                        }
                    },
                    "interface": [
                        {
                            "name": "irb0.0",
                            "oper-state": "down",
                            "oper-down-reason": "net-inst-down",
                        },
                        {
                            "name": "ethernet-1/3.20",
                            "oper-state": "down",
                            "oper-down-reason": "net-inst-down",
                        },
                    ],
                }
            ]
        }
    ]
    # The /interface view of the very same subinterfaces, which is up.
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "irb0",
                    "subinterface": [
                        {
                            "index": 0,
                            "oper-state": "up",
                            "ipv4": {
                                "address": [
                                    {"ip-prefix": "172.16.20.254/24", "anycast-gw": True}
                                ]
                            },
                        }
                    ],
                },
                {
                    "name": "ethernet-1/3",
                    "subinterface": [{"index": 20, "oper-state": "up"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2(
        {"network-instance": network_instances, "subinterface": subinterfaces}
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    assert bd["Oper State"] == "down"
    assert (
        bd["IRB Interface"]
        == "irb0.0 [down: net-inst-down]: 172.16.20.254/24 (anycast-gw: true)"
    )
    assert bd["Sub-Interfaces"] == "ethernet-1/3.20 [down: net-inst-down] (VLAN: 20)"


def test_get_bridge_domains_does_not_degrade_a_service_for_a_standby_segment():
    """A standby ethernet-segment member is intent, so the service stays up.

    This is the shape a live all-active-standby fabric produces: the mac-vrf
    blames the member (``subif-down``), the member blames its port
    (``port-down``), and only the port says what is really going on. Counting
    that as down left every multi-homed service permanently degraded on
    whichever leaf was not forwarding.
    """
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-101",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [
                        {"name": "irb0.101", "oper-state": "up"},
                        {"name": "lag1.101", "oper-state": "up"},
                        {
                            "name": "lag2.101",
                            "oper-state": "down",
                            "oper-down-reason": "subif-down",
                        },
                    ],
                }
            ]
        }
    ]
    subinterfaces = [
        {
            "interface": [
                {"name": "irb0", "subinterface": [{"index": 101, "oper-state": "up"}]},
                {"name": "lag1", "subinterface": [{"index": 101, "oper-state": "up"}]},
                {
                    "name": "lag2",
                    "subinterface": [
                        {
                            "index": 101,
                            "oper-state": "down",
                            "oper-down-reason": "port-down",
                        }
                    ],
                },
            ]
        }
    ]
    parent_reasons = [
        {
            "interface": [
                {"name": "lag2", "oper-down-reason": "standby-signaling"},
            ]
        }
    ]

    dev = _FakeLayer2(
        {
            "network-instance": network_instances,
            "subinterface": subinterfaces,
            "oper-down-reason": parent_reasons,
        }
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    assert bd["Oper State"] == "up"
    assert bd["Sub-Interfaces"] == (
        "lag1.101 [up] (VLAN: 101); lag2.101 [down/standby] (VLAN: 101)"
    )


def test_get_bridge_domains_still_degrades_a_service_for_a_real_fault():
    """The same shape, but the port is down for a reason nobody asked for."""
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-101",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [
                        {"name": "lag1.101", "oper-state": "up"},
                        {
                            "name": "lag2.101",
                            "oper-state": "down",
                            "oper-down-reason": "subif-down",
                        },
                    ],
                }
            ]
        }
    ]
    subinterfaces = [
        {
            "interface": [
                {"name": "lag1", "subinterface": [{"index": 101, "oper-state": "up"}]},
                {
                    "name": "lag2",
                    "subinterface": [
                        {
                            "index": 101,
                            "oper-state": "down",
                            "oper-down-reason": "port-down",
                        }
                    ],
                },
            ]
        }
    ]
    parent_reasons = [
        {"interface": [{"name": "lag2", "oper-down-reason": "min-links-not-met"}]}
    ]

    dev = _FakeLayer2(
        {
            "network-instance": network_instances,
            "subinterface": subinterfaces,
            "oper-down-reason": parent_reasons,
        }
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    assert bd["Oper State"] == "degraded"
    # The reason shown is the port's, not the two that only point at it.
    assert bd["Sub-Interfaces"] == (
        "lag1.101 [up] (VLAN: 101); lag2.101 [down: min-links-not-met] (VLAN: 101)"
    )


def _rib_device() -> "_FakeRouting":
    """A node with three nested prefixes, answering from one fixed payload.

    Like the caches the getter is really served from: the server materializes
    the streamed state, and an unsubscribed path comes off a short-lived Get
    cache, both of which hand out state somebody else still needs.
    """
    rib = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "route-table": {
                        "ipv4-unicast": {
                            "route": [
                                {"ipv4-prefix": "10.0.0.0/8", "active": True},
                                {"ipv4-prefix": "10.1.0.0/16", "active": True},
                                {"ipv4-prefix": "10.1.1.0/24", "active": True},
                            ]
                        }
                    },
                }
            ]
        }
    ]
    empty = [{"network-instance": []}]
    return _FakeRouting(
        {
            "ipv4-unicast": rib,
            "next-hop-group": empty,
            "next-hop[index=": empty,
        }
    )


def _prefixes(out: Dict[str, Any]) -> List[str]:
    return [route.prefix for route in out["ip_rib"][0].routes]


def test_get_rib_narrows_to_the_prefix_an_address_falls_into():
    """What ``fcli ipv4-rib -a`` shows: the route the node would forward on."""
    assert _prefixes(_rib_device().get_rib(afi="ipv4-unicast")) == [
        "10.0.0.0/8",
        "10.1.0.0/16",
        "10.1.1.0/24",
    ]
    narrowed = _rib_device().get_rib(afi="ipv4-unicast", lpm_address="10.1.1.55")
    assert _prefixes(narrowed) == ["10.1.1.0/24"]


def test_a_rib_lookup_leaves_the_whole_table_for_the_next_caller():
    """Narrowing must not prune the payload it was handed.

    The route lists are rewritten to narrow them, and the state behind them is
    not the getter's to keep: the server renders the same report with and
    without a lookup from state it streams once. Pruning it would empty the
    table for whoever is watching all of it.
    """
    device = _rib_device()
    device.get_rib(afi="ipv4-unicast", lpm_address="10.1.1.55")
    assert _prefixes(device.get_rib(afi="ipv4-unicast")) == [
        "10.0.0.0/8",
        "10.1.0.0/16",
        "10.1.1.0/24",
    ]


def _vrf_rib_device(
    afi: str,
    route: Dict[str, Any],
    nhgroups: Dict[str, List[str]],
    nhs: Dict[str, Dict[str, Any]],
    ni: str = "ipvrf-1",
    nh_ni: Optional[str] = None,
) -> "_FakeRouting":
    """A node with one route in *ni*, and the two tables that resolve it.

    *nhgroups* maps a group index to the next-hop indices in it, *nhs* a
    next-hop index to its state; both live in *nh_ni*, which defaults to *ni*.
    """
    nh_ni = nh_ni or ni

    def _table(ni_name: str, table: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"network-instance": [{"name": ni_name, "route-table": table}]}]

    return _FakeRouting(
        {
            afi: _table(
                ni,
                {
                    afi: {
                        "route": [
                            {
                                "route-type": "bgp",
                                "active": True,
                                "metric": 0,
                                "preference": 170,
                                "origin-network-instance": ni,
                                **route,
                            }
                        ]
                    }
                },
            ),
            "next-hop-group": _table(
                nh_ni,
                {
                    "next-hop-group": [
                        {"index": index, "next-hop": [{"next-hop": n} for n in members]}
                        for index, members in nhgroups.items()
                    ]
                },
            ),
            "next-hop[index=": _table(
                nh_ni,
                {"next-hop": [{"index": index, **nh} for index, nh in nhs.items()]},
            ),
        }
    )


def test_get_rib_reads_a_named_instance_from_the_envelope_it_arrives_under():
    """A Get naming the instance is answered under that path, not the instance list."""
    device = _FakeRouting(
        {
            "ipv4-unicast": [
                {
                    "network-instance[name=default]/route-table/ipv4-unicast": {
                        "route": [{"ipv4-prefix": "192.0.2.1/32", "active": True, "next-hop-group": "7"}]
                    }
                }
            ],
            "next-hop-group": [
                {"network-instance[name=default]/route-table": {"next-hop-group": [{"index": "7", "next-hop": [{"next-hop": "9"}]}]}}
            ],
            "next-hop[index=": [
                {
                    "network-instance[name=default]/route-table": {
                        "next-hop": [{"index": "9", "type": "direct", "ip-address": "fe80::1", "subinterface": "ethernet-1/1.0"}]
                    }
                }
            ],
        }
    )
    (table,) = device.get_rib(afi="ipv4-unicast", network_instance="default")["ip_rib"]
    assert table.ni == "default"
    (route,) = table.routes
    assert route.prefix == "192.0.2.1/32"
    assert route.next_hops[0].egress == (Egress("interface", "ethernet-1/1.0"),)


class _PathRouting(RoutingMixin):
    """RoutingMixin answering each path of a Get on its own, and keeping them."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        self.asked: List[str] = []

    def get(self, paths: List[str], datatype: Optional[str] = "config", strip_mod: Optional[bool] = True) -> List[Dict[str, Any]]:
        self.asked.extend(paths)
        return [payload for path in paths for payload in self._responses.get(path, [{}])]


def _instance(name: str, table: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"network-instance": [{"name": name, "route-table": table}]}]


def test_get_routes_asks_for_the_prefixes_and_only_the_next_hops_they_use():
    base = "/network-instance[name=*]/route-table"
    device = _PathRouting(
        {
            f"{base}/ipv4-unicast/route[ipv4-prefix=6.6.6.1/32]": _instance(
                "ipvrf-1",
                {"ipv4-unicast": {"route": [{"ipv4-prefix": "6.6.6.1/32", "active": True, "next-hop-group": "100", "origin-network-instance": "ipvrf-1"}]}},
            ),
            f"{base}/next-hop-group[index=100]": _instance(
                "ipvrf-1", {"next-hop-group": [{"index": "100", "next-hop": [{"next-hop": "1"}, {"next-hop": "2"}]}]}
            ),
            # One payload per next-hop, both of the same instance.
            f"{base}/next-hop[index=1]": _instance(
                "ipvrf-1", {"next-hop": [{"index": "1", "ip-address": "192.0.2.12", "tunnel": {"ip-prefix": "192.0.2.12/32", "type": "vxlan"}}]}
            ),
            f"{base}/next-hop[index=2]": _instance(
                "ipvrf-1", {"next-hop": [{"index": "2", "ip-address": "192.0.2.13", "tunnel": {"ip-prefix": "192.0.2.13/32", "type": "vxlan"}}]}
            ),
        }
    )
    (table,) = device.get_routes("ipv4-unicast", ["6.6.6.1/32", "9.9.9.9/32"])["ip_rib"]
    assert table.ni == "ipvrf-1"
    (route,) = table.routes
    assert [h.address for h in route.next_hops] == ["192.0.2.12", "192.0.2.13"]
    assert [e.label for h in route.next_hops for e in h.egress] == ["vxlan:192.0.2.12/32", "vxlan:192.0.2.13/32"]
    assert not any(p.endswith("[index=*]") for p in device.asked), "never the whole next-hop tables"


def test_get_routes_asks_nothing_for_no_prefixes():
    device = _PathRouting({})
    assert device.get_routes("ipv4-unicast", []) == {"ip_rib": []}
    assert device.asked == []


def test_get_rib_summary_reads_each_table_size():
    from nornir_srl.records import RouteTableSummary

    stats = lambda active: {"statistics": {"active-routes": active}}  # noqa: E731
    device = _PathRouting(
        {
            "/network-instance[name=*]/route-table/ipv4-unicast/statistics/active-routes": [
                {
                    "network-instance": [
                        {"name": "default", "route-table": {"ipv4-unicast": stats(10)}},
                        {"name": "ipvrf-1", "route-table": {"ipv4-unicast": stats(20)}},
                    ]
                }
            ],
        }
    )
    assert device.get_rib_summary()["rib_summary"] == [
        RouteTableSummary("default", "ipv4", active=10),
        RouteTableSummary("ipvrf-1", "ipv4", active=20),
    ]


def test_a_server_reading_holds_the_underlay_rib_and_the_size_of_the_rest():
    from nornir_srl.reports import get_report, reading_reports

    chosen = reading_reports(("lldp", "ipv4_rib", "ipv6_rib"))
    assert chosen == ("lldp", "ipv4_rib_underlay", "ipv6_rib_underlay", "rib_summary")
    underlay = get_report("ipv4_rib_underlay")
    assert underlay.stands_in_for == "ipv4_rib"
    assert all("[name=default]" in spec.path for spec in underlay.subscribe)
    assert all(spec.path.endswith("/statistics/active-routes") for spec in get_report("rib_summary").subscribe)
    assert reading_reports(("lldp",)) == ("lldp",), "no route table, no sizes either"


#: An indirect next-hop as SR Linux reports it: the BGP peer address, and the
#: route it recurses on named by prefix *and* by that route's own next-hop-group.
def _indirect(address: str, prefix: str, via: Optional[str]) -> Dict[str, Any]:
    resolving: Dict[str, Any] = {"ip-prefix": prefix, "route-type": "local"}
    if via is not None:
        resolving["next-hop-group"] = via
    return {
        "type": "indirect",
        "ip-address": address,
        "indirect": {"resolved": True, "resolving-route": resolving},
    }


def test_get_rib_follows_an_indirect_next_hop_to_the_egress_interface():
    """A BGP route in an ip-vrf must name a port, not the route it recurses on.

    The next-hop of a BGP route is the peer, which is rarely connected: the
    next-hop is ``indirect`` and carries only the prefix it resolves through.
    The port lives one level down, in the next-hop-group of *that* route.
    """
    device = _vrf_rib_device(
        "ipv4-unicast",
        {"ipv4-prefix": "5.5.5.5/32", "next-hop-group": "100"},
        nhgroups={"100": ["200"], "742": ["632"]},
        nhs={
            "200": _indirect("10.1.5.1", "10.1.5.0/31", via="742"),
            "632": {
                "type": "direct",
                "ip-address": "10.1.5.1",
                "subinterface": "ethernet-1/3.1",
            },
        },
    )

    out = device.get_rib(afi="ipv4-unicast")
    next_hop = out["ip_rib"][0].routes[0].next_hops[0]
    assert (next_hop.type, next_hop.resolving_route) == ("indirect", "10.1.5.0/31")
    assert next_hop.egress == (Egress("interface", "ethernet-1/3.1"),)
    row = _rib_row(out)
    assert row["next-hop"] == ["10.1.5.0/31 (indirect)"]
    assert row["itf"] == ["ethernet-1/3.1"]


def test_get_rib_follows_an_indirect_ipv6_next_hop_to_the_egress_interface():
    device = _vrf_rib_device(
        "ipv6-unicast",
        {"ipv6-prefix": "2001:db8::5/128", "next-hop-group": "100"},
        nhgroups={"100": ["200"], "742": ["632"]},
        nhs={
            "200": _indirect("2001:db8:5::1", "2001:db8:5::/127", via="742"),
            "632": {
                "type": "direct",
                "ip-address": "2001:db8:5::1",
                "subinterface": "ethernet-1/3.1",
            },
        },
    )

    assert _rib_row(device.get_rib(afi="ipv6-unicast"))["itf"] == ["ethernet-1/3.1"]


def test_get_rib_names_the_resolving_route_when_it_leads_to_no_interface():
    """The prefix is still better than nothing when the chain cannot be walked.

    The route, next-hop-group and next-hop tables are three separate Gets, so
    the group a resolving route names can be one this node did not return.
    """
    device = _vrf_rib_device(
        "ipv4-unicast",
        {"ipv4-prefix": "5.5.5.5/32", "next-hop-group": "100"},
        nhgroups={"100": ["200"]},
        nhs={"200": _indirect("10.1.5.1", "10.1.5.0/31", via="742")},
    )

    out = device.get_rib(afi="ipv4-unicast")
    assert out["ip_rib"][0].routes[0].next_hops[0].egress == (Egress("route", "10.1.5.0/31"),)
    assert _rib_row(out)["itf"] == ["10.1.5.0/31"]


def test_get_rib_reports_the_tunnel_of_an_overlay_next_hop():
    """An EVPN route leaves through a tunnel, which is what to show for it.

    Its next-hop is indirect too, but resolves through a tunnel rather than a
    route, and the VTEP it points at says more than ``vxlan0`` would.
    """
    device = _vrf_rib_device(
        "ipv4-unicast",
        {"ipv4-prefix": "10.0.2.2/32", "next-hop-group": "100"},
        nhgroups={"100": ["200"]},
        nhs={
            "200": {
                "type": "indirect",
                "ip-address": "192.168.255.2",
                "indirect": {
                    "resolved": True,
                    "resolving-tunnel": {
                        "ip-prefix": "192.168.255.2/32",
                        "tunnel-type": "vxlan",
                        "next-hop-group": "319",
                    },
                },
            }
        },
    )

    out = device.get_rib(afi="ipv4-unicast")
    assert out["ip_rib"][0].routes[0].next_hops[0].egress == (
        Egress("tunnel", "192.168.255.2/32", tunnel="vxlan"),
    )
    assert _rib_row(out)["itf"] == ["vxlan:192.168.255.2/32"]


def test_get_rib_resolves_a_next_hop_group_in_another_network_instance():
    """A service VRF can hold the route and the underlay VRF its next-hop-group.

    ``next-hop-group-network-instance`` names where to resolve it, and looking
    in the route's own instance finds nothing.
    """
    device = _vrf_rib_device(
        "ipv4-unicast",
        {
            "ipv4-prefix": "5.5.5.5/32",
            "next-hop-group": "100",
            "next-hop-group-network-instance": "default",
        },
        nhgroups={"100": ["200"]},
        nhs={
            "200": {
                "type": "direct",
                "ip-address": "10.0.0.1",
                "subinterface": "ethernet-1/1.0",
            }
        },
        nh_ni="default",
    )

    row = _rib_row(device.get_rib(afi="ipv4-unicast"))
    assert row["next-hop"] == ["10.0.0.1"]
    assert row["itf"] == ["ethernet-1/1.0"]


class _RecordingLayer2:
    """A Layer2 device that answers by path fragment and remembers what was asked."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        self.requested: List[str] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        path = paths[0]
        self.requested.append(path)
        for key, resp in self._responses.items():
            if key in path:
                return resp
        raise KeyError(f"no scripted response for path {path}")


def _es_payload() -> List[Dict[str, Any]]:
    """One segment on lag1, associated with two mac-vrfs that elect different DFs."""

    def _candidates(*peers: Any) -> Dict[str, Any]:
        return {
            "bgp-instance": [
                {
                    "computed-designated-forwarder-candidates": {
                        "designated-forwarder-candidate": [
                            {"address": address, "designated-forwarder": is_df}
                            for address, is_df in peers
                        ]
                    }
                }
            ]
        }

    return [
        {
            "system/network-instance/protocols/evpn/ethernet-segments": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "ethernet-segment": [
                            {
                                "name": "ES-01",
                                "esi": "00:01:01:00:00:00:66:00:01:01",
                                "multi-homing-mode": "single-active",
                                "oper-state": "up",
                                "interface": [{"ethernet-interface": "lag1"}],
                                "association": {
                                    "network-instance": [
                                        {
                                            "name": "macvrf-101",
                                            **_candidates(
                                                ("10.0.0.1", False), ("10.0.0.2", True)
                                            ),
                                        },
                                        {
                                            "name": "macvrf-202",
                                            **_candidates(
                                                ("10.0.0.1", True), ("10.0.0.2", False)
                                            ),
                                        },
                                    ]
                                },
                            }
                        ],
                    }
                ]
            }
        }
    ]


def test_get_bridge_domains_names_the_segment_each_member_hangs_off():
    """Each member carries its own segment, and its own DF election.

    The segment goes on the member's line rather than in a list of its own,
    because a bridge domain with several multi-homed members leaves the reader
    pairing up two lists. The peers are the ones elected for this mac-vrf: the
    DF is chosen per service, so the candidates of the other services on the
    same segment would say the wrong thing about this one.
    """
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(_RecordingLayer2, Layer2Mixin):
        pass

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-101",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [
                        {"name": "lag1.101", "oper-state": "up"},
                        {"name": "ethernet-1/3.101", "oper-state": "up"},
                    ],
                }
            ]
        }
    ]
    subinterfaces = [
        {
            "interface": [
                {"name": "lag1", "subinterface": [{"index": 101, "oper-state": "up"}]},
                {
                    "name": "ethernet-1/3",
                    "subinterface": [{"index": 101, "oper-state": "up"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2(
        {
            # Before 'network-instance', which is a fragment of the ES path too.
            "ethernet-segments": _es_payload(),
            "network-instance": network_instances,
            "subinterface": subinterfaces,
        }
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    # The single-homed member is left as it was; the ESI loses its longest run
    # of padding, the way an IPv6 address loses one to '::'.
    assert bd["Sub-Interfaces"] == (
        "lag1.101 [up] (VLAN: 101) -> ES: ID: 00:01:01:..:66:00:01:01, ES-01, "
        "mode: single-active, oper: up, peers: 10.0.0.1 10.0.0.2(DF)"
        "; ethernet-1/3.101 [up] (VLAN: 101)"
    )


def test_get_bridge_domains_does_not_ask_about_segments_without_access_members():
    """A bridge domain of nothing but an IRB has no port to be multi-homed on."""
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(_RecordingLayer2, Layer2Mixin):
        pass

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-101",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb0.101", "oper-state": "up"}],
                }
            ]
        }
    ]
    subinterfaces = [
        {"interface": [{"name": "irb0", "subinterface": [{"index": 101, "oper-state": "up"}]}]}
    ]

    dev = _FakeLayer2(
        {
            "ethernet-segments": _es_payload(),
            "network-instance": network_instances,
            "subinterface": subinterfaces,
        }
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    assert bd["Sub-Interfaces"] == "-"
    assert not [p for p in dev.requested if "ethernet-segments" in p]


def _ves_payload() -> List[Dict[str, Any]]:
    """A virtual segment tied to EVI 2, next to an ordinary port-based one."""
    return [
        {
            "system/network-instance/protocols/evpn/ethernet-segments": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "ethernet-segment": [
                            {
                                "name": "L3-ES-1",
                                "type": "virtual",
                                "esi": "01:01:00:00:00:00:00:00:00:00",
                                "multi-homing-mode": "all-active",
                                "oper-state": "up",
                                "next-hop": [
                                    {
                                        "l3-next-hop": "10.1.100.254",
                                        "evi": [{"start": 2}],
                                    }
                                ],
                                "association": {
                                    "network-instance": [
                                        {
                                            "name": "ip-vrf-1",
                                            "bgp-instance": [{"instance": 1}],
                                        }
                                    ]
                                },
                            },
                            {
                                "name": "ES-01",
                                "esi": "00:01:01:00:00:00:66:00:01:01",
                                "multi-homing-mode": "all-active",
                                "oper-state": "up",
                                "interface": [{"ethernet-interface": "lag1"}],
                            },
                        ],
                    }
                ]
            }
        }
    ]


def _es_device(responses: Dict[str, List[Dict[str, Any]]]):
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(_RecordingLayer2, Layer2Mixin):
        pass

    return _FakeLayer2({"features": [{"system/features": ["evpn"]}], **responses})


def test_get_es_reports_the_evi_a_virtual_segment_is_tied_to():
    """A virtual ES has no port, and the EVI under its next-hop names its ip-vrf."""
    dev = _es_device({"ethernet-segments": _ves_payload()})

    segments = {es.name: es for es in dev.get_es()["es"]}

    virtual = segments["L3-ES-1"]
    assert virtual.interfaces == ()
    assert [(nh.address, nh.evis) for nh in virtual.next_hops] == [("10.1.100.254", ("2",))]
    # A port-based segment is not tied to a service by an EVI at all.
    port = segments["ES-01"]
    assert port.interfaces == ("lag1",) and port.next_hops == ()
    # ...and the table shows one column for both.
    rows = {row.values["name"]: row.values for row in ES_TABLE.rows(virtual) + ES_TABLE.rows(port)}
    assert (rows["L3-ES-1"]["itf/nh"], rows["L3-ES-1"]["evi"]) == ("10.1.100.254", "2")
    assert (rows["ES-01"]["itf/nh"], rows["ES-01"]["evi"]) == ("lag1", "")


def test_get_es_names_the_next_hop_of_each_evi_only_when_they_differ():
    """Two next-hops tied to the same EVIs read as one EVI list; differing ones do not."""
    payload = _ves_payload()
    segment = payload[0][
        "system/network-instance/protocols/evpn/ethernet-segments"
    ]["bgp-instance"][0]["ethernet-segment"][0]
    segment["next-hop"] = [
        {"l3-next-hop": "10.1.100.254", "evi": [{"start": 2}]},
        {"l3-next-hop": "10.1.200.254", "evi": [{"start": 2}]},
    ]
    dev = _es_device({"ethernet-segments": payload})
    assert ES_TABLE.rows(dev.get_es()["es"][0])[0].values["evi"] == "2"

    segment["next-hop"][1]["evi"] = [{"start": 3}]
    dev = _es_device({"ethernet-segments": payload})
    assert (
        ES_TABLE.rows(dev.get_es()["es"][0])[0].values["evi"] == "10.1.100.254:2 10.1.200.254:3"
    )


def _routers_device(responses: Dict[str, List[Dict[str, Any]]]):
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, resp: Dict[str, List[Dict[str, Any]]]):
            self._responses = resp

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

    # Responses are matched by path fragment, so callers pass
    # 'ethernet-segments' ahead of 'network-instance': the ethernet-segments
    # path contains both.
    return _FakeLayer2({**responses, "subinterface": [{}]})


def _ip_vrf(name: str, evi: Any, rt: str) -> Dict[str, Any]:
    return {
        "name": name,
        "type": "ip-vrf",
        "oper-state": "up",
        "protocols": {
            "bgp-evpn": {"bgp-instance": [{"id": 1, "evi": evi}]},
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "route-target": {
                            "import-rt": [{"target": rt}],
                            "export-rt": [{"target": rt}],
                        },
                    }
                ]
            },
        },
    }


def test_get_routers_shows_the_virtual_segment_associated_with_it():
    """A virtual ES belongs to the ip-vrf its association state names.

    There is no port to list it under, so the router is where it has to appear,
    with the next-hop it tracks: that address being active in this route table
    is what makes the segment advertise at all.
    """
    data = [
        {
            "network-instance": [
                _ip_vrf("ip-vrf-1", 2, "target:65000:2"),
                _ip_vrf("ip-vrf-9", 9, "target:65000:9"),
            ]
        }
    ]
    dev = _routers_device(
        {"ethernet-segments": _ves_payload(), "network-instance": data}
    )

    rows = {r["IP-VRF"]: r for r in dev.get_routers()["routers"]}

    assert rows["ip-vrf-1"]["EVI"] == "2"
    assert rows["ip-vrf-1"]["Virtual ES"] == (
        "ID: 01:01:.., L3-ES-1, nh: 10.1.100.254, mode: all-active, oper: up"
    )
    # The segment's association does not name the other router.
    assert rows["ip-vrf-9"]["EVI"] == "9"
    assert rows["ip-vrf-9"]["Virtual ES"] == "-"


def test_get_routers_takes_the_evi_of_each_gateway_tile_from_its_own_instance():
    """A DCGW is two tiles, each with the EVI and the segments of its instance."""
    payload = _ves_payload()
    payload[0]["system/network-instance/protocols/evpn/ethernet-segments"][
        "bgp-instance"
    ][0]["ethernet-segment"][0]["association"]["network-instance"] = [
        {"name": "ipvrf-l3dci", "bgp-instance": [{"instance": 1}]}
    ]
    data = [
        {
            "network-instance": [
                {
                    "name": "ipvrf-l3dci",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-evpn": {
                            "bgp-instance": [
                                {"id": 1, "evi": 2},
                                {"id": 2, "evi": 3000},
                            ]
                        },
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "id": 1,
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:2"}],
                                        "export-rt": [{"target": "target:65000:2"}],
                                    },
                                },
                                {
                                    "id": 2,
                                    "route-target": {
                                        "import-rt": [{"target": "target:3000:3000"}],
                                        "export-rt": [{"target": "target:3000:3000"}],
                                    },
                                },
                            ]
                        },
                    },
                }
            ]
        }
    ]
    dev = _routers_device({"ethernet-segments": payload, "network-instance": data})

    rows = {r["BGP Instance"]: r for r in dev.get_routers()["routers"]}

    assert rows["1"]["EVI"] == "2"
    assert "L3-ES-1" in rows["1"]["Virtual ES"]
    # The WAN side of the gateway advertises its own EVI and has no segment.
    assert rows["2"]["EVI"] == "3000"
    assert rows["2"]["Virtual ES"] == "-"


def test_get_routers_does_not_ask_about_segments_without_bgp_evpn():
    """An ip-vrf that runs no bgp-evpn cannot be carrying a virtual segment."""
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(_RecordingLayer2, Layer2Mixin):
        pass

    data = [
        {
            "network-instance": [
                {"name": "ip-vrf-1", "type": "ip-vrf", "oper-state": "up"}
            ]
        }
    ]
    dev = _FakeLayer2(
        {
            "ethernet-segments": _ves_payload(),
            "network-instance": data,
            "subinterface": [{}],
        }
    )

    row = dev.get_routers()["routers"][0]

    assert row["EVI"] == ""
    assert row["Virtual ES"] == "-"
    assert not [p for p in dev.requested if "ethernet-segments" in p]


def test_get_routers():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:999"}],
                                        "export-rt": [{"target": "target:65000:999"}],
                                    }
                                }
                            ]
                        },
                        "bgp": {
                            "neighbor": [
                                {
                                    "peer-address": "10.0.0.2",
                                    "session-state": "established",
                                    "transport": {"local-address": "10.0.0.1"},
                                },
                                {
                                    "peer-address": "2001:db8::2",
                                    "session-state": "idle",
                                    "transport": {"local-address": "2001:db8::1"},
                                },
                            ]
                        },
                    },
                    "interface": [
                        {
                            "name": "irb1.100",
                            "ipv4": {"address": [{"ip-prefix": "10.1.100.1/24"}]},
                            "ipv6": {
                                "address": [
                                    {"ip-prefix": "2001:db8:100::1/64"},
                                    {"ip-prefix": "fe80::1/64"},
                                ]
                            },
                        },
                        {
                            "name": "ethernet-1/10.0",
                            "ipv4": {"address": [{"ip-prefix": "192.168.1.1/30"}]},
                        },
                    ],
                    "vxlan-interface": [{"name": "vxlan1.1"}],
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_routers()
    routers = out["routers"]
    assert len(routers) == 1
    r = routers[0]
    assert r["Router"] == "target:65000:999"
    assert r["IP-VRF"] == "ip-vrf-1"
    assert r["Oper State"] == "up"
    assert r["Route Targets"] == "target:65000:999"
    assert r["MAC-VRFs"] == (
        "mac-vrf-100 (irb1.100 [up]: 10.1.100.1/24, 2001:db8:100::1/64, fe80::1/64)"
    )
    assert r["Routed Interfaces"] == "ethernet-1/10.0 [up] (192.168.1.1/30)"
    assert r["BGP Peers"] == "10.0.0.1 -> 10.0.0.2 UP, 2001:db8::1 -> 2001:db8::2 DOWN"
    assert r["VXLAN Interface"] == "vxlan1.1"
    assert r["Subnets"] == "10.1.100.0/24, 2001:db8:100::/64"
    assert r["Gateway"] == ""
    assert r["BGP Instance"] == ""

    # Test isolated ip-vrf without route targets
    isolated_data = [
        {
            "network-instance": [
                {
                    "name": "isolated-vrf",
                    "type": "ip-vrf",
                    "oper-state": "up",
                }
            ]
        }
    ]
    dev2 = _FakeLayer2({"network-instance": isolated_data, "subinterface": [{}]})
    out2 = dev2.get_routers()
    r2 = out2["routers"][0]
    assert r2["Route Targets"] == "none (isolated)"
    assert r2["Router"] == "none (isolated) - isolated-vrf"
    assert r2["Gateway"] == ""
    assert r2["BGP Peers"] == "-"

    # Test that mgmt ip-vrf is excluded
    mgmt_data = [
        {
            "network-instance": [
                {
                    "name": "mgmt",
                    "type": "ip-vrf",
                    "oper-state": "up",
                }
            ]
        }
    ]
    dev3 = _FakeLayer2({"network-instance": mgmt_data, "subinterface": [{}]})
    out3 = dev3.get_routers()
    assert len(out3["routers"]) == 0


def test_host_ips_from_payload_keeps_only_host_routes():
    from nornir_srl.connections.layer2 import _host_ips_from_payload

    payload = {
        "route": [
            {"ipv4-prefix": "192.0.2.11/32"},
            {"ipv4-prefix": "10.0.0.0/24"},
            {"ipv6-prefix": "2001:db8::1/128"},
            {"ipv6-prefix": "fe80::1/64"},
        ]
    }
    assert _host_ips_from_payload(payload) == ["192.0.2.11", "2001:db8::1"]


def test_assign_underlay_sites_splits_two_dcs_and_ignores_wan_between_gateways():
    """Leaves see only local DCGWs; DCGWs also see the remote DCGWs over the WAN.

    Gateway-to-gateway edges must not merge the two fabrics, or ipvrf-l3dci would
    stay one tile. Each DCGW still joins the DC whose leaves have its system0.
    """
    from nornir_srl.connections.layer2 import assign_underlay_sites

    dc1 = "192.0.2.11 192.0.2.14 192.0.2.151 192.0.2.152"
    dc2 = "192.0.3.15 192.0.3.18 192.0.3.153 192.0.3.154"
    wan = f"{dc1} {dc2}"
    rows = [
        {"Node": "leaf1", "System IPv4": "192.0.2.11", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "leaf4", "System IPv4": "192.0.2.14", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "dcgw1", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw2", "System IPv4": "192.0.2.152", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "leaf5", "System IPv4": "192.0.3.15", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "leaf8", "System IPv4": "192.0.3.18", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "dcgw3", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw4", "System IPv4": "192.0.3.154", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    sites = assign_underlay_sites(rows)
    assert set(sites.values()) == {"1", "2"}
    assert {sites["leaf1"], sites["leaf4"], sites["dcgw1"], sites["dcgw2"]} == {"1"}
    assert {sites["leaf5"], sites["leaf8"], sites["dcgw3"], sites["dcgw4"]} == {"2"}
    assert sites["leaf1"] != sites["leaf5"]


def test_assign_underlay_sites_keeps_gateways_together_when_they_all_see_each_other():
    """The WAN/DCI tile is only DCGWs; they share system0 over the WAN, so one Router."""
    from nornir_srl.connections.layer2 import assign_underlay_sites

    wan = "192.0.2.151 192.0.2.152 192.0.3.153 192.0.3.154"
    rows = [
        {"Node": "dcgw1", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw2", "System IPv4": "192.0.2.152", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw3", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw4", "System IPv4": "192.0.3.154", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    assert assign_underlay_sites(rows) == {}


def test_stamp_underlay_sites_splits_dc_tile_but_not_wan_tile():
    from nornir_srl.connections.layer2 import stamp_underlay_sites

    dc1 = "192.0.2.11 192.0.2.151 192.0.2.152"
    dc2 = "192.0.3.15 192.0.3.153 192.0.3.154"
    wan = f"{dc1} {dc2}"
    rows = [
        {"Node": "leaf1", "Router": "target:3000:3000", "System IPv4": "192.0.2.11", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "dcgw1", "Router": "target:3000:3000", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "leaf5", "Router": "target:3000:3000", "System IPv4": "192.0.3.15", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "dcgw3", "Router": "target:3000:3000", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw1", "Router": "target:65000:3000", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw3", "Router": "target:65000:3000", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    assert stamp_underlay_sites(rows) is True
    dc = [r for r in rows if r["Router"] == "target:3000:3000"]
    wan_rows = [r for r in rows if r["Router"] == "target:65000:3000"]
    assert {r["Site"] for r in dc} == {"1", "2"}
    assert all(r["Site"] == "" for r in wan_rows)


def test_assign_underlay_sites_is_silent_for_a_single_fabric():
    from nornir_srl.connections.layer2 import assign_underlay_sites

    hosts = "192.0.2.11 192.0.2.12"
    rows = [
        {"Node": "leaf1", "System IPv4": "192.0.2.11", "Underlay Hosts": hosts, "Gateway": ""},
        {"Node": "leaf2", "System IPv4": "192.0.2.12", "Underlay Hosts": hosts, "Gateway": ""},
    ]
    assert assign_underlay_sites(rows) == {}


def test_vpn_tile_groups_reads_bare_string_route_targets():
    """SR Linux 26.x JSON_IETF reports import/export-rt as strings, not lists of dicts.

    That is what dci-srl DCGWs return for ``bgp-vpn`` instance 1 (DC) and 2 (WAN).
    """
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-evpn": {"bgp-instance": [{"id": 1, "admin-state": "enable"}]},
            "bgp-ipvpn": {"bgp-instance": [{"id": 2, "admin-state": "enable"}]},
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "route-target": {
                            "export-rt": "target:3000:3000",
                            "import-rt": "target:3000:3000",
                        },
                    },
                    {
                        "id": 2,
                        "route-target": {
                            "export-rt": "target:65000:3000",
                            "import-rt": "target:65000:3000",
                        },
                    },
                ]
            },
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert [(g["primary"], g["id"], g["gateway"]) for g in groups] == [
        ("target:3000:3000", "1", True),
        ("target:65000:3000", "2", True),
    ]


def test_vpn_tile_groups_splits_two_enabled_instances():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65000:1"}],
                            "export-rt": [{"target": "target:65000:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65500:1"}],
                            "export-rt": [{"target": "target:65500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert len(groups) == 2
    assert groups[0] == {
        "primary": "target:65000:1",
        "rts": ["target:65000:1"],
        "id": "1",
        "gateway": True,
    }
    assert groups[1] == {
        "primary": "target:65500:1",
        "rts": ["target:65500:1"],
        "id": "2",
        "gateway": True,
    }


def test_vpn_tile_groups_disambiguates_shared_route_targets():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "route-target": {
                            "export-rt": [{"target": "target:64500:1"}],
                            "import-rt": [{"target": "target:64500:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "route-target": {
                            "export-rt": [{"target": "target:64500:1"}],
                            "import-rt": [{"target": "target:64500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "mac-vrf:BD1")
    assert [g["primary"] for g in groups] == [
        "target:64500:1 (bgp-instance 1)",
        "target:64500:1 (bgp-instance 2)",
    ]
    assert all(g["gateway"] for g in groups)


def test_vpn_tile_groups_ignores_a_disabled_second_instance():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65000:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "admin-state": "disable",
                        "route-target": {
                            "import-rt": [{"target": "target:65500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert len(groups) == 1
    assert groups[0]["gateway"] is False
    assert groups[0]["primary"] == "target:65000:1"
    assert groups[0]["rts"] == ["target:65000:1"]


def test_get_routers_emits_one_tile_per_gateway_bgp_instance():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "ipvrf-3000",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "id": 1,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:3000"}],
                                        "export-rt": [{"target": "target:65000:3000"}],
                                    },
                                },
                                {
                                    "id": 2,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:65500:3000"}],
                                        "export-rt": [{"target": "target:65500:3000"}],
                                    },
                                },
                            ]
                        }
                    },
                }
            ]
        }
    ]
    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    rows = dev.get_routers()["routers"]
    assert len(rows) == 2
    by_rt = {r["Router"]: r for r in rows}
    assert set(by_rt) == {"target:65000:3000", "target:65500:3000"}
    for rt, inst in (("target:65000:3000", "1"), ("target:65500:3000", "2")):
        assert by_rt[rt]["IP-VRF"] == "ipvrf-3000"
        assert by_rt[rt]["Route Targets"] == rt
        assert by_rt[rt]["Gateway"] == "Y"
        assert by_rt[rt]["BGP Instance"] == inst


def test_get_bridge_domains_emits_one_tile_per_gateway_bgp_instance():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "BD1",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "id": 1,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:64500:1"}],
                                        "export-rt": [{"target": "target:64500:1"}],
                                    },
                                },
                                {
                                    "id": 2,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:64500:2"}],
                                        "export-rt": [{"target": "target:64500:2"}],
                                    },
                                },
                            ]
                        }
                    },
                }
            ]
        }
    ]
    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    rows = dev.get_bridge_domains()["bridge_domains"]
    assert len(rows) == 2
    by_rt = {r["Bridge Domain"]: r for r in rows}
    assert set(by_rt) == {"target:64500:1", "target:64500:2"}
    for rt, inst in (("target:64500:1", "1"), ("target:64500:2", "2")):
        assert by_rt[rt]["MAC-VRF"] == "BD1"
        assert by_rt[rt]["Route Targets"] == rt
        assert by_rt[rt]["Gateway"] == "Y"
        assert by_rt[rt]["BGP Instance"] == inst


def test_get_services():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    data = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                },
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_services()
    services = out["services"]
    assert len(services) == 2
    types = [s["Service Type"] for s in services]
    assert "Bridge Domain" in types
    assert "Router" in types

# --------------------------------------------------------------------------- #
# Network-instance and interface getters
# --------------------------------------------------------------------------- #


_SUBITF_RESPONSE = [
    {
        "interface": [
            {
                "name": "ethernet-1/10",
                "subinterface": [
                    {
                        "index": 0,
                        "oper-state": "up",
                        "ip-mtu": 1500,
                        "ipv4": {"address": [{"ip-prefix": "192.168.1.1/30"}]},
                    }
                ],
            },
            {
                "name": "irb1",
                "subinterface": [
                    {
                        "index": 100,
                        "oper-state": "up",
                        "l2-mtu": 9000,
                        "ipv4": {"address": [{"ip-prefix": "10.1.100.1/24"}]},
                    }
                ],
            },
        ]
    }
]


def test_get_nwi_itf_joins_subinterface_details_onto_network_instances():
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "ethernet-1/10.0"}, {"name": "irb1.100"}],
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:1"}],
                                        "export-rt": [{"target": "target:65000:1"}],
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    instances = device.get_nwi_itf()["nwi_itfs"]

    assert len(instances) == 1
    instance = instances[0]
    assert instance.name == "ip-vrf-1"
    assert instance.oper == "up"
    assert instance.import_rts == ("65000:1",)
    assert instance.export_rts == ("65000:1",)
    by_name = {i.name: i for i in instance.interfaces}
    assert by_name["ethernet-1/10.0"].prefixes == ("192.168.1.1/30",)
    assert by_name["ethernet-1/10.0"].mtu == 1500
    # An IRB carries an l2-mtu rather than an ip-mtu.
    assert by_name["irb1.100"].mtu == 9000
    assert by_name["irb1.100"].oper == "up"


def test_get_nwi_itf_reports_the_bgp_evpn_evi_of_each_instance():
    """The EVI is what a virtual ethernet-segment names to find its network-instance.

    A DCGW runs two bgp-evpn instances with an EVI each, and both belong to the
    one network-instance the row is about.
    """
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "protocols": {"bgp-evpn": {"bgp-instance": [{"id": 1, "evi": 100}]}},
                },
                {
                    "name": "ipvrf-l3dci",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-evpn": {
                            "bgp-instance": [{"id": 1, "evi": 2}, {"id": 2, "evi": 3000}]
                        }
                    },
                },
                {"name": "default", "type": "default", "oper-state": "up"},
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    instances = {ni.name: ni for ni in device.get_nwi_itf()["nwi_itfs"]}

    assert instances["mac-vrf-100"].evis == ("100",)
    assert instances["ipvrf-l3dci"].evis == ("2", "3000")
    # An instance that runs no EVPN has no EVI to show.
    assert instances["default"].evis == ()


def test_get_nwi_itf_records_the_other_network_instance_an_irb_is_in():
    """An IRB sits in a mac-vrf and an ip-vrf; each row names the other one."""
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    instances = {ni.name: ni for ni in device.get_nwi_itf()["nwi_itfs"]}

    assert instances["ip-vrf-1"].interfaces[0].associated == ("mac-vrf-100",)
    assert instances["mac-vrf-100"].interfaces[0].associated == ("ip-vrf-1",)


def test_get_nwi_itf_accepts_a_single_network_instance_as_a_bare_dict():
    """gNMI returns a one-entry YANG list unwrapped, which must not crash."""
    ni_response = [
        {"network-instance": {"name": "default", "type": "default", "oper-state": "up"}}
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    assert device.get_nwi_itf()["nwi_itfs"] is not None


def test_get_nwi_itf_survives_an_empty_response():
    device = _FakeInterfaces({"subinterface": [], "network-instance": []})
    assert device.get_nwi_itf() == {"nwi_itfs": []}


def test_get_nwi_itf_reads_route_targets_from_import_and_export_policies():
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": {
                                "import-policy": "import-all",
                                "export-policy": ["export-a", "export-b"],
                            }
                        }
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    instance = device.get_nwi_itf()["nwi_itfs"][0]

    assert instance.import_rts == ("import-all",)
    assert instance.export_rts == ("export-a", "export-b")


def test_get_lag_keeps_the_member_name_and_the_table_shortens_it():
    lag_response = [
        {
            "interface": [
                {
                    "name": "lag1",
                    "oper-state": "up",
                    "mtu": 9000,
                    "lag": {
                        "lag-type": "lacp",
                        "min-links": 1,
                        "member": [
                            {
                                "name": "ethernet-1/1",
                                "oper-state": "up",
                                "lacp": {"activity": "ACTIVE"},
                            }
                        ],
                        "lacp": {"lacp-mode": "ACTIVE", "interval": "SLOW"},
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces({"interface": lag_response})

    (lag,) = device.get_lag()["lag"]

    assert (lag.name, lag.oper, lag.type, lag.min_links, lag.lacp_mode) == ("lag1", "up", "lacp", 1, "ACTIVE")
    assert lag.members[0].name == "ethernet-1/1"
    assert lag.members[0].activity == "ACTIVE"
    # Members are abbreviated so the column stays narrow enough to read.
    assert LAG_TABLE.rows(lag)[0].values["member-itf"] == "et-1/1"


def test_get_lag_survives_an_empty_response():
    device = _FakeInterfaces({"interface": []})
    assert device.get_lag() == {"lag": []}


def test_get_sum_subitf_names_subinterfaces_and_lists_addresses():
    device = _FakeInterfaces({"subinterface": _SUBITF_RESPONSE})

    interfaces = {i.name: i for i in device.get_sum_subitf()["subinterface"]}

    assert set(interfaces) == {"ethernet-1/10", "irb1"}
    subitf = interfaces["ethernet-1/10"].subinterfaces[0]
    assert subitf.name == "ethernet-1/10.0"
    assert subitf.oper == "up"
    assert subitf.ipv4 == ("192.168.1.1/30",)


def test_get_sum_subitf_survives_an_empty_response():
    device = _FakeInterfaces({"subinterface": []})
    assert device.get_sum_subitf() == {"subinterface": []}


def test_get_sum_subitf_does_not_ask_why_a_port_is_down_when_none_is():
    """The parent-port reasons cost a Get, so a healthy node must not spend one."""
    device = _FakeInterfaces({"subinterface": _SUBITF_RESPONSE})

    device.get_sum_subitf()

    assert "oper-down-reason" not in "".join(device.requested)


def test_get_sum_subitf_calls_a_standby_subinterface_standby():
    """``port-down`` is resolved against the port, and standby is not a fault.

    An ethernet-segment holding ``lag2`` in standby leaves every subinterface on
    it down with ``port-down``, which says nothing on its own.
    """
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "lag2",
                    "subinterface": [
                        {
                            "index": 101,
                            "oper-state": "down",
                            "oper-down-reason": "port-down",
                        }
                    ],
                }
            ]
        }
    ]
    parent_reasons = [
        {"interface": [{"name": "lag2", "oper-down-reason": "standby-signaling"}]}
    ]
    device = _FakeInterfaces(
        {"subinterface": subinterfaces, "oper-down-reason": parent_reasons}
    )

    subitf = device.get_sum_subitf()["subinterface"][0].subinterfaces[0]

    assert subitf.name == "lag2.101"
    assert subitf.oper == "down/standby"
    assert subitf.down_reason == "standby-signaling"


def test_get_sum_subitf_reports_the_root_cause_of_a_real_fault():
    """A port that is genuinely down still reads as down, with its own reason."""
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "ethernet-1/4",
                    "subinterface": [
                        {
                            "index": 0,
                            "oper-state": "down",
                            "oper-down-reason": "port-down",
                        }
                    ],
                }
            ]
        }
    ]
    parent_reasons = [
        {
            "interface": [
                {"name": "ethernet-1/4", "oper-down-reason": "port-admin-disabled"}
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": subinterfaces, "oper-down-reason": parent_reasons}
    )

    subitf = device.get_sum_subitf()["subinterface"][0].subinterfaces[0]

    assert subitf.oper == "down"
    assert subitf.down_reason == "port-admin-disabled"


def test_get_arp_and_nd_label_entries_with_the_network_instance():
    from nornir_srl.connections.neighbor_discovery import NeighborDiscoveryMixin

    class _FakeNeighbors(NeighborDiscoveryMixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    nis = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "interface": [{"name": "irb1.100"}],
                },
                {
                    "name": "mac-vrf-100",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]
    arp = [
        {
            "interface": [
                {
                    "name": "irb1",
                    "subinterface": [
                        {
                            "index": 100,
                            "ipv4": {
                                "arp": {
                                    "neighbor": [
                                        {
                                            "ipv4-address": "10.1.100.10",
                                            "link-layer-address": "00:11:22:33:44:55",
                                            "origin": "dynamic",
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        }
    ]
    nd = [
        {
            "interface": [
                {
                    "name": "irb1",
                    "subinterface": [
                        {
                            "index": 100,
                            "ipv6": {
                                "neighbor-discovery": {
                                    "neighbor": [
                                        {
                                            "ipv6-address": "2001:db8::10",
                                            "link-layer-address": "00:11:22:33:44:55",
                                            "origin": "dynamic",
                                            "current-state": "reachable",
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        }
    ]
    device = _FakeNeighbors(
        {"network-instance": nis, "arp/neighbor": arp, "neighbor-discovery": nd}
    )

    (arp,) = device.get_arp()["arp"]
    assert arp.interface == "irb1.100"
    assert arp.nis == ("ip-vrf-1", "mac-vrf-100")
    assert arp.entries[0].address == "10.1.100.10"
    assert arp.entries[0].mac == "00:11:22:33:44:55"
    # No expiration-time on the entry, so no time left to report.
    assert arp.entries[0].expires_in is None

    (nd,) = device.get_nd()["nd"]
    assert nd.interface == "irb1.100"
    assert nd.nis == ("ip-vrf-1", "mac-vrf-100")
    assert nd.entries[0].address == "2001:db8::10"
    assert nd.entries[0].state == "reachable"


def test_get_arp_reads_a_single_interface_dict_on_the_network_instance():
    """gNMI often unwraps a one-entry YANG list to a dict; ARP must still bind NI."""
    from nornir_srl.connections.neighbor_discovery import NeighborDiscoveryMixin

    class _FakeNeighbors(NeighborDiscoveryMixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

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

    nis = [
        {
            "network-instance": {
                "name": "vrf1",
                "interface": {"name": "irb1.100"},
            }
        }
    ]
    arp = [
        {
            "interface": {
                "name": "irb1",
                "subinterface": {
                    "index": 100,
                    "ipv4": {
                        "arp": {
                            "neighbor": [
                                {
                                    "ipv4-address": "10.1.100.10",
                                    "link-layer-address": "00:11:22:33:44:55",
                                    "origin": "dynamic",
                                }
                            ]
                        }
                    },
                },
            }
        }
    ]
    device = _FakeNeighbors({"network-instance": nis, "arp/neighbor": arp})
    (cache,) = device.get_arp()["arp"]
    assert cache.nis == ("vrf1",)
    assert cache.interface == "irb1.100"


def test_received_routes_takes_a_link_local_peer_scoped_to_its_interface():
    """An unnumbered peer is fe80::…%<interface>: a peer, if not an IP address."""
    peer = "fe80::1863:eff:feff:1%ethernet-1/29.0"
    other = "fe80::1863:eff:feff:1%ethernet-1/30.0"

    class Device:
        def get_bgp_rib(self, route_fam, route_type=None, rib="in"):
            routes = (BgpRoute(peer, prefix="10.0.0.1/32"), BgpRoute(other), BgpRoute("0.0.0.0"))
            return {"bgp_rib": [BgpRib("default", route_fam, route_type or "", routes)]}

    spec = get_report("bgp_received_routes")
    params = coerce_params(spec, {"peer": peer, "family": "ipv4"})
    assert params == {"peer": peer, "family": "ipv4"}
    (rib,) = spec.getter(Device(), **params)["bgp_rib"]
    assert [r.neighbor for r in rib.routes] == [peer]
    # Without a peer: what every peer sent, and nothing originated locally.
    ribs = spec.getter(Device())["bgp_rib"]
    assert len(ribs) == 9  # five EVPN route types and four other families
    assert {r.neighbor for rib in ribs for r in rib.routes} == {peer, other}


def test_get_bgp_rib_out_reads_what_was_sent_to_each_peer():
    """rib='out' is the rib-out-post: keyed by the peer a route went to, with
    the attributes it was sent with, and not the local-rib."""
    attr_sets, _ = _ip_rib_payloads("ipv4-unicast", "10.10.0.0/24", {"community": ["65000:100"]})
    sent = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "afi-safi-name": "ipv4-unicast",
                                "ipv4-unicast": {
                                    "rib-in-out": {
                                        "rib-out-post": {
                                            "route": [
                                                {"prefix": "192.0.2.11/32", "neighbor": "10.0.0.6", "attr-id": 1}
                                            ]
                                        }
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ]
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "rib-in-out/rib-out-post/route": sent})
    (rib,) = dev.get_bgp_rib(route_fam="ipv4", rib="out")["bgp_rib"]
    (route,) = rib.routes
    assert (route.prefix, route.neighbor, route.communities) == ("192.0.2.11/32", "10.0.0.6", ("65000:100",))
    assert not (route.used or route.valid or route.best)
    with pytest.raises(ValueError):
        dev.get_bgp_rib(route_fam="ipv4", rib="sideways")


@pytest.mark.parametrize("name,rib", [("bgp_received_routes", "in"), ("bgp_advertised_routes", "out")])
def test_peer_routes_read_their_own_side_of_the_rib(name, rib):
    asked = []

    class Device:
        def get_bgp_rib(self, route_fam, route_type=None, rib="in"):
            asked.append(rib)
            return {"bgp_rib": [BgpRib("default", route_fam, route_type or "", (BgpRoute("10.0.0.6"),))]}

    ribs = get_report(name).getter(Device(), peer="10.0.0.6", family="evpn")["bgp_rib"]
    assert [r.route_type for r in ribs] == ["1", "2", "3", "4", "5"]
    assert set(asked) == {rib}
    assert ("st" in get_report(name).table_for({}).column_names) == (rib == "in")
