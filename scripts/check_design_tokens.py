#!/usr/bin/env python3
"""Check the complete first-party Ribbon Field stylesheet tree for token drift."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
WEB = Path("plugins/agent-coord/scripts/agent_coord/web")
TOKEN_FILES = {WEB / "tokens.css", WEB / "theme.css"}
EXCLUDED_DIRECTORIES = {"vendor", "dist"}

SIZES = dict(zip(("xs", "sm", "md", "base", "lg", "xl", "2xl"), (11, 12, 13, 14, 16, 20, 24)))
RADII = {"sm": 4, "md": 6, "lg": 10, "pill": 999}
SPACES = {index: index * 4 for index in (1, 2, 3, 4, 5, 6, 8)}
SCALE = {
    **{f"--text-{name}": f"{size}px" for name, size in SIZES.items()},
    **{f"--radius-{name}": f"{size}px" for name, size in RADII.items()},
    **{f"--space-{name}": f"{size}px" for name, size in SPACES.items()},
    "--control-sm": "32px",
    "--control-md": "36px",
    "--hit-target": "44px",
    "--layer-sticky": "10",
    "--layer-floating": "20",
    "--layer-popover": "40",
    "--layer-drawer": "60",
    "--layer-dialog": "100",
    "--transition-fast": "150ms ease",
}
COLORS = """
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk
crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki
darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory
khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen
linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen
""".split()
COLOR = re.compile(
    r"#[\da-f]{3,8}(?![\w-])"
    r"|(?<![\w-])(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|device-cmyk)\("
    r"|(?<![\w-])(?:" + "|".join(COLORS) + r")(?![\w-])",
    re.IGNORECASE,
)
COMMENTS_AND_STRINGS = re.compile(r'''/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.DOTALL | re.VERBOSE)
DECLARATION = re.compile(r"(?<=[{;])\s*([\w-]+)\s*:\s*([^;{}]*)(?=[;}])")
URL = re.compile(r'\burl\([^)]*\)', re.IGNORECASE)
CSS_WIDE = r"(?:inherit|initial|unset|revert|revert-layer)"
FONT_SIZE = r"var\(--text-(?:" + "|".join(SIZES) + r")\)"
FONT_FAMILY = r"var\(--font-(?:sans|mono)\)"
FONT_GRAMMAR = {
    "font": re.compile(rf"(?:[\w.-]+\s+)*{FONT_SIZE}(?:\s*/\s*[\w.%+-]+)?\s+{FONT_FAMILY}"),
    "font-size": re.compile(FONT_SIZE),
    "font-family": re.compile(FONT_FAMILY),
}
RADIUS = r"var\(--radius-(?:" + "|".join(RADII) + r")\)"
RADIUS_VALUE = re.compile(rf"(?:0|{RADIUS})(?:(?:\s+|\s*/\s*)(?:0|{RADIUS}))*")
RADIUS_PROPERTY = re.compile(r"border(?:-[\w-]+)?-radius")
SPACING_PROPERTY = re.compile(r"(?:padding|margin|gap|row-gap|column-gap|inset)(?:-[\w-]+)?|left|top|right|bottom")
CONTROL_PROPERTY = re.compile(r"(?:(?:min|max)-)?(?:width|height)|flex|flex-basis")
CONTROL_LENGTH = re.compile(r"(?<![\w.])(?:32|36|44)px\b", re.IGNORECASE)
ABSOLUTE_LENGTH = re.compile(r"(?<![\w.])[-+]?(?:\d*\.)?\d+(?=px\b|rem\b)", re.IGNORECASE)
TIME = re.compile(r"(?<![\w.])(?:\d*\.)?\d+(?:ms|s)\b", re.IGNORECASE)
HIDDEN = re.compile(r"(?:^|[{}])\s*\[hidden\]\s*\{\s*$")
THEME = re.compile(r'\s*:root\[data-theme="dark"\]\s*\{[^{}]*}\s*')


class Violation(NamedTuple):
    path: str
    line: int
    rule: str
    message: str


def mask(value: str) -> str:
    return re.sub(r"[^\n]", " ", value)


def has_absolute_length(value: str) -> bool:
    return any(float(match[0]) for match in ABSOLUTE_LENGTH.finditer(value))


def check_stylesheets(stylesheets: dict[Path, str]) -> dict:
    violations = []
    used_sizes = set()
    used_radii = set()

    for path, source in sorted(stylesheets.items()):
        def report(position, rule, message):
            violations.append(Violation(path.as_posix(), source.count("\n", 0, position) + 1, rule, message))

        def remove_comment(match):
            value = match[0]
            if value.startswith("/*"):
                if re.search(r"\bdrift-ok\b", value, re.IGNORECASE):
                    report(match.start(), "drift-ok", "Remove the token drift exception.")
                return mask(value)
            return value

        without_comments = COMMENTS_AND_STRINGS.sub(remove_comment, source)
        declarations = COMMENTS_AND_STRINGS.sub(lambda match: mask(match[0]), without_comments)
        is_theme = path == WEB / "theme.css"
        is_token_file = path in TOKEN_FILES
        if is_theme and not THEME.fullmatch(without_comments):
            report(0, "theme", "Keep dark theme overrides in one :root[data-theme=\"dark\"] rule.")

        actual_scale = {}
        for match in DECLARATION.finditer(declarations):
            prop, value = match[1], match[2].strip()
            if re.fullmatch(r".*!\s*important\s*", value, re.IGNORECASE | re.DOTALL):
                if (
                    prop != "display"
                    or not re.fullmatch(r"none\s*!\s*important", value, re.IGNORECASE)
                    or not HIDDEN.search(declarations[:match.start()])
                ):
                    report(match.start(1), "important", "Reserve !important for the global [hidden] display rule.")
            if is_theme:
                if prop != "color-scheme" and (not prop.startswith("--") or prop in SCALE or prop.startswith(("--text-", "--radius-", "--font-"))):
                    report(match.start(1), "theme", f"{prop} is outside the semantic dark theme overrides.")
                continue
            if is_token_file:
                if prop in SCALE:
                    if prop not in actual_scale:
                        actual_scale[prop] = value
                    elif prop == "--transition-fast" and value == "0s":
                        pass
                    else:
                        report(match.start(1), "scale", f"{prop} is defined more than once.")
                elif prop.startswith(("--text-", "--radius-", "--space-")):
                    report(match.start(1), "scale", f"{prop} extends a canonical scale.")
                continue

            paint = COLOR.search(URL.sub("", value))
            if paint:
                report(match.start(1), "color", f"{prop} uses {paint[0]} outside the token files.")
            if prop.startswith(("--text-", "--radius-", "--font-", "--space-")) or prop in SCALE:
                report(match.start(1), "scale", f"Define {prop} in tokens.css.")
            if prop in FONT_GRAMMAR and not (
                FONT_GRAMMAR[prop].fullmatch(value) or re.fullmatch(CSS_WIDE, value)
            ):
                report(match.start(1), prop, f"{prop} should use the canonical scale and font families.")
            if RADIUS_PROPERTY.fullmatch(prop) and not (
                RADIUS_VALUE.fullmatch(value) or re.fullmatch(CSS_WIDE, value)
            ):
                report(match.start(1), "radius", f"{prop} should use the canonical radius scale.")
            if prop == "z-index" and re.fullmatch(r"[-+]?\d+", value):
                report(match.start(1), "layer", "Use a named layer for z-index.")
            if SPACING_PROPERTY.fullmatch(prop) and has_absolute_length(value):
                report(match.start(1), "spacing", f"{prop} should use spacing, stroke, or structural tokens.")
            if CONTROL_PROPERTY.fullmatch(prop) and CONTROL_LENGTH.search(value):
                report(match.start(1), "control", f"{prop} should use the shared control or spacing scale.")
            if (prop in {"box-shadow", "text-shadow"} or "drop-shadow(" in value) and has_absolute_length(value):
                report(match.start(1), "shadow", "Use the shadow scale for elevation and stroke tokens for rings.")
            if prop.startswith(("transition", "animation")) and TIME.search(value):
                report(match.start(1), "motion", "Use motion tokens for animation and transition timings.")
            if prop in ("font", "font-size"):
                used_sizes.update(re.findall(FONT_SIZE, value))
            if RADIUS_PROPERTY.fullmatch(prop):
                used_radii.update(re.findall(RADIUS, value))

        if path == WEB / "tokens.css":
            for prop, expected in SCALE.items():
                if actual_scale.get(prop) != expected:
                    report(0, "scale", f"Set {prop} to {expected} in tokens.css.")

    for path in sorted(TOKEN_FILES - stylesheets.keys()):
        violations.append(Violation(path.as_posix(), 1, "missing", "Include the canonical token stylesheet."))

    counts = Counter(violation.rule for violation in violations)
    return {
        "stylesheets": len(stylesheets),
        "colors_outside_tokens": counts["color"],
        "font_sizes": len(used_sizes),
        "border_radii": len(used_radii),
        "drift_ok_comments": counts["drift-ok"],
        "violations": [violation._asdict() for violation in sorted(violations)],
    }


def first_party(path: Path) -> bool:
    return path.suffix == ".css" and not EXCLUDED_DIRECTORIES.intersection(path.relative_to(WEB).parts[:-1])


def read_stylesheets(root: Path, *, staged: bool) -> dict[Path, str]:
    if staged:
        def index(*args):
            return subprocess.check_output(("git", *args), cwd=root).decode()

        paths = index("ls-files", "--cached", "-z", "--", WEB.as_posix()).split("\0")
        return {
            path: index("show", f":{path.as_posix()}")
            for path in map(Path, filter(None, paths)) if first_party(path)
        }
    return {
        path.relative_to(root): path.read_text(encoding="utf-8")
        for path in (root / WEB).rglob("*.css") if first_party(path.relative_to(root))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Read the staged Git index instead of the working tree.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable counts and violations.")
    arguments = parser.parse_args()
    try:
        result = check_stylesheets(read_stylesheets(ROOT, staged=arguments.staged))
    except (OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"{result['stylesheets']} stylesheets, {result['colors_outside_tokens']} colors outside tokens, "
            f"{result['font_sizes']} font sizes, {result['border_radii']} radius steps, "
            f"{result['drift_ok_comments']} drift-ok comments"
        )
        for violation in result["violations"]:
            print("{path}:{line}: {rule}: {message}".format(**violation), file=sys.stderr)
    return int(bool(result["violations"]))


if __name__ == "__main__":
    sys.exit(main())
