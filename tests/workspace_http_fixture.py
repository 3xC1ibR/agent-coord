"""Browser HTTP fixture support for compiled workspace assets."""

import hashlib
import re
from types import SimpleNamespace
from urllib.request import urlopen


def minimal_browser_sessions(store, cwd, **attributes):
    from agent_coord.conversation_open import ConversationOpen

    sessions = SimpleNamespace(store=store, cwd=str(cwd), **attributes)
    sessions.conversation_open = ConversationOpen(sessions)
    return sessions


def workspace_bundle_paths(test, html):
    if isinstance(html, bytes):
        html = html.decode()
    javascript = re.findall(r'<script id="workspace-program" src="(/dist/app-[0-9a-f]{20}\.js)" defer></script>', html)
    stylesheet = re.findall(r'<link rel="stylesheet" href="(/dist/app-[0-9a-f]{20}\.css)">', html)
    test.assertEqual(len(javascript), 1, "Build the workspace assets before running the browser HTTP tests.")
    test.assertEqual(len(stylesheet), 1)
    return {"js": javascript[0], "css": stylesheet[0]}


def assert_workspace_bundles(test, base, html, *, script=(), stylesheet=()):
    paths = workspace_bundle_paths(test, html)
    for suffix, mime, markers in (("js", "javascript", script), ("css", "css", stylesheet)):
        path = paths[suffix]
        with test.subTest(workspace_asset=path), urlopen(base + path, timeout=5) as response:
            test.assertEqual(response.status, 200)
            test.assertIn(mime, response.headers["Content-Type"])
            test.assertEqual(response.headers["Cache-Control"], "private, max-age=31536000, immutable")
            body = response.read()
            test.assertEqual(path, f"/dist/app-{hashlib.sha256(body).hexdigest()[:20]}.{suffix}")
            for marker in markers:
                test.assertIn(marker, body)
    return paths
