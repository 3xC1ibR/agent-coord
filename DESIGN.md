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
This contract records the existing interface; it does not authorize a redesign.

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
The composer has a short touch-oriented prompt above one row of attachment, model,
effort and send controls. Send work, Review and Schedule live in the existing
thread options sheet; reuse their original controls and handlers. Keep 44px touch
targets and a visible send action as drafts grow or the keyboard opens. Mobile
dimensions are declared once in `web/styles.css`.
Jump to latest is a floating, arrow-only 44px button above the composer. Its
anchor never reserves a layout row; retain its accessible name and tooltip.
The asks view is an auxiliary page with a 760px reading width. Its sidebar entry sits
above the thread list; the page opens in the same dialog frame as Usage and Activity
and fills the screen on narrow layouts. Its dimensions are declared once in
`web/asks-page.css` and `web/asks.css`.
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
| Composer controls | Shortcut help in the state-aware placeholder; arrow send and calendar schedule retain accessible names and touch targets |
| Scheduled prompts | Repeat is an attribute of a scheduled prompt; reuse native labelled selects and inputs, existing result cards, and quiet Edit, Pause, Resume and Cancel actions |
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
| Thread list row | List overview: status mark, provider logo, constant-width title, summary, request label, phase and time; actions replace label and phase on hover. App agents start expanded beneath their current supervisor at each depth; a descendant request puts the family in Attention once |
| Agent tree connector | Solid for delegated agents; dotted from a source conversation to its fork, using the existing connector color |
| Role designation | A user-named role beside a conversation's metadata, muted 10px semibold, placed like the Dispatcher designation |
| Stopped conversations | Quiet status-bar count of the user's Stops; a native dialog lists each with its supervising agent, all checked, before Release |
| Asks view | Sidebar entry with an open count, red when an ask is blocking. Rows lead with the title; kind, effort, age, asking thread, state and the link follow as muted meta; the kind's resolution controls lead one row, with Add note, Snooze and Dismiss after them as quiet text controls. Red marks only the Blocking group and the sidebar count; overdue and stale use the attention colour, and resolved outcomes stay muted. Groups use plain headings and one-pixel dividers, not cards. The view's name and wording live only in `web/asks-copy.js` |
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
review or request. The user resolves an ask in the asks view; resolving it tells
the asking thread. An ask belongs to its thread but is not a thread state.

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
| 2026-11-22 | Asks get a primary sidebar entry and an auxiliary page that replaces the Decisions dialog | The dialog behind the question-mark button went unnoticed; asks now have kinds and in-place resolutions, and reuse the Usage and Activity page pattern instead of a new surface type. |
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
