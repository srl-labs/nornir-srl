
![Demo](https://github.com/srl-labs/nornir-srl/blob/main/imgs/fcli_demo.gif)
# nornir-srl

This module provides a [Nornir](https://nornir.readthedocs.io/en/latest/) connection [plugin](https://nornir.tech/nornir/plugins/) for Nokia SRLinux devices. It uses the gNMI management interface of SRLinux to fetch state and push configurations and the [PyGNMI](https://github.com/akarneliuk/pygnmi) Python module to interact with gNMI. 

Rather than limiting the connection plugin to primitives like `open_connection`, `close_connection`, `get`, `set`, etc, this module provides also methods to get information from the device for common resources. Since the device model tends to change between releases, it was considered a better approach to provide this functionality as part of the connection plugin and hide complexity of model changes to the user or Nornir tasks. 

In addition to the connection plugin, there is a set of Nornir tasks that use the connection plugin to perform common operations on the device, like get BGP peers, get MAC table, get subinterfaces, etc. These Nornir tasks are called by a command-line interface `fcli` that provides a network-wide CLI to perform show commands across an entire set or subset for SRLinux nodes.

The current functionality is focused on a read-only _network-wide CLI_ to perform show commands across an entire set or subset for SRLinux nodes, as defined in the Nornir inventory and through command-line filter options. It shows output in a tabular format for easy reading.
Following versions may focus on configuration management and command execution on the nodes.

# Installation

## Docker-based installation

This is the easiest way to get started. It requires [Docker](https://docs.docker.com/get-docker/) and optionally  [Containerlab](https://containerlab.dev/) to be installed on your system.

> NOTE: if you have issues connecting to the docker network of containerlab from the `nornir-srl` container that uses the standard bridge `docker0`, make sure proper `iptables` rules are in place to permit traffic between different Docker networks, which is **by default blocked**. For example, on Ubuntu 20.04, you can use the following command:

```
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

Alternatively, you can attach the `nornir-srl` container to the containerlab network to avoid adding iptables rules (cf. aliases below).

To run fcli, create an alias in your shell session. For example, assuming you're using containerlab and  you have a `clab_topo.yml` file in your current directory and lab is up and running:

```
CLAB_TOPO=clab_topo.yml alias fcli="docker run -t --network $(grep '^name:' $CLAB_TOPO | awk '{print $2}') --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/${CLAB_TOPO}:/topo.yml ghcr.io/srl-labs/nornir-srl -t /topo.yml"
```

This command assumes that the containerlab topology file is named `clab_topo.yml` and is in the current directory. If not, change the `CLAB_TOPO` variable accordingly. Also, it assumes that the containerlab topology is using the default containerlab docker-network naming, i.e. name of the lab. If you have overridden the management network with `.mgmt.network` in the topology file, change the `--network` option accordingly.

To run a report, specify the report name as the first argument to the alias, for example:
```
fcli sys-info # for system information like NOS version, uptime, etc

fcli bgp-peers # for BGP peers
```

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


# Use

Currently, only the network-wide report functionality is supported via the `fcli` command
```
$ fcli --help
Usage: fcli [OPTIONS] REPORT

Options:
  -c, --cfg TEXT             Nornir config file  [default: nornir_config.yaml]
  -i, --inv-filter TEXT      filter inventory, e.g. -i site=lab -i role=leaf
  -f, --field-filter TEXT    filter fields with <field-name>=<glob-pattern>,
                             e.g. -f state=up -f admin_state="ena*"
  -b, --box-type TEXT        box type of printed table, e.g. -b
                             minimal_double_head. 'python -m rich.box' for
                             options
  -r, --report-options TEXT  report-specific options, e.g. -o route_fam=evpn
                             -o route_type=2 for 'bgp-rib report
  -t, --topo-file TEXT       CLAB topology file, e.g. -t topo.yaml
  --cert-file TEXT           CLAB certificate file, e.g. -c ca-root.pem
  --help                     Show this message and exit.
  ```
  `REPORT` is a mandatory argument and specifies the report to run. To know which reports are supported, specify a dummy report name:
```
$ fcli test
Report test not found. Available reports: ['bgp-peers', 'subinterface', 'ipv4-rib', 'mac-table', 'sys-info', 'nwi-itfs', 'lldp-nbrs']
```

## Nornir-based inventory mode

In this mode, a Nornir configuration file must be provided with the `-c` option. The Nornir inventory is polulated by the `InventoryPlugin` and associated options as specified in the config file. See below for an example with the included `YAMLInventory` plugin and the associated inventory files.

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

This mode is used for real hardware-based fabric.

## CLAB-based inventory mode

In this mode, the Nornir inventory is populated by a containerlab topology file and no further configuration files are needed. The containerlab topo file is specified with the `-t` option. 

`fcli` converts the topology file to a _hosts_ and _groups_ file and only nodes of kind=srl are populated in the host inventory. Furthermore, the `prefix` parameter in the topo file is considered to generate the hostnames. The presence of _labels_ in the topo file is mapped into node-specific attribs that can be used in inventory filters (`-i` option).

## Filtering

Optionally, you can specify filters to control the output. There are 2 types of filters:

- inventory filters, specified with the `-i` option, filter on the inventory, e.g. `-i hostname=clab-4l2s-l1`  or `-i role=leaf` based on inventory data
- field filters, specified with the `-f` option. This filters based on the fields shown in the report and a glob pattern, e.g. `-f state="esta*"`. Multiple field filters can be specified by repeated `-f` options
- report-specific options are options specific to a report, if applicable. Currently, the only report that needs extra arguments is 'bgp-rib', i.e. `route_fam=evpn|ipv4|ipv6` and `route_type=1|2|3|4|5`. The latter relates to EVPN route-trypes and is optional. Defaults to '2' (mac-ip-routes). 


# Demo

## Prerequisites

- Containerlab binary installed
- `nornir-srl` installed as described above: `pip install -U nornir-srl` in a Python virtual-env
- sufficient resources to run 8 SRLinux containers
- big screen estate to show the output (or small font size) 

## Run the demo

clone the `nornir-srl` repo and cd into the `demo` folder
```
git clone https://github.com/srl-labs/nornir-srl.git
cd nornir-srl/demo
./run_demo.sh
```

It will spin up a 6-node SRLinux fabric and run all the available `fcli` reports sequentially.

Remove the lab with `clab destroy -t demo.clab.yaml`

  
