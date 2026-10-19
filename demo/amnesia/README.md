# Ribbon Field · Hook B: The Terminal Tab Amnesia / Triage Angle (`?hook=amnesia`)

A 45-second, square (`1080 × 1080`), music-backed animated illustration built in the minimalist editorial diagram style of `demo/hundred-games/`.

## Angle & Hypothesis

- **Hook**: `?hook=amnesia`
- **Core Pain**: Having 9 iTerm/tmux panes across 3 repos means spending 20 minutes every morning running `git status` and scrolling up to remember which agent is blocked on approval, which finished, and which hallucinated.
- **Headline**: 9 terminal tabs. 4 Claude sessions. Zero idea which agent is waiting on you.
- **Subhead**: A native macOS mission control that tracks live agent state, structured checkpoints, and exact next actions across every repo.
- **Falsifiable Hypothesis**: Cognitive overload and context-switching—not file locking—is the primary daily bottleneck; users want an inbox/attention queue (`Now / Later`, `next_step.actor=user`).

## Reproduce

```sh
sh demo/amnesia/build.sh
node demo/hooks/validate.js compose
node demo/hooks/validate.js playback
```

Output: `out/ribbon-field-amnesia.mp4` (`1080 × 1080`, 30 fps, H.264 + 48 kHz AAC stereo soundtrack). Open `player.html` to watch.
