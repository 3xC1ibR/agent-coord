# Feature Ideas

> **IDEAS ONLY. The existence of an idea in this document does NOT mean we should
> build it. Before building any idea, we MUST assess it against the current product
> roadmap and launch requirements. Inclusion here is not approval, a priority, a
> release commitment, or authorization for an agent to begin implementation.**

This document preserves possibilities for Ribbon Field without adding them to
the launch scope. Ideas may be deferred indefinitely or rejected. Technical
feasibility, an attractive design, or time already spent discussing an idea does
not establish that it belongs in the product.

## Assessment before implementation

Ribbon Field's immediate goal is to launch a useful product that does its core
job well: let people start multiple agents, let them work, and return to the
right conversation with the context intact. See the [product purpose](../purpose.md)
and [launch guidance](../AGENTS.md#purpose).

Before selecting an idea for implementation, assess:

- **User need:** Who needs it, what problem does it solve, and what evidence shows
  the problem matters?
- **Roadmap fit:** Does it strengthen the chosen core workflow and current product
  direction?
- **Launch necessity:** Is it required for a useful launch, or can we launch and
  learn without it? Would building it now delay launch for a convenience?
- **Alternatives and cost:** Could an existing tool or smaller change solve the
  problem? What development, maintenance, dependency, and support costs would it
  introduce, and what more important work would it displace?

The assessment must lead to a separate product decision about whether and when
to build. Unresolved roadmap or launch decisions do not authorize implementation.
Keep approved execution work in Beads; this document is an idea reference, not a
task queue. Entry order does not indicate priority.

## Interactive terminal in the UI

**Idea only. Not approved for implementation or established as a launch requirement.**

Allow users to open a shell in their workspace or interact with an agent's managed
terminal from Ribbon Field. The potential benefit is completing command-line
work without switching applications.

One possible approach discussed is xterm.js in the browser, WebSockets through
an aiohttp server, and the existing Agent Coord PTY supervisor. This is an
implementation sketch, not a technology decision; adopting it would also require
revisiting the dependency-free Python constraint. Workspace shells, attachment to
agent terminals, tabs, resizing, and reconnect behavior are possible scope choices,
not a committed feature set.

Before considering it for launch, establish which essential user workflow needs
an embedded terminal and whether an external terminal already serves that need.
Weigh that benefit against the work to support reliable input, process ownership,
screen restoration, and output buffering. A convincing technical approach alone
does not justify building it now.
