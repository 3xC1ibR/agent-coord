# Conversation panes

Choose a saved view, adjust its filters, and click **Tile threads**. Open threads
matching those filters appear side by side inside the same workspace window.
**Ctrl+Option+Command+T** on macOS toggles tiling: press it again to return to
the single conversation or overview you were viewing before tiling. Entering
again restores the existing pane arrangement, sizes, and drafts. The native
menu and command palette call this **Toggle Tiled Threads**.
Closed threads are excluded. Tiling opens existing conversations; it does not
create or restart agent sessions.

The layout uses available width and height to choose up to six readable panes.
Additional threads appear as tabs within those panes. Click a tab to switch;
Left/Right and Home/End also work when a tab is focused.

- Drag a divider to resize adjacent panes. Focus a divider and use the arrow
  keys to resize it with the keyboard.
- **Ctrl+Option+Left/Right** moves focus between panes.
- **Ctrl+Option+Return**, or a pane's maximize button, expands the focused pane.
  Use the command again, the restore button, or Escape to restore the layout.
- The pane's **×** removes its current thread from the layout. It keeps the
  thread open and preserves its draft during this window's lifetime.
- **Overview** returns to the view's thread list. **Tile threads** explicitly
  recomputes the arrangement using the current filters and available space.

Each pane has its own composer, image attachments, scroll position, streaming
output, and approval dialogs. Switching views preserves their layouts and
conversation state for the current window. Layouts and unsent drafts are not
restored after reloading or closing the application window.

Incoming messages and status changes do not rearrange panes. Background panes
do not mark results as read; focusing a conversation does. The outer workspace
owns notifications and one shared event stream, including for overflow tabs.

## Implementation

`thread-panes.js` owns layout, tabs, focus, and conversation lifecycle. It reuses
the existing conversation UI in same-origin child documents, isolating IDs,
drafts, and dialogs while keeping one visible application window. Conversation
frames remain mounted in a fixed deck when the layout changes, because moving
an iframe to another DOM parent would reload it and discard its state.

Only the explicit conversation-pane response allows same-origin framing. Normal
workspace pages and other responses retain their framing restrictions. The
shell verifies that a child document belongs to one of its panes before
accepting its conversation API or focus updates.
