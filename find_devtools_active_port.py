#!/usr/bin/env python3
"""
Find likely DevToolsActivePort files under standard Chrome-family profile roots.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


WINDOWS_ROOTS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Chromium" / "User Data",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data",
]

MACOS_ROOTS = [
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    Path.home() / "Library" / "Application Support" / "Chromium",
    Path.home() / "Library" / "Application Support" / "Microsoft Edge",
]

LINUX_ROOTS = [
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "chromium",
    Path.home() / ".config" / "microsoft-edge",
]


def candidate_roots() -> list[Path]:
    roots = WINDOWS_ROOTS + MACOS_ROOTS + LINUX_ROOTS
    return [path for path in roots if str(path) and path.exists()]


def find_candidates() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for root in candidate_roots():
        for file_path in root.rglob("DevToolsActivePort"):
            try:
                stat = file_path.stat()
            except OSError:
                continue
            results.append(
                {
                    "file": str(file_path.resolve()),
                    "profile_dir": str(file_path.parent.resolve()),
                    "root": str(root.resolve()),
                    "mtime": stat.st_mtime,
                }
            )
    results.sort(key=lambda item: item["mtime"], reverse=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find likely DevToolsActivePort files under standard Chrome-family roots."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output.",
    )
    args = parser.parse_args()

    results = find_candidates()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for item in results:
            print(item["file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
