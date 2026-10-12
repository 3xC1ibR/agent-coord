# Ribbon Field orchestration explainer

A 49-second, square animated illustration of one dispatcher coordinating a team of app agents across model providers. It follows the 39-second social explainer in `../social/` and keeps its editorial language: the real logo, subtle colors, a routing diagram, and no imitation of the product interface. Silent-first, with no voiceover or dependency on sound.

Everything on screen is simulated. One request creates four agents, the agents coordinate with each other, one result is reported back, and two follow-ups return to the same agents. No provider is started, no checkout flow exists, nothing is deployed, and the coordination database is never touched. Activity is condensed rather than presented as a timing benchmark.

## What is real and what is illustrative

- Ribbon Field app agents run on Codex (OpenAI models), Claude Code (Anthropic models), and Google Antigravity (Gemini models). A dispatcher conversation can create app agents on any of these providers and choose their model, so the OpenAI dispatcher launching Gemini, Anthropic, and OpenAI agents reflects shipped behavior.
- **Kimi is illustrative.** No Kimi provider exists in Ribbon Field. The chip reads “Kimi · illustrative” and the persistent footer says “Kimi shown for illustration”.
- Agents messaging each other, declared write scopes that stop overlapping edits, a dedicated release agent, follow-ups routed to the agent that retains context, and the orchestrator reporting the integrated result back follow the installed agent-coordination, dispatch, and orchestrate skills.
- The short-lived subagent beat shows the difference between persistent app agents and provider-native subagents: subagents live inside one agent’s conversation and disappear when they finish; app agents stay addressable.
- No measured speed or cost gains are claimed. The checkout flow, Apple Pay follow-up, test counts, and deploy are a story, not a recording.

The logo is an unchanged copy of the real committed app icon at `plugins/agent-coord/scripts/agent_coord/web/app-icons/icon-512.png` (identical SHA-256 to `../social/assets/ribbon-field.png`).

## Reproduce

From the repository root, with the existing `demo/` Node dependencies and ffmpeg installed:

```sh
node demo/render.js --composition demo/orchestration/index.html --out demo/orchestration/out --width 1080 --height 1080 --stills 1 --to 48
node demo/render.js --composition demo/orchestration/index.html --out demo/orchestration/out --width 1080 --height 1080 --fps 30 --silent --name ribbon-field-orchestration.mp4
node demo/orchestration/validate.js compose      # bounds, overflow, poster, phone previews (bundled Chromium)
node demo/orchestration/validate.js playback     # seeks and playback of the encoded MP4 in installed Google Chrome
```

Open `player.html` to watch. The shareable file is `out/ribbon-field-orchestration.mp4`: 1080 × 1080, 30 fps, H.264, limited-range BT.709 yuv420p, fast-start metadata. The composition is deterministic: `window.__render(seconds)` supports arbitrary seeking without live work, and `window.__sceneState` exposes the visible story state for validation.

Artifacts: `out/poster.png` (thumbnail), `out/phone-check-*.png` (390-pixel previews), `out/stills/` (one frame per second), `out/composition-checks.json`, `out/playback-checks.json`, `out/verification.json` (codec, decode, hash, iCloud copy and share link), and `out/encoded-frame.png` with its PSNR against the matching still.

To share through iCloud Drive, copy the MP4 into `~/Library/Mobile Documents/com~apple~CloudDocs/Ribbon Field/`, wait for the upload, then run `swift demo/orchestration/icloud-share.swift "<that path>"` to print the public link.

## Editorial structure

| Time | Beat |
| --- | --- |
| 0–4.4 s | One request: “Ship the new checkout flow by Friday.” reaches your dispatcher, which runs on an OpenAI model. |
| 4.5–11.1 s | The dispatcher launches four agents: Checkout UI (Gemini), Payments API (Anthropic), Test suite (Kimi, illustrative), Release (OpenAI). |
| 11.2–16.6 s | Two agents reach for the same file. They settle it between themselves: the API owns the order schema, the UI uses its field names. |
| 16.9–21.7 s | The API agent spins up three short-lived subagents inside its own conversation. They vanish; the app agents stay. |
| 19.6–25.2 s | Tests pass, Release waits for all three, deploys, and reports back to the dispatcher. |
| 25.9–29.9 s | One coherent result: “Checkout flow is live.” |
| 30.0–38.4 s | “Add Apple Pay to checkout.” returns to the Payments API agent with its context intact; it tells the UI agent about the new button. |
| 38.6–44.0 s | “Roll the next release out gradually.” returns to the Release agent, which remembers the deploy. |
| 44.2–49 s | Brand lockup: “One dispatcher. Many models. One team.” |

The persistent footer identifies the film as a workflow illustration with condensed activity and names Kimi as illustrative. Primary text stays large enough to read at phone-feed width.
