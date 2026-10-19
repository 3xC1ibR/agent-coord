# Ribbon Field · Hook C: The Cross-Model Orchestration Angle (`?hook=delegation`)

A 45-second, square (`1080 × 1080`), music-backed animated illustration built in the minimalist editorial diagram style of `demo/hundred-games/`.

## Angle & Hypothesis

- **Hook**: `?hook=delegation`
- **Core Pain**: Manually copy-pasting plans from Codex into Claude Code (or vice versa) to get one agent to review or implement another's work.
- **Headline**: Let Codex review Claude's implementation—without copy-pasting between terminals.
- **Subhead**: Direct agent-to-agent messaging, delegation, and shared work-thread state for multi-agent engineering workflows.
- **Falsifiable Hypothesis**: Power users want cross-provider orchestration (specialist routing and inbox handoffs) rather than passive monitoring.

## Reproduce

```sh
sh demo/delegation/build.sh
node demo/hooks/validate.js compose
node demo/hooks/validate.js playback
```

Output: `out/ribbon-field-delegation.mp4` (`1080 × 1080`, 30 fps, H.264 + 48 kHz AAC stereo soundtrack). Open `player.html` to watch.
