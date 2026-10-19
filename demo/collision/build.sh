#!/bin/sh
# Render Hook A (collision): silent MP4 (out/ribbon-field-collision-silent.mp4) and music-backed MP4 (out/ribbon-field-collision.mp4).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT="$HERE/out"
DURATION=45
node "$ROOT/demo/render.js" --composition "$HERE/index.html" --out "$OUT" --width 1080 --height 1080 --fps 30 --silent --name ribbon-field-collision-silent.mp4
node "$ROOT/demo/hundred-games/music.js" "$DURATION" "$OUT/cues.json" "$OUT/soundtrack.wav"
ffmpeg -y -loglevel error -i "$OUT/video-noaudio.mp4" -i "$OUT/soundtrack.wav" -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest -movflags +faststart "$OUT/ribbon-field-collision.mp4"
echo "wrote $OUT/ribbon-field-collision-silent.mp4 and $OUT/ribbon-field-collision.mp4"
