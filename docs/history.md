# History: the timeline, baselines and configurations on disk

`fcli server` watches the fabric and keeps a timeline of what changed, a baseline to compare against, and every configuration the nodes commit. With its history on (the default whenever the timeline runs), all of it outlives the server process: one SQLite file per fabric, `~/.local/state/fcli/history/<fabric>.sqlite`, beside the snapshots, the learned cabling and the acknowledgements.

```bash
fcli -t topo.clab.yml server                       # history on, 30 days of changes
fcli -t topo.clab.yml server --history-days 90     # keep changes longer; 0 keeps them all
fcli -t topo.clab.yml server --history-dir /srv/fcli/history
fcli -t topo.clab.yml server --no-history          # everything in memory, as before
```

The fabric is named by the containerlab topology's `name:`. A fabric loaded from a Nornir config is called `fabric`; give two such fabrics their own `--history-dir`.

## What is kept

| What | When | Kept for |
| --- | --- | --- |
| Every timeline change | As it is recorded | `--history-days` (30 by default) |
| The watched prefixes | When one is added or removed | Until removed |
| The last reading | Every 20 readings, and on a clean stop | Replaced each time |
| Baselines | When someone sets one: the web UI, `POST /api/baseline`, `fcli baseline`, MCP `mark_baseline` | Until deleted; the 20 newest are kept |
| Each node's configuration | After every commit the node logs, and on start for a node whose newest commit has none kept | Until the file is deleted; the same content is stored once |

On start, the server reads back the newest changes (up to 5000) into its timeline, so the Changes page, the drift dating and the flapping check carry on where they were.

## fcli stopping and starting

fcli's own comings and goings are on the timeline as changes of kind `server`, node `fcli`. They are listed whatever inventory filter a view has, since they are about every node.

* A clean stop records `running -> stopped`.
* A start records `stopped -> running` and how long nothing was watched: `started; not running for 3h 12m`.
* A server that was killed records no stop. The server writes a heartbeat on every reading, and the next start records the stop as a **warning** at the last heartbeat: `it stopped without shutting down, and nothing was watched until it started again`.

## What changed while fcli was not running

The last reading is kept as the gNMI data it was made of. On the first reading after the warm-up, the next run reads it back and compares it with the fabric as it is now. Everything that differs is recorded once, with the restart's time and a detail that starts `while fcli was not running (since <when the last reading was taken>)`. These changes happened at some point in that gap. The comparison does not say when.

Until that comparison has run, the kept reading is never replaced, so a server that is stopped again during its warm-up does not lose what happened before it.

## Baselines that last

A baseline is a reading kept under a name, compared against with `since: baseline` on the Changes page. Before the history, it was taken once the server had settled, and again when someone pressed **Set baseline**, and it ended with the server.

Now:

* **Set baseline** (the **📌 Baseline** menu on the Changes page) takes an optional name and note, e.g. `before-upgrade`, `CHG-1042`. The default name is `baseline`.
* The baseline that was set is the *active* one. After a restart it is read back and compared against, rather than a new one being taken.
* The menu lists the kept baselines: **Use** compares against another one, **✕** deletes one, **Compare with the latest reading instead** goes back to an unkept baseline taken now.
* Without an active baseline, the server takes one when it settles, as before, and does not keep it.

The CLI and the MCP server share the same file by the same fabric name. A baseline marked with `fcli baseline` or MCP `mark_baseline` becomes the active one the server compares against, from its next start or as soon as someone picks it in the menu. `fcli drift` and MCP `changes_since_baseline` compare the fabric with any kept baseline without a server running.

### How a reading is kept

A reading is the records the report getters built from what the nodes answered. A baseline is not kept as those records, which change from one fcli release to the next. It is kept as the answers themselves: every gNMI `Get` the getters made, with the paths, the datatype and the response. Reading it back runs today's getters against those answers, the same way the release matrix in the tests replays a lab with no device present. So a baseline taken before an upgrade of fcli still compares after it.

One report is not kept: the interface rates, which the server derives from counter samples rather than a `Get`. A reading read back holds no `ifstats`, and its `itf_errors` findings are left out of both sides of any comparison with it, so that alone never reads as drift.

## Configurations

The `config_commits` report reads each node's commit log (`/system/configuration/commit`: id, user, comment, candidate, status and times), streamed ON_CHANGE. Every commit past the newest one seen becomes a timeline change of kind `config`:

```
12:40:04  leaf1  info  config  commit 3  committed  by root, 'fcli test: BFD and ipv6 off on leaf1', at 2026-10-04T10:40:02Z; +2 -2 lines
```

After a commit, the server reads the node's running configuration (one `Get` of `/`, datatype `config`, past the Get cache), keeps it, and compares it with the previous one. The commit's change then says how many lines it added and removed. Several commits in one reading are kept as one configuration, the newest commit's. A commit that did not complete is a warning.

What a commit changed is one click away. Every commit on the timeline links to the **Config Diff** lens, which shows the lines it removed and added in SR Linux's flat syntax:

```
- set / network-instance default protocols bgp group fabric failure-detection enable-bfd true
+ set / network-instance default protocols bgp group fabric failure-detection enable-bfd false
```

On the lens itself, **Node**, **Commit** and **Against** are drop-downs of what the history holds: the nodes with kept configurations, and for the chosen node each kept commit by id, user, comment and time. **Commit** left at *newest* shows the newest kept commit, **Against** left at *the one before* compares with the configuration kept before it; picking an older commit there shows several commits' worth of change at once. The choice is kept in the URL (`#config_diff?host=leaf1&commit=8&against=3`).

The same diffs are available through `GET /api/config/{node}/diff`, `fcli config-history --diff` and MCP `config_diff`. The Ask agent is told to read the diff of a commit that precedes a fault.

### Set lines

A configuration is kept as the JSON tree the `Get` answered, with module prefixes and annotations taken out. It is rendered as the `set / ...` lines `info flat` prints. JSON does not say which leaves key a YANG list, so the keys are worked out from the entries: the names SR Linux keys its lists by (`name`, `id`, `index`, `sequence-id`, ...), extended until they tell the entries apart, plus a few lists known to need two keys (`acl-filter`: `name`, `type`). Both sides of a comparison are keyed together. On a 26.7 node this matched SR Linux's own `info flat` on 643 of 646 lines, and the three that differed were multi-line strings.

### Secrets

Configurations hold secrets. Before anything is kept or shown, every leaf whose name says it is one (`password`, `hashed-password`, `key`, `private-key`, `pre-shared-key`, `authentication-key`, `community`, anything ending in `-password` or `-secret`, ...) is replaced by a digest of it: `<redacted a23ade5a>`. The digest is salted with a random value kept in the history file, so a changed password still reads as changed, but no value can be looked up from it. Public keys (`ssh-key`) and certificates are not secrets and are kept as they are.

Even redacted, a configuration says a lot about a network. The history directory is created readable by its owner only (`0700`, files `0600`).

## Reading the history

| Surface | What |
| --- | --- |
| Web UI | The Changes page (the timeline, read back on start), the 📌 Baseline menu, the Config Diff lens |
| HTTP | `GET /api/history?since=7d&node=leaf1&kind=config`, `GET /api/baselines`, `GET /api/configs`, `GET /api/config/{node}`, `GET /api/config/{node}/diff` |
| CLI | `fcli history`, `fcli baselines`, `fcli baseline`, `fcli drift`, `fcli config-history`, `fcli running-config` |
| MCP | `fabric_history`, `list_baselines`, `mark_baseline`, `changes_since_baseline`, `config_history`, `config_diff`, `running_config` |

`fcli history` and MCP `fabric_history` read what the server recorded, including the changes older than the 5000 it holds in memory. The server does not need to be running.

## Configuration checks

Two checks compare how the two ends of a session or a link are configured. A mismatch is reported even while the session is up, because what it costs (a family not exchanged, a failure detected slowly on one end) is invisible from either end alone.

| Check | Finds |
| --- | --- |
| `bgp_peer_mismatch` | For every BGP session between two nodes of the fabric, paired by the addresses each node owns (link-local peers included): an end that expects another AS than the far end runs, a session the far end never configured (unless it takes dynamic neighbours), a family enabled on one end only, BFD on one end only |
| `igp_peer_mismatch` | For both ends of every LLDP link: the IGP on one end only (where the other end runs it elsewhere), an OSPF area that differs, an IS-IS or OSPF network type that differs, one end passive |

In the incidents, a mismatch is the root of the sessions and adjacencies it keeps down, so turning BFD off on one leaf's peer group reads as *"BGP configuration mismatch on 4 links"*, with the BFD sessions that went down as its consequences.
