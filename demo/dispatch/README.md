# Dispatch demo preview

A 50-second, silent, 1080p preview of the persistent app-agent workflow. Uses
the current Ribbon Field HTML, CSS, and JavaScript with scripted API responses.
No provider processes, sample app builds, real database, or live API are used.
The video is labeled **Scripted preview** throughout. It is a storyboard, not
evidence of real agent speed or completed apps.

Run from the repository root using the dependencies in `demo/package.json`:

```sh
node demo/dispatch/capture.js
node demo/render.js --composition demo/dispatch/index.html --out demo/dispatch/out --stills 5 --to 49
node demo/render.js --composition demo/dispatch/index.html --out demo/dispatch/out --fps 30 --silent --name ribbon-field-dispatch.mp4
```

Open `player.html` to watch with playback controls, or open
`out/ribbon-field-dispatch.mp4` directly. The original general-purpose demo
composition and output remain separate. Chromium and ffmpeg must be available.

The capture browser intercepts **every** request and serves product assets and
fixtures locally; it does not start the Ribbon Field backend. Unexpected
requests or frontend errors fail capture. `out/validation.json` records the
checks, including that six requests reuse exactly three app agents.

| Time | Sequence |
| --- | --- |
| 0–3s | Overview with dispatcher dock enabled. |
| 3–18s | Three prompts create Chat, Snake, and Pong app agents. |
| 18–21s | All three agents are working. |
| 21–39s | Swipe controls, a dark theme, and rematches route to the existing agents. |
| 39–45s | Open Snake and see the initial task and follow-up together. |
| 45–50s | Return to the overview with the same three agents working. |

Edit the scripted responses in `fixture.js`; edit timing and presentation in
`scenes.js`. `capture.js` uses the actual view controls and grouping preferences.
The video composition adds typing, cursor motion, and editorial highlights.
No synthetic game output or claim of successful implementation is shown.
