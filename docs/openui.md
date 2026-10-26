# OpenUI in thread responses

Agents can include interactive OpenUI cards, charts, tables, and forms directly
in assistant messages. Use a complete fenced `openui` or `openui-lang` block.
All RF providers share the same renderer and receive the authoring guide through
their coordination bootstrap. The guide ships at
`plugins/agent-coord/skills/agent-coordination/references/openui.md`.

```openui
root = Card([title, choices])
title = CardHeader("What should we explore?")
choices = Buttons([signups, retention])
signups = Button("Signups", @ToAssistant("Explore signups"))
retention = Button("Retention", @ToAssistant("Explore retention"), "secondary")
```

Actions become ordinary user messages in the originating thread. An older
card's response queues while the agent works; dispatcher cards address their
dispatcher directly. The composer draft is preserved. Closed threads and
cards in unfinished turns cannot submit. Source remains available for copying
or diagnosing output that does not render.

The integration vendors the official browser bundle and styles at version
0.2.1, loaded only when an OpenUI fence appears. It needs no additional model
service, API key, CDN request, npm install, or frontend build. See
`web/vendor/openui/README.md` for provenance and checksums.

Each preview runs in an opaque-origin iframe with scripts enabled but no
same-origin access, navigation, forms, popups, or tool provider. A dedicated
MessageChannel carries only rendering state, height, diagnostics, and explicit
UI actions. The frame's CSP blocks network connections and external assets;
the app's normal CSP remains unchanged. Raw HTML and other code fences remain
escaped text. OpenUI source is limited to 256 Ki characters and action payloads
to 32 Ki characters. Frames are retained across normal timeline refreshes;
unsent form input survives those refreshes but is not persisted across reloads.

The preview CSP names RF's validated server origin explicitly for scripts and
styles. Do not change those directives to `'self'`: WebKit resolves that source
to the sandbox's opaque origin and blocks the renderer. The preview retains
`sandbox allow-scripts`, no same-origin access, and `connect-src 'none'`.

After changing the integration, run the Python and JavaScript suites, refresh
both installed plugins using `docs/reference.md`, and use a fresh runtime for
browser testing. An already-running native RF app must load the updated backend
and web assets to render OpenUI; refreshing plugins alone does not replace it.

Run `python3 desktop/macos/openui_smoke.py` for the native WebKit regression
check. It uses an isolated temporary database, serves the real frame policy and
runtime assets, and verifies rendering, sandbox isolation, and a form round
trip without starting a model or touching user conversations. Use `--scripts`
to check a refreshed installed plugin's scripts directory.
