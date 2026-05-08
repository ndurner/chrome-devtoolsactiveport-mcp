#!/usr/bin/env python3
"""
Resolve a Chrome DevTools websocket URL from a DevToolsActivePort file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def resolve_devtools_active_port(path_arg: str) -> dict[str, object]:
    input_path = Path(path_arg).expanduser()
    file_path = input_path if input_path.name == "DevToolsActivePort" else input_path / "DevToolsActivePort"

    if not file_path.is_file():
        raise FileNotFoundError(f"DevToolsActivePort not found: {file_path}")

    lines = file_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise ValueError(f"Expected at least 2 lines in {file_path}, found {len(lines)}")

    port_text = lines[0].strip()
    path_text = lines[1].strip()
    if not port_text:
        raise ValueError(f"Missing port on line 1 of {file_path}")
    if not path_text:
        raise ValueError(f"Missing websocket path on line 2 of {file_path}")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"Invalid port '{port_text}' in {file_path}") from exc

    if port < 1 or port > 65535:
        raise ValueError(f"Port out of range in {file_path}: {port}")

    if not path_text.startswith("/"):
        path_text = f"/{path_text}"

    return {
        "input": str(input_path.resolve()),
        "devtools_active_port": str(file_path.resolve()),
        "port": port,
        "path": path_text,
        "ws_url": f"ws://127.0.0.1:{port}{path_text}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read DevToolsActivePort and emit the browser websocket URL."
    )
    parser.add_argument(
        "path",
        help="Path to a profile/user-data directory containing DevToolsActivePort, or the file itself.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON instead of key=value lines.",
    )
    args = parser.parse_args()

    try:
        result = resolve_devtools_active_port(args.path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for key in ("input", "devtools_active_port", "port", "path", "ws_url"):
            print(f"{key}={result[key]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
