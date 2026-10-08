# Draft theory of attention

**Status: Draft, not a source of truth.** This is a proposal for discussion,
not an authoritative specification of current behavior or an approved
implementation plan. Updated October 7, 2026.

**Ribbon Field should make it easy to leave agents working, recognize when your
involvement matters, and return to useful conversations without rebuilding
context.**

Every prominent thread makes a claim on your attention. The app should be able
to explain that claim: something needs your decision, a discussion is ready to
continue, or there is a result you may want to know about.

**Attention belongs to the relationship between your intent and the current
state of the conversation.** An agent finishing a response tells us that it has
stopped speaking. To understand what that means for you, we need to consider
what you asked for and what remains unresolved.

The same reply can therefore deserve different treatment. “Here are two
approaches” might fully satisfy a request for a comparison. In a conversation
about deciding what to build, it might be the next step in an unfinished
discussion.

We should distinguish these situations:

| Situation | Meaning for the user | Presentation |
| --- | --- | --- |
| **Needs you** | A specific answer, decision, approval, or action is required. | Strongest prominence; explain what is needed and why. |
| **Ready to continue** | There is an open discussion or proposal worth returning to. | Visible and easy to resume, with less urgency. |
| **Updates** | There is new information, but no required response. | Quiet visibility; recedes after reading. |
| **Working** | The agent owns the next step and can proceed. | Show progress through phase columns. |
| **Available** | There is no current demand on the user, but the conversation remains useful. | Accessible through pins, Recent, projects, and search. |

**Work phase and attention are independent.** Planning, implementing,
validating, and deploying describe what the agent is doing. Any phase can
contain a user decision; any phase can also proceed without the user watching.
Recoverable problems should remain with the agent. When escalation is
necessary, the thread should explain what the user can do to help.

**A thread is a reusable working relationship.** It may contain many questions,
tasks, and completed deliveries over time. Finishing one piece of work should
preserve the agent’s context and the user’s ability to return.

The KES backfill thread illustrates this: the backfill can be finished, its
latest report can require no response, and the conversation can still deserve
a permanent place among your pins. Pinning expresses continuing usefulness.
It creates no obligation to act.

**Reading, resolving, and deferring mean different things.** Reading an update
acknowledges new information. Reading a question does not answer it. Handling a
response says that it no longer needs consideration for now. Moving a thread
to Later expresses a decision about when to engage. Closing it retires the
conversation from the active workspace while preserving its history.

These actions should have predictable consequences. A new reply may create a
new reason for attention, but it should respect the user’s saved placement.

**Later should support deliberate return.** Deferred threads need recognizable
projects, purposes, and context so the user can revisit a meaningful group of
work. An item’s age alone should not create urgency. Resurfacing should follow
a user choice or an agreed trigger.

**Automation should reduce the work of organizing attention.** Agents report
progress and concrete dependencies. A classifier can interpret the
conversational meaning of a reply. The app combines those signals with actual
running state, pending requests, read state, and user choices.

Classification is a judgment that can be wrong. Explicit pending approvals
take precedence. Uncertain replies should remain visible for consideration,
with a straightforward way to correct their treatment. These principles
should still hold when a classifier is unavailable.

**Interruptions deserve a higher threshold than visibility.** A conversation
can be useful to see without deserving a notification. Interruptions should be
reserved for actionable needs and events the user has chosen to hear about.

We should judge the system by whether a glance answers three questions:
**What needs me? What can I leave working? What can I return to when I choose?**

**Open question about attention counts:** Should the primary attention count
mean “things waiting on you,” with “Ready to continue” visible but counted
separately and more quietly? This remains a proposal, not an agreed product
decision.
