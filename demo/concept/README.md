# Ideas in motion

A 42-second conceptual companion to the real-UI capture in `../dispatch/`.
This version isolates the workflow: one composer, three persistent agent cards,
and a visible route from each request to the agent that owns its context.

The previews are motion graphics, not functioning applications or recorded agent
output. The film is labeled as a concept animation with simulated activity.
Nothing starts a provider, accesses the real coordination database, or builds
the sample apps. The first three requests introduce Chat, Snake, and Pong; the
next three return to those same cards and illustrate swipe controls, a dark
theme, and a rematch button.

From the repository root, using the installed `demo/` dependencies:

```sh
node demo/render.js --composition demo/concept/index.html --out demo/concept/out --stills 3 --to 41
node demo/render.js --composition demo/concept/index.html --out demo/concept/out --fps 30 --silent --name ribbon-field-concept.mp4
```

Open `player.html` for video controls. The standalone composition is also
scrubbable via `window.__render(seconds)`; every frame is derived from time.

| Time | Beat |
| --- | --- |
| 0–15s | Three separate prompts introduce three app agents. |
| 15–19s | Their mini-previews keep moving in parallel. |
| 19–25s | The phone request routes to Snake; its board becomes a phone. |
| 25–31s | The dark-theme request routes to Chat; its preview changes palette. |
| 31–37s | The rematch request routes to Pong; a replay button appears. |
| 37–42s | All three agent identities remain, ready for the next request. |
