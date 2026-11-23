import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.provider_transport import Journal, Owner, TransportError, event_size, retire_dead_owner
from agent_coord.provider_transport import _darwin_child_observer, observe_child_exit


class ChildObservationTests(unittest.TestCase):
    def assert_non_reaping_observation(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(7)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertIsNone(observe_child_exit(process.pid))
            process.stdin.close()
            deadline = time.monotonic() + 5
            while observe_child_exit(process.pid) is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(observe_child_exit(process.pid), process.pid)
            self.assertEqual(observe_child_exit(process.pid), process.pid)
            self.assertIsNone(process.returncode)
            # The real exit status must remain available to waitpid/Popen.
            self.assertEqual(process.wait(timeout=3), 7)
            with self.assertRaises(ChildProcessError):
                observe_child_exit(process.pid)
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

    @unittest.skipUnless(callable(getattr(os, "waitid", None)), "Native waitid unavailable")
    def test_native_observation_retains_child_identity_and_exit_status(self):
        with patch("agent_coord.provider_transport.os.waitid", wraps=os.waitid) as native:
            self.assert_non_reaping_observation()
            self.assertGreater(native.call_count, 2)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin ABI compatibility")
    def test_darwin_fallback_retains_child_identity_and_exit_status(self):
        with patch("agent_coord.provider_transport.os.waitid", None, create=True):
            self.assert_non_reaping_observation()

    def test_missing_native_observer_fails_closed_on_unsupported_platform(self):
        _darwin_child_observer.cache_clear()
        try:
            with patch("agent_coord.provider_transport.os.waitid", None, create=True), \
                    patch("agent_coord.provider_transport.sys.platform", "unsupported"):
                with self.assertRaisesRegex(TransportError, "unavailable"):
                    observe_child_exit(os.getpid())
        finally:
            _darwin_child_observer.cache_clear()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.owner = Owner(self.directory.name)
        self.claim = self.owner.dispatch({"action": "claim", "expected_epoch": 0, "coordinator": "first"})

    def tearDown(self):
        self.owner.journal.stop_waits()
        for process in list(self.owner.processes.values()):
            if process.returncode is None:
                self.owner._close_process(process)
        for reader in self.owner.readers:
            reader.join(timeout=5)
        for process in self.owner.processes.values():
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.owner.journal.close()
        self.directory.cleanup()

    def command(self, identity, operation, value, claim=None):
        return self.owner.dispatch({"action": "command", "command_id": identity, "channel": "provider",
            "operation": operation, "value": value, **(claim or self.claim)})

    def open(self):
        import os
        return self.command("open", "open", {"args": [sys.executable, "-u", "-c",
            "import sys\nfor line in sys.stdin: print(line.rstrip(), flush=True)"], "env": dict(os.environ)})

    def test_fencing_retains_process_and_events(self):
        pid = self.open()["result"]["pid"]
        self.command("first-input", "write", {"data": "first\n"})
        second = self.owner.dispatch({"action": "claim", "expected_epoch": 1, "coordinator": "second"})
        with self.assertRaisesRegex(TransportError, "fenced"):
            self.command("stale-input", "write", {"data": "stale\n"})
        self.command("second-input", "write", {"data": "second\n"}, second)
        deadline = time.monotonic() + 5
        while len(self.owner.journal.events(0)) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        events = self.owner.journal.events(0)
        self.assertEqual([r["data"] for r in events], ["first\n", "second\n"])
        self.assertEqual(self.owner.processes["provider"].pid, pid)
        self.assertIsNone(self.owner.processes["provider"].poll())

    def test_duplicate_command_does_not_write_again(self):
        self.open()
        first = self.command("input", "write", {"data": "once\n"})
        self.assertEqual(self.command("input", "write", {"data": "once\n"}), first)
        with self.assertRaisesRegex(TransportError, "different content"):
            self.command("input", "write", {"data": "twice\n"})
        deadline = time.monotonic() + 5
        while not self.owner.journal.events(0) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual([r["data"] for r in self.owner.journal.events(0)], ["once\n"])

    def test_missing_launch_directory_has_durable_rejection_before_provider_input(self):
        options = {"args": [sys.executable], "env": {}, "cwd": self.directory.name + "/removed-worktree"}
        result = self.command("open", "open", options)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("removed-worktree", result["result"]["error"])
        self.assertEqual(self.command("open", "open", options), result)
        self.assertEqual(self.owner.dispatch({"action": "outcome", "command_id": "open", **self.claim}), result)
        channel = self.owner.dispatch({"action": "status"})["channels"][0]
        self.assertEqual((channel["state"], channel["pid"]), ("closed", None))
        self.assertFalse(self.owner.processes)

    def test_uncertain_write_is_never_repeated(self):
        self.open()
        real = self.owner._command
        count = []

        def interrupted(channel, operation, value):
            count.append(operation)
            real(channel, operation, value)
            raise OSError("lost receipt after pipe write")

        self.owner._command = interrupted
        result = self.command("input", "write", {"data": "once\n"})
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(self.command("input", "write", {"data": "once\n"}), result)
        self.assertEqual(count, ["write"])

    def test_restart_marks_incomplete_delivery_and_lost_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            with journal.db:
                journal.db.execute("INSERT INTO commands VALUES ('id','channel','write','{}','writing',NULL)")
                journal.db.execute("INSERT INTO channels VALUES ('channel','{}',123,'running')")
            journal.close()
            journal = Journal(directory)
            self.addCleanup(journal.close)
            self.assertEqual(journal.db.execute("SELECT outcome FROM commands").fetchone()[0], "uncertain")
            self.assertEqual(journal.db.execute("SELECT state FROM channels").fetchone()[0], "lost")

    def test_claim_compare_and_swap(self):
        self.assertEqual(self.owner.dispatch({"action": "claim", "expected_epoch": 0, "coordinator": "first"}), self.claim)
        with self.assertRaisesRegex(TransportError, "changed"):
            self.owner.dispatch({"action": "claim", "expected_epoch": 0, "coordinator": "second"})

    def ownership_record(self, **fields):
        record = {"instance": self.owner.instance, "boot_id": self.owner.boot_id,
                  "journal_generation": self.owner.instance, **fields}
        (Path(self.directory.name) / "owner.json").write_text(json.dumps(record))
        return record

    def test_dead_owner_refuses_same_boot_provider_ambiguity_without_signals(self):
        self.open()
        record = self.ownership_record()
        original = (Path(self.directory.name) / "owner.json").read_bytes()
        with patch("agent_coord.provider_transport.os.killpg") as signals:
            with self.assertRaisesRegex(TransportError, "surviving providers cannot be verified"):
                retire_dead_owner(self.directory.name, record, lambda *_: self.fail("Retired a live provider"))
            signals.assert_not_called()
        self.assertIsNone(self.owner.processes["provider"].poll())
        self.assertEqual((Path(self.directory.name) / "owner.json").read_bytes(), original)

    def test_dead_owner_refuses_held_lock_even_with_old_boot_record(self):
        import fcntl
        record = self.ownership_record(boot_id="00000000-0000-0000-0000-000000000001")
        with (Path(self.directory.name) / "owner.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TransportError, "still owns its lock"):
                retire_dead_owner(self.directory.name, record, lambda *_: self.fail("Ignored live owner"))
        self.assertTrue((Path(self.directory.name) / "owner.json").exists())

    def test_prior_boot_retirement_preserves_shutdown_error_and_receipts(self):
        with self.owner.journal.db:
            self.owner.journal.db.execute("INSERT INTO commands VALUES ('uncertain','provider','write','hash','uncertain',NULL)")
        record = self.ownership_record(boot_id="00000000-0000-0000-0000-000000000001", shutdown_error="escaped pipe")
        callbacks = []
        with patch("agent_coord.provider_transport.boot_identity", return_value="00000000-0000-0000-0000-000000000002"), \
                patch("agent_coord.provider_transport.os.killpg") as signals:
            retire_dead_owner(self.directory.name, record, lambda archive, reason: callbacks.append((archive, reason)))
            signals.assert_not_called()
        self.assertFalse((Path(self.directory.name) / "owner.json").exists())
        self.assertEqual(json.loads((callbacks[0][0] / "owner.json").read_text())["shutdown_error"], "escaped pipe")
        self.assertEqual(self.owner.journal.db.execute("SELECT outcome FROM commands").fetchone()[0], "uncertain")

    def test_retirement_failure_keeps_active_evidence(self):
        record = self.ownership_record(boot_id="00000000-0000-0000-0000-000000000001", shutdown_error="retained failure")
        def fail(*_):
            raise OSError("interrupted evidence export")
        with patch("agent_coord.provider_transport.boot_identity", return_value="00000000-0000-0000-0000-000000000002"):
            with self.assertRaisesRegex(OSError, "evidence export"):
                retire_dead_owner(self.directory.name, record, fail)
        self.assertEqual(json.loads((Path(self.directory.name) / "owner.json").read_text()), record)

    def test_unused_owner_can_be_retired_without_a_reboot(self):
        record = self.ownership_record()
        callbacks = []
        retire_dead_owner(self.directory.name, record, lambda *args: callbacks.append(args))
        self.assertEqual(len(callbacks), 1)
        self.assertFalse((Path(self.directory.name) / "owner.json").exists())

    def test_missing_boot_identity_never_discards_incomplete_shutdown(self):
        record = self.ownership_record(boot_id=None, shutdown_error="unverified escaped descriptor")
        with self.assertRaisesRegex(TransportError, "no verifiable boot identity"):
            retire_dead_owner(self.directory.name, record, lambda *_: self.fail("Discarded incomplete shutdown"))
        with self.assertRaisesRegex(TransportError, "explicit Stop maintenance"):
            Owner(self.directory.name)
        self.assertEqual(json.loads((Path(self.directory.name) / "owner.json").read_text()), record)

    def test_new_owner_has_fresh_cursor_and_keeps_retired_raw_journal(self):
        self.owner.journal.append("old-provider", "stdout", "old response")
        self.owner.journal.acknowledge_events(1, retain_after=1)
        previous = self.owner.journal.path
        replacement = Owner(self.directory.name)
        try:
            self.assertNotEqual(replacement.journal.path, previous)
            self.assertEqual(replacement.journal.events(0), [])
            self.assertEqual(replacement.journal.sequence(), 0)
            self.assertEqual(replacement.journal.append("new-provider", "stdout", "new response"), 1)
            self.assertEqual(replacement.journal.events(0)[0]["data"], "new response")
            self.assertTrue(previous.exists())
            self.assertEqual(self.owner.journal.bounds()["retained_after"], 1)
        finally:
            replacement.journal.close()

    def assert_descendant_shutdown(self, *, leader_exits):
        import os
        import signal
        marker = Path(self.directory.name) / "descendant-ready"
        descendant = ("import signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      f"Path({str(marker)!r}).write_text('ready'); time.sleep(60)")
        parent = ("import os,subprocess,sys,time; from pathlib import Path\n"
                  f"child = subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
                  f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n" +
                  ("os._exit(0)" if leader_exits else "time.sleep(60)"))
        result = self.command("open", "open", {"args": [sys.executable, "-u", "-c", parent], "env": dict(os.environ)})
        process = self.owner.processes["provider"]
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            if leader_exits:
                process.wait(timeout=3)
                with patch("agent_coord.provider_transport.os.killpg", wraps=os.killpg) as signals:
                    outcome = self.command("close", "close", {})
                    self.assertEqual(outcome["outcome"], "uncertain")
                    self.assertIn("ownership", outcome["result"]["error"])
                    signals.assert_not_called()
                return
            started = time.monotonic()
            self.assertEqual(self.command("close", "close", {})["outcome"], "written")
            for reader in self.owner.readers:
                reader.join(timeout=1)
            self.assertLess(time.monotonic() - started, 6)
            self.assertFalse(any(reader.is_alive() for reader in self.owner.readers),
                             "Descendant retained a provider pipe after explicit close")
        finally:
            # Test-owned process group only; also clean up a failed old implementation.
            try:
                os.killpg(result["result"]["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            for reader in self.owner.readers:
                reader.join(timeout=3)

    def test_close_terminates_descendants_holding_provider_pipes(self):
        self.assert_descendant_shutdown(leader_exits=False)

    def test_close_refuses_group_signals_after_provider_leader_is_reaped(self):
        self.assert_descendant_shutdown(leader_exits=True)

    def test_owner_stop_uses_one_grace_period_for_all_process_groups(self):
        import os
        for index in range(3):
            marker = Path(self.directory.name) / ("group-ready-" + str(index))
            source = ("import signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      f"Path({str(marker)!r}).write_text('ready'); time.sleep(60)")
            self.owner.dispatch({"action": "command", "command_id": "open-" + str(index),
                "channel": "group-" + str(index), "operation": "open",
                "value": {"args": [sys.executable, "-u", "-c", source], "env": dict(os.environ)}, **self.claim})
        deadline = time.monotonic() + 5
        while len(list(Path(self.directory.name).glob("group-ready-*"))) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(list(Path(self.directory.name).glob("group-ready-*"))), 3)
        started = time.monotonic()
        self.owner._close_processes(self.owner.processes.values())
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(all(process.poll() is not None for process in self.owner.processes.values()))

    def test_stream_cleanup_does_not_wait_on_escaped_descendant_buffer_lock(self):
        import os
        import signal
        marker = Path(self.directory.name) / "escaped-descendant"
        descendant = ("import os,time; from pathlib import Path; "
                      f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)")
        parent = ("import subprocess,sys,time\n"
                  f"child = subprocess.Popen([sys.executable, '-c', {descendant!r}], start_new_session=True)\n"
                  "time.sleep(60)")
        self.command("open", "open", {"args": [sys.executable, "-u", "-c", parent], "env": dict(os.environ)})
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        descendant_pid = int(marker.read_text())
        try:
            self.owner.stopping.set()
            self.owner.journal.stop_waits()
            self.owner._close_processes(self.owner.processes.values())
            self.assertTrue(any(reader.is_alive() for reader in self.owner.readers))
            started = time.monotonic()
            with self.assertRaisesRegex(TransportError, "shutdown incomplete"):
                self.owner._close_streams()
            self.assertLess(time.monotonic() - started, 6)
            self.assertTrue(self.owner.processes["provider"].stderr.raw.closed)
        finally:
            # This detached descendant belongs only to this isolated fixture.
            try:
                os.killpg(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for reader in self.owner.readers:
                reader.join(timeout=3)

    def test_shutdown_failure_does_not_prevent_other_owned_groups_stopping(self):
        import os
        for index in range(2):
            self.owner.dispatch({"action": "command", "command_id": "open-" + str(index),
                "channel": "group-" + str(index), "operation": "open",
                "value": {"args": [sys.executable, "-c", "import time; time.sleep(60)"], "env": dict(os.environ)}, **self.claim})
        first, second = list(self.owner.processes.values())
        signal_group = self.owner._signal_process_group

        def reject_one(process, number):
            if process is first:
                raise TransportError("Injected process group ownership mismatch")
            return signal_group(process, number)

        with patch.object(Owner, "_signal_process_group", side_effect=reject_one):
            with self.assertRaisesRegex(TransportError, "shutdown incomplete"):
                self.owner._close_processes([first, second])
        self.assertIsNone(first.poll())
        self.assertIsNotNone(second.poll())

    def test_duplex_backpressure_does_not_hold_journal(self):
        import os
        self.command("open", "open", {"args": [sys.executable, "-u", "-c",
            "import sys\nprint('x' * 2000000, flush=True)\nline=sys.stdin.readline()\nprint(len(line), flush=True)"],
            "env": dict(os.environ)})
        outcomes = []
        writer = threading.Thread(target=lambda: outcomes.append(
            self.command("large", "write", {"data": "y" * 2000000 + "\n"})))
        writer.start()
        writer.join(timeout=5)
        if writer.is_alive():
            self.owner.processes["provider"].kill()
            writer.join(timeout=5)
            self.fail("Transport deadlocked under duplex pipe backpressure")
        self.assertEqual(outcomes[0]["outcome"], "written")
        self.assertEqual(self.owner.dispatch({"action": "status"})["epoch"], 1)

    def test_timed_out_partial_frame_blocks_later_writes_without_killing_provider(self):
        import os
        self.command("open", "open", {"args": [sys.executable, "-u", "-c",
            "import time; time.sleep(30)"], "env": dict(os.environ)})
        data = {"data": "x" * (1024 * 1024) + "\n"}
        with patch("agent_coord.provider_transport.WRITE_TIMEOUT", .1):
            result = self.command("large", "write", data)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertIsNone(self.owner.processes["provider"].poll())
        self.assertEqual(self.command("large", "write", data), result)
        later = self.command("later", "write", {"data": "later\n"})
        self.assertEqual(later["outcome"], "uncertain")
        self.assertIn("unresolved delivery", later["result"]["error"])
        status = self.owner.dispatch({"action": "status"})
        self.assertEqual(status["channels"][0]["state"], "write-uncertain")
        self.assertNotEqual(status["channels"][0]["pgid"], os.getpgrp())

    def test_receipts_are_compact_and_capacity_never_discards_deduplication(self):
        self.open()
        first = self.command("input", "write", {"data": "private-input\n"})
        with patch("agent_coord.provider_transport.MAX_RECEIPTS", 2):
            self.assertEqual(self.command("input", "write", {"data": "private-input\n"}), first)
            with self.assertRaisesRegex(TransportError, "receipt capacity"):
                self.command("new", "write", {"data": "new\n"})
        rows = self.owner.journal.db.execute("SELECT payload FROM commands").fetchall()
        self.assertTrue(all(row[0].startswith("sha256:") and len(row[0]) == 71 for row in rows))
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.owner.channel_locks, {})

    def test_finished_channels_release_process_and_reader_handles(self):
        self.open()
        self.command("close", "close", {})
        deadline = time.monotonic() + 5
        while self.owner.processes and time.monotonic() < deadline:
            self.owner.dispatch({"action": "status"})
            time.sleep(.01)
        self.assertEqual(self.owner.processes, {})
        self.assertEqual(self.owner.channel_readers, {})
        self.assertEqual(self.owner.readers, [])

    def test_large_history_frame_is_not_truncated_at_the_old_eight_megabyte_limit(self):
        import os
        size = 9 * 1024 * 1024
        self.command("open", "open", {"args": [sys.executable, "-u", "-c",
            f"import sys; print('x' * {size}, flush=True); sys.stdin.read()"], "env": dict(os.environ)})
        events = []
        deadline = time.monotonic() + 5
        while not events and time.monotonic() < deadline:
            events = self.owner.journal.events(0)
            time.sleep(.01)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stream"], "stdout")
        self.assertEqual(len(events[0]["data"]), size + 1)


class JournalBoundsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = Journal(self.directory.name)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_capacity_applies_backpressure_until_a_durable_floor_releases_space(self):
        size = event_size("provider", "stdout", "first")
        appended = []
        with patch("agent_coord.provider_transport.MAX_JOURNAL_BYTES", size + 1):
            self.assertEqual(self.journal.append("provider", "stdout", "first"), 1)
            writer = threading.Thread(target=lambda: appended.append(self.journal.append("provider", "stdout", "next")))
            writer.start()
            try:
                self.journal.acknowledge_events(1)
                writer.join(timeout=.05)
                self.assertTrue(writer.is_alive(), "Acknowledgement alone must preserve rollback data")
                self.journal.acknowledge_events(1, retain_after=1)
                writer.join(timeout=3)
                self.assertFalse(writer.is_alive())
                self.assertEqual(appended, [2])
                with self.assertRaisesRegex(TransportError, "no longer retained"):
                    self.journal.events(0)
                self.assertEqual(self.journal.events(1)[0]["data"], "next")
            finally:
                self.journal.stop_waits()
                writer.join(timeout=3)

    def test_full_storage_defers_the_reader_instead_of_ending_the_channel(self):
        database = self.journal.db
        with self.journal.lock:
            database.execute("PRAGMA max_page_count=%d" % database.execute("PRAGMA page_count").fetchone()[0])
        data, appended = "x" * 65536, []
        writer = threading.Thread(target=lambda: appended.append(self.journal.append("provider", "stdout", data)))
        with patch("sys.stderr"):
            writer.start()
            try:
                writer.join(timeout=.3)
                self.assertTrue(writer.is_alive(), "A full volume must not end the provider reader")
                with self.journal.lock:
                    database.execute("PRAGMA max_page_count=1073741823")
                writer.join(timeout=3)
                self.assertFalse(writer.is_alive())
            finally:
                self.journal.stop_waits()
                writer.join(timeout=3)
        self.assertEqual(appended, [1])
        self.assertEqual(self.journal.events(0)[0]["data"], data)

    def test_readers_recovering_from_full_storage_still_respect_journal_capacity(self):
        database = self.journal.db
        with self.journal.lock:
            database.execute("PRAGMA max_page_count=%d" % database.execute("PRAGMA page_count").fetchone()[0])
        data, appended = "x" * 65536, []
        limit = event_size("provider", "stdout", data) + 1  # One event fits.
        writers = [threading.Thread(target=lambda: appended.append(self.journal.append("provider", "stdout", data)))
                   for _ in range(2)]
        with patch("sys.stderr"), patch("agent_coord.provider_transport.MAX_JOURNAL_BYTES", limit):
            for writer in writers:
                writer.start()
            try:
                time.sleep(.3)
                self.assertEqual(appended, [])
                with self.journal.lock:
                    database.execute("PRAGMA max_page_count=1073741823")
                deadline = time.monotonic() + 5
                while not appended and time.monotonic() < deadline:
                    time.sleep(.02)
                time.sleep(.5)
                # Both readers woke after space returned; only one event fits the journal.
                self.assertEqual(appended, [1])
                self.assertLessEqual(self.journal.event_bytes, limit)
                self.journal.acknowledge_events(1)
                time.sleep(.3)
                self.assertEqual(appended, [1], "Acknowledgement alone must preserve rollback data")
                self.journal.acknowledge_events(1, retain_after=1)
                for writer in writers:
                    writer.join(timeout=3)
                self.assertEqual(appended, [1, 2])
                self.assertLessEqual(self.journal.event_bytes, limit)
            finally:
                self.journal.stop_waits()
                for writer in writers:
                    writer.join(timeout=3)

    def test_recovery_floor_and_sequence_survive_restart_and_empty_journal(self):
        self.journal.append("provider", "stdout", "first")
        self.journal.acknowledge_events(1, retain_after=1)
        self.journal.close()
        self.journal = Journal(self.directory.name)
        self.assertEqual(self.journal.sequence(), 1)
        self.assertEqual(self.journal.append("provider", "stdout", "next"), 2)
        with self.assertRaisesRegex(TransportError, "retired"):
            self.journal.acknowledge_events(2, retain_after=0)
        with self.assertRaisesRegex(TransportError, "available sequence"):
            self.journal.acknowledge_events(3, retain_after=1)

    def test_pruning_is_bounded_and_can_repeat_the_same_checkpoint(self):
        for _ in range(300):
            self.journal.append("provider", "stdout", "x")
        first = self.journal.acknowledge_events(300, retain_after=300)
        self.assertEqual(first["retained_after"], 256)
        second = self.journal.acknowledge_events(300, retain_after=300)
        self.assertEqual(second["retained_after"], 300)
        self.assertEqual(second["event_bytes"], 0)
        self.assertEqual(self.journal.sequence(), 300)

    def test_batch_bounds_include_json_escaping(self):
        data = "\x00" * 200
        self.journal.append("provider", "stderr", data)
        self.journal.append("provider", "stderr", data)
        limit = event_size("provider", "stderr", data) * 2 - 1
        with patch("agent_coord.provider_transport.MAX_BATCH_BYTES", limit):
            events = self.journal.events(0)
            self.assertEqual(len(events), 1)
            self.assertLessEqual(len(json.dumps({"events": events}).encode()), limit)


if __name__ == "__main__":
    unittest.main()
