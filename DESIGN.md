---
name: Ribbon Field
colors:
  bg: "#f5f5f4"
  surface: "#ffffff"
  fg: "#18181b"
  fg-muted: "#6a6a71"
  border: "#e4e4e6"
  primary: "#2550d8"
  danger: "#c0392b"
typography:
  body: {fontFamily: system-ui, fontSize: 14px, lineHeight: 21px}
  meta: {fontFamily: system-ui, fontSize: 12px, lineHeight: 18px}
  heading: {fontFamily: system-ui, fontSize: 24px, fontWeight: 600, lineHeight: 30px}
rounded:
  control: 6px
  panel: 10px
spacing:
  small: 8px
  row: 12px
  section: 24px
---

## Overview
Ribbon Field is a persistent workspace for conversations and coordinated agent work.
Its quiet, dense workspace keeps the work prominent. Auxiliary pages such as Usage
and Activity use the same paper, ink, controls and type with more reading room.
The approved conversation composition gives reading and replying a shared measure;
other surfaces retain this contract.

## Colors
`web/styles.css` is the implementation source of truth; `web/theme.css` replaces
semantic tokens in dark mode. Use `--paper`, `--surface`, `--ink`, `--muted`,
`--line`, `--accent` and `--danger`, not new literal colors. Cobalt indicates an
action or link, orange attention, and green an unread response. No decorative color.

## Typography
Use the system font. Titles lead, supporting copy uses muted ink, and numeric
comparisons use tabular figures. Use readable sentence case and explicit labels.

## Layout
The workspace has navigation, an overview or conversation, and optional context
and pane regions. In the Mac app, a workspace window's header is its title bar: a
40px band holds the window controls, project tabs and header actions, and its empty
space moves the window. Settings and detached conversation windows keep the
standard title bar. Auxiliary pages have a simple title, controls, and centered
content. Activity uses a 1120px content maximum, 24px page spacing, 12px row spacing,
and 8px control gaps. Declare additional module dimensions once as CSS properties.
Narrow layouts stack controls and keep tables in labelled horizontal scroll areas.
Mobile conversations use one 56px header with Back, a single truncated title,
status and thread options. Workspace and conversation tabs return in the overview.
The composer has a short state-aware prompt above one row of attachment, model,
effort and send controls on desktop and mobile. Its width and insets align with the
transcript reading column. Desktop shortcut instructions remain available through
the editor description and tooltip; they do not occupy the prompt. The reading
column is capped at 760px. User messages sit at its trailing edge as restrained
request blocks; assistant responses use the full measure. Tool disclosures use
quiet horizontal rules instead of a filled strip. Message metadata uses the
system font at 12px and code uses the monospace font at 12px. The conversation
heading uses the system font at 18px. Context is a compact reference column: phase
beside its heading, checkpoint summary, a next action only when saved, and the
original request in a closed disclosure. Empty scratchpads start at 80px and grow
on focus; unsaved context-reuse advice does not reserve a section. Sidebar running
work uses a small status mark; the selected thread owns the row emphasis. Mobile
working/input status is explicit text beneath the title, with Stop a distinct
44px control. Composer and message geometry uses the chat-composition tokens in
`web/styles.css`. Send work, Review and Schedule live in the existing
thread options sheet; reuse their original controls and handlers. Keep 44px touch
targets and a visible send action as drafts grow or the keyboard opens. Mobile
dimensions are declared once in `web/styles.css`.
Jump to latest uses a downward arrow, floating above the desktop composer in a
44px button. The phone adds a visible Latest label immediately before thread
options in its existing header, retaining a 44px target height. At 360px and below,
the same control moves into thread actions to preserve title space.
Its desktop anchor never reserves a layout row;
phone navigation stays apart from Send and never covers the transcript or draft.
Retain the accessible name and tooltip. Mobile error notices occupy a dedicated
row before the transcript, with a 44px dismiss target; they return to the global
notice position in the overview and on desktop. Checkpoint provenance and
actions stay with the context they describe, above the scratchpad; the short
date uses the 12px metadata scale and its precise value is available on focus.
The asks view opens in a workspace dialog and uses the auxiliary-page grammar: a
simple title, sections, and a 760px reading width. Its sidebar entry sits above
the thread list; the dialog fills the screen on narrow layouts. Its dimensions
are declared once in `web/asks.css`.
A conversation's own asks sit in a strip between the conversation and its
composer, with the request area's insets. One line states how many wait; it folds
the strip. One ask is shown in full and the others as their title and meta line.
The strip takes at most 40% of the window height (55% of the conversation on a
phone) and scrolls within that. It is absent when the conversation has no open
asks: nothing about asks sits above the composer then. The conversation's
resolved asks are a section of its side panel, in the panel's own type.
Scheduled prompt actions wrap on desktop and use two columns on narrow screens.
Their `--schedule-gap` (8px) and `--schedule-action-height` (44px) tokens live in
`web/styles.css`; the form's action row stays visible as its body scrolls.
`--schedule-form-inset` (100px) reserves the existing dialog margin, padding and borders.

## Elevation & Depth
Content precedes controls, popovers, dialogs, and notices. Reuse native dialogs for
deliberate destructive confirmation. Avoid nested cards and unnecessary shadows.

## Shapes
Use the existing 6px control and 10px panel radii, one-pixel dividers, and simple
stroke icons. Auxiliary report sections use whitespace rather than card outlines.

## Components
| Component | Use |
| --- | --- |
| Permission menu | Provider-supplied session modes as checked menu choices; unavailable native choices remain identified; opening never changes a selection |
| Composer controls | Short prompts with shortcut help on desktop focus and in the accessible description; arrow send and calendar schedule retain accessible names and touch targets |
| Scheduled prompts | A small clock in the chat header appears when the thread has upcoming prompts and opens its scheduled-prompt details in the existing dialog. Repeat is an attribute of a scheduled prompt; reuse native labelled selects and inputs, existing result cards, and quiet Edit, Pause, Resume and Cancel actions |
| Thread group label | Group beside the workspace path opens the existing group assignment editor |
| Quiet button | Refresh, dismiss, secondary actions |
| Primary button | The view's principal action |
| Labelled native inputs | Dates, filters, grouping |
| Table with row links | Comparable records and drilldown |
| Inline status/error | Loading, coverage, retry feedback |
| Response actions | Quiet Copy button on code blocks and selection-only Discuss, preserving drafts and native reading position |
| Inline title edit | Enter or focus leaving the title saves; Escape discards; failures remain visible beside the draft |
| HTML output viewer | Existing file-link action opens a full-height preview using the auxiliary-page dialog pattern; closing preserves the conversation and draft underneath |
| Native confirmation dialog | Clear local Activity history |
| Native update action | The existing update menu stages and applies backend updates; reuse the native confirmation, progress screen and error presentation |
| Available update dialog | Native alert with Install and Skip for Now; show each staged version once per launch, keep skipped updates available from the update menu, and require consent before restarting work |
| Thread options menu | Thread actions on sidebar cards and in the conversation header; the mobile sheet lists the header actions inline |
| Thread list row | List overview: status mark, provider logo, constant-width title, summary, request label, phase and time; actions replace label and phase on hover. App agents start collapsed beneath their current supervisor at each depth; expanding a branch survives refresh. An app agent enters Attention on its own row only when it fails, stalls or needs a permission approval, and that row names it "App agent of <supervisor>"; its questions and checkpoint requests stay with its supervisor. Sections are Pinned and Quiet (each only when it holds threads), Attention, Working and Idle, with Pinned first; a pinned thread or app agent appears only in Pinned. The row menu adds Dismiss from Attention and Mark as read when they apply; live approvals and blocking questions resolve only by answering |
| Agent tree connector | Solid for delegated agents; dotted from a source conversation to its fork, using the existing connector color |
| Closed agent in the tree | A closed sub-agent folds to its provider icon at 40px with its title as the tooltip; a click or Enter opens its card in place and Minimize folds it again. The root and the selected agent always show their cards |
| Role designation | A user-named role beside a conversation's metadata, muted 10px semibold, placed like the Dispatcher designation |
| Stopped conversations | Quiet status-bar count of the user's Stops; a native dialog lists each with its supervising agent, all checked, before Release |
| Asks view | Sidebar entry with an open count, red when an ask is blocking. Rows lead with the title; kind, effort, age, asking thread, state and the link follow as muted meta. Every row has a reply field in view with Send reply beside it; the kind's resolutions lead the next row (a decision's options and Answer with my text; Mark done and Not done, and so on), with Reply in conversation, Snooze and Dismiss after them as quiet text controls. The arrangement is the same for every kind. Enter sends the reply and never picks a resolution; once text is typed a muted line says what each control does with it. A replied ask shows the last reply and its time as a muted line. Red marks only the Blocking group and the sidebar count; overdue and stale use the attention colour, and resolved outcomes stay muted. Groups use plain headings and one-pixel dividers, not cards. The view's name and wording live only in the copy block of `web/asks.js` |
| Conversation asks | The asks view's rows above the composer, without the thread name: open asks only, with the Blocking flag in red on the row. Unfolded while an ask waits; the user's fold is kept per conversation. One ask shows in full, the first in order unless the user opens another by its title; the rest show title and meta. Folded, the summary line carries the blocking count and the first ask's title. No card, no heading: dividers and the summary line only |
| Resolved asks in the side panel | A section of the thread context panel after the scratchpad: the panel's heading, then each resolution as its title, a muted outcome line (resolution, kind, time) and its answer or note. The ten most recent; a muted line says when earlier ones are in the asks view. Absent when the conversation has none |
| Navigation toggle count | The sidebar entry's count pill on the corner of the navigation toggle, ringed in the paper colour, only while the navigation is closed (a phone, or a collapsed sidebar); red when an ask is blocking. The toggle's name and action do not change |
| Agent Coord system row | A runtime-generated prompt (message delivery, subscription event, inbox wake, automatic continuation) in a conversation: one muted row in the tool-row grammar with an icon, "Agent Coord message from <sender>" or the prompt's title, its kind and time. Mouse hover or a tap opens a popover with the readable details; the exact prompt is a closed disclosure inside it. A prompt whose messages already have cards is not shown; each card keeps it as a closed Delivered prompt disclosure |
| Title band | Mac workspace windows: native window controls centered on the project tab labels; the sidebar, a collapsed header and the compact header start clear of them |

## Do's and Don'ts
Never use UI eyebrow labels. Do not invent percentages or time-spent estimates
from visits. Show missing data as unknown, not zero. Keep focus visible, text
wrappable, light/dark states legible, and controls usable by keyboard and touch.
Do not add global style overrides for a local report.

## Conceptual model
A thread is the durable conversation. Projects, domains and groups organize
threads. Activity is a retrospective view of recorded interactions with those
threads: messages, visits, returns and answered decisions. It is not a productivity
score. A visit is a foreground interaction or return; background restores and
agent output are not human visits. An unfinished visit is not a no-message visit.
Restored foreground exposures are raw system observations; deliberate interaction
starts a separate human visit. Background restoration creates neither.
Filtering and grouping change the view; reviewing creates an ordinary conversation
with a fixed snapshot of the selected period. Clearing history affects the local
activity ledger, not the conversations.
An ask is an item an agent addresses to the user: a decision, action, approval,
review or request. The user resolves an ask in the asks view or in its own
conversation; resolving it tells the asking thread. The conversation shows a
second view of the same asks, not another object. An ask belongs to its thread
but is not a thread state.
Quiet is a saved attribute of a thread, changed from the thread options menu.
A quiet thread does not notify or enter Attention for ordinary results. It does
for failures, stalls, approvals, blocking questions and checkpoints that need the
user. Otherwise it is listed in Working while it runs and in Quiet afterwards.
A reply is the user's own words about an open ask. It is a message in the asking
conversation that names the ask, not a resolution: the ask stays open and shows
the last reply. Text typed on an ask is a reply when sent alone and the note when
a resolution is chosen.

## Motion and states
Reuse the existing control transitions and honor reduced motion. Loading and
errors use honest inline copy. An empty filtered period explains the lack of
records and leaves filters available. Review actions stay disabled until a
successful query establishes a cutoff.

## Signature details
Neutral paper and ink, restrained cobalt links/actions, thin separators, compact
system typography, and a dark workspace sidebar are the existing signatures.

## Decision log
| Date | Decision | Reason |
| --- | --- | --- |
| 2026-11-25 | Approved chat composition: aligned reading and composing, quiet task chrome, compact reference context | User approved integration after reviewing the alternatives. Narrow phones retain title space and allocate a layout row for alerts. |
| 2026-11-24 | A Pinned section leads the inbox; inbox row menus offer Dismiss from Attention and Mark as read; a phone opens a conversation on the conversation, not the agent tree | The owner expected pins at the top regardless of state, had no way to dismiss or read from an inbox row, and had to close the tree sheet to read a thread with agents |
| 2026-11-24 | The tab dot and the agent tree badge share one needs-you derivation built on the inbox's request rule; closed tree children fold to icons | Dismissing a request left the tree badge and the parent's tab dot orange, and a parent showed orange while working, because three copies derived attention differently |
| 2026-11-24 | Runtime-generated prompts render as Agent Coord system rows with a popover, or fold into the matching message card | Deliveries and subscription events appeared as user bubbles of raw JSON beside their own cards; they are not the user's words |
| 2026-11-22 | Upcoming chat prompts use a clock in the header and the existing details dialog | Keep the conversation readable while preserving prompt details and scheduling actions |
| 2026-11-22 | Every ask has a reply field in view; resolved asks leave the conversation for its side panel | The owner had to copy an ask by hand to say something its buttons did not offer, and wanted no ask history above the composer. The earlier Add note control hid the only free text behind a click. |
| 2026-11-22 | A conversation shows its own asks above its composer, and the navigation toggle carries the open count while the navigation is closed | The owner could not see a thread's asks in the thread, and on a phone the count was inside the drawer. Both are views of existing asks built from the asks view's rows and count, in the existing `asks.js` and `asks.css`. |
| 2026-11-22 | Asks get a primary sidebar entry and a view that replaces the Decisions dialog | The dialog behind the question-mark button went unnoticed. Asks now have kinds and in-place resolutions. The view opens in a workspace dialog with the auxiliary-page grammar, and ships in the existing `asks.js` and `asks.css` so an older backend can serve it before its update is applied. |
| 2026-11-24 | Quiet threads get their own List section; app agents reach Attention only on failure, stall or approval, named by supervisor | Background threads (scheduled writers, app agents) stop competing with work that needs the user, without hiding what goes wrong. |
| 2026-11-21 | List inbox starts app-agent families collapsed at every depth | Keep the inbox compact by default while preserving explicit expansion and descendant attention counts. |
| 2026-11-21 | Mobile conversations use a single header and a two-row composer; secondary actions move into thread options | Return reading space from repeated navigation and desktop instructions while preserving the existing actions. |
| 2026-11-12 | List inbox shows app agents beneath their immediate supervisor, expanded by default | Keep parent-child context visible and bring descendant requests to Attention without duplicate rows. |
| 2026-11-08 | Offer available updates through a native Install / Skip for Now dialog | Make updates discoverable while keeping installation explicit and deferral temporary. |
| 2026-11-04 | Apply staged backends through the existing native update menu | One operation owns restart, rollback and guarded continuation, with no new settings surface. |
| 2026-10-28 | Document existing tokens and standalone page grammar | Preserve the current design while adding user-approved Activity. |
| 2026-10-28 | Activity uses shared queries and a shell-owned visit recorder | Comparable reports without duplicated pane counts or inferred attention time. |
| 2026-10-29 | Browser HTML links open an auxiliary output viewer | Let paired phones read agent output using existing typography, colors, and quiet controls. |
| 2026-10-29 | Add a List overview with Attention, Working and Idle rows | Re-entry: replies leave Attention once read, requests stay until resolved, and rows follow the user's own last message. |
| 2026-10-29 | Conversation header lists thread actions in one menu; a main window's title names its active project | Actions remain available in expanded chat, and the thread title already appears in its tab and heading. |
| 2026-10-29 | Direct response actions and title autosave | Reduce friction while preserving drafts, selection and reading position. |
| 2026-10-29 | Simplify composer controls and expose native permission choices | Remove redundant chrome while preserving shortcuts, actual provider modes and group persistence. |
| 2026-10-29 | List overview uses the shared group tabs and Filters | Remove duplicate group navigation; ungrouped threads remain available through the No group filter. |
| 2026-10-30 | Chat agent trees include fork lineage with dotted connectors | Show shared conversation origins without implying supervision ownership. |
| 2026-10-30 | Agents group under their current supervisor; roles and Stop release use existing menu, designation and dialog patterns | Replace a lead without restarting its agents, and release many Stops in one action while supervisors decide what restarts. |
| 2026-10-31 | Scheduled prompts include recurrence and quiet lifecycle actions in their existing form and list | Run recurring jobs without introducing another navigation surface or object. |
| 2026-11-02 | External Codex threads use the existing conversation view with a read-only notice; Continue in browser verifies ownership when requested | Reading saved history does not require execution ownership or an ended-session hook. |
| 2026-11-09 | Mac workspace windows draw the project tabs in the title bar instead of a separate row | The native title only repeated the active project; tabs keep each project's attention badges visible while returning about 40px to the work. |
