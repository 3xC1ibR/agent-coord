#!/bin/sh
# Render the 100 games film: silent frames (Playwright + ffmpeg), the procedural soundtrack, then one muxed MP4.
# Run from anywhere; requires the demo/ Node dependencies and ffmpeg.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT="$HERE/out"
DURATION=59
node "$ROOT/demo/render.js" --composition "$HERE/index.html" --out "$OUT" --width 1080 --height 1080 --fps 30 --silent --name video-silent.mp4
node "$HERE/music.js" "$DURATION" "$OUT/cues.json" "$OUT/soundtrack.wav"
ffmpeg -y -loglevel error -i "$OUT/video-noaudio.mp4" -i "$OUT/soundtrack.wav" -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest -movflags +faststart "$OUT/ribbon-field-hundred-games.mp4"
rm -f "$OUT/video-silent.mp4"
echo "wrote $OUT/ribbon-field-hundred-games.mp4"
