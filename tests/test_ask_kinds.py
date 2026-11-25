"""Asks stage 1: kinds and resolutions, migration, duplicate prevention, derived states, measurement and delivery."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord import asks as asks_module
from agent_coord.asks import AskStore, KINDS, RESOLUTIONS, initialize_schema, normalized_title, timestamp
from agent_coord.cli import _parser
from agent_coord.cli import run as cli_run
from agent_coord.store import CoordinationError, CoordinationStore

HOUR = 3600.0
LEGACY_SCHEMA = """CREATE TABLE asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    question TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    blocking INTEGER NOT NULL DEFAULT 0,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    answer_message_id INTEGER REFERENCES messages(id),
    supersedes INTEGER REFERENCES asks(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    answered_at REAL,
    withdrawn_at REAL,
    notification_claimed_at REAL
)"""


class AskFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1700000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        for name in ("asker", "peer"):
            self.store.register(session_id=name, client="claude", cwd=str(self.root), name=name.title())
        self.asks = self.store.asks

    def action(self, title="Add payout details in Apify", session="asker", **fields):
        fields.setdefault("blocking", True)
        fields.setdefault("unblocks", "Pricing for all 18 verified actors.")
        return self.asks.create(session, title, kind="action", **fields)

    def inbox(self, session="asker"):
        return self.store.inbox(session, mark_delivered=False)


class AskKindTests(AskFixture):
    def test_fields_are_validated_and_recorded(self):
        with self.assertRaisesRegex(CoordinationError, "kind must be one of"):
            self.asks.create("asker", "Which?", blocking=True, kind="question")
        for kind in ("action", "request"):
            with self.assertRaisesRegex(CoordinationError, f"this {kind} unblocks"):
                self.asks.create("asker", "Do it", blocking=True, kind=kind)
        with self.assertRaisesRegex(CoordinationError, "Options apply to decisions"):
            self.action(options=["A", "B"])
        for effort in (0, -5, 1.5, True, "10", 10081):
            with self.assertRaisesRegex(CoordinationError, "Effort"):
                self.action(effort_minutes=effort)
        for link in ("http://example.com", "example.com/billing", "javascript:alert(1)", "https://", "https://a b.com",
                     "relative/path", "/tmp/a\nb"):
            with self.assertRaisesRegex(CoordinationError, "Link"):
                self.action(link=link)
        with self.assertRaisesRegex(CoordinationError, "Due time"):
            self.action(due_at="next Tuesday")
        with self.assertRaisesRegex(CoordinationError, "Project"):
            self.action(project="p" * 161)
        ask = self.action(context=" Stripe needs it. ", effort_minutes=10, link="https://console.apify.com/billing",
                          due_at="2023-11-20T12:00:00Z", project=" HoldCo ", request_id=" apify-payout-1 ")
        self.assertEqual({key: ask[key] for key in ("kind", "question", "unblocks", "effort_minutes", "link", "due_at",
                                                    "project", "request_id", "status", "resolution", "note",
                                                    "resolved_at", "snoozed_until", "blocking", "options")},
                         {"kind": "action", "question": "Add payout details in Apify",
                          "unblocks": "Pricing for all 18 verified actors.", "effort_minutes": 10,
                          "link": "https://console.apify.com/billing", "due_at": "2023-11-20T12:00:00+00:00",
                          "project": "HoldCo", "request_id": "apify-payout-1", "status": "open", "resolution": None,
                          "note": None, "resolved_at": None, "snoozed_until": None, "blocking": True, "options": []})
        self.assertEqual(self.asks.create("asker", "Which?", blocking=True)["kind"], "decision")
        for link in ("agentcoord://thread/abc", "/Users/me/My Reports/result.html"):
            self.assertEqual(self.action(title="Open " + link, link=link)["link"], link)

    def test_timestamps_accept_unix_seconds_and_iso_text(self):
        self.assertEqual(timestamp(1700000000, "Due time"), 1700000000.0)
        self.assertEqual(timestamp("1700000000.5", "Due time"), 1700000000.5)
        self.assertEqual(timestamp("2023-11-14T22:13:20Z", "Due time"), 1700000000.0)
        self.assertEqual(timestamp("2023-11-14T23:13:20+01:00", "Due time"), 1700000000.0)
        self.assertIsNone(timestamp(None, "Due time"))
        local_day_end = timestamp("2023-11-14", "Due time")
        self.assertEqual(timestamp("2023-11-14T23:59:59", "Due time"), local_day_end)
        self.assertEqual(timestamp(0, "Since"), 0.0)
        for value in (True, "", "soon", float("nan"), float("inf"), -1):
            with self.assertRaises(CoordinationError, msg=repr(value)):
                timestamp(value, "Due time")

    def test_each_kind_accepts_its_resolutions_and_dismissal_only(self):
        every = {resolution for values in RESOLUTIONS.values() for resolution in values} | {"dismissed"}
        for kind in KINDS:
            fields = {"unblocks": "The next step."} if kind in ("action", "request") else {}
            allowed = (*RESOLUTIONS[kind], "dismissed")
            for resolution in sorted(every):
                with self.subTest(kind=kind, resolution=resolution):
                    ask = self.asks.create("asker", f"{kind} {resolution}", blocking=True, kind=kind, **fields)
                    self.assertEqual(self.asks.show(ask["id"])["resolutions"], list(allowed))
                    extra = {"answer": "Yes"} if resolution == "answered" else {}
                    if resolution not in allowed:
                        with self.assertRaisesRegex(CoordinationError, f"is an? {kind}; resolve it with"):
                            self.asks.resolve(ask["id"], resolution, **extra)
                        self.assertEqual(self.asks.get(ask["id"])["status"], "open")
                        self.asks.withdraw(ask["id"], "asker")
                        continue
                    resolved = self.asks.resolve(ask["id"], resolution, note="Because.", **extra)
                    self.assertEqual((resolved["status"], resolved["resolution"], resolved["note"]),
                                     ("resolved", resolution, "Because."))
                    self.assertEqual(resolved["resolved_at"], "2023-11-14T22:13:20+00:00")
                    self.assertEqual(resolved["answered_at"], resolved["resolved_at"] if resolution == "answered" else None)
                    with self.assertRaisesRegex(CoordinationError, "already resolved"):
                        self.asks.resolve(ask["id"], resolution, **extra)

    def test_resolution_message_states_kind_resolution_and_note(self):
        action = self.action(context="Stripe needs it.")
        with self.assertRaisesRegex(CoordinationError, "applies to an answered decision"):
            self.asks.resolve(action["id"], "done", answer="Finished")
        with self.assertRaisesRegex(CoordinationError, "Note must be at most"):
            self.asks.resolve(action["id"], "done", note="n" * 4001)
        self.assertEqual(self.inbox(), [])
        done = self.asks.resolve(action["id"], "done", note="  Saved the card ending 4242.  ")
        message = self.inbox()[0]
        self.assertEqual((message["id"], message["thread_id"], message["classification"], message["reply_required"]),
                         (done["answer_message_id"], f"ask:{action['id']}", "action_required", False))
        self.assertEqual(message["body"], "\n".join([
            f"The user resolved your blocking action ask #{action['id']}: done.", "",
            "Ask: Add payout details in Apify", "Your context: Stripe needs it.",
            "Unblocks: Pricing for all 18 verified actors.", "", "Resolution: done",
            "Note: Saved the card ending 4242.", "", "Verify the result before relying on it."]))
        request = self.asks.create("asker", "Provide a SerpAPI key", blocking=False, kind="request",
                                   context="Default: the public endpoint.", unblocks="Removes the rate limit.")
        self.asks.resolve(request["id"], "granted", note="Key is in 1Password under SerpAPI.")
        granted = self.inbox()[1]["body"]
        self.assertIn(f"The user resolved your non-blocking request ask #{request['id']}: granted.", granted)
        self.assertIn("Note: Key is in 1Password under SerpAPI.", granted)
        self.assertNotIn("Verify the result", granted)
        review = self.asks.create("asker", "Read the pricing memo", blocking=True, kind="review")
        self.asks.resolve(review["id"], "changes_requested")
        self.assertIn("Resolution: changes requested", self.inbox()[2]["body"])
        stale = self.asks.create("asker", "May I email support?", blocking=True, kind="approval")
        self.asks.resolve(stale["id"], "dismissed")
        dismissed = self.inbox()[3]["body"]
        self.assertIn(f"The user dismissed your blocking approval ask #{stale['id']} without acting on it.", dismissed)
        self.assertIn("Do not assume it happened.", dismissed)
        decision = self.asks.create("asker", "Which format?", blocking=True, options=["CSV", "JSON"])
        self.asks.resolve(decision["id"], "answered", option="CSV", note="JSON later.")
        answered = self.inbox()[4]["body"]
        self.assertIn("Answer: CSV", answered)
        self.assertIn("Note: JSON later.", answered)
        with self.assertRaisesRegex(CoordinationError, "exactly one"):
            self.asks.resolve(self.asks.create("asker", "Name?", blocking=True)["id"], "answered")

    def test_request_id_and_open_title_return_the_existing_ask(self):
        first = self.action(request_id="apify-payout-1")
        self.assertNotIn("existing", first)
        replayed = self.action(title="A different title", request_id="apify-payout-1")
        self.assertEqual((replayed["id"], replayed["existing"]), (first["id"], True))
        self.assertEqual(normalized_title("  Add   Payout details in APIFY?! "), normalized_title("add payout details in apify"))
        duplicate = self.action(title="  add   payout details in APIFY. ")
        self.assertEqual((duplicate["id"], duplicate["existing"]), (first["id"], True))
        other_session = self.action(session="peer", request_id="apify-payout-1")
        self.assertNotEqual(other_session["id"], first["id"])
        self.assertNotIn("existing", other_session)
        rewritten = self.action(context="Now with the exact menu path.", supersedes=first["id"])
        self.assertEqual((rewritten["supersedes"], self.asks.get(first["id"])["status"]), (first["id"], "withdrawn"))
        self.assertNotIn("existing", rewritten)
        self.asks.resolve(rewritten["id"], "done")
        again = self.action()
        self.assertNotIn(again["id"], (first["id"], rewritten["id"]))
        # A replayed request ID returns its ask whatever happened to it since.
        self.assertEqual(self.action(request_id="apify-payout-1")["status"], "withdrawn")
        self.assertEqual(len(self.asks.list(status="all", session_id="asker")), 3)
        with self.assertRaisesRegex(CoordinationError, "not yours"):
            self.action(title="Replacement", supersedes=other_session["id"])
        self.assertEqual(self.asks.get(other_session["id"])["status"], "open")

    def test_concurrent_request_id_is_unique_per_session(self):
        self.action(request_id="one")
        with self.assertRaises(sqlite3.IntegrityError), self.store._connection() as db:
            db.execute("""INSERT INTO asks (session_id, question, request_id, created_at, updated_at)
                VALUES ('asker', 'Raced', 'one', 1, 1)""")

    def test_snooze_hides_an_open_ask_until_its_time_without_changing_attention(self):
        ask = self.action()
        for until in (self.now, self.now - 1, "yesterday", True):
            with self.assertRaises(CoordinationError, msg=repr(until)):
                self.asks.snooze(ask["id"], until)
        snoozed = self.asks.snooze(ask["id"], self.now + 2 * HOUR)
        self.assertEqual(snoozed["snoozed_until"], "2023-11-15T00:13:20+00:00")
        self.assertTrue(self.asks.show(ask["id"])["snoozed"])
        self.assertEqual(self.inbox(), [])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(AskStore.open_blocking(db)["asker"]["id"], ask["id"])
        self.assertEqual([item["id"] for item in self.asks.list(status="open", blocking=True)], [ask["id"]])
        self.now += 2 * HOUR
        self.assertFalse(self.asks.show(ask["id"])["snoozed"])
        self.asks.snooze(ask["id"], self.now + HOUR)
        self.assertIsNone(self.asks.snooze(ask["id"], None)["snoozed_until"])
        self.asks.snooze(ask["id"], self.now + HOUR)
        resolved = self.asks.resolve(ask["id"], "not_done")
        self.assertIsNone(resolved["snoozed_until"])
        with self.assertRaisesRegex(CoordinationError, "already resolved"):
            self.asks.snooze(ask["id"], self.now + HOUR)
        with self.assertRaisesRegex(CoordinationError, "Unknown ask"):
            self.asks.snooze(999, self.now + HOUR)

    def test_stale_and_overdue_are_derived_from_thread_and_clock(self):
        quiet = self.asks.create("peer", "Tone?", blocking=False, context="Default: formal.",
                                 due_at=self.now + HOUR)
        ask = self.action(due_at=self.now + 80 * HOUR)
        fresh = {item["id"]: item for item in self.asks.overview()}
        self.assertEqual([(item["stale"], item["stale_reason"], item["overdue"], item["snoozed"], item["thread_closed"])
                          for item in fresh.values()], [(False, None, False, False, False)] * 2)
        self.now += 71 * HOUR
        self.store.touch("asker")
        self.now += 2 * HOUR
        states = {item["id"]: item for item in self.asks.overview()}
        self.assertEqual((states[quiet["id"]]["stale"], states[quiet["id"]]["stale_reason"], states[quiet["id"]]["overdue"]),
                         (True, "inactive", True))
        self.assertEqual(states[quiet["id"]]["idle_seconds"], 73 * HOUR)
        self.assertEqual((states[ask["id"]]["stale"], states[ask["id"]]["overdue"], states[ask["id"]]["age_seconds"]),
                         (False, False, 73 * HOUR))
        self.store.threads.update("asker", attention="archived")
        closed = self.asks.show(ask["id"])
        self.assertEqual((closed["stale"], closed["stale_reason"], closed["thread_closed"]), (True, "thread_closed", True))
        self.now += 8 * HOUR
        self.assertTrue(self.asks.show(ask["id"])["overdue"])
        # Derived states describe open asks; a resolved ask is neither stale nor overdue.
        self.asks.resolve(ask["id"], "done")
        resolved = self.asks.show(ask["id"])
        self.assertEqual((resolved["stale"], resolved["overdue"], resolved["snoozed"], resolved["thread_closed"]),
                         (False, False, False, True))

    def test_overview_selects_by_status_and_period_with_display_fields(self):
        early = self.asks.create("peer", "Tone?", blocking=False, context="Default: formal.")
        self.now += 60
        withdrawn = self.action(title="Withdrawn later")
        self.asks.withdraw(withdrawn["id"], "asker")
        self.now += 60
        blocking = self.action(project="HoldCo")
        self.now += 60
        resolved = self.asks.create("asker", "Which format?", blocking=True, options=["CSV", "JSON"])
        self.asks.resolve(resolved["id"], "answered", option="CSV")
        ids = lambda **selection: [item["id"] for item in self.asks.overview(**selection)]
        self.assertEqual(ids(), [blocking["id"], early["id"]])
        self.assertEqual(ids(status="open"), ids())
        self.assertEqual(ids(status="resolved"), [resolved["id"]])
        self.assertEqual(ids(status="withdrawn"), [withdrawn["id"]])
        self.assertEqual(ids(status="all"), [resolved["id"], blocking["id"], withdrawn["id"], early["id"]])
        self.assertEqual(ids(status="all", since=self.now - 60), [resolved["id"], blocking["id"]])
        self.assertEqual(ids(status="all", session_id="peer"), [early["id"]])
        with self.assertRaisesRegex(CoordinationError, "Ask status"):
            self.asks.overview(status="closed")
        item = self.asks.show(blocking["id"])
        self.assertEqual((item["thread_title"], item["project_label"], item["cwd"], item["client"], item["kind"]),
                         ("Asker", "HoldCo", str(self.root), "claude", "action"))
        self.assertEqual(self.asks.show(early["id"])["project_label"], self.asks.show(early["id"])["project_name"])
        with self.assertRaisesRegex(CoordinationError, "Unknown ask"):
            self.asks.show(999)


class AskMigrationTests(AskFixture):
    def legacy_database(self):
        with self.store._connection() as db:
            db.execute("DROP TABLE asks")
            db.execute(LEGACY_SCHEMA)
            db.execute("CREATE INDEX asks_open_idx ON asks(status, blocking, created_at)")
            db.execute("CREATE INDEX asks_session_idx ON asks(session_id, status)")
            db.executemany("""INSERT INTO asks (id, session_id, question, options_json, blocking, context, status, answer,
                supersedes, created_at, updated_at, answered_at, withdrawn_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
                (1, "asker", "How long?", '["30m", "60m"]', 1, None, "answered", "60m", None, 100.0, 400.0, 400.0, None),
                (2, "asker", "Open Stripe", "[]", 0, "Default: wait.", "withdrawn", None, None, 200.0, 300.0, None, 300.0),
                (3, "asker", "Upload ID photo", "[]", 0, "Default: wait.", "open", None, 2, 300.0, 300.0, None, None),
            ])

    def test_a_reply_is_recorded_on_an_open_ask_without_resolving_it(self):
        ask = self.action()
        self.assertEqual((ask["reply_text"], ask["replied_at"]), (None, None))
        for text, message in ((None, "must not be empty"), ("  ", "must not be empty"), (7, "must be text"),
                              ("x" * 4001, "4000")):
            with self.assertRaisesRegex(CoordinationError, message, msg=repr(text)[:20]):
                self.asks.reply(ask["id"], text)
        replied = self.asks.reply(ask["id"], "  which payout method?  ")
        self.assertEqual((replied["status"], replied["reply_text"], replied["replied_at"]),
                         ("open", "which payout method?", "2023-11-14T22:13:20+00:00"))
        # The reply travels as the user's own message; recording it sends nothing and changes no other state.
        self.assertEqual(self.inbox(), [])
        self.assertEqual([item["id"] for item in self.asks.list(status="open", blocking=True)], [ask["id"]])
        shown = self.asks.overview()[0]
        self.assertEqual((shown["reply_text"], shown["replied_at"], shown["snoozed"]),
                         ("which payout method?", "2023-11-14T22:13:20+00:00", False))
        self.now += HOUR
        self.assertEqual(self.asks.reply(ask["id"], "and the login?")["replied_at"], "2023-11-14T23:13:20+00:00")
        # A resolution keeps the last reply beside it.
        resolved = self.asks.resolve(ask["id"], "done", note="Card saved.")
        self.assertEqual((resolved["status"], resolved["note"], resolved["reply_text"]), ("resolved", "Card saved.", "and the login?"))
        with self.assertRaisesRegex(CoordinationError, "already resolved"):
            self.asks.reply(ask["id"], "too late")
        with self.assertRaisesRegex(CoordinationError, "Unknown ask"):
            self.asks.reply(999, "nobody")

    def test_a_reader_accepts_a_database_from_before_reply_records(self):
        ask = self.action()
        with self.store._connection() as db:
            for column in ("reply_text", "replied_at"):
                db.execute(f"ALTER TABLE asks DROP COLUMN {column}")
        reader = AskStore(self.store, initialize=False)
        record = reader.get(ask["id"])
        self.assertEqual((record["status"], record["reply_text"], record["replied_at"]), ("open", None, None))
        self.assertEqual(reader.overview()[0]["reply_text"], None)
        # Opening the store for writing adds the columns back without touching the rows.
        with self.store._connection() as db:
            initialize_schema(db)
        self.assertEqual(self.asks.reply(ask["id"], "now recorded")["reply_text"], "now recorded")

    def test_existing_rows_become_decisions_and_answered_becomes_resolved(self):
        self.legacy_database()
        for _ in range(2):  # The migration is idempotent.
            with self.store._connection() as db:
                initialize_schema(db)
        answered, withdrawn, still_open = (self.asks.get(ask_id) for ask_id in (1, 2, 3))
        self.assertEqual((answered["kind"], answered["status"], answered["resolution"], answered["answer"]),
                         ("decision", "resolved", "answered", "60m"))
        self.assertEqual(answered["resolved_at"], "1970-01-01T00:06:40+00:00")
        self.assertEqual(answered["answered_at"], answered["resolved_at"])
        self.assertEqual((withdrawn["kind"], withdrawn["status"], withdrawn["resolution"]), ("decision", "withdrawn", None))
        self.assertEqual((still_open["kind"], still_open["status"], still_open["supersedes"]), ("decision", "open", 2))
        self.assertEqual((still_open["reply_text"], still_open["replied_at"]), (None, None))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status, resolution, resolved_at FROM asks WHERE id = 1").fetchone()[:],
                             ("resolved", "answered", 400.0))
        self.assertEqual([ask["id"] for ask in self.asks.overview()], [3])
        self.assertEqual(self.asks.resolve(3, "answered", answer="Uploaded")["status"], "resolved")
        self.assertEqual(self.asks.metrics()["asks"]["resolved"], 2)

    def test_readers_accept_a_legacy_answered_row(self):
        ask = self.asks.create("asker", "Which?", blocking=True)
        with self.store._connection() as db:
            db.execute("UPDATE asks SET status = 'answered', answer = 'A', answered_at = ? WHERE id = ?",
                       (self.now + 60, ask["id"]))
        record = self.asks.get(ask["id"])
        self.assertEqual((record["status"], record["resolution"], record["answer"]), ("resolved", "answered", "A"))
        self.assertEqual(record["resolved_at"], "2023-11-14T22:14:20+00:00")
        self.assertEqual([item["id"] for item in self.asks.overview(status="resolved")], [ask["id"]])
        self.assertEqual([item["id"] for item in self.asks.list(status="resolved")], [ask["id"]])
        with self.assertRaisesRegex(CoordinationError, "already resolved"):
            self.asks.resolve(ask["id"], "answered", answer="B")
        with self.assertRaisesRegex(CoordinationError, "already resolved"):
            self.asks.withdraw(ask["id"], "asker")
        metrics = self.asks.metrics()
        self.assertEqual((metrics["asks"]["resolved"], metrics["time_to_resolution"]["decision"]["median_seconds"]), (1, 60.0))


class AskMetricsTests(AskFixture):
    def test_metrics_report_latency_closure_rewrites_requests_blocked_time_and_stale_asks(self):
        empty = self.asks.metrics()
        self.assertEqual((empty["asks"]["total"], empty["closure"]["resolved_share"], empty["blocked_hours"],
                          empty["time_to_resolution"]["all"], empty["supersede_chains"]["asks_per_need"],
                          empty["requests"]["granted_share"], empty["stale"]),
                         (0, None, 0.0, {"count": 0, "median_seconds": None, "p90_seconds": None}, None, None,
                          {"count": 0, "ask_ids": []}))
        start = self.now
        # Three actions resolved after 1, 2 and 10 hours; the last was rewritten twice first.
        first = self.action(title="Action one")
        second = self.action(title="Action two", blocking=False, context="Default: wait.")
        draft = self.action(title="Action three, first wording", blocking=False, context="Default: wait.")
        self.now = start + 1 * HOUR
        self.asks.resolve(first["id"], "done")
        revised = self.action(title="Action three, second wording", blocking=False, context="Default: wait.",
                              supersedes=draft["id"])
        self.now = start + 2 * HOUR
        self.asks.resolve(second["id"], "not_done")
        final = self.action(title="Action three, final wording", blocking=False, context="Default: wait.",
                            supersedes=revised["id"])
        request_granted = self.asks.create("asker", "Provide a SerpAPI key", blocking=False, kind="request",
                                           context="Default: public endpoint.", unblocks="Removes the rate limit.")
        request_declined = self.asks.create("asker", "Provide a larger GPU budget", blocking=False, kind="request",
                                            context="Default: one GPU.", unblocks="Halves the wall time.")
        request_open = self.asks.create("peer", "Provide dataset access", blocking=True, kind="request",
                                        unblocks="Evaluation on real traffic.")
        self.asks.resolve(request_granted["id"], "granted")
        self.asks.resolve(request_declined["id"], "declined")
        abandoned = self.asks.create("asker", "Which tone?", blocking=True)
        self.now = start + 5 * HOUR
        self.asks.withdraw(abandoned["id"], "asker")
        self.now = start + 12 * HOUR
        self.asks.resolve(final["id"], "dismissed")
        metrics = self.asks.metrics()
        self.assertEqual(metrics["period"], {"since": None, "as_of": "2023-11-15T10:13:20+00:00"})
        self.assertEqual(metrics["asks"], {"total": 9, "open": 1, "resolved": 5, "withdrawn": 3,
                                           "by_kind": {"decision": 1, "action": 5, "approval": 0, "review": 0, "request": 3}})
        self.assertEqual(metrics["time_to_resolution"]["action"],
                         {"count": 3, "median_seconds": 2 * HOUR, "p90_seconds": 10 * HOUR})
        self.assertEqual(metrics["time_to_resolution"]["request"], {"count": 2, "median_seconds": 0.0, "p90_seconds": 0.0})
        self.assertEqual(metrics["time_to_resolution"]["decision"], {"count": 0, "median_seconds": None, "p90_seconds": None})
        self.assertEqual(metrics["time_to_resolution"]["all"], {"count": 5, "median_seconds": HOUR, "p90_seconds": 10 * HOUR})
        self.assertEqual(metrics["closure"], {
            "closed": 8, "resolved_by_user": 5, "withdrawn_by_agent": 3, "withdrawn_superseded": 2,
            "withdrawn_without_successor": 1, "resolved_share": 0.625, "withdrawn_share": 0.375,
            "resolutions": {"declined": 1, "dismissed": 1, "done": 1, "granted": 1, "not_done": 1}})
        self.assertEqual(metrics["supersede_chains"], {"distinct_needs": 7, "asks_per_need": 1.29,
                                                       "rewritten_needs": 1, "longest_chain": 3})
        self.assertEqual(metrics["requests"], {"filed": 3, "open": 1, "granted": 1, "declined": 1, "dismissed": 0,
                                               "withdrawn": 0, "granted_share": 0.333})
        # Blocking: action one for 1 h, the abandoned decision for 3 h, the open request for 10 h.
        self.assertEqual(metrics["blocked_hours"], 14.0)
        self.assertEqual(metrics["stale"], {"count": 0, "ask_ids": []})
        self.now = start + 80 * HOUR
        self.assertEqual(self.asks.metrics()["stale"], {"count": 1, "ask_ids": [request_open["id"]]})
        recent = self.asks.metrics(since=start + 2 * HOUR)
        self.assertEqual((recent["asks"]["total"], recent["period"]["since"]), (5, "2023-11-15T00:13:20+00:00"))
        # The period's first ask supersedes one outside it, so it starts its own need.
        self.assertEqual(recent["supersede_chains"], {"distinct_needs": 5, "asks_per_need": 1.0,
                                                      "rewritten_needs": 0, "longest_chain": 1})
        self.assertEqual(recent["stale"]["count"], 1)


class AskCliTests(AskFixture):
    def run_cli(self, *argv):
        # The fixture store carries the fixed clock that derived states are computed against.
        return cli_run(_parser().parse_args(["--db", str(self.store.database_path), *argv]), store=self.store)

    def test_cli_files_every_field_shows_lists_and_measures(self):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "asker"}, clear=False):
            with self.assertRaisesRegex(CoordinationError, "this action unblocks"):
                self.run_cli("ask", "--kind", "action", "--blocking", "Add payout billing details in Apify")
            created = self.run_cli(
                "ask", "--kind", "action", "--blocking", "--unblocks", "Pricing for all 18 verified actors.",
                "--effort", "10", "--link", "https://console.apify.com/billing", "--project", "HoldCo",
                "--due", "2023-12-01T00:00:00Z", "--request-id", "apify-payout-1", "Add payout billing details in Apify")
            self.assertEqual((created["kind"], created["unblocks"], created["effort_minutes"], created["link"],
                              created["project"], created["request_id"], created["due_at"], created["blocking"]),
                             ("action", "Pricing for all 18 verified actors.", 10, "https://console.apify.com/billing",
                              "HoldCo", "apify-payout-1", "2023-12-01T00:00:00+00:00", True))
            replayed = self.run_cli("ask", "--kind", "action", "--blocking", "--unblocks", "Pricing.",
                                    "--request-id", "apify-payout-1", "Add payout billing details")
            self.assertEqual((replayed["id"], replayed["existing"]), (created["id"], True))
            request = self.run_cli("ask", "--kind", "request", "--non-blocking",
                                   "--context", "Default: continue with the public endpoint at 10 requests per minute.",
                                   "--unblocks", "A paid key cuts the crawl from 9 hours to 40 minutes.",
                                   "Provide a SerpAPI key")
            self.assertEqual((request["kind"], request["blocking"]), ("request", False))
            shown = self.run_cli("ask", "--show", str(created["id"]))
            self.assertEqual((shown["id"], shown["thread_title"], shown["project_label"], shown["stale"],
                              shown["resolutions"]),
                             (created["id"], "Asker", "HoldCo", False, ["done", "not_done", "dismissed"]))
            self.store.asks.create("peer", "Peer question?", blocking=True)
            self.assertEqual([item["id"] for item in self.run_cli("ask", "--list")], [created["id"], request["id"]])
            self.assertEqual(len(self.run_cli("ask", "--list", "--all-sessions")), 3)
            self.store.asks.resolve(created["id"], "done")
            self.assertEqual([item["id"] for item in self.run_cli("ask", "--list", "--status", "resolved")], [created["id"]])
            self.assertEqual(len(self.run_cli("ask", "--list", "--all-sessions", "--status", "all")), 3)
            self.assertEqual(self.run_cli("ask", "--list", "--all-sessions", "--status", "all",
                                          "--since", str(self.now + 1)), [])
            metrics = self.run_cli("ask", "--metrics")
            self.assertEqual((metrics["asks"]["total"], metrics["closure"]["resolutions"]), (3, {"done": 1}))
            self.assertEqual(self.run_cli("ask", "--metrics", "--since", str(self.now + 1))["asks"]["total"], 0)
            for argv, message in (
                    (("--show", "1", "--list"), "exactly one"),
                    (("--metrics", "Which?"), "exactly one"),
                    (("--list", "--kind", "action"), "--list does not accept --kind"),
                    (("--withdraw", "1", "--unblocks", "x"), "--withdraw does not accept --unblocks"),
                    (("--metrics", "--blocking"), "--metrics does not accept --blocking"),
                    (("--show", "1", "--all-sessions"), "--all-sessions applies to --list"),
                    (("--metrics", "--status", "open"), "--status applies to --list"),
                    (("--show", "1", "--since", "5"), "--since applies to"),
                    (("--list", "--since", "soon"), "Since must be"),
                    (("--show", "999"), "Unknown ask")):
                with self.assertRaisesRegex(CoordinationError, message, msg=argv):
                    self.run_cli("ask", *argv)

    def test_read_only_inspection_needs_no_caller_session(self):
        ask = self.action()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.run_cli("ask", "--metrics")["asks"]["total"], 1)
            self.assertEqual(self.run_cli("ask", "--show", str(ask["id"]))["id"], ask["id"])
            self.assertEqual([item["id"] for item in self.run_cli("ask", "--list", "--all-sessions")], [ask["id"]])
            for argv in (("--list",), ("--withdraw", str(ask["id"])), ("--blocking", "Which?")):
                with self.assertRaisesRegex(CoordinationError, "No caller session", msg=argv):
                    self.run_cli("ask", *argv)
        self.assertEqual(self.asks.get(ask["id"])["status"], "open")


class AskBrowserTests(unittest.TestCase):
    def setUp(self):
        from test_browser_wake import claude_factory
        from test_codex_app_server import BrowserSessions, FakeCodex
        from agent_coord.ui import make_ui_server
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        for component in (self.server.app_control, self.sessions.inbox_wake, self.sessions.sidecars):
            component.close()
        # The background wake worker has ended; each test delivers with an explicit process_once().
        self.sessions.inbox_wake.stopped.clear()
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.token = self.get("/api/browser/config")["token"]

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Agent-Coord-Token": self.token})
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def rejected(self, path, body=None):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get(path) if body is None else self.post(path, body)
        self.addCleanup(failure.exception.close)
        return failure.exception.code, json.load(failure.exception)["error"]

    def app_thread(self, client="codex"):
        thread_id = self.sessions.create({"client": client})["session"]["thread_id"]
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))
        return thread_id

    def run_cli(self, session, *argv):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": session}, clear=False):
            return cli_run(_parser().parse_args(["--db", str(self.store.database_path), *argv]), store=self.store)

    def turns(self, thread_id):
        return self.sessions.read(thread_id)["thread"]["turns"]

    def test_browser_resolves_and_snoozes_asks_and_lists_by_status(self):
        thread = self.app_thread()
        action = self.run_cli(thread, "ask", "--kind", "action", "--blocking", "--unblocks", "Pricing.",
                              "--link", "https://console.apify.com/billing", "Add payout billing details in Apify")
        decision = self.run_cli(thread, "ask", "--non-blocking", "--context", "Default: CSV.", "--option", "CSV",
                                "--option", "JSON", "Which format?")
        listed = self.get("/api/browser/asks")["data"]
        self.assertEqual([item["id"] for item in listed], [action["id"], decision["id"]])
        self.assertEqual((listed[0]["kind"], listed[0]["resolutions"], listed[0]["stale"], listed[0]["overdue"],
                          listed[0]["snoozed"], listed[0]["link"]),
                         ("action", ["done", "not_done", "dismissed"], False, False, False,
                          "https://console.apify.com/billing"))
        self.assertEqual(self.get("/api/browser/asks?status=open")["data"], listed)
        for path, body, message in (
                (f"/api/browser/asks/{action['id']}", {"resolution": "granted"}, "is an action; resolve it with"),
                (f"/api/browser/asks/{action['id']}", {"answer": "Finished"}, "is an action; resolve it with"),
                (f"/api/browser/asks/{action['id']}", {"resolution": "done", "answer": "x"}, "answered decision"),
                (f"/api/browser/asks/{action['id']}", {"resolution": "done", "until": 5}, "Resolve an ask with"),
                (f"/api/browser/asks/{action['id']}", {"note": "x"}, "either option or answer"),
                (f"/api/browser/asks/{action['id']}/snooze", {"until": 5}, "must be in the future"),
                (f"/api/browser/asks/{action['id']}/snooze", {}, "Snooze an ask with until"),
                (f"/api/browser/asks/{action['id']}/unknown", {"until": 5}, "Choose an ask"),
                ("/api/browser/asks/nope", {"resolution": "done"}, "positive integer"),
                ("/api/browser/asks?status=closed", None, "Ask status"),
                ("/api/browser/asks?since=soon", None, "Since must be")):
            code, error = self.rejected(path, body)
            self.assertEqual(code, 400, path)
            self.assertIn(message, error)
        snoozed = self.post(f"/api/browser/asks/{decision['id']}/snooze", {"until": "2999-01-01T00:00:00Z"})
        self.assertEqual(snoozed["snoozed_until"], "2999-01-01T00:00:00+00:00")
        self.assertEqual([item["snoozed"] for item in self.get("/api/browser/asks")["data"]], [False, True])
        self.assertIsNone(self.post(f"/api/browser/asks/{decision['id']}/snooze", {"until": None})["snoozed_until"])
        done = self.post(f"/api/browser/asks/{action['id']}", {"resolution": "done", "note": "Card saved."})
        self.assertEqual((done["status"], done["resolution"], done["note"]), ("resolved", "done", "Card saved."))
        answered = self.post(f"/api/browser/asks/{decision['id']}", {"resolution": "answered", "option": "JSON"})
        self.assertEqual((answered["resolution"], answered["answer"]), ("answered", "JSON"))
        self.assertEqual(self.get("/api/browser/asks")["data"], [])
        self.assertEqual([item["id"] for item in self.get("/api/browser/asks?status=resolved")["data"]],
                         [decision["id"], action["id"]])
        self.assertEqual(len(self.get("/api/browser/asks?status=all&since=0")["data"]), 2)
        self.assertEqual(self.get("/api/browser/asks?status=all&since=2999-01-01")["data"], [])

    def test_listings_carry_what_a_conversation_needs_to_show_its_own_asks(self):
        # The strip above a conversation's composer selects that thread's asks from these listings in the page.
        mine, other = self.app_thread(), self.app_thread()
        waiting = self.run_cli(mine, "ask", "--kind", "request", "--non-blocking", "--context", "Default: the public endpoint.",
                               "--unblocks", "No rate limit.", "Provide a SerpAPI key")
        settled = self.run_cli(mine, "ask", "--kind", "approval", "--non-blocking", "--context", "Default: hold.",
                               "Approve a $40 test budget")
        self.run_cli(other, "ask", "--non-blocking", "--context", "Default: CSV.", "Which format?")
        resolution = self.post(f"/api/browser/asks/{settled['id']}", {"resolution": "approved", "note": "For one week."})
        self.assertEqual(resolution["session_id"], mine)
        self.assertTrue(resolution["resolved_at"])
        open_asks = self.get("/api/browser/asks")["data"]
        self.assertEqual([item["id"] for item in open_asks if item["session_id"] == mine], [waiting["id"]])
        self.assertEqual({item["session_id"] for item in open_asks}, {mine, other})
        log = [item for item in self.get("/api/browser/asks?status=resolved")["data"] if item["session_id"] == mine]
        self.assertEqual([(item["id"], item["resolution"], item["note"], item["kind"]) for item in log],
                         [(settled["id"], "approved", "For one week.", "approval")])
        self.assertEqual(log[0]["resolved_at"], resolution["resolved_at"])

    def test_a_reply_reaches_the_conversation_as_the_users_message_and_marks_the_open_ask(self):
        self.assertIs(self.get("/api/browser/config")["askReplies"], True)
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.app_thread(client)
                ask = self.run_cli(thread, "ask", "--kind", "action", "--non-blocking", "--context", "Default: wait for you.",
                                   "--unblocks", "Keyword data for the audit.", "Verify the DataForSEO account")
                # The interface sends the reply through the ordinary message route, naming the ask.
                message = f'Re ask #{ask["id"]} "Verify the DataForSEO account" (still open): what do you need for dataforseo?'
                self.post(f"/api/browser/sessions/{thread}/messages", {"message": message, "requestId": f"reply-{client}"})
                turns = self.turns(thread)
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["items"][0]["type"], "userMessage")
                self.assertIn(message, turns[0]["items"][0]["content"][0]["text"])
                # Then it asks the backend to keep the mark. The ask stays open and nothing more is delivered.
                recorded = self.post(f"/api/browser/asks/{ask['id']}/reply", {"text": " what do you need for dataforseo? "})
                self.assertEqual((recorded["status"], recorded["reply_text"], recorded["session_id"]),
                                 ("open", "what do you need for dataforseo?", thread))
                self.assertTrue(recorded["replied_at"])
                listed = next(item for item in self.get("/api/browser/asks")["data"] if item["id"] == ask["id"])
                self.assertEqual((listed["reply_text"], listed["replied_at"]), (recorded["reply_text"], recorded["replied_at"]))
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                # The asking agent can read the reply on its ask.
                self.assertEqual(self.run_cli(thread, "ask", "--show", str(ask["id"]))["reply_text"], recorded["reply_text"])
                for body, expected in (({}, "Record a reply to an ask with text"), ({"text": "x", "note": "y"}, "Record a reply"),
                                       ({"text": "  "}, "Reply must not be empty"), ({"text": 5}, "Reply must be text")):
                    code, error = self.rejected(f"/api/browser/asks/{ask['id']}/reply", body)
                    self.assertEqual(code, 400, body)
                    self.assertIn(expected, error)
                self.post(f"/api/browser/asks/{ask['id']}", {"resolution": "done", "note": "Verified."})
                code, error = self.rejected(f"/api/browser/asks/{ask['id']}/reply", {"text": "late"})
                self.assertEqual((code, "already resolved" in error), (400, True))

    def test_config_advertises_ask_kinds_and_the_view_needs_no_new_urls(self):
        self.assertIs(self.get("/api/browser/config")["askKinds"], True)
        with urllib.request.urlopen(self.base + "/") as response:
            workspace = response.read().decode()
        for bundle in ("/dist/app.js?v=", "/dist/app.css?v="):
            self.assertIn(bundle, workspace)
        for asset in ("/asks.js", "/asks.css"):
            with urllib.request.urlopen(self.base + asset) as response:
                self.assertEqual(response.status, 200)
        for absent in ("asks-page", "asks-copy", "asks-frame"):
            self.assertNotIn(absent, workspace)

    def test_resolving_an_action_or_request_wakes_the_idle_app_conversation(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.app_thread(client)
                action = self.run_cli(thread, "ask", "--kind", "action", "--non-blocking", "--context",
                                      "Default: wait for you.", "--unblocks", "Pricing for all 18 verified actors.",
                                      "Add payout billing details in Apify")
                self.sessions.inbox_wake.process_once()
                self.assertEqual(self.turns(thread), [])
                self.post(f"/api/browser/asks/{action['id']}", {"resolution": "done"})
                self.sessions.inbox_wake.process_once()
                turns = self.turns(thread)
                self.assertEqual(len(turns), 1)
                delivered = turns[0]["items"][0]["content"][0]["text"]
                self.assertIn(f"The user resolved your non-blocking action ask #{action['id']}: done.", delivered)
                self.assertIn("Verify the result before relying on it.", delivered)
                if client == "claude":
                    self.sessions.claude.connections[thread].callback({"type": "result", "subtype": "success"})
                else:
                    turn = self.sessions.rpc.threads[thread]["turns"][-1]
                    turn["status"] = "completed"
                    self.sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": dict(turn)}})
                self.assertTrue(self.sessions.persistence.flush(key=thread))
                request = self.run_cli(thread, "ask", "--kind", "request", "--non-blocking", "--context",
                                       "Default: the public endpoint.", "--unblocks", "Removes the rate limit.",
                                       "Provide a SerpAPI key")
                self.post(f"/api/browser/asks/{request['id']}",
                          {"resolution": "granted", "note": "The key is in 1Password under SerpAPI."})
                self.sessions.inbox_wake.process_once()
                turns = self.turns(thread)
                self.assertEqual(len(turns), 2)
                granted = turns[1]["items"][0]["content"][0]["text"]
                self.assertIn(f"The user resolved your non-blocking request ask #{request['id']}: granted.", granted)
                self.assertIn("Note: The key is in 1Password under SerpAPI.", granted)


if __name__ == "__main__":
    unittest.main()
