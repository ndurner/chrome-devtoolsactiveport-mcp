# Chrome DevToolsActivePort MCP Server

An [MCP](https://modelcontextprotocol.io) server that attaches MCP clients to an already running local Chrome, Chromium, or Edge session by reading `DevToolsActivePort` directly, and manages a persistent local CDP broker so Chrome does not prompt for approval on every new debugging connection.

This is the MCP equivalent of the [`chrome-devtoolsactiveport`](../chrome-devtoolsactiveport-skill) skill: same workflow, same scripts, exposed as MCP tools instead of as a Codex skill.

## What it does

- reads `DevToolsActivePort` from the selected Chrome-family profile
- constructs the browser websocket URL exactly as `ws://127.0.0.1:<port><path>` (no HTTP discovery via `/json/version`)
- searches only standard Chrome, Chromium, and Edge user-data roots to avoid broad recursive scans
- starts and supervises a persistent local broker that holds one approved upstream CDP socket and exposes a stable local websocket for the MCP client, so Chrome does not re-prompt for approval on every connection

## Tools

| Tool | Purpose |
| --- | --- |
| `find_devtools_active_port` | List `DevToolsActivePort` candidates under standard Chrome-family roots, newest first. |
| `resolve_devtools_active_port` | Read `DevToolsActivePort` for a given profile/user-data path and return `{ port, path, ws_url }`. |
| `start_broker` | Start the persistent local CDP broker in the background. Reuses an existing broker if the status file says one is alive. |
| `broker_status` | Return the current broker status from its JSON status file, including `local_ws_url` and `upstream_ws_url`. |
| `stop_broker` | Terminate the broker process recorded in the status file. |

The broker's `local_ws_url` is the websocket that downstream CDP clients should connect to. The MCP server itself does not speak CDP — it just resolves the endpoint and manages the broker.

## Install

Requires Python 3.10+.

```powershell
cd C:\dev\chrome-devtoolsactiveport-mcp
pip install -e .
```

This installs an entry point `chrome-devtoolsactiveport-mcp` that launches the server over stdio, plus the `mcp` and `websockets` dependencies.

## Configure an MCP client

Add an entry to your MCP client's server configuration. Example (Claude Desktop / Claude Code style):

```json
{
  "mcpServers": {
    "chrome-devtoolsactiveport": {
      "command": "python",
      "args": ["C:\\dev\\chrome-devtoolsactiveport-mcp\\server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Or, after `pip install -e .`:

```json
{
  "mcpServers": {
    "chrome-devtoolsactiveport": {
      "command": "chrome-devtoolsactiveport-mcp",
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

`PYTHONIOENCODING=utf-8` is recommended on Windows so the broker subprocess and any downstream Python tooling do not crash with `UnicodeEncodeError` on `cp1252` consoles.

## Typical usage

1. Launch Chrome with remote debugging enabled (e.g. `chrome.exe --remote-debugging-port=0 --user-data-dir="C:\path\to\chrome-profile"`).
2. From the MCP client, call `find_devtools_active_port` and pick the profile with the most recent `mtime`.
3. Call `start_broker` with that `profile_dir`. Approve the Chrome confirmation dialog once.
4. Read `local_ws_url` from `broker_status` and point any CDP tooling at it. Chrome will not prompt again as long as the broker stays alive.
5. Call `stop_broker` when the work is done.

If the environment does not prompt for approval, you can skip the broker and call `resolve_devtools_active_port` to get a direct `ws://127.0.0.1:<port><path>` URL.

## Layout

- [server.py](server.py): FastMCP server exposing the tools above
- [find_devtools_active_port.py](find_devtools_active_port.py): targeted profile discovery
- [resolve_devtools_active_port.py](resolve_devtools_active_port.py): deterministic websocket URL resolution
- [cdp_connection_broker.py](cdp_connection_broker.py): persistent local broker; spawned by `start_broker`
- [pyproject.toml](pyproject.toml): packaging and entry point

## Design notes

- Broker-first is the default. Direct websocket attach is an exception path, useful only when one long-lived connection can be reused for the whole task.
- `DevToolsActivePort` is read from disk; HTTP discovery endpoints like `/json/version` and `/json/list` are intentionally avoided.
- Profile discovery is scoped to standard Chrome, Chromium, and Edge roots. Broad recursive scans of `%LOCALAPPDATA%`, `%APPDATA%`, or `%USERPROFILE%` are intentionally not performed.
- The broker writes a JSON status file (default `%TEMP%\chrome-cdp-broker.json`). All broker tools accept an explicit `status_file` if you want to run multiple brokers.
