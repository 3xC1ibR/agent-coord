#!/usr/bin/env python3
"""Serve the real web UI with seeded asks, for screenshots of the asks view and manual checks.

Uses a temporary database (or an explicit copy passed with --database) and
fake providers, so no agent process starts and no agent work is submitted.
Only ask resolution, snooze and the boot-time workspace probe accept POSTs.
Never point --database at a live coordination database: opening it migrates
its schema.

    python3 tests/asks_fixture_server.py --port 18791            # seeded asks
    python3 tests/asks_fixture_server.py --port 18791 --empty    # no asks
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from agent_coord.store import CoordinationStore, default_database_path
from agent_coord.ui import make_ui_server

HOUR, DAY = 3600.0, 86400.0
PERMITTED_POSTS = ("/api/browser/asks/", "/api/browser/workspaces/open")


def seed(store, clock, root):
    """Asks of every kind and state: blocking, overdue, stale, snoozed and resolved."""
    now = time.time()
    threads = {}
    for key, name, project, idle in (
            ("apify", "HoldCo product 2: Apify data actors", "HoldCo", 0),
            ("iris", "HoldCo operator: FIRE-to-IRIS product", "HoldCo", 0),
            ("crawler", "Search crawler benchmark", "Crawler", 0),
            ("saga", "Repeated-attempt meta-analysis", "Research", 12 * DAY),
            ("release", "Release 0.9 distribution", "Ribbon Field", 0)):
        directory = root / project
        directory.mkdir(exist_ok=True)
        clock[0] = now - max(idle, 14 * DAY)
        store.register(session_id=key, client="claude", cwd=str(directory), name=name)
        clock[0] = now - idle
        store.touch(key)
        threads[key] = key

    def file(thread, age, title, **fields):
        clock[0] = now - age
        fields.setdefault("blocking", False)
        if not fields["blocking"]:
            fields.setdefault("context", "Default: continue without it.")
        return store.asks.create(threads[thread], title, **fields)["id"]

    asks = store.asks
    file("apify", 9 * HOUR, "Add payout billing details in Apify", kind="action", blocking=True,
         context="Apify will not publish paid actors until a payout method is on file. Only the account owner can add one.",
         unblocks="Pricing for all 18 verified actors.", effort_minutes=10,
         link="https://console.apify.com/billing", project="HoldCo")
    file("iris", 3 * DAY, "Approve a $40 test budget for Google Ads", kind="approval",
         context="Default: hold the campaign. The landing page and three ad groups are ready.",
         unblocks="The first paid-search measurement for FIRE-to-IRIS.", effort_minutes=2,
         due_at=now - 6 * HOUR, project="HoldCo")
    file("iris", 14 * HOUR, "Upload a photo of your ID to Stripe for the 1099 Bridge account", kind="action",
         context="Default: keep the account in test mode. Stripe asks for this under Settings, Business details.",
         unblocks="Live payments for the first product.", effort_minutes=5,
         link="https://dashboard.stripe.com/settings/business-details", due_at=now + 5 * DAY, project="HoldCo")
    file("crawler", 5 * HOUR, "Provide a SerpAPI key", kind="request",
         context="Default: continue with the public endpoint at 10 requests per minute.",
         unblocks="A paid key removes the rate limit and cuts the crawl from 9 hours to 40 minutes.",
         effort_minutes=3, link="https://serpapi.com/manage-api-key")
    file("crawler", 2 * HOUR, "Which ranking should the benchmark report first?",
         options=["Recall at 10", "Mean reciprocal rank", "Both, side by side"],
         context="Default: recall at 10, the metric the earlier report used.")
    file("release", 26 * HOUR, "Try the new update dialog before it ships", kind="review",
         context="Default: ship on Thursday with the current copy.",
         unblocks="Confidence that Install and Skip for Now read correctly.", effort_minutes=5,
         link=str(root / "Ribbon Field" / "update-dialog-preview.html"))
    file("saga", 12 * DAY, "Veto or adjust the saga inclusion criteria before deep coding?",
         context="Default: proceed with the three criteria in the plan.")
    snoozed = file("release", 2 * DAY, "Choose the release notes headline",
                   options=["Faster search", "Queue for asks"], context="Default: Faster search.")
    clock[0] = now - HOUR
    asks.snooze(snoozed, now + 20 * HOUR)
    for thread, age, title, fields, resolved_after, resolution, extra in (
            ("apify", 4 * DAY, "Create the Apify account for HoldCo product 2",
             dict(kind="action", blocking=True, unblocks="Publishing any actor.", effort_minutes=15,
                  link="https://console.apify.com/sign-up"),
             3 * HOUR, "done", dict(note="Created under the HoldCo address. Two-factor is on.")),
            ("crawler", 3 * DAY, "Provide access to the click-log dataset",
             dict(kind="request", unblocks="Evaluation on real traffic instead of synthetic queries."),
             20 * HOUR, "granted", dict(note="Read access granted to the service account; it expires in 30 days.")),
            ("iris", 6 * DAY, "May agents email Lium support about the profiling host?",
             dict(kind="approval", unblocks="A repair request for the profiling host."),
             2 * HOUR, "declined", dict(note="I will write to them myself.")),
            ("release", 5 * DAY, "Read the pricing memo", dict(kind="review", unblocks="The pricing page copy."),
             30 * HOUR, "changes_requested", dict(note="Lead with the annual plan.")),
            ("iris", 8 * DAY, "How long should the book club block be?",
             dict(options=["30 minutes", "60 minutes", "90 minutes"]),
             40 * 60, "answered", dict(option="60 minutes")),
            ("saga", 20 * DAY, "Confirm the speaker test was audible", dict(kind="action", unblocks="The audio check."),
             9 * DAY, "dismissed", dict())):
        identifier = file(thread, age, title, **fields)
        clock[0] = now - age + resolved_after
        asks.resolve(identifier, resolution, **extra)
    clock[0] = None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--empty", action="store_true", help="Serve the view with no asks.")
    parser.add_argument("--database", type=Path, help="Serve an existing database copy instead of seeded asks.")
    args = parser.parse_args()
    if args.database and args.database.resolve() == default_database_path().resolve():
        parser.error("Refusing the live coordination database; pass a copy.")
    from test_browser_providers import claude_factory
    from test_codex_app_server import BrowserSessions, FakeCodex

    with tempfile.TemporaryDirectory(prefix="ribbon-asks-fixture-") as directory:
        root = Path(directory).resolve()
        clock = [None]
        store = CoordinationStore(args.database or root / "state.sqlite3",
                                  clock=lambda: time.time() if clock[0] is None else clock[0])
        if not args.database and not args.empty:
            seed(store, clock, root)
        sessions = BrowserSessions(store, str(root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        server = make_ui_server(store, port=args.port, cwd=str(root), browser_sessions=sessions)
        for component in (server.app_control, sessions.inbox_wake, sessions.sidecars):
            component.close()
        parent = server.RequestHandlerClass

        class FixtureHandler(parent):
            def do_POST(self):
                if not self.path.startswith(PERMITTED_POSTS):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "The asks fixture cannot submit agent work."})
                    return
                super().do_POST()

        server.RequestHandlerClass = FixtureHandler
        try:
            print(f"Asks fixture: http://127.0.0.1:{server.server_port}/asks", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            sessions.close()


if __name__ == "__main__":
    main()
