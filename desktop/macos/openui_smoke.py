#!/usr/bin/env python3
"""Check actual OpenUI rendering, isolation, and form submission in WKWebView."""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
SOURCE = '''root = Card([heading, test])
heading = CardHeader("OpenUI test", "Send a note back to this conversation.")
test = Form("openui_test", actions, [note])
note = FormControl("Test note", Input("note", "Hello from OpenUI"))
actions = Buttons([send])
send = Button("Send test response", @ToAssistant("Confirm this OpenUI test reached this conversation and repeat my note."))'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts", type=Path, default=ROOT / "plugins/agent-coord/scripts",
                        help="Backend scripts to test, including an installed plugin's scripts directory.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This regression check requires macOS and the Swift compiler.")
    sys.path.insert(0, str(args.scripts.resolve()))
    from agent_coord.store import CoordinationStore
    from agent_coord.ui import _handler

    with tempfile.TemporaryDirectory(prefix="openui-webkit-") as directory:
        temporary = Path(directory)
        base_handler = _handler(CoordinationStore(temporary / "state.sqlite3"), None, None)
        page = ('''<!doctype html><html><head><meta charset="utf-8">
<script src="/markdown.js"></script><script src="/openui.js"></script>
<link rel="stylesheet" href="/openui.css"></head><body><main id="timeline"></main><pre id="received"></pre>
<script>
const source = SOURCE;
const article = document.createElement('article');
article.dataset.openuiKey = 'native-smoke';
article.dataset.openuiText = source;
article.dataset.openuiStreaming = 'false';
article.innerHTML = messageMarkdown.render('```openui\\n' + source + '\\n```');
const fragment = document.createDocumentFragment(); fragment.append(article);
OpenUIThread.commit(document.getElementById('timeline'), fragment, {
  thread:'native-smoke', theme:'dark', disabled:false,
  onAction: async text => {
    const event = JSON.parse(text.slice(text.indexOf('\\n') + 1));
    document.getElementById('received').textContent = JSON.stringify(event);
    window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'result', event});
  }
});
</script></body></html>'''.replace("SOURCE", json.dumps(SOURCE))).encode()

        class Handler(base_handler):
            def do_GET(self):
                if self.path == "/openui-smoke" and self._local_request():
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", page)
                else:
                    super().do_GET()

        binary = temporary / "openui-smoke"
        subprocess.run(["swiftc", "-module-cache-path", str(ROOT / "build/macos/module-cache"),
                        str(Path(__file__).with_suffix(".swift")), "-o", str(binary)], check=True, timeout=120)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = subprocess.run([str(binary), f"http://127.0.0.1:{server.server_port}/openui-smoke"],
                                    capture_output=True, text=True, timeout=40)
            print(result.stdout.strip())
            if result.returncode:
                print(result.stderr[-4000:], file=sys.stderr)
            return result.returncode
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    raise SystemExit(main())
