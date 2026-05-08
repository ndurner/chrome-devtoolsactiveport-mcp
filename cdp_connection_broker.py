#!/usr/bin/env python3
"""
Keep one approved upstream Chrome CDP connection alive and proxy it locally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Optional

import websockets
from websockets.connection import State as WsState

from resolve_devtools_active_port import resolve_devtools_active_port


class BrokerState:
    def __init__(self, upstream_ws_url: str, local_host: str, local_port: int):
        self.upstream_ws_url = upstream_ws_url
        self.local_host = local_host
        self.local_port = local_port
        self.active_downstream = None
        self.upstream = None
        self.stop_event = asyncio.Event()
        self.downstream_changed = asyncio.Event()
        self.status_file: Optional[Path] = None

    @property
    def local_ws_url(self) -> str:
        return f"ws://{self.local_host}:{self.local_port}"

    def status_payload(self) -> dict[str, object]:
        return {
            "pid": os.getpid(),
            "upstream_ws_url": self.upstream_ws_url,
            "local_ws_url": self.local_ws_url,
            "active_downstream": self.active_downstream is not None,
            "updated_at": int(time.time()),
        }

    def write_status(self) -> None:
        if self.status_file is None:
            return
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(
            json.dumps(self.status_payload(), indent=2),
            encoding="utf-8",
        )

    def clear_status(self) -> None:
        if self.status_file and self.status_file.exists():
            self.status_file.unlink()


async def handle_downstream(state: BrokerState, websocket):
    if state.active_downstream is not None:
        await websocket.close(code=1013, reason="Broker already has an active downstream client")
        return

    state.active_downstream = websocket
    state.downstream_changed.set()
    state.write_status()
    try:
        async for message in websocket:
            if state.upstream is None:
                await websocket.send(
                    json.dumps({"error": "Upstream Chrome connection is not ready"})
                )
                continue
            await state.upstream.send(message)
    finally:
        if state.active_downstream is websocket:
            state.active_downstream = None
            state.downstream_changed.set()
            state.write_status()


async def forward_upstream_to_downstream(state: BrokerState):
    while not state.stop_event.is_set():
        try:
            async with websockets.connect(
                state.upstream_ws_url,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            ) as upstream:
                state.upstream = upstream
                state.write_status()
                async for message in upstream:
                    downstream = state.active_downstream
                    if downstream is not None and downstream.state is WsState.OPEN:
                        await downstream.send(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            if state.stop_event.is_set():
                break
            await asyncio.sleep(1)
        finally:
            state.upstream = None
            state.write_status()


async def status_reporter(state: BrokerState):
    while not state.stop_event.is_set():
        state.write_status()
        try:
            await asyncio.wait_for(state.downstream_changed.wait(), timeout=5)
        except asyncio.TimeoutError:
            continue
        else:
            state.downstream_changed.clear()


async def run_broker(args) -> int:
    resolved = resolve_devtools_active_port(args.chrome_path)
    state = BrokerState(
        upstream_ws_url=str(resolved["ws_url"]),
        local_host=args.host,
        local_port=args.port,
    )
    if args.status_file:
        state.status_file = Path(args.status_file).expanduser().resolve()

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            try:
                loop.add_signal_handler(getattr(signal, signame), state.stop_event.set)
            except NotImplementedError:
                pass

    server = await websockets.serve(
        lambda websocket: handle_downstream(state, websocket),
        args.host,
        args.port,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
    )

    state.local_port = server.sockets[0].getsockname()[1]
    state.downstream_changed.set()
    state.write_status()
    print(json.dumps(state.status_payload(), indent=2), flush=True)

    tasks = [
        asyncio.create_task(forward_upstream_to_downstream(state)),
        asyncio.create_task(status_reporter(state)),
    ]

    try:
        await state.stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()
        state.clear_status()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Proxy a single persistent Chrome CDP connection through a local websocket."
    )
    parser.add_argument(
        "chrome_path",
        help="Path to a profile/user-data directory containing DevToolsActivePort, or the file itself.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local host to bind for downstream websocket clients.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port to bind for downstream websocket clients. Default: random free port.",
    )
    parser.add_argument(
        "--status-file",
        help="Optional JSON file updated with local and upstream websocket metadata.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run_broker(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
