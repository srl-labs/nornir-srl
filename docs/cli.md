# CLI reports

Same inventory, same getters, one shot. Output defaults to a Rich table; `-o json|yaml|csv` for structured output.

```
❯ fcli --help
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf
  -b, --box-type TEXT    box type of printed table ('python -m rich.box')
  -t, --topo-file PATH   CLAB topology file. Mutually exclusive with -c
  --fabric TEXT          name the fabric's state is kept under [env: FCLI_FABRIC;
                         default: the lab's name, or the Nornir config's directory]
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
  endpoints     Displays the hosts ARP/ND know, with their VRFs, access port and ES
  routing-pol   Displays Routing Policies (json/yaml only)
  checks        Runs the fabric sanity checks and lists what they found
  incidents     Groups the checks' findings by root cause, worst first
  where         Finds which nodes know about a MAC or IP address
  path          Walks the route tables hop by hop towards a destination
  service       Shows one service as every node that carries it sees it
  snapshot      Keeps a report as it is now, to compare a fabric against later
  diff          Compares a report against a snapshot, or one node against another
  config-commits Displays the commits each node logged to its configuration
  history       Lists what the server's timeline recorded, from its history on disk
  baseline      Keeps the fabric as it is now as a named baseline
  baselines     Lists the baselines kept for this fabric
  drift         Shows how the fabric now differs from a kept baseline
  config-history Lists the configurations the server kept after each commit, or what one changed
  running-config Prints each node's running configuration as set lines, secrets redacted
```

Two kinds of filter, plus report-specific options:

- **inventory** (`-i`, global): `-i hostname=clab-4l2s-l1` or `-i role=leaf`, based on inventory data. Multiple filters are ANDed.
- **field** (`-f`, per report): `-f state="esta.*"`. Values are case-insensitive regexes; repeat `-f` to filter on several columns.
- **report-specific**: `bgp-rib` needs `-r evpn|ipv4|ipv6|l3vpn-v4|l3vpn-v6` (or the long `l3vpn-*-unicast` names) and optionally `-t 1|2|3|4|5` for EVPN route type. `ipv4-rib` / `ipv6-rib` take `-a` for an LPM lookup. `ifstats` takes `-s` for the sampling interval.

`fcli <report> --help` shows the options for that report.

## Examples

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

## History and configuration

The CLI reads and writes the same history file `fcli server` keeps, by the same fabric name (see [History](history.md)), so these work with or without a server running:

```bash
fcli -t topo.clab.yml history --since 7d -k config -k bgp   # what the server recorded
fcli -t topo.clab.yml baseline before-upgrade --note CHG-1042
fcli -t topo.clab.yml baselines
fcli -t topo.clab.yml drift before-upgrade                  # exits non-zero if something stopped working
fcli -t topo.clab.yml -i node=leaf1 config-history          # the configurations kept after each commit
fcli -t topo.clab.yml -i node=leaf1 config-history --diff --commit 42
fcli -t topo.clab.yml -i role=leaf running-config -m 'protocols bgp group'
```

`baseline` makes the new baseline the one the server compares against (`--keep-only` to only keep it). `drift` compares with the active baseline when given no name. `running-config` reads the configuration live; `config-history` reads what the server kept, so it is empty for a fabric no server has watched with its history on.

## Debug logging

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

