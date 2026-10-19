# Ribbon Field · Hook A: The Repo Collision / Overwrite Angle (`?hook=collision`)

A 45-second, square (`1080 × 1080`), music-backed animated illustration built in the minimalist editorial diagram style of `demo/hundred-games/`.

## Angle & Hypothesis

- **Hook**: `?hook=collision`
- **Core Pain**: Running two agents in the same repo causes silent file stomping, broken tests, and git index lock wars without spinning up heavyweight git worktrees for every tiny fix.
- **Headline**: Stop Claude Code and Codex from overwriting each other's files.
- **Subhead**: Lease-based write scopes and file-conflict locks for parallel coding agents—in one workspace, without worktree sprawl.
- **Falsifiable Hypothesis**: Developers already running 2+ agents concurrently feel acute pain from file collisions and will install specifically for `begin-work` scope locking.

## Reproduce

```sh
sh demo/collision/build.sh
node demo/hooks/validate.js compose
node demo/hooks/validate.js playback
```

Output: `out/ribbon-field-collision.mp4` (`1080 × 1080`, 30 fps, H.264 + 48 kHz AAC stereo soundtrack). Open `player.html` to watch.
