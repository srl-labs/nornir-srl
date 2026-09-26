# fcli

[![ci](https://github.com/srl-labs/fcli/actions/workflows/ci.yml/badge.svg)](https://github.com/srl-labs/fcli/actions/workflows/ci.yml)
[![SR Linux](https://img.shields.io/badge/SR%20Linux-25.3.2%20%7C%2025.10.3%20%7C%2026.3.1%20%7C%2026.7.1-blue)](#tested-sr-linux-releases)
[![PyPI](https://img.shields.io/pypi/v/nornir-srl)](https://pypi.org/project/nornir-srl/)

**fcli** is a fabric observability tool for Nokia [SR Linux](https://www.nokia.com/ip-networks/service-router-linux-NOS/). It talks gNMI to every node in the inventory and presents the same reports on three surfaces:

1. **`fcli server`** — a live web UI, kept current by gNMI subscriptions (the primary way to use it)
2. **`fcli <report>`** — one-shot CLI tables, JSON, YAML or CSV
3. **`fcli-mcp`** — the same reports as MCP tools for AI agents

Inventory comes from a [containerlab](https://containerlab.dev/) topology file or a [Nornir](https://nornir.readthedocs.io/en/latest/) config. Report getters hide YANG and path differences across SR Linux releases, so a column means the same thing on 25.3 as on 26.7.

## Table of contents

- [Quick start](#quick-start)
- [GitHub Codespaces](#github-codespaces)
- [Installation](#installation)
- [Inventory](#inventory)
- [Live server](#live-server)
  - [In the browser](#in-the-browser)
  - [Topology](#topology)
  - [Health, incidents and the timeline](#health-incidents-and-the-timeline)
  - [Ask (LLM troubleshooting)](#ask-llm-troubleshooting)
  - [How the live data works](#how-the-live-data-works)
  - [gNMI sessions](#gnmi-sessions)
  - [HTTP API](#http-api)
- [CLI reports](#cli-reports)
  - [Examples](#examples)
  - [Debug logging](#debug-logging)
- [MCP server](#mcp-server)
- [Reports](#reports)
- [Lenses](#lenses)
- [Tested SR Linux releases](#tested-sr-linux-releases)

## Quick start

With a running containerlab topology that has SR Linux nodes:

```bash
# install (once)
uv tool install git+https://github.com/srl-labs/fcli

# live UI — open http://127.0.0.1:8080
fcli -t topo.clab.yml server
```

That is the usual workflow. The CLI and MCP surfaces share the same inventory flags (`-t` / `-c`, `-i`) and the same report getters.

```bash
fcli -t topo.clab.yml bgp-peers
fcli -t topo.clab.yml -i role=leaf mac -f NI=macvrf-202
fcli-mcp --topo-file topo.clab.yml
```

## GitHub Codespaces

Run fcli against a live 3-stage EVPN-VXLAN fabric in the cloud — no local containerlab setup required.

---
<div align=center>
<a href="https://codespaces.new/srl-labs/fcli?quickstart=1">
<img src="https://gitlab.com/rdodin/pics/-/wikis/uploads/d78a6f9f6869b3ac3c286928dd52fa08/run_in_codespaces-v1.svg?sanitize=true" style="width:50%"/></a>

**[Run](https://codespaces.new/srl-labs/fcli?quickstart=1) fcli in GitHub Codespaces for free**.  
[Learn more](https://containerlab.dev/manual/codespaces/) about Containerlab for Codespaces.  
<small>Machine type: 8 vCPU · 16 GB RAM</small>
</div>
---

Creating the Codespace hands the slow work to a background script, so the editor is usable immediately. [`.devcontainer/setup.sh`](.devcontainer/setup.sh) narrates five steps into `/tmp/fcli-codespace-setup.log`:

1. wait for the Docker daemon
2. install fcli from the sources in the repo (`uv sync`)
3. deploy the [demo lab](labs/demo/) — 8 SR Linux nodes and 9 servers
4. wait until every node answers gNMI, which is only once SR Linux has booted
5. start `fcli server` on port 8080

Step 3 is the long one: each SR Linux image is about 1 GB, so the first run usually takes **15–30 minutes**. Watch it work, or ask for a summary:

```bash
tail -f /tmp/fcli-codespace-setup.log
bash .devcontainer/status.sh
```

If the lab did not start at all, run it yourself — it is safe to repeat, and a second run does nothing while the first is still going:

```bash
bash .devcontainer/launch-setup.sh
```

Once step 5 is reached, open port 8080 in the **Ports** tab for the live web UI (Overview, Topology, BGP, EVPN services, …).

```bash
# CLI reports against the same fabric
fcli -t labs/demo/demo.clab.yaml bgp-peers
fcli -t labs/demo/demo.clab.yaml -i hostname=leaf1 mac
```

Optional: set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `XAI_API_KEY` as [Codespaces secrets](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces) to enable the **Ask** troubleshooting chat.

## Installation

Requires Python 3.10+.

> [!NOTE]
> `pip install -U nornir-srl` (or `uv tool install nornir-srl`) installs the latest stable release published on PyPI.
> `uv tool install git+https://github.com/srl-labs/fcli` installs the bleeding-edge development version directly from git `main`.

### `uv` (recommended)

[`uv`](https://github.com/astral-sh/uv) is a standalone Python package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install latest development version from git main:
uv tool install git+https://github.com/srl-labs/fcli

# Or install the latest release published on PyPI:
uv tool install nornir-srl
```

This puts `fcli` and `fcli-mcp` on your `PATH` (typically `~/.local/bin`).

From a clone of this repo, `uv tool install .` or `uv pip install -e .` does the same against local sources.

### pip

Install the latest release published on PyPI into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U nornir-srl
```

To install the latest development version from `main` via pip:
```bash
pip install git+https://github.com/srl-labs/fcli
```

### Docker

The image is [ghcr.io/srl-labs/fcli](https://github.com/srl-labs/fcli/pkgs/container/fcli). Attach it to the containerlab management network, publish port 8080, and bind-mount the topology file:

```bash
CLAB_TOPO=topo.clab.yml
NET=$(grep '^name:' "$CLAB_TOPO" | awk '{print $2}')
alias fcli="docker run -it --network $NET --rm \
  -p 8080:8080 \
  -v /etc/hosts:/etc/hosts:ro \
  -v ${PWD}/${CLAB_TOPO}:/topo.yml \
  ghcr.io/srl-labs/fcli:latest -t /topo.yml"

fcli server --listen 0.0.0.0
fcli bgp-peers
```

`--listen 0.0.0.0` is required inside Docker so the published port is reachable from the host. Running the container with no arguments drops into a shell.

If the container cannot reach the lab from Docker's default `docker0` bridge, either attach it to the containerlab network as above, or allow inter-network traffic (blocked by default on some hosts):

```bash
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

If the topology overrides the management network with `.mgmt.network`, use that name for `--network` instead of the lab name.

## Inventory

`fcli` needs a set of SR Linux nodes. Two ways to provide them, mutually exclusive:

| Flag | Source | Typical use |
| --- | --- | --- |
| `-t` / `--topo-file` | containerlab topology | labs |
| `-c` / `--cfg` | Nornir config file | hardware fabrics |

An inventory filter (`-i key=value`, repeatable, ANDed) scopes every surface to a subset of those nodes.

### Containerlab topology

```bash
fcli -t topo.clab.yml server
fcli -t topo.clab.yml -i role=leaf bgp-peers
```

Only nodes of kind `srl` / `nokia_srlinux` (or whose image is SR Linux) are inventoried. The topology `prefix` is applied to hostnames the same way containerlab does. Node `labels:` become host data and are the keys `-i` can filter on. The default gNMI port is 57400 (`--gnmi-port` / `-p` to override, e.g. 57410 for EDA-deployed labs).

### Nornir config

```bash
fcli -c nornir_config.yaml server
fcli -c nornir_config.yaml -i role=leaf mac
```

If neither `-t` nor `-c` is given, `nornir_config.yaml` in the current directory is used. A typical layout:

```yaml
# nornir_config.yaml
inventory:
    plugin: YAMLInventory
    options:
        host_file: "./inventory/hosts.yaml"
        group_file: "./inventory/groups.yaml"
        defaults_file: "./inventory/defaults.yaml"
runner:
    plugin: threaded
    options:
        num_workers: 20
```

```yaml
# inventory/hosts.yaml
leaf1:
    hostname: 192.0.2.11
    groups: [srl, leafs]
spine1:
    hostname: 192.0.2.21
    groups: [srl, spines]
```

```yaml
# inventory/groups.yaml
srl:
    connection_options:
        srlinux:
            port: 57400
            username: admin
            password: NokiaSrl1!
            extras:
                path_cert: "./root-ca.pem"
spines:
    data:
        role: spine
leafs:
    data:
        role: leaf
```

The certificate is specified once for the `srl` group via `connection_options.srlinux.extras.path_cert`. Host `data:` keys are what `-i` filters on.

### gNMI TLS

gNMI is always TLS, but by default the certificate a node presents is trusted as it comes: nothing tells `fcli` that the thing answering on port 57400 is the node it asked for, so the fabric credentials it sends are only as safe as the path to the node. That is all a freshly deployed containerlab node can offer, so it stays the default for labs.

Point `--cert-file` at the CA that issued the node certificates, and they are verified against it:

```bash
fcli -c nornir_config.yaml --cert-file root-ca.pem bgp-peers
```

The same anchor can live in the inventory as `extras.path_cert`, as above. Two flags cover the rest:

| Flag | Effect |
| --- | --- |
| `--verify` / `--skip-verify` | Force verification on or off, whatever the inventory says |
| `--tls-server-name` | Name the certificate is checked against, when it is not the node's hostname |

`--tls-server-name` is what a mismatch needs: SR Linux issues its certificate to a name of its own, which is rarely the address the inventory reaches it on. For mutual TLS, add `path_key` and `path_root` to `extras` alongside `path_cert`.

## Live server

`fcli server` is the main interface. It serves the same reports as the CLI as a web UI, kept up to date by gNMI **subscriptions** instead of one-shot polls. Every node in the inventory gets a single `Subscribe` RPC carrying the paths the opened reports need, and the browser is pushed a new table over server-sent events whenever the data actually changes.

The tables are the CLI reports, rendered by the same getters, so a column in the browser means what it means in `fcli`. Reports that need arguments on the CLI are pre-bound for streaming: `bgp-rib` is split into one report per address family (and per EVPN route type). `routing-pol` is nested JSON rather than a table, so it is not served. The server also has reports the CLI does not: an **Overview** dashboard, a live **Topology** drawing, and EVPN **Services** / **Bridge Domains** / **Routers** views.

```
❯ fcli -t topo.clab.yml server
fcli server on http://127.0.0.1:8080 (6 node(s))
```

The global options are the same as for the CLI, so the server can be pointed at a containerlab topology (`-t`), a Nornir config (`-c`) and scoped to a subset of the fabric (`-i`):

```
❯ fcli -c nornir_config.yaml -i role=leaf server --listen 0.0.0.0 --port 8080
```

```
❯ fcli -t topo.clab.yml server --help

 Usage: fcli server [OPTIONS]

 Serves live report tables over HTTP, fed by gNMI subscriptions

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --listen           -L      TEXT     Address to bind the web server to. Use   │
│                                     0.0.0.0 to expose it on all interfaces   │
│                                     [default: 127.0.0.1]                     │
│ --port             -P      INTEGER  TCP port to listen on [default: 8080]    │
│ --sample-interval  -S      INTEGER  Override the gNMI SAMPLE interval        │
│                                     (seconds) of every subscription          │
│                                     [default: None]                          │
│ --refresh          -R      FLOAT    How often (seconds) a table is           │
│                                     re-rendered and pushed to the browser    │
│                                     [default: 2.0]                           │
│ --resync                   INTEGER  Interval (seconds) for a full gNMI       │
│                                     re-read per node; 0 disables it          │
│                                     [default: 300]                           │
│ --idle-timeout             INTEGER  Stop streaming paths no report has read  │
│                                     for this long (seconds); 0 keeps every   │
│                                     path subscribed for the lifetime of the  │
│                                     server                                   │
│                                     [default: 900]                           │
│ --watch-interval           FLOAT    How often (seconds) the fabric is read   │
│                                     to keep the change timeline, the         │
│                                     baseline and the health on the           │
│                                     topology; 0 disables the timeline        │
│                                     [default: 15.0]                          │
│ --persist-acks                      Keep acknowledged incidents across       │
│                                     server restarts                          │
│ --watch-prefix             TEXT     A prefix or address whose route changes  │
│                                     the timeline reports one by one;         │
│                                     repeatable                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The server binds to localhost by default. It has no authentication of its own, so put it behind a reverse proxy (or keep it on localhost) before exposing it with `--listen 0.0.0.0`.

### In the browser

* **Reports** are listed in the sidebar, grouped by category, with a filter box on top. The selected report is kept in the URL fragment (`http://localhost:8080/#bgp_peers`), so a view can be bookmarked or shared.
* **Overview** is a KPI dashboard: fabric health (incidents by severity, the worst one, changes in the last 15 minutes and when the baseline was taken), node connectivity, interface and BGP-session health, derived from the same streamed trees as the tables. Click the health card for the incidents.
* **Incidents** and **Changes** head the lenses: the checks' findings grouped by root cause, and what changed and when. See [Health, incidents and the timeline](#health-incidents-and-the-timeline).
* **Lenses** sit under the reports: *Where*, *Path* and *Service* take an argument in the bar above the table and answer from the state the server is already streaming, re-asked at the refresh interval. The answer is drawn hierarchically, the way the services pages fold a fabric into cards — one card per address, hop or service, the nodes inside it, and under each node what it reports — with a **Table View** toggle for the flat rows. *Path* opens as a **Path View**: the walk drawn hop by hop, one box per lookup, an edge to each lookup it leads to, so the ECMP fan-out and where it converges again are visible at a glance and a branch that dies ends in a red stop. A lens's arguments are kept in the URL (`#path?source=leaf1&destination=10.0.2.51&ni=ipvrf-1`), so a walk can be shared. The **Ask** chat can call them too.
* **Sorting**: click a column header to sort, click again to reverse. Sorting is natural, so `ethernet-1/10` comes after `ethernet-1/2`.
* **Filtering**: each column has its own filter box, and the search box above the table filters on all visible columns at once. Both accept a regular expression and fall back to a substring match if the expression is not valid (yet).
* **Inventory filter**: the same `key=value` filter as the CLI's `-i`, applied live — e.g. `role=leaf`.
* **Live updates**: cells that changed since the previous update flash, and new rows flash as a whole. **Pause** freezes the table without dropping the subscriptions.
* **Columns** hides columns you do not need, **CSV** downloads exactly what the table currently shows (filters, column selection and all).
* **Nodes** in the sidebar show per-node subscription state (`up` / unreachable / pending). Clicking a node jumps to details.
* **Side pane**: drag its right edge to widen or narrow it (double-click resets). The width is remembered per browser.
* **Theme** toggles light and dark.
* **🐞 issues**, at the bottom of the sidebar, opens the [GitHub issues](https://github.com/srl-labs/fcli/issues) page to report a bug or ask for a feature.

### Topology

The **Topology** page draws the fabric from LLDP, one tier per row, and works out what each node is from what it runs rather than from an inventory label:
* **Leaf**: Nodes with configured `mac-vrf`s (and optional `ip-vrf`s).
* **DCGW**: Nodes with two enabled `bgp-vpn` instances (the WAN side of a stitched service).
* **Spine**: Nodes with no `mac-vrf` or `ip-vrf` that interconnect two or more leaves.
* **WAN / Core**: Transit routers and super-spines without customer services.
* **Clients & Ethernet Segments**: External bridged or routed customer endpoints and multi-homed bundles grouped by LLDP neighbor name or ESI.

Cables are de-duplicated from LLDP, annotated with parallel link counts (`2×`), and colored by interface oper-state. Disjoint fabrics are cleanly partitioned into separate tabs with zoom and pan controls.

A few lines above the drawing brief the fabric: what it is built of, what it carries, and what is wrong with it. Every node carries a badge counting its findings. The **overlay** selector colours the cables by **traffic** (the default), by **health** (the worst finding on either end), or lights up one **service** and the nodes and clients that carry it. A cable that goes down is still drawn, dotted, after LLDP has lost it: the server remembers every cable it has seen, across restarts. Clicking a node or a cable lists its findings.

See [Fabric Topology](docs/topology.md) for the complete design document on tier inference, client grouping, multi-fabric partitioning, and navigation.

### Health, incidents and the timeline

The server reads the fabric every `--watch-interval` seconds (15 by default) and keeps:

* **Incidents**: every check's findings grouped by root cause. A link that goes down arrives as *one* incident, together with the BGP, BFD and IGP sessions that went down over it. A node that stopped answering becomes the root of everything that points at it. The same cause in many places folds into one pattern, e.g. *"BFD session down on 16 links: the far end has never answered — is BFD enabled there?"*.
* **A timeline** of what changed: sessions, ports, LLDP neighbours, BFD and IGP adjacencies, DF elections, MAC moves, route counts that halved, nodes that stopped answering, and findings raised and cleared. It feeds the **flapping** check.
* **Acknowledgements**: **✓ ACK** on an incident's card takes a known problem out of the Overview, the topology badges, colours and summary, with an optional note. It comes back on its own if a new finding joins it, and the acknowledgement ends when the fault clears. **↺ Un-ACK** undoes it. **✓ ACK all** in the Incidents toolbar acknowledges every open incident in the current view at once, with one note. Acks last as long as the server runs; add `--persist-acks` to keep them across restarts.
* **Route tables and neighbour caches**: one change per underlay route table ("312 changed next-hops, 4 withdrawn …") and a VRF's route count halving, with the default routes, every node's system address and your **watched prefixes** (`--watch-prefix`, or 👁 Watched on the Changes page) reported one by one, ECMP width included; an IP that starts answering from another MAC.
* **A baseline**: the fabric as it was once the server settled, or whenever you press **📌 Set baseline** on the Changes page. `since: baseline` shows the drift from it, which is what a maintenance window or a config push actually changed.

```
❯ fcli -t topo.clab.yml incidents          # the same grouping, one shot
```

See [Health](docs/health.md) for how findings are anchored to links, nodes and sessions, what the timeline records, and the checks behind it.

### Ask (LLM troubleshooting)

**Ask** (top bar) opens a read-only troubleshooting chat as soon as one LLM provider has a key on the server process. The agent uses the live report tables, then JSON-RPC `show` / `info` on a node if needed (containerlab enables JSON-RPC on mgmt; hardware fabrics may not). Keys never go to the browser.

**🩺 Triage** asks the question every session starts with: what is wrong, what changed, the likely root cause and what to check next. The agent is told to answer it from the incidents and the timeline first.

The drawer shows what the agent is doing while it works — thinking, or which tool it is running, with how long each one took — and **Send** turns into **Stop** for as long as a turn is running. Answers are rendered as markdown. Drag the drawer's left edge to widen it (double-click the edge to reset).

| Provider | Key | Default model | API | Effort levels |
| --- | --- | --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.6-sol` | Responses | `none` … `max` |
| Claude | `ANTHROPIC_API_KEY` | `claude-sonnet-5` | Messages | `low` … `max` |
| Grok | `XAI_API_KEY` | `grok-4.6` | Chat Completions | `low` … `xhigh` |

Each provider also honours its own `_MODEL` and `_BASE_URL` variable (`OPENAI_MODEL`, `ANTHROPIC_BASE_URL`, `XAI_MODEL`, …). Claude also accepts `CLAUDE_API_KEY`; Grok also accepts `GROK_API_KEY`.

Set several keys and the drawer gets a provider selector; the browser remembers the last one you picked. `FCLI_LLM_PROVIDER=claude` sets which one is offered first, otherwise it is OpenAI, Claude, Grok in that order.

All three are reasoning models, and the drawer has an effort selector next to the provider. It defaults to `auto`, which leaves the choice to the model (medium on GPT-5.6, high on Claude and Grok); `OPENAI_REASONING_EFFORT`, `ANTHROPIC_EFFORT` and `XAI_REASONING_EFFORT` set the default per provider. Lower effort answers faster and costs less, higher effort holds up better on a fabric-wide "why is this broken" question.

OpenAI runs against the Responses API with `store=false`: fcli replays the model's reasoning itself between tool rounds, and nothing about your fabric is kept in OpenAI's response store. If you front OpenAI with a proxy that only speaks Chat Completions, set `OPENAI_API=chat`.

### How the live data works

1. **Automatic discovery**: When a report is opened for the first time, its getter runs against a recording proxy to discover the exact gNMI paths it reads.
2. **Streaming cache**: Each path is bootstrapped with a gNMI `Get` to seed an in-memory state tree and pin down the response shape, then kept current via a `Subscribe` RPC: ON_CHANGE for state that changes rarely, SAMPLE for counters. Tables render directly from the tree with zero device round-trips.
3. **Resilience & pending paths**: Unpopulated paths (e.g. empty MAC tables) fall back to periodic `Get`s until state appears, automatically joining the subscription once live. Lost nodes are detected across RPC errors, hanging calls, and missed SAMPLE intervals (with a sampled heartbeat when a node streams ON_CHANGE), with background reconnects for rebooting nodes.

See [How the live data works](docs/live-data.md) for the complete design document on subscription lifecycle, state trees, pending path resolution, and failure recovery.

### gNMI sessions

SR Linux enforces a concurrent session limit per gRPC server (`/system/grpc-server[name=mgmt]/session-limit`, 20 by default). `fcli server` stays at **one session per node**: all open reports share a single `Subscribe` RPC, path additions are batched, and idle paths are dropped after `--idle-timeout`.

See [docs/live-data.md#gnmi-sessions-and-scalability](docs/live-data.md#gnmi-sessions-and-scalability) for details.

### HTTP API

The UI is a client of a small JSON API, which is just as usable from scripts:

| Endpoint | Description |
| --- | --- |
| `GET /api/reports` | The available reports, version, and whether chat is enabled |
| `GET /api/inventory` | Inventory nodes, labels and connection state |
| `GET /api/status` | Per-node subscription state |
| `GET /api/overview` | Fabric KPI dashboard payload |
| `GET /api/topology` | The fabric graph: nodes with their inferred tier and the fabric they are cabled into, the clients hanging off them, and the links between them, each with the findings on it; plus the incidents and a summary |
| `GET /api/report/{name}` | One rendered table as JSON |
| `GET /api/stream/{name}` | The same table, pushed as server-sent events |
| `GET /api/timeline` | How many changes the timeline holds, and when the latest reading and the baseline were taken |
| `POST /api/baseline` | Keep the fabric as it is now as the baseline |
| `GET /api/acks` | The acknowledged findings, with when and the note |
| `GET /api/watch`; `POST /api/watch`, `POST /api/unwatch` | The watched prefixes; watch or stop watching one: `{"prefix": "10.1.4.16"}` |
| `POST /api/ack-all` | Acknowledge every open incident: `{"note": "...", "inv_filter": "k=v"}` |
| `POST /api/ack`, `POST /api/unack` | Acknowledge an incident, or take the acknowledgement off: `{"incident": "<id>", "note": "..."}` |
| `POST /api/chat` | LLM troubleshooting turn (SSE: `start`, `token`, `tool`, `error`, `done`). Takes an optional `provider` (`openai`, `claude`, `grok`) and `effort`; 503 unless a provider key is set |

Report, stream, overview and topology endpoints accept `inv_filter=key=value,key=value`; the stream endpoint also accepts `refresh=<seconds>`.

```
❯ curl -s 'http://localhost:8080/api/report/bgp_peers?inv_filter=role%3Dleaf' | jq '.rows[0]'
```

## CLI reports

Same inventory, same getters, one shot. Output defaults to a Rich table; `-o json|yaml|csv` for structured output.

```
❯ fcli --help
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf
  -b, --box-type TEXT    box type of printed table ('python -m rich.box')
  -t, --topo-file PATH   CLAB topology file. Mutually exclusive with -c
  --cert-file PATH       PEM trust anchor for the gNMI certificate of a node
  --verify/--skip-verify verify that certificate [default: --skip-verify
                         unless --cert-file is given]
  --tls-server-name TEXT name to verify it against, if not the hostname
  -p, --gnmi-port        gNMI port [default: 57400]
  -o, --output           table | json | yaml | csv [default: table]
  -l, --log-level        DEBUG | INFO | WARNING | ERROR | CRITICAL
                         [default: ERROR]
  -f, --log-file PATH    also write the log to this file
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  server        Serves live report tables over HTTP
  sys-info      Displays System Info of nodes
  bgp-peers     Displays BGP Peers and their status
  bgp-rib       Displays BGP RIB
  ipv4-rib      Displays IPv4 RIB entries
  ipv6-rib      Displays IPv6 RIB entries
  static-routes Displays static routes
  tunnel-table  Displays the IP tunnel-table (LDP, SR-ISIS, VXLAN, ...)
  ni            Displays Network Instances and interfaces
  subif         Displays Sub-Interfaces of nodes
  lag           Displays LAGs of nodes
  ifstats       Displays per-interface in/out bps from two consecutive samples
  mac           Displays MAC Table
  irb           Displays IRB sub-interfaces
  es            Displays Ethernet Segments
  es-dest       Displays ES Destinations on the bridge table
  vxlan         Displays VXLAN tunnel interfaces and unicast destinations
  lldp          Displays LLDP Neighbors
  bfd           Displays BFD sessions and how often they failed
  isis          Displays IS-IS interfaces and adjacencies
  ospf          Displays OSPF interfaces and neighbors
  resources     Displays CPU, memory and forwarding-table utilization
  components    Displays cards, fabric modules, fans and power supplies
  transceivers  Displays optics with their light levels and DOM alarms
  arp           Displays ARP table
  nd            Displays IPv6 Neighbors
  routing-pol   Displays Routing Policies (json/yaml only)
  checks        Runs the fabric sanity checks and lists what they found
  incidents     Groups the checks' findings by root cause, worst first
  where         Finds which nodes know about a MAC or IP address
  path          Walks the route tables hop by hop towards a destination
  service       Shows one service as every node that carries it sees it
  snapshot      Keeps a report as it is now, to compare a fabric against later
  diff          Compares a report against a snapshot, or one node against another
```

Two kinds of filter, plus report-specific options:

- **inventory** (`-i`, global): `-i hostname=clab-4l2s-l1` or `-i role=leaf`, based on inventory data. Multiple filters are ANDed.
- **field** (`-f`, per report): `-f state="esta.*"`. Values are case-insensitive regexes; repeat `-f` to filter on several columns.
- **report-specific**: `bgp-rib` needs `-r evpn|ipv4|ipv6|l3vpn-v4|l3vpn-v6` (or the long `l3vpn-*-unicast` names) and optionally `-t 1|2|3|4|5` for EVPN route type. `ipv4-rib` / `ipv6-rib` take `-a` for an LPM lookup. `ifstats` takes `-s` for the sampling interval.

`fcli <report> --help` shows the options for that report.

### Examples

MAC entries on leafs in `macvrf-202` matching `1A:DC`:

```
fcli -i role=leaf mac -f NI=macvrf-202 -f mac="1A:DC:*"
```

BGP peers that are not established:

```
fcli bgp-peers -f state=active
```

Column headers use two lines in the live table: the address family as SR Linux names it (`evpn`, `ipv4-unicast`, `ipv6-unicast`, `l3vpn-ipv4-unicast`, `l3vpn-ipv6-unicast`), then **Rx/Act/Tx** (routes received / active / sent). A family shows its counts, or `disabled`, `down`, or `-` when the session is not configured for it. A `state` of `up` is an established session; the other values are BGP's own (`active`, `idle`, `connect`...). CSV keys collapse the header newline to a single space; `-o json` / `-o yaml` emit the records, where each family is an object and `state` is the session state as the device reports it (`established`).

LPM lookup for `192.168.0.7` across every network-instance:

```
fcli ipv4-rib -a 192.168.0.7
```

Active IPv4 BGP routes for a prefix:

```
fcli bgp-rib -r ipv4 -f Pfx="192.168.255.4/32" -f 0_st="u*>"
```

EVPN RT=2 for a MAC:

```
fcli bgp-rib -r evpn -t 2 -f MAC="1A:DC:*"
```

VPN-IPv4 / VPN-IPv6 BGP RIB (per network-instance) use **RD** and **Pfx** columns. Nodes that do not expose the L3VPN RIB gNMI path (for example EVPN-only leaves) contribute **no rows** for that family instead of failing the whole report:

```
fcli bgp-rib -r l3vpn-v4 -f Pfx="10.*"
fcli bgp-rib -r l3vpn-v6
```

The `bgp-rib` table shows a curated set of priority fields so it stays readable. Non-table output (`-o json`, `-o yaml`, `-o csv`) and the MCP tool automatically include the full set of path attributes for each route: standard `communities`, Site-of-Origin (`soo`), BGP domain-path (`dpath`), `tunnel-encap` extended-community, route-target (`RT`), `as-path`, route status (`valid`/`best`/`used`), `tie-break` reason and `internal-tags`. Use `--detail`/`-d` to also include these columns in the table:

```
fcli -o json bgp-rib -r evpn -t 5
fcli -d bgp-rib -r evpn -t 5
```

Tunnel table with resolved egress interface, next-hop and pushed MPLS label-stack:

```
fcli tunnel-table -f type=ldp
```

### Debug logging

`-l DEBUG` traces what fcli does on the wire and why a report came out the way it
did: the inventory it resolved and the filter it applied, every gNMI `Get` with
its paths and round-trip time, the paths each report discovers, the `Subscribe`
RPCs the server opens and the notifications they carry, cache hits, reconnects,
and a traceback for every node that failed. Each line names the thread and call
site, so the per-node threads stay readable when they interleave.

```
fcli -l DEBUG -f /tmp/fcli.log bgp-peers
```

It is deliberately chatty - on the live server, expect a line per notification
per node - so `-f/--log-file` is usually easier to read than the terminal. The
log goes to stderr, which leaves `-o json|csv` on stdout pipeable while a trace
is running. Dependencies (gRPC, HTTP, the LLM clients) stay at INFO so their
frame-level logs do not bury fcli's own; set `FCLI_DEBUG_ALL=1` to let those
through as well.

## MCP server

`fcli-mcp` exposes the CLI reports as [Model Context Protocol](https://modelcontextprotocol.io/) tools (`stdio` by default, or HTTP). An agent can query fabric state without wrapping `fcli` itself.

```bash
fcli-mcp --topo-file topo.clab.yml
fcli-mcp --config-file nornir_config.yaml
fcli-mcp --transport http --port 8080
```

It can start with no topology loaded. These tools then pick or switch the fabric at runtime:

- `list_topologies` — discover `*.clab.yml` and `nornir_config.yaml` files in a directory
- `load_topology` — initialize or switch from a containerlab file
- `load_config` — initialize or switch from a Nornir config file
- `show_topology` — nodes, labels, and the keys `inv_filter` can use

Report tools take the same `inv_filter` and `field_filter` as the CLI (comma-separated `key=value`).

For "what is wrong", `fabric_incidents` is the tool to start with: the checks' findings grouped by root cause. `mark_baseline` and `changes_since_baseline` bracket a change: mark before a maintenance or a config push, then ask what it did.

### Claude Desktop

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

### Gemini CLI

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

## Reports

One registry drives all three surfaces, so a report cannot drift between CLI, MCP and the server. Not every report is on every surface: `overview` and `topology` only exist in the browser; `bgp-rib` takes an address family the streaming server cannot be given, so the server gets one pre-bound report per family instead; `routing-pol` is nested JSON that no table can represent.

| Report | CLI | Server | What it shows |
| --- | --- | --- | --- |
| Overview | | yes | Fabric KPIs (nodes, interfaces, BGP sessions) |
| Topology | | yes | LLDP graph with inferred leaf / spine / DCGW / client tiers |
| System Info | `sys-info` | yes | Chassis, serial, software version, last boot |
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
| MAC Table | `mac` | yes | Bridge-table MAC entries |
| IRB Interfaces | `irb` | yes | IRB sub-interfaces and anycast gateways |
| Ethernet Segments | `es` | yes | ESI, MH mode, DF state, EVI of a virtual ES |
| L2-ES Destinations | `es-dest` | yes | ES destinations in the bridge table |
| VXLAN Tunnels | `vxlan` | yes | VXLAN interfaces and unicast destinations |
| LLDP Neighbors | `lldp` | yes | Neighbours per interface |
| BFD Sessions | `bfd` | yes | Session state, the protocols protected, failures, diagnostics |
| IS-IS Adjacencies | `isis` | yes | IS-IS interfaces and their adjacencies, with level, state and flap count |
| OSPF Neighbors | `ospf` | yes | OSPF interfaces and their neighbours per area |
| Resources | `resources` | yes | CPU, memory and forwarding-table (ASIC/XDP) utilization |
| Hardware | `components` | yes | Control and line cards, fabric modules, fans, power supplies |
| Transceivers | `transceivers` | yes | Optics with rx/tx power, temperature and the DOM thresholds crossed |
| ARP Table | `arp` | yes | IPv4 neighbours per sub-interface |
| IPv6 Neighbors | `nd` | yes | ND entries per sub-interface |
| Checks | `checks` | yes | Fabric sanity checks, worst first - BGP, BFD, IGP, interfaces, LLDP, MTU, EVPN services, ethernet-segments, resources, hardware, optics, flapping; exits non-zero on an error |

A getter and the table it renders as are split apart. Every getter-backed report returns records (`nornir_srl/records.py`) — a network-instance with its subinterfaces as a list, a BGP neighbour with each address family as an object carrying its route counts, a bridge-table entry with its destination already read apart into interface, VTEP, VNI or ESI, a route with each next-hop resolved to the interface, tunnel or prefix it leaves through, a BGP route with every path attribute and the route-targets, SoO and tunnel encapsulation read out of its communities, an interface with its subinterfaces and their resolved down reason, an interface's counters with the errors and discards counted over the sample, an LLDP interface with its neighbours, an ARP or ND cache with each entry's time left as a number of seconds, an irb with each address's flags and its ARP/ND settings as fields, a LAG with its members, a tunnel with each next-hop's port and label stack — and the table is declared next to the report as the columns that read a record. `bgp-rib` has one table per family and EVPN route type, and `--detail` only adds columns to it: the records always carry everything. `-o json` and `-o yaml`, like the MCP tools, emit the records rather than the table's cells (`"families": [{"name": "evpn", "received": 74, ...}]` instead of `"evpn Rx/Act/Tx": "74/0/94"`); the table and `-o csv` are unchanged. The checks and lenses read the same records, so nothing downstream parses a cell back apart. What has no table is what no getter produces: the server-only services and dashboards, which the store builds from its streams, the nested `routing-pol`, and `checks`, whose findings are collected fabric-wide.

## Lenses

A report renders one node's state; a check asks the fabric a fixed question. A **lens** joins state across multiple nodes and reports to answer operational troubleshooting questions like *where is this MAC*, *how does this leaf reach that address*, or *what does this service look like fabric-wide*.

One registry (`nornir_srl/lenses.py`) drives the CLI commands, MCP tools, browser pages, and AI chat:

| Lens | CLI | MCP tool | Browser | What it answers |
| --- | --- | --- | --- | --- |
| Incidents | `incidents` | `fabric_incidents` | yes | The checks' findings grouped by root cause: a link, a node, a session, the underlay, or one cause repeated as a pattern |
| Changes | | `mark_baseline` + `changes_since_baseline` | yes | What changed and when, or the drift from the baseline (server only: it keeps the timeline) |
| Where | `where <mac\|ip>` | `locate_address` | yes | Which node owns an address, which learned it over the overlay, and duplicate conflicts |
| Path | `path <from> <to>` | `trace_path` | yes | Hop by hop from the route tables, every ECMP branch, through VXLAN or MPLS tunnels to ARP/ND |
| Service | `service <name>` | `service_detail` | yes | One network-instance, one row per node: EVI, VNI, RTs, interfaces, VTEPs, MAC counts, ES |

```bash
# which leaf owns this host, and does anyone else think they do
fcli -t topo.clab.yml where 00:C1:AB:00:01:21

# why does this tenant address not reach that one
fcli -t topo.clab.yml path leaf1 10.0.1.4 --ni ipvrf-1

# every node's view of one bridge domain, side by side
fcli -t topo.clab.yml service subnet-1
```

`path` is computed offline from route tables rather than active probes, showing complete ECMP fan-outs and recursive tunnel resolution without synthetic traffic. In the browser, lenses are rendered as cards, flat tables, or interactive path DAGs. Structured formats (`-o json`, `-o yaml`, MCP) emit complete data records.

See [Lenses](docs/lenses.md) for the complete design document on path computation, tunnel resolution, and lens architecture.

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

The lab, the per-release datamodel changes and how to re-record are described in [`tests/fixtures/releases/MATRIX.md`](tests/fixtures/releases/MATRIX.md).
