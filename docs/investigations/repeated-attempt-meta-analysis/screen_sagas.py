#!/usr/bin/env python3
"""Screening aid: match candidate symptom families across threads, Beads and Git.

The regular expressions only gather candidate records for the lead coder to
read. Inclusion decisions are recorded, with reasons, in saga-inventory.json.

Usage: python3 screen_sagas.py RAW_DIRECTORY OUTPUT_JSON
"""

import datetime
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RF_REFERENCE = re.compile(r"ribbon ?field|agent[- ]coord|RF|rf app", re.I)

FAMILIES = {
 "F01_responsiveness": r"slow|latenc|responsiv|refresh(?:ing)? (?:this )?conversation|timed? ?out|busy|stall|freeze|hang\b|contention|sqlite|database lock|persistence writes|search projection|history persistence|opening .{0,20}conversation|reopening|excessive history|connection instab|inventory refresh",
 "F02_update_interrupts_agents": r"interrupt|without (?:stopping|interrupting)|after (?:the current )?agents finish|after active agents|pause agents for deploy|runtime update|backend update|rollout|independent (?:desktop |update )?runtime|uninterrupted|coordinator (?:replacement|upgrade)|live upgrade|handover|updater|signed (?:macos |app )?update",
 "F03_wake_delivery": r"\bwake|wake-?up|message delivery|inbox (?:wake|deliver)|missing steering|steering messages? (?:lost|missing)|deliver(?:y|ed)? .{0,25}(?:idle|closed|agent)|follow-?ups? (?:missed|lost)|stranded wake",
 "F04_turn_state_false": r"stall(?:ed)? (?:agent )?turn|false .{0,20}(?:failure|failed)|marked failed|premature .{0,10}completion|stale (?:blocked|running|active)|turn (?:failure|state)|silent app turns|orphaned app turns|stopped-turn|interrupted (?:and failed )?turns|liveness|hour-long turn",
 "F05_plugin_refresh_stranding": r"plugin (?:refresh|cache)|cache[- ]refresh|refresh (?:workflow|helpers|instructions)|stranded .{0,20}(?:hook|ui)|removed by plugin refresh|hook executable|plugin and mcp|cachebuster",
 "F06_native_smoke": r"native (?:full-)?smoke|smoke test|pane-toggle|window-focus failures",
 "F07_window_restoration": r"restor(?:e|ation).{0,30}(?:window|relaunch)|window(?:s)? .{0,20}(?:restor|persist|relaunch)|reopening the app|quitting and reopening|window-state|window move|working folders per window",
 "F08_composer_focus": r"focus after|composer focus|permission command focus|kicks me out of the input|keep composer focused|preserve focus",
 "F09_permission_prompts": r"yolo .{0,30}(?:regress|prompt|still)|permission (?:prompt|bypass)|bypass prompts|asks? (?:me )?for permission|waiting asking me for permissions|update existing thread yolo|yolo changes on existing",
 "F10_attention_indicators": r"badge mismatch|attention (?:count|dismissal|badge|queue oscillat)|dismissed review still|group badge|oscillat|unread (?:tab )?indicator|dismissal indicator",
 "F11_project_tabs": r"project tab|project switch|switch warning|wrong project|misplaced tab|tabs in their own project|restore project tabs",
 "F12_model_selection": r"model selection|model settings|choose (?:an available|codex or claude) model|model catalog|model suggestions|fable model",
 "F13_provider_kimi": r"\bkimi\b",
 "F14_provider_antigravity": r"antigravity|gravity delegation|gemini",
 "F15_provider_muse": r"\bmuse\b",
 "F16_checkpoint_links": r"checkpoint (?:app-)?thread links|links in checkpoints|agentcoord:// routes",
 "F17_chat_expansion": r"expand(?:ed)? chat|chat expansion",
 "F18_notification_routing": r"notification .{0,20}(?:navigation|routing|thread opening|crash|context)|phone thread navigation|mobile navigation fix|notification window|notification threads",
 "F19_history_missing": r"missing thread messages|empty (?:codex )?thread|empty messaging|empty rollout|restore saved history|history (?:lost|missing)|transcripts expire|session retention|restore terminal conversation history|retention and backups",
 "F20_tests_ci": r"\bci\b|test suite runtime|test feedback|faster test|regression state assertions|ci prerequisites",
 "F21_source_drift": r"reconcil(?:e|iation) .{0,30}(?:working tree|build|source)|repository drift|preserve installed build|shared working tree",
 "F22_connection_failure": r"connection failure|outage|blank page|restart loop|startup crash|repair ribbon field startup|start the runtime despite",
 "F23_scheduled_continue": r"scheduled (?:prompts? )?(?:cannot )?continue|scheduling prompts in existing|deliver follow-ups to their existing thread|scheduled continue",
 "F24_skill_autocomplete": r"skill (?:autocomplete|completion|shortcuts)",
 "F25_thread_close": r"thread clos|close threads|closing behavior|closure blocked|stop and close|close when done|close thread",
 "F26_duplicate_tabs": r"duplicate (?:conversation )?tab|detach(?:ed|able)? (?:conversation )?tab|duplicate conversation|duplicate inbox",
 "F27_hover_preview": r"hover (?:card|popover|preview)|pop ?over",
 "F28_mobile_tabs": r"mobile conversation tabs|persist web conversation tabs",
 "F29_coordination_session_identity": r"session identity|caller (?:context|identity)|bind coordination actions|identity ergonomics|mcp identity",
 "F30_macos_access_prompts": r"access prompts|privacy approvals",
}


def main():
    raw, output = Path(sys.argv[1]), Path(sys.argv[2])
    threads = json.loads((raw / "threads.json").read_text())
    scope = [
        t for t in threads
        if t["root"] == "/opt/projects/agent-coord"
        or (t["root"] == "/opt/projects" and RF_REFERENCE.search((t["title"] or "") + " " + (t["original_request"] or "")[:3000]))
    ]
    beads = json.loads((raw / "beads_show.json").read_text()).values()
    commits = json.loads((raw / "git_all.json").read_text())["commits"]
    interactions = [json.loads(line) for line in (REPO / ".beads/interactions.jsonl").read_text().splitlines() if line.strip()]
    reopened = {r["issue_id"] for r in interactions if r["extra"].get("old_value") == "closed"}
    result = {"universe": {
        "threads_all_workspaces": len(threads),
        "threads_screened": len(scope),
        "threads_agent_coord_root": sum(t["root"] == "/opt/projects/agent-coord" for t in scope),
        "threads_parent_root_referencing_ribbon_field": sum(t["root"] == "/opt/projects" for t in scope),
        "beads_issues": len(list(beads)) if not isinstance(beads, list) else len(beads),
        "commits": len(commits),
        "beads_reopened": sorted(reopened),
    }, "families": {}}
    beads = json.loads((raw / "beads_show.json").read_text()).values()
    for family, pattern in FAMILIES.items():
        rx = re.compile(pattern, re.I)
        matched_threads = [t for t in scope if rx.search(t["title"] or "") or rx.search((t["original_request"] or "")[:1500])]
        matched_beads = [b for b in beads if rx.search(b["title"]) or rx.search((b.get("description") or "")[:800])]
        matched_commits = [c for c in commits if rx.search(c["subject"]) and not c["subject"].startswith("t3 checkpoint")]
        dates = [t["created_at"] for t in matched_threads]
        result["families"][family] = {
            "pattern": pattern,
            "threads": [t["thread_id"] for t in matched_threads],
            "beads": [b["id"] for b in matched_beads],
            "beads_reopened": [b["id"] for b in matched_beads if b["id"] in reopened],
            "commits": [c["hash"] for c in matched_commits],
            "thread_span_utc": [
                datetime.datetime.fromtimestamp(min(dates), datetime.UTC).isoformat() if dates else None,
                datetime.datetime.fromtimestamp(max(dates), datetime.UTC).isoformat() if dates else None,
            ],
        }
    output.write_text(json.dumps(result, indent=1))
    for family, record in result["families"].items():
        print(f"{family:36} threads={len(record['threads']):3} beads={len(record['beads']):2} "
              f"reopened={len(record['beads_reopened'])} commits={len(record['commits']):2}")


if __name__ == "__main__":
    main()
