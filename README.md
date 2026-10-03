# fcli

[![ci](https://github.com/srl-labs/fcli/actions/workflows/ci.yml/badge.svg)](https://github.com/srl-labs/fcli/actions/workflows/ci.yml)
[![SR Linux](https://img.shields.io/badge/SR%20Linux-25.3.2%20%7C%2025.10.3%20%7C%2026.3.1%20%7C%2026.7.1-blue)](docs/reports.md#tested-sr-linux-releases)
[![PyPI](https://img.shields.io/pypi/v/nornir-srl)](https://pypi.org/project/nornir-srl/)

**fcli** shows a network engineer what a Nokia [SR Linux](https://www.nokia.com/ip-networks/service-router-linux-NOS/) fabric looks like, what is wrong with it, and what changed. It answers those questions from live gNMI state, with no agents or controller on the nodes.

Point it at a [containerlab](https://containerlab.dev/) topology or a [Nornir](https://nornir.readthedocs.io/) inventory, and you get:

- **Overview**: a fabric dashboard and a topology drawn from LLDP, with each node's role (leaf, spine, DC gateway, client) worked out from what it runs.
- **Insight**: the checks' findings grouped by root cause, so one broken cable is one incident rather than a dozen alarms, and a timeline of what changed, with a baseline to compare against.
- **Troubleshooting**: fabric-wide questions answered in one step. *Where is this MAC or IP?* *How does this leaf reach that address?* *What does this service look like on every node?*

fcli only reads. It never changes configuration.

## Quick start

```bash
uv tool install git+https://github.com/srl-labs/fcli     # or: pip install nornir-srl

fcli -t topo.clab.yml server                             # live web UI on http://127.0.0.1:8080
```

No lab at hand? [Run fcli in GitHub Codespaces](https://codespaces.new/srl-labs/fcli?quickstart=1) against a demo EVPN-VXLAN fabric of 8 SR Linux nodes.

## Three ways to use it

| Surface | Command | Use it for |
| --- | --- | --- |
| **Live server** | `fcli server` | Day-to-day operation: dashboard, topology, incidents, timeline, live tables, path view, AI chat |
| **CLI** | `fcli <report>` | One-shot answers, scripts and CI; output as a table, JSON, YAML or CSV |
| **MCP** | `fcli-mcp` | AI agents (Claude Desktop, Gemini CLI, …) querying the fabric |

All three share the same reports. A column means the same thing on every surface and on every tested SR Linux release.

## A troubleshooting session

**1. What is wrong?** Start from the incidents: every check's findings grouped by root cause, worst first.

```bash
fcli -t topo.clab.yml incidents
```

In the server, the **Overview** health card and the **Topology** health overlay show the same incidents. **Ask → 🩺 Triage** has an LLM summarise them, if a provider key is set.

**2. What changed?** The server's **Changes** page lists every BGP, BFD, IGP, port, LLDP, designated-forwarder, MAC and route change as it happened. Press **📌 Set baseline** before a maintenance window, then look at `since: baseline` afterwards to see what it changed.

**3. Follow the traffic.**

```bash
fcli -t topo.clab.yml where 00:C1:AB:00:01:21          # who owns this host, any duplicates?
fcli -t topo.clab.yml path leaf1 10.0.1.4 --ni ipvrf-1 # every ECMP branch, through the tunnels
fcli -t topo.clab.yml service subnet-1                 # one service, as each node sees it
```

**4. Drill into the detail.**

```bash
fcli -t topo.clab.yml bgp-peers -f state=active        # sessions that are not up
fcli -t topo.clab.yml -i role=leaf mac -f NI=macvrf-202 # MAC table, leaves only
fcli -t topo.clab.yml ipv4-rib -a 192.168.0.7          # longest-prefix match in every VRF
```

`-i key=value` limits the run to some nodes, using the topology labels or inventory data. `-f column=regex` filters rows. `fcli --help` lists all the reports, and `fcli <report> --help` shows each one's options.

**5. Gate a change.** `fcli checks` exits non-zero when it finds an error, and `fcli snapshot` and `fcli diff` compare a report before and after a change.

## What it covers

- **Underlay**: interfaces and sub-interfaces, LAG, LLDP, BFD, IS-IS, OSPF, IPv4/IPv6 route tables, static routes, tunnel table
- **Overlay**: BGP peers and RIBs, including EVPN by route type, VPN-IPv4/IPv6, and the routes received from or sent to each peer; network instances, MAC, IRB, ethernet segments, VXLAN, ARP/ND
- **Platform**: system info, CPU, memory and forwarding-table utilisation, hardware components, optics
- **Checks**: BGP, BFD and IGP down; interface down or erroring; one-sided LLDP; MTU mismatch; EVPN service mismatch; designated-forwarder disagreement; resources; hardware; optics; flapping

fcli does not cover ACLs, QoS, multicast or SRv6. `path` works out forwarding from the route tables, so it doesn't send probe traffic or test the data plane.

## Before you deploy it

- **Access**: the web server has no authentication. It listens on localhost by default. Put it behind a reverse proxy before using `--listen 0.0.0.0`.
- **TLS**: gNMI always uses TLS, but node certificates are not verified unless you pass `--cert-file` (or set `path_cert` in the inventory). That is fine for labs. Set it up for production fabrics.
- **gNMI**: the server opens one gNMI session per node, well within SR Linux's default limit of 20. The default port is 57400. Set another with `-p`.
- **History**: the timeline is kept in memory and is lost when the server restarts. Learned cabling is always kept across restarts, and acknowledgements are too with `--persist-acks`.

## Documentation

| Topic | |
| --- | --- |
| [Installation and inventory](docs/installation.md) | uv, pip, Docker, Codespaces, containerlab and Nornir inventories, gNMI TLS |
| [Live server](docs/server.md) | Options, the browser UI, Ask (LLM) setup, HTTP API |
| [CLI reports](docs/cli.md) | Options, filters, examples, debug logging |
| [MCP server](docs/mcp.md) | Tools and client configuration |
| [Reports](docs/reports.md) | Every report and where it is available; tested SR Linux releases |
| [Health](docs/health.md) | Incidents, checks, timeline, baseline, acknowledgements |
| [Lenses](docs/lenses.md) | `where`, `path`, `service` |
| [Topology](docs/topology.md) | How node roles and clients are worked out |
| [Live data](docs/live-data.md) | gNMI subscriptions, caching and recovery |

Found a bug or missing a feature? [Open an issue](https://github.com/srl-labs/fcli/issues).
