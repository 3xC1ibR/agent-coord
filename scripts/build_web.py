#!/usr/bin/env python3
"""Build or verify the bundled Ribbon Field workspace assets (dist/app.js, dist/app.css).

Uses esbuild to compile and validate the ordered workspace stylesheets and scripts
into single-request bundles with external source maps, and records cryptographic
input/output digests in dist/manifest.json so CI can verify bundle freshness
deterministically without network installs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEB_ROOT = ROOT / "plugins/agent-coord/scripts/agent_coord/web"

# Canonical stylesheet order: tokens.css first, component stylesheets in deterministic order, theme.css last.
WORKSPACE_CSS_FILES: tuple[str, ...] = (
    "tokens.css",
    "styles.css",
    "thread-hover.css",
    "agent-messages.css",
    "filter-menu.css",
    "image-attachments.css",
    "views.css",
    "folders.css",
    "thread-panes.css",
    "thread-tabs.css",
    "atlas.css",
    "working-set.css",
    "provider-usage.css",
    "activity.css",
    "conversation-search.css",
    "speech.css",
    "html-preview.css",
    "openui.css",
    "thread-mentions.css",
    "sidecars.css",
    "reviews.css",
    "asks.css",
    "send-work.css",
    "jev.css",
    "dispatch.css",
    "dispatcher-workspace.css",
    "project-scratchpad.css",
    "context-lifecycle.css",
    "agent-tree.css",
    "theme.css",
)

# Canonical script dependency order matching workspace execution order (theme.js runs before paint in <head>).
WORKSPACE_JS_FILES: tuple[str, ...] = (
    "provider-usage.js",
    "activity.js",
    "activity-collector.js",
    "speech.js",
    "thread-groups.js",
    "atlas-layout.js",
    "atlas.js",
    "overview-layout.js",
    "working-set.js",
    "organization.js",
    "notifications.js",
    "push-notifications.js",
    "navigation.js",
    "markdown.js",
    "html-preview.js",
    "openui.js",
    "image-attachments.js",
    "slash-commands.js",
    "views.js",
    "view-drops.js",
    "folders.js",
    "thread-panes.js",
    "thread-tabs.js",
    "roll-up.js",
    "conversation-scroll.js",
    "response-actions.js",
    "mobile-chat.js",
    "model-picker.js",
    "permission-menu.js",
    "thread-usage.js",
    "thread-mentions.js",
    "schedules.js",
    "agent-messages.js",
    "conversation-search.js",
    "transient-notice.js",
    "thread-actions.js",
    "sidecars.js",
    "reviews.js",
    "asks.js",
    "dispatch.js",
    "send-work.js",
    "jev.js",
    "dispatcher-workspace.js",
    "scratchpad.js",
    "project-scratchpad.js",
    "context-lifecycle.js",
    "agent-tree.js",
    "settings.js",
    "app.js",
    "thread-hover.js",
    "filter-menu.js",
)

TOP_LEVEL_DECL_RE = re.compile(r"^(?:const|let|class|async\s+function|function)\s+([A-Za-z_$][\w$]*)")
LEADING_STRICT_RE = re.compile(r'^\s*["\']use strict["\'];\s*\n?')


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_esbuild() -> str | None:
    candidates = [
        shutil.which("esbuild"),
        str(Path.home() / ".npm-global/bin/esbuild"),
        "/opt/homebrew/bin/esbuild",
        "/usr/local/bin/esbuild",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def check_top_level_collisions(web_root: Path) -> None:
    declarations: dict[str, list[str]] = defaultdict(list)
    for name in WORKSPACE_JS_FILES:
        source = (web_root / name).read_text(encoding="utf-8")
        for line in source.splitlines():
            match = TOP_LEVEL_DECL_RE.match(line)
            if match:
                declarations[match.group(1)].append(name)
    collisions = {ident: files for ident, files in declarations.items() if len(files) > 1}
    if collisions:
        formatted = ", ".join(f"{ident} ({' vs '.join(files)})" for ident, files in sorted(collisions.items()))
        raise RuntimeError(f"Duplicate top-level identifiers across workspace scripts: {formatted}")


def assemble_js(web_root: Path) -> str:
    parts = ['"use strict";\n']
    for name in WORKSPACE_JS_FILES:
        path = web_root / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing workspace script: {path}")
        body = LEADING_STRICT_RE.sub("", path.read_text(encoding="utf-8"), count=1)
        parts.append(f"// --- {name} ---\n{body.rstrip()}\n")
    return "\n".join(parts)


def assemble_css(web_root: Path) -> str:
    parts = []
    for name in WORKSPACE_CSS_FILES:
        path = web_root / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing workspace stylesheet: {path}")
        parts.append(f"/* --- {name} --- */\n{path.read_text(encoding='utf-8').rstrip()}\n")
    return "\n".join(parts)


def build(web_root: Path) -> dict:
    check_top_level_collisions(web_root)
    dist = web_root / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    js_source = assemble_js(web_root)
    css_source = assemble_css(web_root)
    esbuild = find_esbuild()

    out_js = dist / "app.js"
    out_css = dist / "app.css"

    if esbuild:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ribbon-web-build-") as scratch:
            tmp_js = Path(scratch) / "workspace-bundle.js"
            tmp_css = Path(scratch) / "workspace-bundle.css"
            tmp_js.write_text(js_source, encoding="utf-8")
            tmp_css.write_text(css_source, encoding="utf-8")
            subprocess.run(
                [
                    esbuild,
                    str(tmp_js),
                    "--target=es2022",
                    "--charset=utf8",
                    "--sourcemap=external",
                    f"--outfile={out_js}",
                ],
                check=True,
            )
            subprocess.run(
                [
                    esbuild,
                    str(tmp_css),
                    "--target=safari16,chrome115,firefox115",
                    "--charset=utf8",
                    "--sourcemap=external",
                    f"--outfile={out_css}",
                ],
                check=True,
            )
        bundler = "esbuild"
    else:
        out_js.write_text(js_source, encoding="utf-8")
        out_css.write_text(css_source, encoding="utf-8")
        bundler = "concat-fallback"

    manifest = {
        "version": 1,
        "bundler": bundler,
        "css_order": list(WORKSPACE_CSS_FILES),
        "js_order": list(WORKSPACE_JS_FILES),
        "css_inputs_sha256": {name: sha256_file(web_root / name) for name in WORKSPACE_CSS_FILES},
        "js_inputs_sha256": {name: sha256_file(web_root / name) for name in WORKSPACE_JS_FILES},
        "outputs": {
            "dist/app.css": {"sha256": sha256_file(out_css), "bytes": out_css.stat().st_size},
            "dist/app.js": {"sha256": sha256_file(out_js), "bytes": out_js.stat().st_size},
        },
    }
    (dist / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify(web_root: Path) -> dict:
    check_top_level_collisions(web_root)
    manifest_path = web_root / "dist/manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Missing web bundle manifest: {manifest_path}. Run python3 scripts/build_web.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("css_order") != list(WORKSPACE_CSS_FILES):
        raise RuntimeError("dist/manifest.json css_order is out of date; run python3 scripts/build_web.py")
    if manifest.get("js_order") != list(WORKSPACE_JS_FILES):
        raise RuntimeError("dist/manifest.json js_order is out of date; run python3 scripts/build_web.py")
    for name in WORKSPACE_CSS_FILES:
        path = web_root / name
        if not path.is_file():
            raise RuntimeError(f"Missing stylesheet {name}")
        if sha256_file(path) != manifest["css_inputs_sha256"].get(name):
            raise RuntimeError(f"Stylesheet {name} changed since dist/app.css was built; run python3 scripts/build_web.py")
    for name in WORKSPACE_JS_FILES:
        path = web_root / name
        if not path.is_file():
            raise RuntimeError(f"Missing script {name}")
        if sha256_file(path) != manifest["js_inputs_sha256"].get(name):
            raise RuntimeError(f"Script {name} changed since dist/app.js was built; run python3 scripts/build_web.py")
    for rel, meta in manifest["outputs"].items():
        out = web_root / rel
        if not out.is_file():
            raise RuntimeError(f"Missing bundled output {rel}; run python3 scripts/build_web.py")
        if sha256_file(out) != meta["sha256"]:
            raise RuntimeError(f"Bundled output {rel} digest mismatch; run python3 scripts/build_web.py")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT)
    parser.add_argument("--check", action="store_true", help="Verify dist/ bundles match current source files.")
    args = parser.parse_args()
    web_root = args.web_root.resolve()
    result = verify(web_root) if args.check else build(web_root)
    print(json.dumps({
        "status": "verified" if args.check else "built",
        "bundler": result["bundler"],
        "outputs": result["outputs"],
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"build_web failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
