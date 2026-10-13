# Ribbon Field · 100 games

A 59-second, square, music-backed animated illustration of the arcade idea from the Orchestrator demo brainstorm: one request launches one hundred game agents across four model providers, every chat is closed, and one later request wakes the same three agents with their context intact. It keeps the editorial language of the earlier films (real logo, subtle colors, diagram rather than product interface) and adds a live wall of one hundred animated game tiles, lens close-ups, and an original beat-synced soundtrack.

Everything on screen is simulated. No provider is started, no game exists, nothing is deployed, and the coordination database is never touched. Activity is condensed rather than presented as a timing benchmark. The persistent footer says so.

## What is real and what is illustrative

- Ribbon Field app agents run on Codex (OpenAI models), Claude Code (Anthropic models), Google Antigravity (Gemini models), and Kimi Code (Kimi models). A dispatcher conversation can create app agents on any of these providers, so the OpenAI dispatcher launching a mixed team reflects the product's provider support.
- Agents messaging each other, declared write scopes that stop overlapping edits, closed conversations that keep their history and reopen when routed new work, and follow-ups delivered to the agent that retains the context follow the installed agent-coordination and dispatch skills.
- One hundred simultaneous game agents, the weird game titles, test counts, the arcade launcher, and the deploy are a story, not a recording. No speed or cost gains are claimed.

The logo is an unchanged copy of the real committed app icon at `plugins/agent-coord/scripts/agent_coord/web/app-icons/icon-512.png`.

## Reproduce

From the repository root, with the existing `demo/` Node dependencies and ffmpeg installed:

```sh
sh demo/hundred-games/build.sh                      # silent frames → soundtrack from out/cues.json → muxed MP4
node demo/hundred-games/validate.js compose         # bounds, overflow, story-state checks, poster, phone previews (bundled Chromium)
node demo/hundred-games/validate.js playback        # seeks, playback, and audio decode of the MP4 in installed Google Chrome
node demo/render.js --composition demo/hundred-games/index.html --out demo/hundred-games/out --width 1080 --height 1080 --stills 1 --to 58
```

Open `player.html` to watch. The shareable file is `out/ribbon-field-hundred-games.mp4`: 1080 × 1080, 30 fps, H.264 limited-range BT.709 yuv420p with a 48 kHz AAC stereo track and fast-start metadata. The composition is deterministic: `window.__render(seconds)` supports arbitrary seeking without live work, `window.__sceneState` exposes the story state, and `window.__cues()` exports the sound cues (landing pops, build ticks, whooshes, risers, impacts, section marks) that `music.js` turns into the soundtrack. The music is generated procedurally in pure Node at 120 BPM, so every bar lands on an even second and section changes line up with the story beats.

Artifacts: `out/poster.png`, `out/phone-check-*.png` (390-pixel previews), `out/stills/`, `out/waveform.png` and `out/spectrum.png` (soundtrack structure), `out/composition-checks.json`, `out/playback-checks.json`, `out/verification.json`, and `out/encoded-frame.png` with its PSNR against the matching still. `icloud-share.swift` prints a public iCloud Drive link for a copy placed in `~/Library/Mobile Documents/com~apple~CloudDocs/Ribbon Field/`.

## Editorial structure

| Time | Beat | Music |
| --- | --- | --- |
| 0–4 s | “Build an arcade with 100 one-screen games. Give each game its own agent. Make them weird.” reaches your OpenAI dispatcher. | Intro pad, riser |
| 4–12 s | One chat becomes a swarm: 100 packets burst into a 10 × 10 wall of agents, 25 per provider. | Impact, groove, pentatonic landing cascade |
| 12–20 s | A hundred weird games built in parallel; close-ups on Pigeon Bread Heist (Anthropic) and Parallel Parking on Mars (Gemini). | Build ticks, accent at 100/100 |
| 20.5–26 s | Spreadsheet Boss Fight (Kimi) and Escape the Meeting (OpenAI) reach for launcher.js and settle it agent to agent. | Groove, resolution chime |
| 26.6–30.6 s | Arcade live. The result is reported back to your conversation. | Impact |
| 31–36 s | Now close every chat. A wave closes all 100; the work stays. | Breakdown, riser |
| 36–51.4 s | “Make Pigeon Bread Heist multiplayer, give Mars Parking moon gravity, and make Spreadsheet Boss Fight playable on my phone.” The right three agents wake up with their context; one direct follow-up lands in a reopened chat. | Impact, groove, three wake pops |
| 51.8–59 s | “Keep the context. Lose the chat management.” | Final chord, outro |
