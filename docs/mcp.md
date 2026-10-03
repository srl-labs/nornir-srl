# MCP server

`fcli-mcp` exposes the CLI reports as [Model Context Protocol](https://modelcontextprotocol.io/) tools (`stdio` by default, or HTTP). An agent can query fabric state without wrapping `fcli` itself.

```bash
fcli-mcp --topo-file topo.clab.yml
fcli-mcp --config-file nornir_config.yaml
fcli-mcp --transport http --port 8080
```

It can start with no topology loaded. These tools then pick or switch the fabric at runtime:

- `list_topologies` — discover `*.clab.yml` and `nornir_config.yaml` files in a directory
- `load_topology` — initialize or switch from a containerlab file
- `load_config` — initialize or switch from a Nornir config file
- `show_topology` — nodes, labels, and the keys `inv_filter` can use

Report tools take the same `inv_filter` and `field_filter` as the CLI (comma-separated `key=value`).

For "what is wrong", `fabric_incidents` is the tool to start with: the checks' findings grouped by root cause. `mark_baseline` and `changes_since_baseline` bracket a change: mark before a maintenance or a config push, then ask what it did.

## Claude Desktop

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

## Gemini CLI

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

