# Installation and inventory

Requires Python 3.10+.

> [!NOTE]
> `pip install -U nornir-srl` (or `uv tool install nornir-srl`) installs the latest stable release published on PyPI.
> `uv tool install git+https://github.com/srl-labs/fcli` installs the bleeding-edge development version directly from git `main`.

## `uv` (recommended)

[`uv`](https://github.com/astral-sh/uv) is a standalone Python package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install latest development version from git main:
uv tool install git+https://github.com/srl-labs/fcli

# Or install the latest release published on PyPI:
uv tool install nornir-srl
```

This puts `fcli` and `fcli-mcp` on your `PATH` (typically `~/.local/bin`).

From a clone of this repo, `uv tool install .` or `uv pip install -e .` does the same against local sources.

## pip

Install the latest release published on PyPI into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U nornir-srl
```

To install the latest development version from `main` via pip:
```bash
pip install git+https://github.com/srl-labs/fcli
```

## Docker

The image is [ghcr.io/srl-labs/fcli](https://github.com/srl-labs/fcli/pkgs/container/fcli). Attach it to the containerlab management network, publish port 8080, and bind-mount the topology file:

```bash
CLAB_TOPO=topo.clab.yml
NET=$(grep '^name:' "$CLAB_TOPO" | awk '{print $2}')
alias fcli="docker run -it --network $NET --rm \
  -p 8080:8080 \
  -v /etc/hosts:/etc/hosts:ro \
  -v ${PWD}/${CLAB_TOPO}:/topo.yml \
  ghcr.io/srl-labs/fcli:latest -t /topo.yml"

fcli server --listen 0.0.0.0
fcli bgp-peers
```

`--listen 0.0.0.0` is required inside Docker so the published port is reachable from the host. Running the container with no arguments drops into a shell.

If the container cannot reach the lab from Docker's default `docker0` bridge, either attach it to the containerlab network as above, or allow inter-network traffic (blocked by default on some hosts):

```bash
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

If the topology overrides the management network with `.mgmt.network`, use that name for `--network` instead of the lab name.

## GitHub Codespaces

Creating the Codespace hands the slow work to a background script, so the editor is usable immediately. [`.devcontainer/setup.sh`](../.devcontainer/setup.sh) narrates five steps into `/tmp/fcli-codespace-setup.log`:

1. wait for the Docker daemon
2. install fcli from the sources in the repo (`uv sync`)
3. deploy the [demo lab](../labs/demo/) — 8 SR Linux nodes and 9 servers
4. wait until every node answers gNMI, which is only once SR Linux has booted
5. start `fcli server` on port 8080

Step 3 is the long one: each SR Linux image is about 1 GB, so the first run usually takes **15–30 minutes**. Watch it work, or ask for a summary:

```bash
tail -f /tmp/fcli-codespace-setup.log
bash .devcontainer/status.sh
```

If the lab did not start at all, run it yourself — it is safe to repeat, and a second run does nothing while the first is still going:

```bash
bash .devcontainer/launch-setup.sh
```

Once step 5 is reached, open port 8080 in the **Ports** tab for the live web UI (Overview, Topology, BGP, EVPN services, …).

```bash
# CLI reports against the same fabric
fcli -t labs/demo/demo.clab.yaml bgp-peers
fcli -t labs/demo/demo.clab.yaml -i hostname=leaf1 mac
```

Optional: set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `XAI_API_KEY` as [Codespaces secrets](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces) to enable the **Ask** troubleshooting chat.

## Inventory

`fcli` needs a set of SR Linux nodes. Two ways to provide them, mutually exclusive:

| Flag | Source | Typical use |
| --- | --- | --- |
| `-t` / `--topo-file` | containerlab topology | labs |
| `-c` / `--cfg` | Nornir config file | hardware fabrics |

An inventory filter (`-i key=value`, repeatable, ANDed) scopes every surface to a subset of those nodes.

### Containerlab topology

```bash
fcli -t topo.clab.yml server
fcli -t topo.clab.yml -i role=leaf bgp-peers
```

Only nodes of kind `srl` / `nokia_srlinux` (or whose image is SR Linux) are inventoried. The topology `prefix` is applied to hostnames the same way containerlab does. Node `labels:` become host data and are the keys `-i` can filter on. The default gNMI port is 57400; `--gnmi-port` / `-p` sets another.

### Nornir config

```bash
fcli -c nornir_config.yaml server
fcli -c nornir_config.yaml -i role=leaf mac
```

If neither `-t` nor `-c` is given, `nornir_config.yaml` in the current directory is used. A typical layout:

```yaml
# nornir_config.yaml
inventory:
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

```yaml
# inventory/hosts.yaml
leaf1:
    hostname: 192.0.2.11
    groups: [srl, leafs]
spine1:
    hostname: 192.0.2.21
    groups: [srl, spines]
```

```yaml
# inventory/groups.yaml
srl:
    connection_options:
        srlinux:
            port: 57400
            username: admin
            password: NokiaSrl1!
            extras:
                path_cert: "./root-ca.pem"
spines:
    data:
        role: spine
leafs:
    data:
        role: leaf
```

The certificate is specified once for the `srl` group via `connection_options.srlinux.extras.path_cert`. Host `data:` keys are what `-i` filters on.

### gNMI TLS

gNMI is always TLS, but by default the certificate a node presents is trusted as it comes: nothing tells `fcli` that the thing answering on port 57400 is the node it asked for, so the fabric credentials it sends are only as safe as the path to the node. That is all a freshly deployed containerlab node can offer, so it stays the default for labs.

Point `--cert-file` at the CA that issued the node certificates, and they are verified against it:

```bash
fcli -c nornir_config.yaml --cert-file root-ca.pem bgp-peers
```

The same anchor can live in the inventory as `extras.path_cert`, as above. Two flags cover the rest:

| Flag | Effect |
| --- | --- |
| `--verify` / `--skip-verify` | Force verification on or off, whatever the inventory says |
| `--tls-server-name` | Name the certificate is checked against, when it is not the node's hostname |

`--tls-server-name` is what a mismatch needs: SR Linux issues its certificate to a name of its own, which is rarely the address the inventory reaches it on. For mutual TLS, add `path_key` and `path_root` to `extras` alongside `path_cert`.

