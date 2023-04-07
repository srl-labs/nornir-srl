# nornir_srl
This module provides a [Nornir](https://nornir.readthedocs.io/en/latest/) connection [plugin](https://nornir.tech/nornir/plugins/) for Nokia SRLinux devices. It uses the gNMI management interface of SRLinux to fetch state and push configurations.

The current functionality is focused on a read-only _network-wide CLI_ to perform show commands across an entire set or subset for SRLinux nodes, as defined in the Nornir inventory and through command-line filter options. It shows output in a tabular format for easy reading.
Following versions will focus on configuration management and command execution on the nodes.

# Prerequisites

This module requires [Nornir Core](https://github.com/nornir-automation/nornir) that includes the Nornir core components for which this module is an add-on.
Nornir needs a [configuration file](https://nornir.readthedocs.io/en/latest/configuration/index.html) to tell it at a minimum where to find the inventory and what inventory plugin is used. Also runner configuration parameters, like #threads/workers for parallel execution are defined here but sane defaults are used.

Since this module is using gNMI as the management inteface, at a minimum, a CA certificate is required that was used to create per-device certs and keys. If you use [Containerlab](https://containerlab.dev/) this root cert is auto-generated for SRLinux nodes and available in the lab subfolder created by the containerlab cli. This file needs to be referenced via the inventory (per-device or per-group). See below for details.

# Installation

Create a Python virtual-env using your favorite workflow, For example:
```
python3 -m venv .venv
source .venv/bin/activate
```
Following command will install the the `nornir_srl` module and all its dependencies, including Nornir core.

```
pip install nornir_srl
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
fcli --help
Usage: fcli [OPTIONS] REPORT

Options:
  -c, --cfg TEXT           Nornir config file  [default: nornir_config.yaml]
  -i, --inv-filter TEXT    filter inventory, e.g. -i site=lab -i role=leaf
  -f, --field-filter TEXT  filter fields, e.g. -f state=up -f
                           admin_state=enable
  --help                   Show this message and exit.
  ```
  `REPORT` is a mandatory argument and specifies the report to run. To know which reports are supported, specify a dummy report name:
```
  fcli test
Report test not found. Available reports: ['bgp-peers', 'subinterface', 'ipv4-rib', 'mac-table', 'sys-info', 'nwi-itfs', 'lldp-nbrs']
```
The nornir configuration file (`-c` option) is mandatory for nornir to find the inventory files.
Optionally, you can specify filters to control the output. There are 2 types of filters:

    - inventory filters, specified with the `-i` option, filter on the inventory, e.g. `-i hostname=clab-4l2s-l1`  or `-i role=leaf` based on inventory data
    - field filters, specified with the `-f` option. This filters based on the fields shown in the report and a value substring, e.g. `-f state=esta`. Multiple field filters can be specified by repeated `-f` options

An example:
```
$ fcli bgp-peers -i role=spine
                                        BGP Peers                                         
                               Inventory:{'role': 'spine'}                                
               ╷          ╷                 ╷         ╷          ╷         ╷              
  Node         │ NetwInst │ 1_Peer          │ 2_Group │ local_as │ peer_as │ state        
 ══════════════╪══════════╪═════════════════╪═════════╪══════════╪═════════╪═════════════ 
  clab-4l2s-s1 │ default  │ 192.168.0.1     │ leafs   │ [65100]  │ 65001   │ established  
               │          │ 192.168.0.3     │ leafs   │ [65100]  │ 65002   │ established  
               │          │ 192.168.0.5     │ leafs   │ [65100]  │ 65003   │ established  
               │          │ 192.168.0.7     │ leafs   │ [65100]  │ 65004   │ established  
               │          │ 192.168.0.225   │ dcgw    │ [65100]  │ 65200   │ active       
               │          │ 192.168.0.227   │ dcgw    │ [65100]  │ 65201   │ active       
               │          │ 192.168.255.1   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.2   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.3   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.4   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.200 │ overlay │ [100]    │ 100     │ connect      
               │          │ 192.168.255.201 │ overlay │ [100]    │ 100     │ connect      
 ──────────────┼──────────┼─────────────────┼─────────┼──────────┼─────────┼───────────── 
  clab-4l2s-s2 │ default  │ 192.168.0.229   │ dcgw    │ [65100]  │ 65200   │ active       
               │          │ 192.168.0.231   │ dcgw    │ [65100]  │ 65201   │ active       
               │          │ 192.168.1.1     │ leafs   │ [65100]  │ 65001   │ established  
               │          │ 192.168.1.3     │ leafs   │ [65100]  │ 65002   │ established  
               │          │ 192.168.1.5     │ leafs   │ [65100]  │ 65003   │ established  
               │          │ 192.168.1.7     │ leafs   │ [65100]  │ 65004   │ established  
               │          │ 192.168.255.1   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.2   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.3   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.4   │ overlay │ [100]    │ 100     │ established  
               │          │ 192.168.255.200 │ overlay │ [100]    │ 100     │ connect     
```

  
