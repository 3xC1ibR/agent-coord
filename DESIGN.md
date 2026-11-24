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
  xs: {fontFamily: system-ui, fontSize: 11px}
  sm: {fontFamily: system-ui, fontSize: 12px}
  md: {fontFamily: system-ui, fontSize: 13px}
  body: {fontFamily: system-ui, fontSize: 14px, lineHeight: 21px}
  lg: {fontFamily: system-ui, fontSize: 16px}
  heading: {fontFamily: system-ui, fontSize: 20px, fontWeight: 600}
  display: {fontFamily: system-ui, fontSize: 24px, fontWeight: 600}
rounded:
  inline: 4px
  control: 6px
  panel: 10px
  pill: 999px
spacing:
  space-1: 4px
  space-2: 8px
  space-3: 12px
  space-4: 16px
  space-5: 20px
  space-6: 24px
  space-8: 32px
  small-control: 32px
  medium-control: 36px
  touch-target: 44px
---

## Overview
Ribbon Field is a persistent workspace for conversations and coordinated agent work.
Its quiet, dense workspace keeps the work prominent. Auxiliary pages such as Usage
and Activity use the same paper, ink, controls and type with more reading room.
This contract records the existing interface; it does not authorize a redesign.

## Colors
`web/tokens.css` defines the design scales and semantic colors. `web/theme.css`
overrides their color values for dark mode. Cobalt indicates actions and links,
orange marks attention, red marks blocking states and failures, and green marks
unread replies and success. Data displays derive their tints from these roles.
Sidebar colors stay on the dark palette in both themes.

## Typography
Use `--font-sans` for interface text and `--font-mono` for code and compact
technical values. Use the seven `--text-*` sizes from 11px to 24px. View titles use
20px semibold. Titles lead, supporting copy uses muted ink, and numeric
comparisons use tabular figures. Use sentence case and explicit labels.

## Layout
The workspace retains its existing sidebar, projects bar, conversation tabs,
views bar, workspace tools and overview status bar. The overview opens in List.
Focus, Atlas and Dispatcher Workspace remain available. The sidebar's Asks entry
opens the Asks view. Send work names direct delegation, and Dispatcher names
the autonomous project dispatcher.
The workspace has an overview or conversation and optional context and pane regions. In the Mac app, a workspace window's header is its title bar: a
40px band holds the window controls, project tabs and header actions, and its empty
space moves the window. Settings and detached conversation windows keep the
standard title bar. Auxiliary pages have a simple title, controls, and centered
content. Activity uses a 1120px content maximum, 24px page spacing, 12px row spacing,
and 8px control gaps. Declare shared geometry in `web/tokens.css`. Use the grid
for spacing and stroke widths for fine insets and rings. Intrinsic layout may
use percentages and viewport units. Transcript spacing uses em units where it
follows the text. Media-query breakpoints are written in the query because CSS
custom properties resolve inside declarations.
Narrow layouts stack controls and keep tables in labelled horizontal scroll areas.
At 720px and below, a mobile conversation has one 56px header with Back, a
single ellipsized title, status and thread options. Workspace and conversation
tabs return in the overview. The existing thread options sheet houses secondary
actions, including Send work, Review and Schedule, with their original controls
and handlers. On coarse pointers the composer uses “Message agent…” and a 16px
input above attachment, model, effort and send controls. Keep 44px touch targets
and a visible send action as drafts grow or the keyboard opens. Keep Send work
and Dispatcher dock affordances in their overview and workspace surfaces.
Jump to latest is a floating, arrow-only 44px button above the composer. Its
anchor never reserves a layout row; retain its accessible name and tooltip.
The asks view opens in a workspace dialog and uses the auxiliary-page grammar: a
simple title, sections, and a 760px reading width. Its sidebar entry sits above
the thread list; the dialog fills the screen on narrow layouts. Its reading width is defined in `web/tokens.css`.
A conversation's own asks sit in a strip between the conversation and its
composer, with the request area's insets. One line states how many wait; it folds
the strip. One ask is shown in full and the others as their title and meta line.
The strip takes at most 40% of the window height (55% of the conversation on a
phone) and scrolls within that. It is absent when the conversation has no open
asks: nothing about asks sits above the composer then. The conversation's
resolved asks are a section of its side panel, in the panel's own type.
Scheduled prompt actions wrap on desktop and use two columns on narrow screens.
They use an 8px gap and 44px touch targets. The form's action row stays visible as
its body scrolls.
`--schedule-form-inset` (100px) reserves the existing dialog margin, padding and borders.

## Elevation & Depth
Content precedes sticky controls, floating controls, popovers, drawers and
dialogs. Use the named layers and the small and large shadows from the token
file. Reuse native dialogs for deliberate destructive confirmation.

## Shapes
Use 4px for inline badges and keys, 6px for controls and rows, 10px for panels
and popovers, and the pill radius for round controls and marks. Use one-pixel
dividers and simple stroke icons. Auxiliary report sections use whitespace.

## Components
| Component | Use |
| --- | --- |
| Permission menu | Provider-supplied session modes as checked menu choices; unavailable native choices remain identified; opening never changes a selection |
| Composer controls | Shortcut help in the state-aware placeholder; arrow send and calendar schedule retain accessible names and touch targets |
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
| Role designation | A user-named role beside a conversation's metadata, muted 11px semibold, placed like the Dispatcher designation |
| Stopped conversations | Quiet status-bar count of the user's Stops; a native dialog lists each with its supervising agent, all checked, before Release |
| Asks view | Sidebar entry with an open count, red for a blocking ask. Rows lead with the title, followed by muted metadata. Enter and Answer resolve the ask with typed text. Preset actions resolve with their selected option and include typed text as a note. Shift+Enter and the quiet Discuss in thread action send a comment and leave the ask open. Snooze and Dismiss are quiet actions. Red marks Blocking. Overdue and stale use the attention colour. Resolutions and the last discussion comment use muted text. Groups use headings and one-pixel dividers. Shared wording lives in the copy block of `web/asks.js` |
| Agent breadcrumb | A compact Root › Lead › Specialist path in the conversation header, with status dots and a switcher for siblings and children. The workspace tab keeps the root task name while the user switches family members. Agents and Context open on explicit selection |
| Later action | One control moves the thread to Later immediately and offers a menu for a timed reminder |
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
A thread is the durable conversation and retains its execution directory.
Workspaces and Groups can include threads across repositories and directories.
A workspace has a default launch directory and may offer a folder filter.
Opening a member or following a direct thread link keeps the workspace and its
tabs in place. A family keeps one tab named for its root supervisor task.
Activity is a retrospective view of recorded interactions with those
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
An informational reply or routine completion clears from Attention when read.
Blocking asks, live approvals, stalls and failures stay in Attention until resolved
or explicitly dismissed where allowed. Attention orders severity before recency.
A quiet send suppresses routine completion notifications.
Answering an ask closes it and sends the answer to the asking thread. A preset
resolution uses typed text as its note. Discuss in thread delivers a comment to
the asking thread and leaves the ask open.

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
| 2026-11-24 | Canonical web tokens and one mobile conversation header | Consolidate theme, type, shape, spacing and layers while keeping the existing sidebar and bars. The viewport controls header collision, and touch inputs keep readable text and full-size targets. |
| 2026-11-24 | List is the default overview, with Focus, Atlas and Dispatcher Workspace retained | Remove the duplicate Cards overview and the duplicate Asks shortcut. Reserve Dispatcher for the autonomous dispatcher and use Send work for delegation. |
| 2026-11-24 | Workspace membership spans execution directories and delegation stays in a stable root tab | Follow work across repositories and choose family members in the header while retaining the current workspace and root tab. |
| 2026-11-24 | Informational attention clears on read and critical attention stays actionable | Reading a routine reply completes acknowledgement. Blocking asks, approvals, stalls and failures retain their resolution paths. |
| 2026-11-24 | Enter answers an ask and Shift+Enter discusses it | Match the primary Answer control and keep clarification available through the quiet Discuss in thread action. |
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
