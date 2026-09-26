# Lenses

A report renders a single node's state. A check asks the fabric a fixed health question. Neither matches how network engineers troubleshoot operational issues:
* *Where is this MAC or IP across the fabric?*
* *How would this leaf route towards that destination?*
* *What does this EVPN service look like on every node that participates in it?*

Answering these questions requires correlating state across multiple nodes and reports. A **lens** provides this multi-node operational abstraction: it evaluates fabric-wide state, accepts query parameters, and produces both structured records and visual outputs.

A single unified registry (`nornir_srl/lenses.py`) powers the CLI commands, MCP tools, web UI, and AI troubleshooting agent.

## Available lenses

| Lens | CLI Command | MCP Tool | Web UI | Purpose |
| --- | --- | --- | --- | --- |
| **Incidents** | `incidents` | `fabric_incidents` | Yes | Groups every check's findings by root cause - a link, a node, a BGP session, the underlay, or one cause repeated across the fabric - so a broken cable reads as one incident rather than a dozen findings. See [Health](health.md). |
| **Changes** | - | - (server only) | Yes | What changed and when, from the server's timeline, or with `since=baseline` the drift from the baseline. See [Health](health.md). |
| **Where** | `where <mac\|ip>` | `locate_address` | Yes | Pinpoints which nodes own an address, which learned it via EVPN/VXLAN, and highlights duplicate IP/MAC conflicts. A MAC learned locally on several leaves over the same ethernet-segment is reported as multihomed, with the segment named, not as a duplicate. An IP that is a BGP-learned host route (/32 or /128) is reported on each node that installed it, with the route type and its next-hops. |
| **Path** | `path <from> <to> [--ni <vrf>]` | `trace_path` | Yes | Traces route lookups hop by hop across route tables, following all ECMP branches through VXLAN, LDP, or SR-MPLS tunnels to the destination ARP/ND. |
| **Service** | `service <name>` | `service_detail` | Yes | Consolidates all nodes participating in a network-instance (MAC-VRF or IP-VRF), displaying EVI, VNI, RTs, interfaces, and active MAC counts side by side. |

### CLI examples

```bash
# Locate a host across the fabric and detect ownership conflicts
fcli -t topo.clab.yml where 00:C1:AB:00:01:21

# Trace the forwarding path from leaf1 to a tenant IP inside a specific VRF
fcli -t topo.clab.yml path leaf1 10.0.1.4 --ni ipvrf-1

# View a bridge domain across all participating leaves and spines
fcli -t topo.clab.yml service subnet-1
```

---

## Offline path computation (`path`)

Unlike active probing tools (such as traceroute), `path` computes forwarding trajectories directly from the streaming route tables, ARP caches, and tunnel tables. This offers several distinct advantages:
* **No synthetic traffic**: It requires no active data plane injection and functions even if the control plane is working while data-plane traffic is impaired.
* **Complete ECMP visibility**: Rather than following the single random hash path chosen by a probe packet, it computes and displays the complete ECMP fan-out and shows exactly where paths reconverge.
* **Recursive tunnel resolution**: When a VRF route resolves to an overlay tunnel (such as a VXLAN VTEP or an MPLS gateway via LDP or SR-MPLS), the tracer hands the walk over to the underlay route table towards the tunnel endpoint. Upon reaching the remote endpoint, lookup resumes within the destination VRF (matching by name or imported route-target).
* **Clear termination**: The walk completes when reaching an ARP/ND cache entry for the destination or a local interface. If forwarding fails (e.g. missing route or missing LLDP neighbor on an egress port), the path explicitly halts and reports the failure reason.

---

## Rendering and presentation

### Web UI presentation
In `fcli server`, lenses are evaluated dynamically on top of already-streamed report data and recalculated at the server's refresh interval:
* **Cards View**: Groups findings into hierarchical cards (e.g., grouped by target address or service), displaying participating nodes and per-node findings.
* **Table View**: Renders the result as a flat table matching the CLI columns.
* **Path View**: A dedicated interactive DAG visualization for `path` queries. It draws every lookup hop, highlights ECMP branches, indicates tunnel encap/decap transitions, and displays clear stop markers at any branch that fails to resolve.
* **Deep linking**: Lens arguments are encoded in the URL fragment (e.g. `http://localhost:8080/#path?source=leaf1&destination=10.0.2.51&ni=ipvrf-1`), allowing specific troubleshooting traces to be bookmarked and shared.

### Structured records vs. formatted tables
Lenses decouple raw structured records from human-facing display tables:
* **Structured formats (`-o json`, `-o yaml`, MCP tools)**: Emit raw data structures (e.g., list of VTEPs, numerical MAC counts, structured hop outcomes). Downstream automation and AI agents consume clean typed fields without regex parsing.
* **Human formats (Rich table, `-o csv`)**: Format data for operational readability (e.g., `4 local / 4 remote` or formatted `Detail` summary sentences).
