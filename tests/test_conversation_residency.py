from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.conversation_residency import Resident, releasable


class ConversationResidencyTests(unittest.TestCase):
    def test_busy_conversations_are_never_released(self):
        residents = [Resident("running", 0, True), Resident("approval", 1, True), Resident("queued", 2, True)]
        self.assertEqual(releasable(residents, 10_000, warm_limit=0, minimum_idle=0), [])

    def test_most_recently_used_idle_conversations_stay_warm(self):
        residents = [Resident(f"t{index}", index, False) for index in range(10)]
        self.assertEqual(releasable(residents, 10_000, warm_limit=6, minimum_idle=0), ["t3", "t2", "t1", "t0"])

    def test_busy_conversations_do_not_use_warm_places(self):
        residents = [Resident("busy-recent", 100, True), Resident("idle-new", 50, False), Resident("idle-old", 10, False)]
        self.assertEqual(releasable(residents, 10_000, warm_limit=1, minimum_idle=0), ["idle-old"])

    def test_recently_used_conversations_wait_for_the_idle_period(self):
        residents = [Resident("new", 9_900, False), Resident("old", 9_000, False), Resident("warm", 9_950, False)]
        self.assertEqual(releasable(residents, 10_000, warm_limit=1, minimum_idle=600), ["old"])

    def test_equal_timestamps_release_in_a_stable_order(self):
        residents = [Resident("b", 5, False), Resident("a", 5, False), Resident("c", 5, False)]
        self.assertEqual(releasable(residents, 10_000, warm_limit=1, minimum_idle=0), ["b", "a"])


if __name__ == "__main__":
    unittest.main()
