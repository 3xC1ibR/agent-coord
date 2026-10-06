#!/usr/bin/env python3
"""Exercise the built native app with a temporary database and no model turns."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos/Agent Coord.app")
    parser.add_argument("--report", type=Path, default=ROOT / "build/macos/smoke.json")
    args = parser.parse_args()
    executable = args.app.expanduser().resolve() / "Contents/MacOS/Agent Coord"
    report = args.report.expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    with subprocess.Popen([str(executable), "--smoke-test", str(report)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as app:
        try:
            _, errors = app.communicate(timeout=50)
        except subprocess.TimeoutExpired:
            app.kill()
            _, errors = app.communicate(timeout=5)
            raise RuntimeError("Native smoke test timed out: " + errors[-4000:])
        if app.returncode != 0 or not report.exists():
            raise RuntimeError(f"Native app failed (exit {app.returncode}): {errors[-4000:]}")
    result = json.loads(report.read_text())
    if result.get("error"):
        raise RuntimeError(result["error"])
    for key in ("ui_loaded", "notification_bridge", "window_closed", "backend_survived_close",
                "window_reopened", "preferences_persisted"):
        if result.get(key) is not True:
            raise RuntimeError(f"Native behavior check failed: {key}")
    if not stopped(result["backend_pid"]):
        raise RuntimeError("The owned backend survived Quit")
    try:
        with urllib.request.urlopen(result["url"], timeout=1):
            raise RuntimeError("The backend port remained open after Quit")
    except OSError:
        pass
    result["backend_exited_on_quit"] = True
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"Screenshot: {report}.png")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
