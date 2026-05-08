#!/usr/bin/env python3
"""
MCP server for attaching to a local Chrome-family browser via DevToolsActivePort.

Exposes tools:

- start_broker / broker_status / stop_broker: persistent local broker lifecycle
  (profile auto-discovered; no prior find/resolve step needed)
- send_cdp: send any CDP command; returns ImageContent for screenshots,
  JSON text for everything else
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import websockets
from mcp.server.fastmcp.utilities.types import Image as _FastMCPImage

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))

from find_devtools_active_port import find_candidates  # noqa: E402
from resolve_devtools_active_port import (  # noqa: E402
    resolve_devtools_active_port as _resolve,
)


HERE = Path(__file__).resolve().parent
BROKER_SCRIPT = HERE / "cdp_connection_broker.py"
DEFAULT_STATUS_FILE = Path(tempfile.gettempdir()) / "chrome-cdp-broker.json"


mcp = FastMCP("chrome-devtoolsactiveport")

# CDP methods whose result["data"] is base64-encoded binary content.
# Value: callable(params) → MIME type string.
_BINARY_RESULT_METHODS: dict[str, Callable[[dict[str, Any]], str]] = {
    "Page.captureScreenshot": lambda p: "image/" + p.get("format", "png"),
    "Page.printToPDF":        lambda p: "application/pdf",
}

# Maps opaque UUID token → Path of a binary file handed out by send_cdp.
# Only tokens in this registry can be fetched via the resource accessor.
_binary_registry: dict[str, Path] = {}


@mcp.resource("cdp-binary://{token}")
def _serve_cdp_binary(token: str) -> bytes:
    """Serve binary CDP result data by its opaque token.

    Only tokens issued by send_cdp are accepted; all others raise an error so
    the accessor cannot be used to read arbitrary files.
    """
    path = _binary_registry.get(token)
    if path is None or not path.exists():
        raise ValueError(f"Unknown or expired CDP binary token: {token!r}")
    return path.read_bytes()


def find_devtools_active_port() -> list[dict[str, Any]]:
    """Internal helper: list DevToolsActivePort candidates sorted newest-first."""
    return find_candidates()


def resolve_devtools_active_port(path: str) -> dict[str, Any]:
    """Internal helper: read DevToolsActivePort and return the browser WS URL."""
    return _resolve(path)


@mcp.tool()
def start_broker(
    profile_dir: Optional[str] = None,
    status_file: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> dict[str, Any]:
    """Start the persistent CDP connection broker in the background.

    The broker opens one approved upstream websocket to Chrome and exposes a stable
    local websocket for downstream clients. Use this to avoid repeated Chrome
    confirmation dialogs on every new debugging connection.

    profile_dir: Chrome profile or user-data directory containing DevToolsActivePort.
        If omitted, the most recently active Chrome/Chromium/Edge profile is
        auto-discovered.  Pass explicitly only when multiple profiles are running
        and you need a specific one.
    status_file: path for the JSON status file. Defaults to
        %TEMP%/chrome-cdp-broker.json (or the platform tempdir equivalent).
    host: bind address for the local websocket. Default 127.0.0.1.
    port: bind port. 0 (default) picks a free port.

    If a broker is already running for this status file, returns its status
    instead of starting a new one.
    """
    status_path = _resolve_status_path(status_file)

    existing = _read_status_if_alive(status_path)
    if existing:
        return {"reused": True, "status_file": str(status_path), **existing}

    # Auto-discover profile_dir if not supplied.
    if not profile_dir:
        candidates = find_devtools_active_port()
        if not candidates:
            return {
                "error": (
                    "No active Chrome/Chromium/Edge profile found. "
                    "Start Chrome with --remote-debugging-port or supply profile_dir."
                )
            }
        profile_dir = candidates[0]["profile_dir"]

    # Remove any stale status file so the polling loop below only wakes on a fresh write.
    try:
        status_path.unlink()
    except FileNotFoundError:
        pass

    cmd = [
        sys.executable,
        str(BROKER_SCRIPT),
        profile_dir,
        "--host", host,
        "--port", str(port),
        "--status-file", str(status_path),
    ]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    popen_kwargs: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": str(HERE),
    }
    if sys.platform == "win32":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        popen_kwargs["creationflags"] = detached | new_group
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if data and data.get("pid"):
                return {"reused": False, "status_file": str(status_path), **data}
        if proc.poll() is not None:
            return {
                "error": "Broker exited before writing status file",
                "returncode": proc.returncode,
                "status_file": str(status_path),
            }
        time.sleep(0.1)

    return {
        "error": "Broker did not write status file within 10s",
        "pid": proc.pid,
        "status_file": str(status_path),
    }


@mcp.tool()
def broker_status(status_file: Optional[str] = None) -> dict[str, Any]:
    """Report broker status from the JSON status file.

    Returns pid, upstream_ws_url, local_ws_url, active_downstream, updated_at,
    plus a `running` boolean confirming the PID is alive.
    """
    status_path = _resolve_status_path(status_file)
    if not status_path.exists():
        return {"running": False, "status_file": str(status_path)}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "running": False,
            "status_file": str(status_path),
            "error": f"could not parse status file: {exc}",
        }
    data["running"] = _pid_alive(data.get("pid"))
    data["status_file"] = str(status_path)
    return data


@mcp.tool()
def stop_broker(status_file: Optional[str] = None) -> dict[str, Any]:
    """Stop the broker by terminating the PID recorded in its status file."""
    status_path = _resolve_status_path(status_file)
    if not status_path.exists():
        return {"stopped": False, "reason": "status file does not exist", "status_file": str(status_path)}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"stopped": False, "reason": f"could not parse status file: {exc}"}

    pid = data.get("pid")
    if not pid:
        return {"stopped": False, "reason": "no pid in status file"}

    if not _pid_alive(pid):
        try:
            status_path.unlink()
        except OSError:
            pass
        return {"stopped": True, "reason": "process was already gone", "pid": pid}

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception as exc:
        return {"stopped": False, "reason": f"kill failed: {exc}", "pid": pid}

    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)

    return {"stopped": not _pid_alive(pid), "pid": pid}


@mcp.tool()
async def send_cdp(
    method: str,
    params: Optional[dict[str, Any]] = None,
    target_index: Optional[int] = None,
    status_file: Optional[str] = None,
) -> Any:
    """Send any Chrome DevTools Protocol command via the CDP broker.

    Returns:
    - ImageContent  when the result contains image data (e.g. Page.captureScreenshot)
    - JSON text     for all other results

    method:       CDP method, e.g. "Target.getTargets" or "Page.captureScreenshot".
    params:       CDP params dict.  Omit or pass {} for commands with no parameters.
    target_index: Attach to this page tab before sending (0 = first/most recent).
                  Required for page-level methods (Page.*, Runtime.*, DOM.*, …).
                  Omit for browser-level methods (Target.getTargets, Browser.*, …).
                  Note: if a tab has been discarded by Chrome's memory manager,
                  Page.captureScreenshot will hang.  Call Target.activateTarget
                  first to restore it, then take the screenshot.
    status_file:  Path to broker JSON status file.  Uses default when omitted.

    Examples
    --------
    Browser-level (no target needed):
        send_cdp("Target.getTargets")
        send_cdp("Browser.getVersion")

    Page-level (target required):
        send_cdp("Runtime.evaluate", {"expression": "document.title"}, target_index=0)
        send_cdp("Page.captureScreenshot", {"format": "jpeg", "quality": 70}, target_index=0)
        send_cdp("Page.printToPDF", {}, target_index=0)
    """
    if params is None:
        params = {}

    status_path = _resolve_status_path(status_file)
    status = _read_status_if_alive(status_path)
    if not status:
        raise ValueError("Broker is not running. Call start_broker first.")

    local_ws_url: str = status["local_ws_url"]

    async with websockets.connect(local_ws_url, max_size=None) as ws:
        session_id: Optional[str] = None

        if target_index is not None:
            # ── 1. discover page targets ──────────────────────────────────
            await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
            resp = json.loads(await ws.recv())
            pages = [t for t in resp["result"]["targetInfos"] if t["type"] == "page"]
            if not pages:
                raise ValueError("No page targets found in Chrome.")
            page = pages[target_index % len(pages)]
            target_id = page["targetId"]

            # ── 2. attach to get a session ID ─────────────────────────────
            await ws.send(json.dumps({
                "id": 3,
                "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            }))
            while session_id is None:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 3:
                    session_id = msg["result"]["sessionId"]

        # ── 4. send the actual command ────────────────────────────────────
        cmd: dict[str, Any] = {"id": 99, "method": method, "params": params}
        if session_id:
            cmd["sessionId"] = session_id
        await ws.send(json.dumps(cmd))

        result_msg: Optional[dict[str, Any]] = None
        while result_msg is None:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 99:
                result_msg = msg

    if "error" in result_msg:
        raise ValueError(f"CDP error for {method!r}: {result_msg['error']}")

    result: dict[str, Any] = result_msg.get("result", {})

    # ── 5. smart return: image/binary vs plain JSON ───────────────────────
    if method in _BINARY_RESULT_METHODS and "data" in result:
        mime_type = _BINARY_RESULT_METHODS[method](params)
        raw = base64.b64decode(result["data"])

        if mime_type.startswith("image/"):
            # Return as ImageContent — multimodal clients render this as a
            # vision input, not as base64 text, so it doesn't pollute the
            # context window with characters.
            fmt = mime_type.split("/")[1]  # e.g. "jpeg", "png"
            return _FastMCPImage(data=raw, format=fmt)

        # Non-image binary (e.g. PDF): stash behind a token so the caller
        # can fetch it via resources/read without it appearing in context.
        suffix = "." + mime_type.split("/")[1]
        tmp = Path(tempfile.mktemp(suffix=suffix, prefix="cdp-binary-"))
        tmp.write_bytes(raw)
        token = str(uuid.uuid4())
        _binary_registry[token] = tmp
        from mcp.types import ResourceLink
        return ResourceLink(
            type="resource_link",
            uri=f"cdp-binary://{token}",  # type: ignore[arg-type]
            mimeType=mime_type,
            size=tmp.stat().st_size,
        )

    return result


def _resolve_status_path(status_file: Optional[str]) -> Path:
    if status_file:
        return Path(status_file).expanduser().resolve()
    return DEFAULT_STATUS_FILE


def _read_status_if_alive(status_path: Path) -> Optional[dict[str, Any]]:
    if not status_path.exists():
        return None
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if _pid_alive(data.get("pid")):
        return data
    return None


def _pid_alive(pid: Any) -> bool:
    if pid in (None, "", 0):
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid_int}", "/NH", "/FO", "CSV"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except Exception:
            return False
        return result.stdout is not None and f'"{pid_int}"' in result.stdout
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
