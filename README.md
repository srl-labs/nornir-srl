
![Demo](https://github.com/srl-labs/nornir-srl/blob/main/imgs/fcli_demo.gif)
# nornir-srl

This module provides a [Nornir](https://nornir.readthedocs.io/en/latest/) connection [plugin](https://nornir.tech/nornir/plugins/) for Nokia SRLinux devices. It uses the gNMI management interface of SRLinux to fetch state and push configurations and the [PyGNMI](https://github.com/akarneliuk/pygnmi) Python module to interact with gNMI. 

Rather than limiting the connection plugin to primitives like `open_connection`, `close_connection`, `get`, `set`, etc, this module provides also methods to get information from the device for common resources. Since the device model tends to change between releases, it was considered a better approach to provide this functionality as part of the connection plugin and hide complexity of model changes to the user or Nornir tasks. 

In addition to the connection plugin, there is a set of Nornir tasks that use the connection plugin to perform common operations on the device, like get BGP peers, get MAC table, get subinterfaces, etc. These Nornir tasks are called by a command-line interface `fcli` that provides a network-wide CLI to perform show commands across an entire set or subset for SRLinux nodes.

> **Note:** The current functionality is focused on a read-only _network-wide CLI_ to perform show commands across an entire set or subset for SRLinux nodes, as defined in the Nornir inventory and through command-line filter options. It shows output in a tabular format for easy reading.
Following versions may focus on configuration management and command execution on the nodes.

# Quickstart

## Prerequisites

- have [Containerlab](https://containerlab.dev/) installed
- have a running containerlab topology with SRLinux nodes
- Internet access to pull the `nornir-srl` container image

## Create a shell alias for `fcli`

- go to the directory where your containerlab topology file is located
- create an alias for `fcli` as follows and modify the `CLAB_TOPO` to match your topology file name
- modify the `--network` option to match your containerlab network name (default is the name of the lab)
- latest version of `nornir-srl` container image is [here](https://github.com/srl-labs/nornir-srl/pkgs/container/nornir-srl). Modify the tag accordingly if you want to use a different version

```
CLAB_TOPO=topo.yaml && alias fcli="docker run -t --network $(grep '^name:' $CLAB_TOPO | awk '{print $2}') --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/${CLAB_TOPO}:/topo.yml ghcr.io/srl-labs/nornir-srl:latest -t /topo.yml"
```

## Run `fcli`

```
❯ fcli --help
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
                         [default: nornir_config.yaml]
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf.
                         Possible filter-fields are defined in inventory.
                         Multiple filters are ANDed
  -b, --box-type TEXT    box type of printed table, e.g. -b
                         minimal_double_head. 'python -m rich.box' for options
  -t, --topo-file PATH   CLAB topology file, e.g. -t topo.yaml. Mutually
                         exclusive with -c
  --cert-file PATH       CLAB certificate file, e.g. -c ca-root.pem
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  bgp-peers  Displays BGP Peers and their status
  bgp-rib    Displays BGP RIB
  ipv4-rib   Displays IPv4 RIB entries, LPM lookup
  lldp       Displays LLDP Neighbors
  mac        Displays MAC Table
  ni         Displays Network Instances and interfaces
  subif      Displays Sub-Interfaces of nodes
  sys-info   Displays System Info of nodes
```

# Installation

## Docker-based installation

This is the easiest way to get started. It requires [Docker](https://docs.docker.com/get-docker/) and optionally  [Containerlab](https://containerlab.dev/) to be installed on your system.

> NOTE: if you have issues connecting to the docker network of containerlab from the `nornir-srl` container that uses the standard bridge `docker0`, make sure proper `iptables` rules are in place to permit traffic between different Docker networks, which is **by default blocked**. For example, on Ubuntu 20.04, you can use the following command:

```
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

Alternatively, you can attach the `nornir-srl` container to the containerlab network to avoid adding iptables rules (cf. aliases below).

To run `fcli`, create an alias in your shell session. For example, assuming you're using containerlab and  you have a `clab_topo.yml` file in your current directory and lab is up and running:

```
CLAB_TOPO=clab_topo.yml && alias fcli="docker run -t --network $(grep '^name:' $CLAB_TOPO | awk '{print $2}') --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/${CLAB_TOPO}:/topo.yml ghcr.io/srl-labs/nornir-srl:0.2.1 -t /topo.yml"
```

This command assumes that the containerlab topology file is named `clab_topo.yml` and is in the current directory. If not, change the `CLAB_TOPO` variable accordingly. Also, it assumes that the containerlab topology is using the default containerlab docker-network naming, i.e. name of the lab. If you have overridden the management network with `.mgmt.network` in the topology file, change the `--network` option accordingly.

## Python-based installation with `pip`

Create a Python virtual-env using your favorite workflow, For example:
```
mkdir nornir-srl && cd nornir-srl
python3 -m venv .venv
source .venv/bin/activate
```
Following command will install the the `nornir-srl` module and all its dependencies, including Nornir core.

```
pip install wheel
pip install -U nornir-srl
```

## Nornir-based inventory mode

In this mode, a Nornir configuration file must be provided with the `-c` option. The Nornir inventory is polulated by the `InventoryPlugin` and associated options as specified in the config file. See below for an example with the included `YAMLInventory` plugin and the associated inventory files. This mode is typically used for real hardware-based fabric.

Create the Nornir confguration file, for example:

```yaml
# nornir_config.yaml
inventory:
    #    plugin: SimpleInventory
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

Create the inventory files as referenced in the above configuration file, for example:

```yaml
## hosts.yaml
clab-4l2s-l1:
    hostname: clab-4l2s-l1
    groups: [srl, fabric, leafs]
clab-4l2s-l2:
    hostname: clab-4l2s-l2
    groups: [srl, fabric, leafs]
clab-4l2s-s1:
    hostname: clab-4l2s-s1
    groups: [srl, fabric, spines]
```
```yaml
## groups.yaml
global:
    data:
        domain: clab
srl:
    connection_options:
        srlinux:
            port: 57400
            username: admin
            password: admin
            extras:
                path_cert: "./root-ca.pem"
spines:
    groups: [ global ]
    data:
        role: spine
        type: ixr-d3
leafs:
    groups: [ global ]
    data:
        role: leaf
        type: ixr-d2
```
The root certificate is specified once for all devices in group `srl` via the `connection_options.srlinux.extras.path_cert` parameter.

## CLAB-based inventory mode

In this mode, the Nornir inventory is populated by a containerlab topology file and no further configuration files are needed. The containerlab topo file is specified with the `-t` option. 

`fcli` converts the topology file to a _hosts_ and _groups_ file and only nodes of kind=srl are populated in the host inventory. Furthermore, the `prefix` parameter in the topo file is considered to generate the hostnames. The presence of _labels_ in the topo file is mapped into node-specific attribs that can be used in inventory filters (`-i` option).

# Usage

` fcli` supports a set of reports that can be run against a set of SRLinux nodes. The set of nodes is defined by the Nornir inventory and optionally filtered by the `-i` option.

```
❯ fcli
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
                         [default: nornir_config.yaml]
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf.
                         Possible filter-fields are defined in inventory.
                         Multiple filters are ANDed
  -b, --box-type TEXT    box type of printed table, e.g. -b
                         minimal_double_head. 'python -m rich.box' for options
  -t, --topo-file PATH   CLAB topology file, e.g. -t topo.yaml. Mutually
                         exclusive with -c
  --cert-file PATH       CLAB certificate file, e.g. -c ca-root.pem
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  bgp-peers  Displays BGP Peers and their status
  bgp-rib    Displays BGP RIB
  ipv4-rib   Displays IPv4 RIB entries, LPM lookup
  lldp       Displays LLDP Neighbors
  mac        Displays MAC Table
  ni         Displays Network Instances and interfaces
  subif      Displays Sub-Interfaces of nodes
  sys-info   Displays System Info of nodes
```

To run a specific report, use the corresponding command, e.g. `fcli mac` to display the MAC table of all nodes in the inventory. The output is a table with columns relevant to the report.

```
❯ fcli -b ascii mac
                                                              MAC Table                                                              
+-----------------------------------------------------------------------------------------------------------------------------------+
| Node            | NI          | Address           | Dest                                                  | Type                  |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l1    | macvrf-202  | 00:00:5E:00:01:01 | irb                                                   | irb-interface-anycast |
|                 |             | 1A:B4:09:FF:00:42 | vxlan-interface:vxlan1.202 vtep:192.168.255.2 vni:202 | evpn-static           |
|                 |             | 1A:B9:08:FF:00:42 | irb                                                   | irb-interface         |
|                 |             | 1A:DC:0E:FF:00:41 | lag1.1                                                | evpn                  |
|                 | macvrf-203  | 1A:B9:08:FF:00:42 | irb                                                   | irb-interface         |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l2    | macvrf-202  | 00:00:5E:00:01:01 | irb                                                   | irb-interface-anycast |
|                 |             | 1A:B4:09:FF:00:42 | irb                                                   | irb-interface         |
|                 |             | 1A:B9:08:FF:00:42 | vxlan-interface:vxlan1.202 vtep:192.168.255.1 vni:202 | evpn-static           |
|                 |             | 1A:DC:0E:FF:00:41 | lag1.1                                                | learnt                |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l4    | macvrf-201  | 1A:3B:0B:FF:00:41 | irb                                                   | irb-interface         |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-tor12 | macvrf-9998 | 00:00:5E:00:01:01 | lag1.1                                                | learnt                |
|                 |             | 1A:DC:0E:FF:00:41 | irb                                                   | irb-interface         |
|                 | macvrf-9999 | 1A:DC:0E:FF:00:41 | irb                                                   | irb-interface         |
+-----------------------------------------------------------------------------------------------------------------------------------+
```

Some reports have additional options. You can get help on the options with the `--help` option __after__ the report name, e.g. `fcli bgp-rib --help`:

```
❯ fcli bgp-rib --help
Usage: fcli bgp-rib [OPTIONS]

  Displays BGP RIB

Options:
  -f, --field-filter TEXT       filter fields with <field-name>=<glob-
                                pattern>, e.g. -f state=up -f
                                admin_state="ena*". Fieldnames correspond to
                                column names of a report
  -r, --route-fam [evpn|ipv4]   Route family for BGP RIB  [required]
  -t, --route-type [1|2|3|4|5]  Route type for EVPN routes
  --help                        Show this message and exit.
```

## Filtering

Optionally, you can specify filters to control the output. There are 2 types of filters:

- inventory filters, specified with the global `-i` option, filter on the inventory, e.g. `-i hostname=clab-4l2s-l1`  or `-i role=leaf` based on inventory data
- field filters, specified with the report-specific `-f` option. This filters based on the fields shown in the report and a glob pattern, e.g. `-f state="esta*"`. Multiple field filters can be specified by repeated `-f` options
- report-specific options are options specific to a report, if applicable. Currently, the only report that needs extra arguments is 'bgp-rib', i.e. `route_fam=evpn|ipv4|ipv6` and `route_type=1|2|3|4|5`. The latter relates to EVPN route-trypes and is optional. Defaults to '2' (mac-ip-routes). 

## Examples

### mac-table

Find all MAC entries on all leafs in mac-vrf `macvrf-202` that matches the pattern `1A:DC`

`fcli -i role=leaf mac -f NI=macvrf-202 -f Address="1A:DC:*"`

```
                             MAC Table                             
     Fields filter:{'NI': 'macvrf-202', 'Address': '1A:DC:*'}      
                 Inventory filter:{'role': 'leaf'}                 
+-----------------------------------------------------------------+
| Node         | NI         | Address           | Dest   | Type   |
|--------------+------------+-------------------+--------+--------|
| clab-4l2s-l1 | macvrf-202 | 1A:DC:0E:FF:00:41 | lag1.1 | evpn   |
|--------------+------------+-------------------+--------+--------|
| clab-4l2s-l2 | macvrf-202 | 1A:DC:0E:FF:00:41 | lag1.1 | learnt |
+-----------------------------------------------------------------+
```

### bgp-peers

Show all BGP peers on all nodes that are in state `active`:

`fcli bgp-peers -f state=active`

```
                                                                  BGP Peers                                                                   
                                                      Fields filter:{'state': 'active'}                                                       
+--------------------------------------------------------------------------------------------------------------------------------------------+
|              |           |                 | AFI/SAFI  | AFI/SAFI  |               |         |               |          |         |        |
|              |           |                 | EVPN      | IPv4-UC   |               |         |               |          |         |        |
| Node         | NI        | 1_peer          | Rx/Act/Tx | Rx/Act/Tx | export_policy | group   | import_policy | local_as | peer_as | state  |
|--------------+-----------+-----------------+-----------+-----------+---------------+---------+---------------+----------+---------+--------|
| clab-4l2s-l4 | ipvrf-200 | 10.200.4.100    | disabled  | down      | v200-out      | clients |               | 6848     | 65534   | active |
|--------------+-----------+-----------------+-----------+-----------+---------------+---------+---------------+----------+---------+--------|
| clab-4l2s-s1 | default   | 192.168.0.225   | disabled  | down      | pass-all      | dcgw    | pass-all      | 65100    | 65200   | active |
|              |           | 192.168.255.201 | 0/0/0     | disabled  | pass-evpn     | overlay | pass-evpn     | 100      | 100     | active |
|--------------+-----------+-----------------+-----------+-----------+---------------+---------+---------------+----------+---------+--------|
| clab-4l2s-s2 | default   | 192.168.0.229   | disabled  | down      | pass-all      | dcgw    | pass-all      | 65100    | 65200   | active |
|              |           | 192.168.0.231   | disabled  | down      | pass-all      | dcgw    | pass-all      | 65100    | 65201   | active |
+--------------------------------------------------------------------------------------------------------------------------------------------+
```

### ipv4-rib

Show all IPv4 routes on all nodes across all network-instances that matches address `192.168.0.7` with LPM (longest-prefix-match):

`fcli ipv4-rib -a 192.168.0.7`

```
                                       IPv4 RIB - hunting for 192.168.0.7                                       
+--------------------------------------------------------------------------------------------------------------+
| Node         | NI      | Act | Prefix         | itf                | metric | next-hop        | pref | type  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l1 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.0'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l2 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.2'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l3 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.4'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l4 | default | Yes | 192.168.0.7/32 |                    | 0      | [None]          | 0    | host  |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-s1 | default | Yes | 192.168.0.6/31 | ['ethernet-1/4.0'] | 0      | ['192.168.0.6'] | 0    | local |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-s2 | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
+--------------------------------------------------------------------------------------------------------------+
```

### bgp-rib

Show all active BGP routes with AF=ipv4 that are active and used for prefix `192.168.255.4/32`:

`fcli bgp-rib -r ipv4 -f Pfx="192.168.255.4/32" -f 0_st="u*>"`

```
                                                        BGP RIB - IPV4                                                         
                                   Fields filter:{'Pfx': '192.168.255.4/32', '0_st': 'u*>'}                                    
+-----------------------------------------------------------------------------------------------------------------------------+
| Node         | NI      | 0_st | Pfx              | as-path          | communities | lpref | med | neighbor    | next-hop    |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l1 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.0 | 192.168.0.0 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.0 | 192.168.1.0 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l2 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.2 | 192.168.0.2 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.2 | 192.168.1.2 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l3 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.4 | 192.168.0.4 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.4 | 192.168.1.4 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l4 | default | u*>  | 192.168.255.4/32 | i                |             | 100   | 0   | 0.0.0.0     | 0.0.0.0     |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-s1 | default | u*>  | 192.168.255.4/32 | [65004] i        |             | 100   | 0   | 192.168.0.7 | 192.168.0.7 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-s2 | default | u*>  | 192.168.255.4/32 | [65004] i        |             | 100   | 0   | 192.168.1.7 | 192.168.1.7 |
+-----------------------------------------------------------------------------------------------------------------------------+
```

Show all EVPN RT=2 routes for MAC address that starts with "1A:DC":

`fcli bgp-rib -r evpn -t 2 -f MAC="1A:DC:*"`

```
                                                                           BGP RIB - EVPN route-type 2                                                                            
                                                                         Fields filter:{'MAC': '1A:DC*'}                                                                          
+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------+
| Node         | NI      | 0_st | ESI                           | IP          | MAC               | RD                | as-path | next-hop      | origin | peer            | vni |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-l1 | default | u*>  | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.101 | 202 |
|              |         | *    | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.102 | 202 |
|              |         | u*>  | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.101 | 202 |
|              |         | *    | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.102 | 202 |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-s1 | default | *>   | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
|              |         | *>   | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-s2 | default | *>   | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
|              |         | *>   | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------+
```

