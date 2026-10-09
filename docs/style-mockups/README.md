# Style mockups

Color-direction mockups for the Ribbon Field workspace. The layout is the shipped
one: `mockup.html` links the real stylesheets from `plugins/agent-coord/scripts/agent_coord/web/`
and only adds static sample content.

- The shipped stylesheets define every color as a variable (see the `:root` block in
  `styles.css` and the dark block in `theme.css`). Cobalt is the shipped palette.
- `direction-*.css` are alternative palettes kept for comparison. Each sets only variables.
- `render.sh` renders every direction with headless Chrome into `out/` and builds a
  contact sheet per direction (`out/sheet-<direction>.png`).

Open a single mockup directly:

```
open "docs/style-mockups/mockup.html?dir=oxblood&page=conversation&theme=dark"
```

`dir=current` (the default) renders the shipped styles.
