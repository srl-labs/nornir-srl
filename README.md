# nornir-srl
This module provides a [Nornir](https://nornir.readthedocs.io/en/latest/) connection [plugin](https://nornir.tech/nornir/plugins/) for Nokia SRLinux devices. It uses the gNMI management interface of SRLinux to fetch state and push configurations and the [PyGNMI](https://github.com/akarneliuk/pygnmi) Python module to interact with gNMI. 

Rather than limiting the connection plugin to primitives like `open_connection`, `close_connection`, `get`, `set`, etc, this module provides also methods to get information from the device for common resources. Since the device model tends to change between releases, it was considered a better approach to provide this functionality as part of the connection plugin and hide complexity of model changes to the user or Nornir tasks. 

In addition to the connection plugin, there is a set of Nornir tasks that use the connection plugin to perform common operations on the device, like get BGP peers, get MAC table, get subinterfaces, etc. These Nornir tasks are called by a command-line interface `fcli` that provides a network-wide CLI to perform show commands across an entire set or subset for SRLinux nodes.

The current functionality is focused on a read-only _network-wide CLI_ to perform show commands across an entire set or subset for SRLinux nodes, as defined in the Nornir inventory and through command-line filter options. It shows output in a tabular format for easy reading.
Following versions may focus on configuration management and command execution on the nodes.

# Prerequisites

This module requires [Nornir Core](https://github.com/nornir-automation/nornir) that includes the Nornir core components for which this module is an add-on.
Nornir needs a [configuration file](https://nornir.readthedocs.io/en/latest/configuration/index.html) to tell it at a minimum where to find the inventory and what inventory plugin is used. Also runner configuration parameters, like #threads/workers for parallel execution are defined here but sane defaults are used.

Since this module is using gNMI as the management inteface, at a minimum, a CA certificate is required that was used to create per-device certs and keys. If you use [Containerlab](https://containerlab.dev/) this root cert is auto-generated for SRLinux nodes and available in the lab subfolder created by the containerlab cli. This file needs to be referenced via the inventory (per-device or per-group). See below for details.

# Installation

Create a Python virtual-env using your favorite workflow, For example:
```
mkdir nornir-srl && cd nornir-srl
python3 -m venv .venv
source .venv/bin/activate
```
Following command will install the the `nornir_srl` module and all its dependencies, including Nornir core.

```
pip install wheel
pip install -U nornir-srl
```
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

# Use

Currently, only the network-wide cli functionality is supported via the `fcli` command
```
$ fcli --help
Usage: fcli [OPTIONS] REPORT

Options:
  -c, --cfg TEXT             Nornir config file  [default: nornir_config.yaml]
  -i, --inv-filter TEXT      filter inventory, e.g. -i site=lab -i role=leaf
  -f, --field-filter TEXT    filter fields, e.g. -f state=up -f
                             admin_state=enable
  -b, --box-type TEXT        box type of printed table, e.g. -b
                             minimal_double_head. 'python -m rich.box' for
                             options
  -r, --report-options TEXT  report-specific options, e.g. -o route_fam=evpn
                             -o route_type=2 for 'bgp-rib report
  --help                     Show this message and exit.
  ```
  `REPORT` is a mandatory argument and specifies the report to run. To know which reports are supported, specify a dummy report name:
```
$ fcli test
Report test not found. Available reports: ['bgp-peers', 'subinterface', 'ipv4-rib', 'mac-table', 'sys-info', 'nwi-itfs', 'lldp-nbrs']
```

## Nornir-based inventory mode

In this mode, a Nornir configuration file must be provided with the `-c` option. The Nornir inventory is polulated by the `InventoryPlugin` and associated options as specified in the config file. See above for an example with the included `YAMLInventory` plugin and the associated inventory files.

This mode is used for real hardware-based fabric.
## CLAB-based inventory mode

In this mode, the Nornir inventory is populated by a containerlab topology file and no further configuration files are needed. The containerlab topo file is specified with the `-t` option. `fcli` converts the topology file to a _hosts_ and _groups_ file. Only nodes of kind=srl are populated in the host inventory. Furthermore, the `prefix` parameter in the topo file is considered to generate the hostnames. Also, the presence of _labels_ in the topo file is mapped into node-specific attribs that can be used in inventory filters (`-i` option).

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

  
