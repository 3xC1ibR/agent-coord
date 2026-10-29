#!/usr/bin/env python3
"""Relaunch a disposable native interface without stopping its private runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    resources = app / "Contents/Resources"
    executable = app / "Contents/MacOS/Agent Coord"
    config = json.loads((resources / "backend.json").read_text())
    python = Path(config["python"])
    if not python.is_absolute():
        python = resources / python
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    preferences = "com.agentcoord.desktop.smoke.restore." + uuid.uuid4().hex
    results = []
    with tempfile.TemporaryDirectory(prefix="ribbon-window-restore-") as temporary:
        directory = Path(temporary)
        database = directory / "state.sqlite3"
        common = [str(executable), "--smoke-directory", str(directory), "--smoke-preferences", preferences,
                  "--smoke-preserve-runtime", "--smoke-window-restoration"]
        try:
            for stage in ("seed", "verify", "close", "closed", "again", "lastclose", "empty"):
                output = report.parent / (stage + ".json")
                output.unlink(missing_ok=True)
                command = common + ["--smoke-test", str(output), "--smoke-restore-stage", stage]
                ready = directory / "ready"
                proceed = directory / "continue"
                for path in (ready, proceed):
                    path.unlink(missing_ok=True)
                if stage == "verify":
                    command += ["--smoke-restore-ready", str(ready), "--smoke-restore-continue", str(proceed)]
                with (report.parent / (stage + ".stdout.log")).open("w") as stdout, (report.parent / (stage + ".stderr.log")).open("w") as stderr:
                    with subprocess.Popen(command, stdout=stdout, stderr=stderr) as process:
                        try:
                            if stage == "verify":
                                duplicate_report = report.parent / "duplicate.json"
                                duplicate_report.unlink(missing_ok=True)
                                deadline = time.monotonic() + 85
                                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                                    time.sleep(.05)
                                if not ready.exists():
                                    if output.exists():
                                        failed = json.loads(output.read_text())
                                        if failed.get("error"):
                                            raise RuntimeError(f"Native stage {stage}: {failed['error']}")
                                    raise RuntimeError("Restored interface did not reach the duplicate-launch probe")
                                duplicate = subprocess.run(common + ["--smoke-test", str(duplicate_report),
                                    "--smoke-restore-stage", "again"], capture_output=True, text=True, timeout=20)
                                if duplicate.returncode:
                                    raise RuntimeError("Additional interface launch failed: " + duplicate.stderr[-2000:])
                                if duplicate_report.exists():
                                    raise RuntimeError("Additional launch ran a second native window-restoration stage")
                                proceed.touch()
                            process.wait(timeout=110)
                        except BaseException:
                            process.kill()
                            process.wait(timeout=10)
                            raise
                    if process.returncode or not output.exists():
                        raise RuntimeError(f"Native stage {stage} failed; see {stage}.stderr.log")
                current = json.loads(output.read_text())
                if current.get("error"):
                    raise RuntimeError(f"Native stage {stage}: {current['error']}")
                assertions = {key: value for key, value in current.items() if key.startswith("restore_") and isinstance(value, bool)}
                if not assertions or not all(assertions.values()):
                    raise RuntimeError(f"Native assertions failed at {stage}: {assertions}")
                os.kill(current["backend_pid"], 0)  # Observe only this freshly reported private runtime.
                saved = json.loads((directory / "windows/state.json").read_text())
                expected_count = 5 if stage in {"seed", "verify"} else 1 if stage in {"lastclose", "empty"} else 4
                if len(saved["windows"]) != expected_count:
                    raise RuntimeError(f"Quit-time window set at {stage} had {len(saved['windows'])}, expected {expected_count}")
                current["persisted_ids"] = [window["id"] for window in saved["windows"]]
                current["state_file_mode"] = oct((directory / "windows/state.json").stat().st_mode & 0o777)
                results.append(current)
            if len({(r["backend_pid"], r["url"]) for r in results}) != 1:
                raise RuntimeError("Interface relaunch replaced the independent runtime")
            if results[0]["persisted_ids"] != results[1]["persisted_ids"] or results[2]["persisted_ids"] != results[3]["persisted_ids"] or results[3]["persisted_ids"] != results[4]["persisted_ids"]:
                raise RuntimeError("Repeated launch duplicated or changed the saved window identities")
        finally:
            # Explicitly stop only the runtime created for this private database.
            # Never read the installed runtime's discovery or signal its PID.
            if (directory / ".state.sqlite3.runtime/discovery.json").exists():
                stopped = subprocess.run([str(python), "-B", str(resources / "backend.py"), "--db", str(database),
                    "--runtime-command", "stop"], capture_output=True, text=True, timeout=45,
                    env={**os.environ, "PYTHONPATH": str(resources / "backend"), "PYTHONNOUSERSITE": "1"})
                if stopped.returncode:
                    raise RuntimeError("Private test runtime cleanup failed: " + stopped.stderr[-2000:])
            subprocess.run(["defaults", "delete", preferences], capture_output=True, text=True, timeout=10)
    report.write_text(json.dumps({"passed": True, "stages": results, "same_runtime": True,
        "duplicate_launch": True, "private_runtime_cleaned_up": True, "user_runtime_touched": False}, indent=2) + "\n")
    print(str(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
