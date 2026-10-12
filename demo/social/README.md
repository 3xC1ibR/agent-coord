# Ribbon Field social explainer

A 39-second, square animated illustration of persistent app agents. Designed to explain the workflow without implying that the diagram is the product interface. Silent-first, with no voiceover or dependency on sound.

The six requests are simulated. Three new ideas create three agents; three follow-ups route to those same agents. No real providers, backend sessions, sample applications, or deployment are involved. Work is condensed rather than presented as an actual timing benchmark.

The logo is an unchanged copy of the real committed app icon at `plugins/agent-coord/scripts/agent_coord/web/app-icons/icon-512.png`. There are no invented product controls, signup URLs, or shipping claims. Symbols represent agent specialties rather than generated applications.

## Reproduce

From the repository root, with the existing demo Node dependencies and ffmpeg installed:

```sh
node demo/render.js --composition demo/social/index.html --out demo/social/out --width 1080 --height 1080 --stills 3 --to 38
node demo/render.js --composition demo/social/index.html --out demo/social/out --width 1080 --height 1080 --fps 30 --silent --name ribbon-field-explainer.mp4
```

Open `player.html` to watch. The shareable file is `out/ribbon-field-explainer.mp4`. The film is 1080 × 1080, 30 fps, H.264, limited-range BT.709 yuv420p, with fast-start metadata. The frame counter is deterministic: `window.__render(seconds)` supports arbitrary seeking without live work.

The thumbnail is `out/poster.png`; the 390-pixel phone preview is `out/phone-check.png`. Composition, codec, and browser playback evidence lives in `out/verification.json`, `out/composition-checks.json`, and `out/playback-checks.json`. The original UI demonstration and conceptual study remain separate under `demo/dispatch/` and `demo/concept/`.

## Editorial structure

- 0–14.6 seconds: one conversation receives three ideas and creates Chat, Snake, and Pong agents.
- 14.7–17.7 seconds: the agents work in parallel and keep their own conversations.
- 17.8–34.5 seconds: follow-ups return to the same agents, which retain their identities and context.
- 34.5–39 seconds: real brand lockup and the promise, “Start something. Keep it moving.”

The persistent footer identifies the film as a workflow illustration with condensed activity. The primary text remains large enough to read when the square is viewed at phone-feed width.
