# Ribbon Field demo video

A fully scripted, reproducible product video built from the real UI. Nothing is
mocked in an image editor: a seeded coordination database drives the actual
Ribbon Field web app, Playwright captures its states, and a deterministic
motion-graphics composition animates those captures frame by frame.

Output: `out/ribbon-field-demo.mp4` (1920x1080, 60 fps, AAC soundtrack).

## Pipeline

| Step | Command | What it does |
| --- | --- | --- |
| 1. Seed | `python3 seed_demo.py` | Builds three small Git repos under `out/workspaces/`, then fills `out/demo.sqlite3` with projects, repositories, saved views, 19 threads in every attention state, conversation history, a delegation tree with terminal output, write scopes, and durable messages. The user's real database is never touched. |
| 2. Capture | `node capture.js` | Starts `agent-coord ui` against the demo database on port 47311 and screenshots 33 UI states at 2x into `out/shots/`, plus element bounding boxes in `boxes.json` used to aim callouts and the cursor. |
| 3. Compose + render | `node render.js` | Loads `video/index.html` in headless Chromium, steps `window.__render(t)` for every frame, pipes JPEG frames from four parallel browser contexts into ffmpeg, concatenates the segments, generates the soundtrack, and muxes. |

Useful variants:

```bash
node render.js --stills 2.5            # one PNG every 2.5 s into out/stills for review
node render.js --preview               # quick 540p24 pass
node render.js --from 20 --to 32       # render a time window
node audio.js 106 out/cues.json out/soundtrack.wav   # soundtrack only
```

Requirements: Python 3.10+, Node 22, ffmpeg, and `npm install` in this directory
(Playwright 1.55 with its bundled Chromium).

## Send it to a phone

`out/share/` holds a small landing page that plays the video inline and offers a
save button. Serve it with byte-range support (iPhone Safari needs ranges for
inline playback) and print a scannable QR code for the link:

```bash
npx http-server out/share -p 8787 -a 0.0.0.0 -c-1     # Wi-Fi and Tailscale clients
node qr.js "http://$(ipconfig getifaddr en0):8787/"    # QR code in the terminal + PNG
```

Copying the MP4 into `~/Library/Mobile Documents/com~apple~CloudDocs/` also
delivers it to the Files app on any device signed into the same iCloud account.
AirDrop has no command-line interface, so it is not scripted here.

## Structure

- `video/engine.js` — time-driven primitives: background field, macOS window frame with a camera, phone frame, cursor with click ripple, callouts, spotlight, kinetic text, typed terminal.
- `video/scenes.js` — the storyboard. Scene starts sit on beats of the 104 BPM track; every scene is a `mount` / `update(t)` pair.
- `video/styles.css` — composition styles. No CSS transitions or animations anywhere, so rendering is deterministic.
- `audio.js` — procedural soundtrack (pads, sub, plucks, drums, risers) plus click / pop / whoosh / impact cues exported by the composition.

## Storyboard

| Time | Scene |
| --- | --- |
| 0:00 | Twelve chaotic agent panes. "Which one needs you?" |
| 0:07 | Logo reveal. |
| 0:13 | Overview: attention queue above the work stages, a working thread's live border. |
| 0:22 | Attention deep dive: Blocked, Review requested, Update, Findings ready, Done. |
| 0:34 | Checkpoints: phase, next step and owner, links, history, then the full conversation. |
| 0:44 | Codex and Claude Code model picker, slash commands, send / queue / steer. |
| 0:54 | Tiled threads. |
| 1:00 | Saved views by project, repository, and attention. |
| 1:07 | Now / Later / Closed and Roll up. |
| 1:14 | Coordination: write scopes, conflict detection, delegation to an owned PTY, atomic handoff, the monitor. |
| 1:29 | Native app, iPhone over Tailscale, notifications, dark mode, shortcuts. |
| 1:35 | One Python CLI, SQLite, hooks; install commands. |
| 1:41 | Outro. |
