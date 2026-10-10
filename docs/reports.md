# Reports

One registry drives all three surfaces, so a report cannot drift between CLI, MCP and the server. Not every report is on every surface: `overview` and `topology` only exist in the browser; `bgp-rib` takes an address family the streaming server cannot be given, so the server gets one pre-bound report per family instead; `routing-pol` is nested JSON that no table can represent.

| Report | CLI | Server | What it shows |
| --- | --- | --- | --- |
| Overview | | yes | Fabric KPIs (nodes, interfaces, BGP sessions) |
| Topology | | yes | LLDP graph with inferred leaf / spine / DCGW / client tiers |
| System Info | `sys-info` | yes | Chassis, serial, software version, last boot |
| Config Commits | `config-commits` | yes | Each node's log of commits to its running configuration: id, user, comment, candidate, status, times |
| Interface Stats | `ifstats` | yes | Per-interface rates and error/discard counters over the sample; the live table adds the port state |
| Sub-Interfaces | `subif` | yes | Type, addresses, oper-state |
| LAGs | `lag` | yes | LAG members and LACP |
| Network Instances | `ni` | yes | NIs, their EVPN EVI and the interfaces bound to them |
| BGP Peers | `bgp-peers` | yes | Session state and per-AF Rx/Act/Tx route counts; Rx links to the routes the peer sent, Tx to the ones sent to it |
| BGP RIB | `bgp-rib -r …` | split per family / EVPN type | RIB-in-post with path attributes |
| BGP Received Routes | | yes | The routes one peer sent, in one family or all of them, from the RIB-in-post |
| BGP Advertised Routes | | yes | The routes sent to one peer, in one family or all of them, from the RIB-out-post |
| IPv4 / IPv6 RIB | `ipv4-rib`, `ipv6-rib` | yes | Route table with resolved next-hops; `-a` for LPM |
| Static Routes | `static-routes` | yes | Configured statics and their state |
| Tunnel Table | `tunnel-table` | yes | VXLAN, LDP, SR-ISIS, RSVP, … |
| Routing Policies | `routing-pol` | | Nested policy JSON (`-o json\|yaml` only) |
| Services | | yes | MAC-VRF and IP-VRF grouped by route-target |
| Bridge Domains | | yes | MAC-VRFs with access ports, ethernet-segments and VXLAN overlays |
| Routers | | yes | IP-VRFs with bound MAC-VRFs, virtual ethernet-segments and overlays |
| Endpoints | `endpoints` | yes | Every ARP/ND entry of a host attached to the node - not on the management port or a link between fabric nodes, and not learned over EVPN unless behind one of its ethernet-segments - with its sub-interface, IP-VRF and MAC-VRF, and from the bridge table the access sub-interface or VTEP it was learned on, its ethernet-segment, and the LLDP neighbour name heard on that port |
| MAC Table | `mac` | yes | Bridge-table MAC entries |
| IRB Interfaces | `irb` | yes | IRB sub-interfaces and anycast gateways |
| Ethernet Segments | `es` | yes | ESI, MH mode, DF state, EVI of a virtual ES |
| L2-ES Destinations | `es-dest` | yes | ES destinations in the bridge table |
| VXLAN Tunnels | `vxlan` | yes | VXLAN interfaces and unicast destinations |
| LLDP Neighbors | `lldp` | yes | Neighbours per interface |
| BFD Sessions | `bfd` | yes | Session state, the protocols protected, failures, diagnostics |
| IS-IS Adjacencies | `isis` | yes | IS-IS interfaces and their adjacencies, with level, state and flap count |
| OSPF Neighbors | `ospf` | yes | OSPF interfaces and their neighbours per area |
| Resources | `resources` | yes | CPU, memory and every forwarding table the datapath counts (ARP/ND, next-hops, ECMP, IP hosts, MACs, LPM routes, DLB groups...), with the node's own alarm threshold in `-o json`/MCP. A container image's virtual datapath counts fewer tables than an ASIC |
| Hardware | `components` | yes | Control and line cards, fabric modules, fans, power supplies |
| Transceivers | `transceivers` | yes | Optics with rx/tx power, temperature and the DOM thresholds crossed |
| ARP Table | `arp` | yes | IPv4 neighbours per sub-interface |
| IPv6 Neighbors | `nd` | yes | ND entries per sub-interface |
| Checks | `checks` | yes | Fabric sanity checks, worst first - BGP, BFD, IGP, interfaces, LLDP, MTU, BGP and IGP configured differently on the two ends, EVPN services, ethernet-segments, resources, hardware, optics, flapping; exits non-zero on an error |

A getter and the table it renders as are split apart. Every getter-backed report returns records (`nornir_srl/records.py`) — a network-instance with its subinterfaces as a list, a BGP neighbour with each address family as an object carrying its route counts, a bridge-table entry with its destination already read apart into interface, VTEP, VNI or ESI, a route with each next-hop resolved to the interface, tunnel or prefix it leaves through, a BGP route with every path attribute and the route-targets, SoO and tunnel encapsulation read out of its communities, an interface with its subinterfaces and their resolved down reason, an interface's counters with the errors and discards counted over the sample, an LLDP interface with its neighbours, an ARP or ND cache with each entry's time left as a number of seconds, an irb with each address's flags and its ARP/ND settings as fields, a LAG with its members, a tunnel with each next-hop's port and label stack — and the table is declared next to the report as the columns that read a record. `bgp-rib` has one table per family and EVPN route type, and `--detail` only adds columns to it: the records always carry everything. `-o json` and `-o yaml`, like the MCP tools, emit the records rather than the table's cells (`"families": [{"name": "evpn", "received": 74, ...}]` instead of `"evpn Rx/Act/Tx": "74/0/94"`); the table and `-o csv` are unchanged. The checks and lenses read the same records, so nothing downstream parses a cell back apart. What has no table is what no getter produces: the server-only services and dashboards, which the store builds from its streams, the nested `routing-pol`, and `checks`, whose findings are collected fabric-wide.


## Tested SR Linux releases

The reports hard-code gNMI paths and the YANG structure they expect back, and both move between SR Linux releases. When they move, the failure is usually silent: a path that no longer carries a value leaves a column empty rather than raising anything.

So CI does not mock the device. Every report is run once against a real, fully configured EVPN-VXLAN fabric per release, and the entire gNMI exchange — every `Get` and the payload or error the device answered with — is recorded. Each pull request replays those recordings through the production report code with no lab present:

| SR Linux release | Nodes recorded | Reports replayed per node |
|---|---|---|
| 25.3.2 | leaf + spine | 32 |
| 25.10.3 | leaf + spine | 32 |
| 26.3.1 | leaf + spine | 32 |
| 26.7.1 | leaf + spine | 32 |

That is 522 test cases, the bulk of the suite. Each one asserts that the report does not raise, that it still produces the exact table the live device produced, and that the set of paths a release rejects is the documented one — so a path that newly breaks, or one that quietly started working, fails the build instead of emptying a column.

All four releases produce **identical columns for every report**, despite 43–89 leaves being added and 3–28 removed under those paths between consecutive releases. The two `bgp-rib -r l3vpn-v4|l3vpn-v6` variants are rejected by all four, because the `bgp-rib` model only carries the l3vpn containers on a node configured for MPLS IP-VPN; those reports degrade to an empty table and the rejection is pinned as expected.

The lab, the per-release datamodel changes and how to re-record are described in [`tests/fixtures/releases/MATRIX.md`](../tests/fixtures/releases/MATRIX.md).
