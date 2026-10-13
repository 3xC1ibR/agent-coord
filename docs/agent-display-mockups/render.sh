#!/bin/zsh
# Render each mockup (or the ones named as arguments) in light and dark with
# headless Chrome into out/, then build a contact sheet.
cd "$(dirname "$0")" || exit 1
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCALE="${SCALE:-small}"
mkdir -p out
if [[ $# -gt 0 ]]; then FILES=("$@"); else FILES=(mockups/*.html); fi
for file in $FILES; do
  name="${${file:t}:r}"
  for theme in light dark; do
    "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
      --window-size=1440,1000 --virtual-time-budget=2500 \
      --screenshot="out/$name-$theme-$SCALE.png" \
      "file://$PWD/$file?theme=$theme&scale=$SCALE" >/dev/null 2>&1
    [[ -s "out/$name-$theme-$SCALE.png" ]] || echo "warning: no screenshot for $name ($theme, $SCALE); Chrome failed" >&2
  done
  echo "rendered $name ($SCALE)"
done
{
  echo '<!doctype html><meta charset="utf-8"><title>Agent display mockups · contact sheet</title><style>'
  echo 'body{margin:0;background:#2b2b2b;font:13px -apple-system,sans-serif;color:#ddd;padding:18px}h1{margin:0 0 12px;font-weight:600;font-size:16px}'
  echo '.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}figure{margin:0}img{display:block;width:100%;border-radius:6px;box-shadow:0 2px 10px #0008}figcaption{margin:6px 0 0;color:#aaa}'
  echo "</style><h1>Agent display mockups · scale $SCALE</h1><div class=g>"
  for file in mockups/*.html; do
    name="${${file:t}:r}"
    for theme in light dark; do
      if [[ -f out/$name-$theme-$SCALE.png ]]; then echo "<figure><img src=\"$name-$theme-$SCALE.png\"><figcaption>$name · $theme</figcaption></figure>"; fi
    done
  done
  echo '</div>'
} > out/sheet-$SCALE.html
echo "contact sheet: out/sheet-$SCALE.html"
