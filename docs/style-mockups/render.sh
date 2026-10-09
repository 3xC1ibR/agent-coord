#!/bin/zsh
# Render every direction in light and dark for both pages, then build contact sheets.
set -e
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DIRS=(${=${@:-current ochre cobalt oxblood mono}})
mkdir -p out
for dir in $DIRS; do
  for page in overview conversation; do
    for theme in light dark; do
      "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
        --window-size=1440,900 --virtual-time-budget=1500 \
        --screenshot="out/$dir-$page-$theme.png" \
        "file://$PWD/mockup.html?dir=$dir&page=$page&theme=$theme" >/dev/null 2>&1
    done
  done
  cat > out/sheet-$dir.html <<HTML
<!doctype html><meta charset="utf-8"><style>
body{margin:0;background:#2b2b2b;font:13px -apple-system,sans-serif;color:#ddd;padding:18px}
h1{margin:0 0 12px;font-weight:600;font-size:16px}.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}
figure{margin:0}img{display:block;width:100%;border-radius:6px;box-shadow:0 2px 10px #0008}figcaption{margin:6px 0 0;color:#aaa}
</style><h1>Direction: $dir</h1><div class="g">
<figure><img src="$dir-overview-light.png"><figcaption>Overview · light</figcaption></figure>
<figure><img src="$dir-overview-dark.png"><figcaption>Overview · dark</figcaption></figure>
<figure><img src="$dir-conversation-light.png"><figcaption>Conversation · light</figcaption></figure>
<figure><img src="$dir-conversation-dark.png"><figcaption>Conversation · dark</figcaption></figure>
</div>
HTML
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size=1600,1070 --virtual-time-budget=1500 --screenshot="out/sheet-$dir.png" \
    "file://$PWD/out/sheet-$dir.html" >/dev/null 2>&1
  echo "rendered $dir"
done
