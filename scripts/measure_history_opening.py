#!/usr/bin/env python3
"""Serve normal history routes on an inert private APFS workload clone.

The source must be quiescent. Neither this process nor its providers can run
operator work. HTTP response contents stay in the private output directory.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch


def clone(source, destination):
    candidates = [source, Path(str(source) + "-wal")]
    def observations():
        return [(path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None for path in candidates]
    before = observations()
    for path, observed in zip(candidates, before):
        if observed is None:
            continue
        target = Path(str(destination) + str(path)[len(str(source)):])
        subprocess.run(["cp", "-c", str(path), str(target)], check=True)
        target.chmod(0o600)
    if before != observations():
        raise RuntimeError("Source changed during clone; discard destination and retry when quiescent")
    transcript = Path(str(source) + ".transcripts")
    if transcript.exists():
        Path(str(destination) + ".transcripts").symlink_to(transcript, target_is_directory=True)


class InertProvider:
    def __init__(self, *args, **kwargs):
        pass

    def close(self, *args, **kwargs):
        pass

    def request(self, method, *args, **kwargs):
        if method == "model/list":
            return {"data": []}
        raise RuntimeError("Provider execution is forbidden in history observation")


def quantiles(values):
    ordered = sorted(values)
    return {"n": len(values), **{key: ordered[round((len(ordered) - 1) * q)]
                               for key, q in (("p50", .5), ("p95", .95), ("max", 1))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--hold", type=int, default=0)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--keep-database", action="store_true")
    parser.add_argument("--check", action="store_true", help="Fail when raw HTTP p95 exceeds 5ms; browser qualification remains separate")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must include an initial observation and at least one warm observation")
    os.umask(0o077)
    args.output.mkdir(mode=0o700)
    database = args.output / "state.sqlite3"
    clone(args.snapshot.resolve(), database)
    for name in ("AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "AGENT_COORD_DB", "CLAUDECODE"):
        os.environ.pop(name, None)
    os.environ["AGENT_COORD_DB"] = str(database)
    sys.path.insert(0, str(args.source))
    from agent_coord.store import CoordinationStore
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord import ui, desktop_runtime
    try:
        from agent_coord.notification_broadcast import NotificationBroadcast
    except ImportError:
        NotificationBroadcast = None
    from agent_coord.remote_access import RemoteAccess
    from agent_coord.coordination_metrics import CoordinationMetrics
    from agent_coord.provider_limits import ProviderLimits

    def forbidden(*args, **kwargs):
        raise RuntimeError("Shared transcript mutation is forbidden in history observation")

    # Shared files are immutable; fail rather than silently diverge from that
    # contract if a read accidentally tries to persist a transcript.
    try:
        from agent_coord.transcript_store import TranscriptStore
    except ImportError:
        pass
    else:
        for name in ("save", "save_turns", "append_live", "migrate"):
            patch.object(TranscriptStore, name, forbidden).start()
    store = CoordinationStore(database)
    desktop_runtime._OWNED_DATABASES.add(str(database.resolve()))
    sessions = BrowserSessions(store, rpc_factory=InertProvider, claude_factory=InertProvider,
                               antigravity_factory=InertProvider, kimi_factory=InertProvider,
                               muse_factory=InertProvider)
    # Use the real handler and read methods, without make_ui_server's automatic
    # schedules, wakeups, provider subscriptions, dispatch, sidecars or connector.
    remote = RemoteAccess(database)
    metrics = CoordinationMetrics(database)
    limits = ProviderLimits(sessions, collectors={})
    # Match make_ui_server's foreground scheduling without starting its jobs.
    sys.setswitchinterval(min(sys.getswitchinterval(), .001))
    handler = ui._handler(
        store, None, None, sessions, "private-history-observation", remote,
        provider_limits=limits, coordination_metrics=metrics)
    path = "/api/browser/sessions/" + args.thread
    class ObservedHandler(handler):
        def _browser_get(self, parsed):
            if self.server.profile_request and parsed.path == path:
                self.server.profile_request = False
                import cProfile
                profile = cProfile.Profile()
                try:
                    return profile.runcall(super()._browser_get, parsed)
                finally:
                    profile.dump_stats(str(args.output / "snapshot.profile"))
            return super()._browser_get(parsed)
    server = ui.LoopbackHTTPServer(("127.0.0.1", 0), ObservedHandler)
    server.profile_request = False
    try:
        from agent_coord.snapshot_cache import prepare_snapshot_cache
    except ImportError:
        pass
    else:
        prepare_snapshot_cache(store)
    if NotificationBroadcast is not None:
        server.notification_broadcast = NotificationBroadcast(store, sessions)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    port = server.server_address[1]
    headers = {"X-Agent-Coord-History-View": "recent", "X-Agent-Coord-Thread-Controls": "1"}
    report = {"revision": args.revision, "source": str(args.source), "port": port,
              "thread_id": args.thread, "path": path, "headers": headers,
              "snapshot_bytes": args.snapshot.stat().st_size,
              "thread_switch_interval": sys.getswitchinterval(),
              "workers": "No automatic work; inert provider factories; immutable transcript write guard",
              "samples_ms": [], "started_at": time.time()}
    try:
        for index in range(args.samples):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            started = time.perf_counter()
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            connection.close()
            if response.status != 200:
                raise RuntimeError(f"Normal opening returned HTTP {response.status}")
            if index == 0:
                (args.output / "response.json").write_bytes(body)
                result = json.loads(body)
                history = result["thread"]
                report.update(first_ms=elapsed, response_bytes=len(body),
                    response_sha256=hashlib.sha256(body).hexdigest(),
                    visible_turns=len(history["turns"]),
                    visible_items=sum(len(turn.get("items", [])) for turn in history["turns"]),
                    earlier_turns=history.get("earlierTurns"), total_turns=history.get("totalTurns"))
            else:
                report["samples_ms"].append(elapsed)
        report["http_ms"] = quantiles(report["samples_ms"])
        report["http_budget_ms"] = 5
        report["http_budget_passed"] = report["http_ms"]["p95"] <= 5
        if getattr(store, "snapshot_cache", None) is not None:
            report["snapshot_cache"] = store.snapshot_cache.snapshot()
        if args.profile:
            server.profile_request = True
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                response.read()
                if response.status != 200:
                    raise RuntimeError(f"Profiled opening returned HTTP {response.status}")
            finally:
                connection.close()
            report["profile"] = "Normal HTTP handler; one additional untimed request"
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key != "samples_ms"}), flush=True)
        if args.hold:
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            stop.wait(args.hold)
    finally:
        server.shutdown()
        if NotificationBroadcast is not None:
            server.notification_broadcast.close()
        server.server_close()
        sessions.close()
        metrics.close()
        desktop_runtime._OWNED_DATABASES.discard(str(database.resolve()))
        worker.join(5)
        if not args.keep_database:
            for suffix in ("", "-wal", "-shm", ".transcripts"):
                path = Path(str(database) + suffix)
                if path.is_file() or path.is_symlink():
                    path.unlink()
    return 0 if not args.check or report["http_budget_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
