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


