# Ribbon Field · Auto-Wake Demo (`job start --wake-self`)

A 30-second, square (`1080 × 1080`) animated illustration showing how `agent-coord job start --wake-self` replaces repeated `sleep 30` polling loops with a detached watcher that automatically wakes the conversation when the job finishes.

## Reproduce

```sh
sh demo/autowake/build.sh
node demo/hooks/validate.js compose
node demo/hooks/validate.js playback
```

Outputs:
- `out/ribbon-field-autowake-silent.mp4` (30s silent)
- `out/ribbon-field-autowake.mp4` (30s with 120 BPM soundtrack)
