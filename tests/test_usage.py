from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.store import CoordinationStore
from agent_coord.usage import (
    UsageParseError,
    capture_delegation_usage,
    parse_transcript_usage,
)


class DelegationUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_transcript(self, name: str, events: list[dict]) -> Path:
        path = self.root / name
        path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return path

    def test_codex_uses_latest_cumulative_token_count(self) -> None:
        transcript = self.write_transcript(
            "codex.jsonl",
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 4,
                                "cache_write_input_tokens": 1,
                                "output_tokens": 3,
                                "reasoning_output_tokens": 2,
                                "total_tokens": 13,
                            }
                        },
                    },
                },
                {"type": "response_item", "payload": {"type": "message"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 25,
                                "cached_input_tokens": 12,
                                "cache_write_input_tokens": 2,
                                "output_tokens": 8,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 33,
                            }
                        },
                    },
                },
            ],
        )
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"unfinished":')

        usage = parse_transcript_usage("codex", transcript)

        self.assertEqual(usage["normalized"]["total_tokens"], 33)
        self.assertEqual(usage["normalized"]["cached_input_tokens"], 12)
        self.assertEqual(usage["client"]["reasoning_output_tokens"], 5)

    def test_claude_deduplicates_repeated_assistant_records(self) -> None:
        first = {
            "type": "assistant",
            "uuid": "event-a",
            "message": {
                "id": "message-a",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            },
        }
        second = {
            "type": "assistant",
            "uuid": "event-b",
            "message": {
                "id": "message-b",
                "usage": {
                    "input_tokens": 4,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 6,
                    "output_tokens": 3,
                },
            },
        }
        transcript = self.write_transcript(
            "claude.jsonl", [first, dict(first, uuid="duplicate-a"), second]
        )

        usage = parse_transcript_usage("claude", transcript)

        self.assertEqual(usage["client"]["input_tokens"], 14)
        self.assertEqual(usage["client"]["cache_read_input_tokens"], 36)
        self.assertEqual(usage["normalized"]["input_tokens"], 70)
        self.assertEqual(usage["normalized"]["output_tokens"], 8)
        self.assertEqual(usage["normalized"]["total_tokens"], 78)

    def test_missing_usage_is_reported_without_reading_transcript_content(self) -> None:
        transcript = self.write_transcript(
            "no-usage.jsonl", [{"type": "response_item", "secret": "not copied"}]
        )

        with self.assertRaisesRegex(UsageParseError, "no cumulative token usage"):
            parse_transcript_usage("codex", transcript)

    def test_capture_persists_status_and_repository_artifact(self) -> None:
        store = CoordinationStore(self.root / "state.sqlite3")
        store.register(session_id="parent", client="codex", cwd=str(self.root))
        store.register(session_id="child", client="codex", cwd=str(self.root))
        store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        store.attach_delegation("delegation-a", "child")
        delegation = store.finish_delegation(
            "delegation-a",
            child_session_id="child",
            outcome="completed",
            message="Done.",
        )
        transcript = self.write_transcript(
            "codex.jsonl",
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "total_tokens": 15,
                            }
                        },
                    },
                }
            ],
        )

        captured = capture_delegation_usage(
            store,
            delegation=delegation,
            session_id="child",
            transcript_path=str(transcript),
            capture_event="Stop",
            model="gpt-test",
        )

        self.assertEqual(captured["token_usage"]["normalized"]["total_tokens"], 15)
        self.assertEqual(captured["token_usage_capture_event"], "Stop")
        artifact_path = Path(captured["token_usage_artifact_path"])
        self.assertEqual(
            artifact_path,
            self.root.resolve() / ".agent-coord/delegations/delegation-a.usage.json",
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["usage"]["model"], "gpt-test")
        self.assertNotIn("secret", json.dumps(artifact))


if __name__ == "__main__":
    unittest.main()
