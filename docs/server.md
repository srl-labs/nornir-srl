# Live server

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
│ --history/--no-history              Keep the timeline, the baselines and the │
│                                     configurations on disk, one SQLite file  │
│                                     per fabric [default: history]            │
│ --history-dir              PATH     Where the history files are kept         │
│                                     [default: ~/.local/state/fcli/history]   │
│ --history-days             FLOAT    Days of changes the history keeps; 0     │
│                                     keeps them all [default: 30.0]           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The server binds to localhost by default. It has no authentication of its own, so put it behind a reverse proxy (or keep it on localhost) before exposing it with `--listen 0.0.0.0`.

## In the browser

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

## Topology

The **Topology** page draws the fabric from LLDP, one tier per row, and works out what each node is from what it runs rather than from an inventory label:
* **Leaf**: Nodes with configured `mac-vrf`s (and optional `ip-vrf`s).
* **DCGW**: Nodes with two enabled `bgp-vpn` instances (the WAN side of a stitched service).
* **Spine**: Nodes with no `mac-vrf` or `ip-vrf` that interconnect two or more leaves.
* **WAN / Core**: Transit routers and super-spines without customer services.
* **Clients & Ethernet Segments**: External bridged or routed customer endpoints and multi-homed bundles grouped by LLDP neighbor name or ESI.

Cables are de-duplicated from LLDP, annotated with parallel link counts (`2×`), and colored by interface oper-state. Disjoint fabrics are cleanly partitioned into separate tabs with zoom and pan controls.

A few lines above the drawing brief the fabric: what it is built of, what it carries, and what is wrong with it. Every node carries a badge counting its findings. The **overlay** selector colours the cables by **traffic** (the default), by **health** (the worst finding on either end), or lights up one **service** and the nodes and clients that carry it. A cable that goes down is still drawn, dotted, after LLDP has lost it: the server remembers every cable it has seen, across restarts. Clicking a node or a cable lists its findings.

See [Fabric Topology](topology.md) for the complete design document on tier inference, client grouping, multi-fabric partitioning, and navigation.

## Health, incidents and the timeline

The server reads the fabric every `--watch-interval` seconds (15 by default) and keeps:

* **Incidents**: every check's findings grouped by root cause. A link that goes down arrives as *one* incident, together with the BGP, BFD and IGP sessions that went down over it. A node that stopped answering becomes the root of everything that points at it. The same cause in many places folds into one pattern, e.g. *"BFD session down on 16 links: the far end has never answered — is BFD enabled there?"*.
* **A timeline** of what changed: sessions, ports, LLDP neighbours, BFD and IGP adjacencies, DF elections, MAC moves, route counts that halved, nodes that stopped answering, and findings raised and cleared. It feeds the **flapping** check.
* **Acknowledgements**: **✓ ACK** on an incident's card takes a known problem out of the Overview, the topology badges, colours and summary, with an optional note. It comes back on its own if a new finding joins it, and the acknowledgement ends when the fault clears. **↺ Un-ACK** undoes it. **✓ ACK all** in the Incidents toolbar acknowledges every open incident in the current view at once, with one note. Acks last as long as the server runs; add `--persist-acks` to keep them across restarts.
* **Route tables and neighbour caches**: one change per underlay route table ("312 changed next-hops, 4 withdrawn …") and a VRF's route count halving, with the default routes, every node's system address and your **watched prefixes** (`--watch-prefix`, or 👁 Watched on the Changes page) reported one by one, ECMP width included; an IP that starts answering from another MAC.
* **A baseline**: the fabric as it was once the server settled, or a named one set in the **📌 Baseline** menu on the Changes page, with a note. `since: baseline` shows the drift from it, which is what a maintenance window or a config push actually changed. A baseline that was set is kept and read back after a restart; the menu lists the kept ones to use or delete.
* **Configurations**: every commit a node logs is on the timeline, with who made it and how many lines it changed, and links to the **Config Diff** lens: the lines it removed and added, as `set / ...` lines with secrets redacted.
* **History**: all of the above is kept on disk, one SQLite file per fabric, so a restart loses nothing. fcli stopping and starting is on the timeline, and what changed while it was not running is reported when it starts again. See [History](history.md).

```
❯ fcli -t topo.clab.yml incidents          # the same grouping, one shot
```

See [Health](health.md) for how findings are anchored to links, nodes and sessions, what the timeline records, and the checks behind it.

## Ask (LLM troubleshooting)

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

## How the live data works

1. **Automatic discovery**: When a report is opened for the first time, its getter runs against a recording proxy to discover the exact gNMI paths it reads.
2. **Streaming cache**: Each path is bootstrapped with a gNMI `Get` to seed an in-memory state tree and pin down the response shape, then kept current via a `Subscribe` RPC: ON_CHANGE for state that changes rarely, SAMPLE for counters. Tables render directly from the tree with zero device round-trips.
3. **Resilience & pending paths**: Unpopulated paths (e.g. empty MAC tables) fall back to periodic `Get`s until state appears, automatically joining the subscription once live. Lost nodes are detected across RPC errors, hanging calls, and missed SAMPLE intervals (with a sampled heartbeat when a node streams ON_CHANGE), with background reconnects for rebooting nodes.

See [How the live data works](live-data.md) for the complete design document on subscription lifecycle, state trees, pending path resolution, and failure recovery.

## gNMI sessions

SR Linux enforces a concurrent session limit per gRPC server (`/system/grpc-server[name=mgmt]/session-limit`, 20 by default). `fcli server` stays at **one session per node**: all open reports share a single `Subscribe` RPC, path additions are batched, and idle paths are dropped after `--idle-timeout`.

See [live-data.md#gnmi-sessions-and-scalability](live-data.md#gnmi-sessions-and-scalability) for details.

## HTTP API

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
| `POST /api/baseline` | Keep the fabric as it is now as the baseline; takes an optional `{"name": "before-upgrade", "note": "..."}` |
| `GET /api/baselines` | The kept baselines, and which one is active |
| `POST /api/baseline/use` | Compare against a kept baseline: `{"name": "..."}`, or `null` for the latest reading |
| `DELETE /api/baseline/{name}` | Delete a kept baseline |
| `GET /api/history` | Changes from the history on disk: `since`, `until` (`2h`, `7d` or a Unix time), `node`, `kind`, `limit` |
| `GET /api/configs` | The configurations kept after each commit (`?node=` for one node) |
| `GET /api/config/{node}` | One kept configuration as set lines (`?commit=`, the newest by default) |
| `GET /api/config/{node}/diff` | What a commit changed (`?commit=`, `?against=`) |
| `GET /api/acks` | The acknowledged findings, with when and the note |
| `GET /api/watch`; `POST /api/watch`, `POST /api/unwatch` | The watched prefixes; watch or stop watching one: `{"prefix": "10.1.4.16"}` |
| `POST /api/ack-all` | Acknowledge every open incident: `{"note": "...", "inv_filter": "k=v"}` |
| `POST /api/ack`, `POST /api/unack` | Acknowledge an incident, or take the acknowledgement off: `{"incident": "<id>", "note": "..."}` |
| `POST /api/chat` | LLM troubleshooting turn (SSE: `start`, `token`, `tool`, `error`, `done`). Takes an optional `provider` (`openai`, `claude`, `grok`) and `effort`; 503 unless a provider key is set |

Report, stream, overview and topology endpoints accept `inv_filter=key=value,key=value`; the stream endpoint also accepts `refresh=<seconds>`.

```
❯ curl -s 'http://localhost:8080/api/report/bgp_peers?inv_filter=role%3Dleaf' | jq '.rows[0]'
```

