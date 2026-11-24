import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_design_tokens.py"
SPEC = importlib.util.spec_from_file_location("check_design_tokens", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DesignTokenTests(unittest.TestCase):
    def setUp(self):
        self.token_sources = {
            path: (ROOT / path).read_text(encoding="utf-8") for path in guard.TOKEN_FILES
        }

    def check_sample(self, css):
        return guard.check_stylesheets({
            **self.token_sources, guard.WEB / "sample.css": css,
        })

    def test_current_stylesheets_follow_design_contract(self):
        current = guard.check_stylesheets(guard.read_stylesheets(ROOT, staged=False))
        self.assertEqual(current["violations"], [])
        self.assertEqual(current["font_sizes"], 7)
        self.assertEqual(current["border_radii"], 4)

    def test_reports_literals_in_nested_rules_and_fallbacks(self):
        result = self.check_sample("""
            @media (pointer: coarse) {
              .notice {
                background: var(--paper, rgb(20 30 40 / 50%));
                color: Rebeccapurple;
                font: 600 13px system-ui;
                font-size: clamp(12px, 2vw, 16px);
                border-start-start-radius: 8px;
                z-index: 99;
                display: flex !important;
                /* drift-ok */
              }
            }
        """)
        self.assertCountEqual(
            (item["rule"] for item in result["violations"]),
            ["color", "color", "font", "font-size", "radius", "layer", "important", "drift-ok"],
        )
        self.assertEqual(result["colors_outside_tokens"], 2)

    def test_enforces_spacing_control_shadow_and_motion_tokens(self):
        result = self.check_sample("""
            @media (max-width: 720px) {
              .header {
                left: 16px;
                padding: 2px;
                gap: .4rem;
                flex: 0 0 36px;
                min-height: 44px;
                box-shadow: 0 4px 14px var(--shadow-color);
                filter: drop-shadow(0 3px var(--accent));
                transition: color 150ms ease;
                animation-delay: 1s;
              }
            }
        """)
        self.assertCountEqual(
            (item["rule"] for item in result["violations"]),
            ["spacing", "spacing", "spacing", "control", "control", "shadow", "shadow", "motion", "motion"],
        )

    def test_accepts_platform_paints_inheritance_and_hidden(self):
        result = self.check_sample("""
            [hidden] { display: none !important; }
            #abc {
              --status: var(--warm-tint);
              color: currentColor;
              background: color-mix(in srgb, var(--surface) 80%, transparent);
              mask: url("#fff");
              content: "brown; color: red";
              font: 600 var(--text-sm)/1.5 var(--font-sans);
              border-radius: var(--radius-lg) var(--radius-lg) 0 0;
              padding: var(--space-3);
              margin: 1em 0;
              inset: auto 0 max(0px, env(safe-area-inset-bottom));
              min-height: var(--hit-target);
              box-shadow: var(--shadow-sm);
              transition: background var(--transition-fast);
            }
            @media (forced-colors: active) {
              .selection { outline: 2px solid Highlight; }
            }
        """)
        self.assertEqual(result["violations"], [])

    def test_theme_cannot_add_component_styles_or_change_type_scale(self):
        theme = guard.WEB / "theme.css"
        self.token_sources[theme] = (
            ':root[data-theme="dark"] { --ink: #fff; --text-xl: 21px; }'
            '.panel { background: #141418; }'
        )
        items = self.check_sample("")["violations"]
        self.assertEqual([item["rule"] for item in items], ["theme", "theme", "theme"])

    def test_scale_rejects_extra_or_redefined_sizes(self):
        tokens = guard.WEB / "tokens.css"
        self.token_sources[tokens] += ":root { --text-3xl: 30px; --radius-pill: 50px; }"
        items = self.check_sample("")["violations"]
        self.assertEqual([item["rule"] for item in items], ["scale", "scale"])

    def test_staged_mode_reads_index_blobs_including_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "--quiet"), cwd=root, check=True)
            indexed = guard.WEB / "indexed.css"
            destination = root / indexed
            destination.parent.mkdir(parents=True)
            destination.write_text(".status { color: var(--warm); }", encoding="utf-8")
            vendor = destination.parent / "vendor" / "package.css"
            vendor.parent.mkdir()
            vendor.write_text(".status { color: red; }", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            destination.write_text(".status { color: red; }", encoding="utf-8")
            self.assertEqual(guard.read_stylesheets(root, staged=True),
                             {indexed: ".status { color: var(--warm); }"})
            self.assertEqual(guard.read_stylesheets(root, staged=False),
                             {indexed: ".status { color: red; }"})


if __name__ == "__main__":
    unittest.main()
