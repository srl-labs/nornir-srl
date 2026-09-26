# How the Live Data Works

`fcli server` serves the same reports as the CLI as a live web UI, kept continuously up-to-date by gNMI **subscriptions** rather than periodic polling. Every node in the inventory receives a single `Subscribe` RPC carrying the paths needed by the open reports. Whenever data changes on a device, updated tables are rendered and pushed to the browser via Server-Sent Events (SSE).

## Telemetry architecture

```mermaid
flowchart TD
    UI[Browser UI] <-- SSE Updates --- Srv[fcli server]
    Srv --> Mem[In-Memory Node State Tree]
    Mem --> Getters[Report Getters]
    Getters --> Tables[Rendered Table Records]
    Tables --> Srv
    Dev[SR Linux Device] -- gNMI Subscribe STREAM (ON_CHANGE / SAMPLE) --> Mem
    Dev -. fallback / shape bootstrap Get .-> Mem
```

### 1. Automatic path discovery
When a report is opened for the first time, the server executes its getter once against a recording proxy of the gNMI connection. This captures the exact set of gNMI paths the report reads. As a result, subscription paths never have to be manually declared or maintained separately from report getters.

### 2. State bootstrapping & shape pinning
Each discovered path is bootstrapped with a standard gNMI `Get`. This seeds an in-memory state tree for the node and pins down the response structure the report getter expects.

### 3. STREAM subscriptions: ON_CHANGE and SAMPLE
A gNMI `Subscribe` in STREAM mode keeps the node's state tree current. Report getters execute directly against this local tree instead of querying the physical device, rendering tables with zero device round-trips.

Each path is streamed in one of two modes, set in one place (`ON_CHANGE_PATHS` in `nornir_srl/reports.py`):

* **ON_CHANGE** for state that changes rarely and has no counters: a service's type, state, interfaces and overlay, port admin/oper state and down reason, the route tables, ethernet segments, and LLDP and BGP neighbours. A change is all it sends, and a delete when something goes, so on a quiet fabric these paths cost nothing. On SR Linux they are silent at steady state; BGP neighbours send a few updates per keepalive for their message counters, still a fraction of what re-sending every session every interval takes.
* **SAMPLE** for what carries counters, such as interface and subinterface statistics, BFD and the control plane's CPU, where ON_CHANGE would send every tick of every counter. The ARP and ND caches are sampled too, because they sit under the sampled subinterfaces.

A path is streamed the same way by every report that reads it, and no ON_CHANGE path lies under a sampled one or the other way round. A test in `tests/test_registry.py` enforces both.

Because SR Linux streams entire subtrees when subscribing to a branch, multiple reports may overlap (for example, `/interface[name=lag*]` and `/interface[name=*]/statistics` both live under `/interface`). What a report reads from the tree is strictly filtered back down to what its path selects—matching key predicates and specific named branches—ensuring each report only sees what its own `Get` would have returned.

### 4. Fallback polling & cache resynchronization
Paths that cannot be streamed fall back to short-TTL `Get` operations. Additionally, every node is fully re-read every `--resync` seconds (default: 300s) to guarantee that missed deletions cannot leave stale rows behind. Re-sync sweeps are executed round-robin across nodes rather than simultaneously.

Whatever arrives while a node is being re-read is recorded and applied again, in order, to the re-read tree once it is swapped in. SAMPLE would re-send it on the next tick, but ON_CHANGE never would. When one path covers another, such as `route-table/ipv4-unicast` and its `route/ipv4-prefix` keys, only the wider path's `Get` is written to the tree. A `Get` does not say which leaves key a list, so the narrower answer would otherwise replace every entry with its keys alone.

In between sweeps, list entries that a SAMPLE subscription ceases to send are aged out after a few sample intervals. This aging is measured relative to the last update timestamp received by that subtree rather than wall-clock time, preventing a node with a lagging telemetry stream from spuriously clearing out data.

### 5. Narrow path subscriptions
Subscriptions stream everything under their path (configuration and state alike). `fcli` subscribes strictly to the specific branches read by the getter. For example, subscribing to `/network-instance[name=*]` entirely would stream all route tables and the entire BGP RIB—on a spine router, this represents massive payload volume capable of delaying the telemetry stream. Instead, `fcli` selectively subscribes only to types, interfaces, overlays, and BGP instances.

The same applies to the readings behind the health view and the change timeline. They stream only the `default` network-instance's route table, which is the underlay the checks read, plus the `active-routes` counter of every other table. They never stream every prefix of every VRF. Those full tables are subscribed only while someone has a RIB report or a lens open that reads them, and they are retired once nothing has read them for `--idle-timeout`.

---

### 6. Evicting what the device stopped sending

An ON_CHANGE path is told what goes: the device sends a delete, and the entry is removed as soon as it arrives. That applies to a peer, a neighbour, a route or an instance. A subscription below a list entry is only told about what is under it. For `interface[name=*]/subinterface`, the device reports the subinterface going, never the interface. So an entry that is left with nothing but its keys is removed too.

Two things make that hold across the mixed modes:

* An entry written by an ON_CHANGE path is *pinned*. The SAMPLE sweep below skips pinned entries, since ON_CHANGE saying nothing means nothing changed, not that the entry is gone. The sweep still ages what sampled paths put inside them. Once a delete leaves no ON_CHANGE data in an entry, it is unpinned and ages like any other.
* A subscription is replaced whenever a report adds paths, and deletes made during the swap are sent to nobody. The initial sync of the new subscription re-sends everything its ON_CHANGE paths hold, so once it ends (`sync_response`), any pinned entry the sync did not re-send is dropped.

A SAMPLE subscription re-sends every leaf of its subtree on each tick and never reports a delete, so an entry that disappears on the device - a dynamic BGP neighbour whose link went down, an LLDP neighbour, a DF candidate - is recognised by no longer being refreshed. Each list entry is stamped when it is written, and a sweep drops the ones its envelope has had three sample intervals (at least 45 s) of newer data without. SR Linux names every element of an update with its YANG module (`srl_nokia-network-instance:network-instance[...]`); envelopes are matched with those prefixes taken off, which is what lets the sweep see an envelope as being refreshed at all.

### 7. Staying within 36 paths per Subscribe

SR Linux accepts at most 36 paths in one Subscribe request. One more, and it refuses the whole request with `OUT_OF_RANGE` (*"Exceeded the maximum of 36 subscribed paths per subscribe request"*), and every path of the node stops streaming. The request is therefore planned before it is sent:

* a path another path of the request covers (same elements, keys at least as wide: `interface[name=*]/subinterface` covers `interface[name=irb*]/subinterface` and the ARP/ND neighbour lists under it) is left out; the updates for it arrive through the covering path, which is sampled as fast as the fastest path it stands in for;
* if the request is still too long, the most slowly sampled paths are polled with a `Get` at their sample interval instead, e.g. the fan-tray and power-supply lists.

`GET /api/status` shows each path as `covered_by` another or `polled`. A node that refuses a request with a lower limit is believed, and the request is planned again within it.

## Handling cold starts and pending paths

SR Linux responds to a `Get` request for an empty subtree with an empty response, which does not reveal the YANG schema shape expected by the report getter. Control-plane tables often start empty (no MACs learned yet, no IPv6 neighbors, no Ethernet-Segment destinations, or a spine with no bridge table).

`fcli` handles this gracefully:
* An empty path is marked as **`pending`** rather than failed.
* It is temporarily excluded from the `Subscribe` RPC and queried using the fallback short-TTL `Get`.
* The report renders cleanly as an empty table.
* As soon as a fallback `Get` returns an entry, the response shape is pinned and the path automatically transitions to the active subscription stream without requiring a manual reload.
* The status of paths can be inspected via `GET /api/status` (marked as `pending` or `streaming`).

---

## Connection lifecycle and failure recovery

### Booting nodes
When `fcli server` starts up alongside a freshly booted fabric, some nodes may still be initializing. Attempting to fetch TLS certificates from an unreachable node will fail. Rather than marking the node permanently dead:
* Unreachable nodes are retried in the background at most once every 30 seconds while an active report requests them.
* As nodes finish booting, `fcli` automatically connects, establishes subscriptions, and populates the UI without requiring a server restart.
* Unreachable nodes are reported under `unreachable` in `GET /api/status`.

### Reconnection and node recovery
If a node reboots or the lab is redeployed while `fcli server` is running, recovery occurs at multiple layers:
1. **Candidate paths**: A failed `Get` does not disqualify a path from streaming; it remains a candidate and will be re-subscribed on the next `--resync` sweep that receives a response.
2. **Graceful degradation**: Until an answer returns, tables retain their last-known state (timestamped with `generated` and `oldest_update` metadata) rather than blanking out.
3. **Cache suppression**: Failed `Get` requests are cached for the TTL duration to avoid spamming an offline device.

### Three-tier failure detection
Losing connectivity to a node is detected via three distinct mechanisms:
* The `Subscribe` RPC stream aborts or returns a gRPC error.
* Explicit `Get` RPCs time out or fail.
* **Missing SAMPLE telemetry**: If the route to a node disappears cleanly, TCP connections can silently hang without gRPC keepalives failing. A sampled path reports at predictable intervals, so missing updates indicate a silent link failure and change the node status in the sidebar from `up` to unreachable. ON_CHANGE paths have no cadence to miss, so a subscription that carries any also samples `/system/information/current-datetime` every 10 s as a heartbeat.

Under the hood:
* The gRPC channel uses `max_reconnect_backoff_ms = 10000` (10 seconds) instead of gRPC's default 2-minute backoff.
* If a node's `Get` requests fail or hang for longer than 30 seconds, its channel and connection are replaced completely.

---

## gNMI Sessions and Scalability

SR Linux enforces a concurrent session limit per gRPC server:
```
/system/grpc-server[name=mgmt]/session-limit  (default: 20)
```
This budget is shared across all gRPC clients connecting to the switch. Every active RPC, including long-lived streaming `Subscribe` RPCs, counts against this limit.

To prevent exhausting session limits:
* **One session per node**: `fcli server` multiplexes all open reports into a **single `Subscribe` RPC** per node, with at most one auxiliary `Get` in flight at any time.
* **Batched RPC restarts**: Because gNMI does not support dynamically adding paths to an existing subscription, adding new paths requires restarting the `Subscribe` RPC. These restarts are debounced and batched, so navigating a dashboard with multiple reports triggers a single re-subscription rather than one per widget.
* **Idle path pruning**: Paths that have not been requested by any active report for longer than `--idle-timeout` (default: 900s) are automatically pruned from the subscription.

Node session utilization can be verified on the SR Linux CLI:
```bash
info from state /system grpc-server mgmt client *
```
or via the `fcli` HTTP API at `GET /api/status` under `max_sessions_per_node`.
