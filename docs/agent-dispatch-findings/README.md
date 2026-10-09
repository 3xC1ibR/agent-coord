# Agent workflow findings

Each agent records useful observations in its own file. The dispatcher later
combines recurring findings and corrections in
[the experiment journal](../agent-dispatch-experiment.md).
This convention applies to future assignments from October 8, 2026 onward;
currently running agents do not need new instructions or retrospective reports.

## Ownership and paths

- The dispatcher assigns `docs/agent-dispatch-findings/<session-id>.md` in the
  handoff, using the agent's actual stable session ID. Reused agents append to
  their existing file. For another workspace, assign an absolute path in that
  workspace and record the exact location in the dispatcher checkpoint.
- Each agent writes only its own findings file and declares that exact path
  when coordination requires a write scope. Do not reserve the whole directory.
- The dispatcher owns this README and consolidated journal updates. Agents
  should not ask unrelated specialists to transcribe findings because they
  happen to own a shared file.

## Useful entries

Put the agent's role and session ID at the top. Add a short dated entry when
normal task work reveals something useful: what worked, what failed, a blocker,
an unnecessary interaction, or a concrete improvement. Include the relevant
task or Bead, actual model/effort when useful, and evidence such as a message ID,
artifact, or validation result. Distinguish observed behavior from hypotheses
and proposed changes. A few sentences or bullets usually suffice; omit filler
and copied transcripts. Tasks and follow-up work remain in Beads.

Notes are part of the existing task, not a reason to start another agent turn
or require a peer reply. A normal final response or checkpoint can link to the
file; no additional message is required solely to announce that it was updated.

## Consolidation

When reviewing results or at the user's request, the dispatcher reads the
reports, merges duplicate findings, resolves superseded conclusions, and writes
a concise synthesis in the experiment journal. Retain source references so
conclusions can be traced without copying every report. Recheck file scopes
before updating the journal; an existing reservation remains valid. This is a
manual workflow convention, with no background collector or product change.
