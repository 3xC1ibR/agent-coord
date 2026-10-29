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
and pane regions. Auxiliary pages have a simple title, controls, and centered
content. Activity uses a 1120px content maximum, 24px page spacing, 12px row spacing,
and 8px control gaps. Declare additional module dimensions once as CSS properties.
Narrow layouts stack controls and keep tables in labelled horizontal scroll areas.

## Elevation & Depth
Content precedes controls, popovers, dialogs, and notices. Reuse native dialogs for
deliberate destructive confirmation. Avoid nested cards and unnecessary shadows.

## Shapes
Use the existing 6px control and 10px panel radii, one-pixel dividers, and simple
stroke icons. Auxiliary report sections use whitespace rather than card outlines.

## Components
| Component | Use |
| --- | --- |
| Quiet button | Refresh, dismiss, secondary actions |
| Primary button | The view's principal action |
| Labelled native inputs | Dates, filters, grouping |
| Table with row links | Comparable records and drilldown |
| Inline status/error | Loading, coverage, retry feedback |
| HTML output viewer | Existing file-link action opens a full-height preview using the auxiliary-page dialog pattern; closing preserves the conversation and draft underneath |
| Native confirmation dialog | Clear local Activity history |
| Thread options menu | Thread actions on sidebar cards and in the conversation header; the mobile sheet lists the header actions inline |
| Thread list row | List overview: status mark, provider logo, constant-width title, summary, request label, phase and time; actions replace label and phase on hover |

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
| 2026-10-28 | Document existing tokens and standalone page grammar | Preserve the current design while adding user-approved Activity. |
| 2026-10-28 | Activity uses shared queries and a shell-owned visit recorder | Comparable reports without duplicated pane counts or inferred attention time. |
| 2026-10-29 | Browser HTML links open an auxiliary output viewer | Let paired phones read agent output using existing typography, colors, and quiet controls. |
| 2026-10-29 | Add a List overview with Attention, Working and Idle rows | Re-entry: replies leave Attention once read, requests stay until resolved, and rows follow the user's own last message. |
| 2026-10-29 | Conversation header lists thread actions in one menu; a main window's title names its active project | Actions remain available in expanded chat, and the thread title already appears in its tab and heading. |
