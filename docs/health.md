# Health: Incidents, the Timeline and the Baseline

A table shows a fabric as it is now. Troubleshooting needs three more things: **what is wrong**, **what changed**, and **how the fabric differs from when it last worked**. `fcli server` keeps the state for all three as it streams, and every surface can read it: the browser, the Ask chat, the CLI and MCP.

## Incidents: findings grouped by root cause

One broken cable produces a dozen findings: the interface down on both ends, the BGP sessions over it, the BFD sessions protecting them, maybe an IGP adjacency. Each one is true, but none of them says they are all the same thing. The **Incidents** lens (`fcli incidents`, MCP `fabric_incidents`) reads the findings for you before you have to.

Every finding is **anchored** to what it is about. Findings on the same anchor form one incident, and within it the finding that explains the others is the **root**:

| Anchor | Findings that land on it |
| --- | --- |
| **Link** (both ends of one cable) | Interface down, errors, MTU mismatch, one-sided LLDP, optic alarms, IGP adjacencies on its ports, and the BGP/BFD sessions running over it |
| **Node** | Everything about a node that answered no report at all, plus the sessions that point at it (root: `node_unreachable`) |
| **Underlay** | An overlay BGP session to a loopback the node has no route to (root: `underlay_unreachable`): look at the underlay, not at BGP |
| **Session** | Both ends of one BGP session, where nothing more specific explains it |
| **Platform** | A node's hardware faults and exhausted resources |

Where a finding sits is taken from the fabric's own state, never guessed:

* **Cable**: what LLDP shows now; what it showed before the link went down (the timeline records it, and the server keeps the learned cabling across restarts in `~/.local/state/fcli/cabling/`); or the two ends of one point-to-point subnet.
* **Session**: a link-local peer names its interface (`fe80::1%ethernet-1/1.0`); BFD sessions carry theirs; a numbered peer sits on a connected subnet.

Within an anchor, the root is chosen by precedence: a node gone, then hardware, optics, interface down, one-sided LLDP, MTU, errors, IGP, BFD, BGP, and so on.

**Patterns.** The same cause in three or more places, with the same finding and the same detail once names and numbers are taken out, is folded into one incident. For example, *"BFD session down on 16 links: the far end has never answered — is BFD enabled there?"* is one configuration mistake, not sixteen problems. No finding is dropped by grouping, and the incidents together hold all of them.

## Acknowledging

A known problem, such as a link waiting for a technician or BFD due to be enabled in the next change window, keeps drawing the eye on every page until it is fixed. **✓ ACK** on an incident's card acknowledges it, with an optional note:

* it leaves the Overview's health card, the node badges, the link colours of the health overlay and the summary line;
* **✓ ACK all** in the Incidents toolbar does the same for every open incident in the current view at once, under one note;
* it stays at the bottom of Incidents, greyed and marked, with **↺ Un-ACK**, and its findings stay listed (marked) in the topology's detail panels;
* acknowledging and un-acknowledging are events on the timeline (`kind: ack`), with the note.

Acks are kept per **finding**, not per incident. An incident is acknowledged while *every* finding it holds is, so a new finding joining it, such as a second session going down over the same link, brings it back. An ack ends when its finding has been gone for two readings in a row, so the same fault returning later is an alarm again. Acks are shared by everyone using the server. By default they are kept in memory and last as long as the server runs. With `fcli server --persist-acks` they are also kept on disk per fabric in `~/.local/state/fcli/acks/`, so they survive a restart. The Ask agent sees the `acknowledged` flag and is told to focus on the open incidents.

## The timeline

The server reads the fabric every `--watch-interval` seconds (15 by default; `0` turns this off) from the streams it already holds. It diffs each reading against the previous one. What changed is kept, newest first, in a bounded buffer:

| Kind | Recorded when |
| --- | --- |
| `bgp`, `bfd`, `isis`, `ospf`, `interface`, `es`, `hardware`, `optic` | the state changes, or the entry appears or disappears |
| `lldp` | a neighbour appears, is lost, or is replaced on a port |
| `es-df` | the designated forwarder of a segment moves |
| `mac` | a MAC moves between ports, VTEPs or segments (learning and ageing out are not news) |
| `arp`, `nd` | an address answers from another MAC or interface: a duplicate address, a spoof or a moved host, recorded as a warning (learning and ageing out are not news) |
| `bgp-routes` | a session's received count halves, or goes to or from zero |
| `routes` | one change per route table (node × network-instance × family) per reading: how many prefixes changed next-hops, were withdrawn or are new, with examples. A warning if half the table or a default route went. The server holds only the `default` table prefix by prefix; every other table is followed by its size, and reported when it halves (a warning) or goes to or from zero |
| `route` | one of the prefixes that matter changes: the default routes, the host routes to every node's system address (VTEPs, loopbacks) in `default`, and **watched prefixes**. Withdrawn is an error, an ECMP narrowing a warning, installed or widened is ok |
| `node` | a node stops or starts answering |
| `finding` | a finding is raised or cleared, once it has lasted two readings (so a single-sample blip never reaches the timeline) |

Severity reads as `error` (something stopped working), `warning`, `ok` (something recovered) or `info`.

The **flapping** check reads the timeline. Three or more transitions of one session, port, adjacency or MAC within 10 minutes is a finding, and a MAC moving back and forth between two ports is what a loop looks like.

Most changes are seen on the next reading. Ports, services, routes, ethernet segments, and LLDP and BGP neighbours are streamed ON_CHANGE, so the device reports a change or a delete as it happens. What is sampled - the counters, BFD sessions, ARP and ND caches - never reports a delete. An entry that disappears there is only noticed once the stream's stale-entry sweep drops it: up to three sample intervals of its path, and at least 45 s.

### Watched prefixes

A route table is summarized, because one link flapping moves the next-hops of thousands of prefixes at once, and a timeline listing each would bury the cause. The prefixes that matter are reported one by one instead. The defaults and the system addresses are always watched; add your own:

* at startup: `fcli server --watch-prefix 10.1.4.16 --watch-prefix 6.6.6.0/24` (an address means its host route);
* at runtime: **👁 Watched** on the Changes page, or `POST /api/watch` / `POST /api/unwatch` with `{"prefix": "..."}`;
* over MCP: `changes_since_baseline(watch_prefixes="10.1.4.16,6.6.6.1/32")`.

A watched prefix is matched exactly, in every network-instance, and its next-hops going back and forth count towards the flapping check. Watched prefixes added at runtime last as long as the server does.

The server streams the `default` route table only. The VRF tables of a large fabric are too big to stream and re-read on every reading. A watched prefix outside `default` is instead looked up on every node with a gNMI Get of that prefix, and of the next-hops it names, on each reading. Its next-hops are shown to the address or route they point at, not followed further down to a port. The full tables are still streamed while an IPv4/IPv6 RIB report or a lens that reads them is open.

## The baseline

A baseline is one reading kept aside as *what good looks like*. It is taken automatically once the server has settled after starting, and again whenever you press **📌 Set baseline** on the Changes page, `POST /api/baseline`, or call the MCP tool `mark_baseline`. **Changes** with `since: baseline` shows the drift from it: the same comparison as the timeline, but between the baseline and now. Use it to see exactly what a maintenance window or a config push did.

The MCP server keeps a baseline too: `mark_baseline` before a change, `changes_since_baseline` after it.

## On the topology

* **Overlay: health** colours every cable by the worst finding on either end, and marks every node with a badge counting its findings.
* **Overlay: service** highlights the nodes and clients that carry one mac-vrf or ip-vrf and dims everything else.
* **Summary**: a few lines above the drawing brief the fabric: what it is built of, what it carries, and what is wrong with it, linked to the incidents.
* **Lost cables**: a cable LLDP no longer shows is still drawn (dotted, in its port's state) from the cabling the server learned, so the one link that matters does not vanish from the picture when it goes down.

Clicking a node or a cable lists its findings in the detail panel.

## What the checks read

Six reports were added for the layer the overlay depends on, each with a check:

| Report | Check | Finds |
| --- | --- | --- |
| `bfd` | `bfd_down` | BFD sessions not up. A far end that has never answered (`remote-discriminator 0`) is called out, since BFD is then usually not enabled there |
| `isis`, `ospf` | `igp_adjacency_down` | IS-IS adjacencies not up; OSPF neighbours not `full`/`two-way` |
| | `igp_no_adjacency` | An IGP interface that is up and not passive, with no adjacency: area, level, authentication or MTU mismatch |
| `resources` | `resource_high` | CPU (5-minute average), memory or a forwarding table at ≥80% (warning) or ≥95% (error) |
| `es` | `es_df` (extended) | Nodes that elect different designated forwarders for one segment in one network-instance: two leaves both forwarding on a single-active segment |
| `components` | `hardware_fault` | A fitted card, fan or PSU that is not up, or that the platform health model calls unhealthy |
| `transceivers` | `optic_dom` | An optic reporting one of its own DOM alarm or warning thresholds as crossed |

## Containerlab

On a containerlab node, `itf_errors` is not checked and the Overview does not count interface errors. The kernel discards packets on a veth that a real port would forward (IPv6 multicast the container's own stack does not want, among others), so the counters move on a healthy lab all the time. Every node of a topology loaded with `-t` is in the Nornir group `containerlab`. A hand-written Nornir inventory of containerlab nodes can put its hosts in that group to get the same behaviour.
