#!/usr/bin/env python3
"""Build the Ribbon Field workspace document and content-hashed assets."""

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "plugins/agent-coord/scripts/agent_coord/web"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path, default=WEB)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--development", action="store_true", help="Include source maps for individual browser modules.")
    parser.add_argument("--watch", action="store_true", help="Rebuild affected browser modules after source changes.")
    args = parser.parse_args()
    if not (ROOT / "node_modules/esbuild").is_dir():
        raise SystemExit("Install the locked build tool with npm ci.")
    command = ["node", str(Path(__file__).with_suffix(".mjs")), str(args.web_root.resolve()),
               str((args.output or args.web_root / "dist").resolve())]
    if args.development:
        command.append("--development")
    if args.watch:
        command.append("--watch")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
