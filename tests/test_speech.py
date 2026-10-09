from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.speech import DEFAULTS, Speech, spoken_text, validate_settings
from agent_coord.store import CoordinationError
import test_remote_access as remote


class SpeechTests(unittest.TestCase):
    def test_final_reply_selection_and_readable_markdown(self):
        turn = {"status": "completed", "items": [
            {"type": "agentMessage", "phase": "commentary", "text": "Working now"},
            {"type": "commandExecution", "text": "secret command"},
            {"type": "agentMessage", "phase": "final_answer", "text": "# Result\n**Done**. [File](/tmp/file)\n```py\nsecret()\n```"},
        ]}
        self.assertEqual(spoken_text(turn), "Result Done. File Code block omitted.")
        turn["items"] = [{"type": "agentMessage", "text": "Working"}, {"type": "agentMessage", "text": "Final reply"}]
        self.assertEqual(spoken_text(turn), "Final reply")
        for status in ["inProgress", "failed", "interrupted"]:
            turn["status"] = status
            with self.assertRaises(CoordinationError): spoken_text(turn)
        with self.assertRaises(CoordinationError): spoken_text({"status": "completed", "items": []})

    def test_configuration_is_portable_private_and_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Speech(Path(directory) / "state.sqlite3")
            self.assertEqual(service.settings(), DEFAULTS)
            value = {**DEFAULTS, "base_url": "http://localhost:9000/v1", "voice": "bf_emma", "speed": 1.2}
            service.configure(value)
            self.assertEqual(Speech(Path(directory) / "state.sqlite3").settings(), value)
            self.assertEqual(service.path.stat().st_mode & 0o777, 0o600)
        for url in ["https://external.example:443/v1", "http://127.0.0.1:8880/other", "file:///tmp/voice", "http://user:secret@localhost:8880/v1", "http://localhost:8880/v1?key=secret", "http://localhost:8880/v1#x", "http://localhost:bad/v1"]:
            with self.subTest(url=url), self.assertRaises(CoordinationError): validate_settings({**DEFAULTS, "base_url": url})
        for speed in [True, float("nan"), 0, 3, "1"]:
            with self.assertRaises(CoordinationError): validate_settings({**DEFAULTS, "speed": speed})

    def test_real_transport_mp3_errors_redirect_and_size_bounds(self):
        requests = []
        class Kokoro(BaseHTTPRequestHandler):
            mode = "audio"
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                if self.mode == "redirect":
                    self.send_response(302); self.send_header("Location", "/redirected"); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg" if self.mode == "audio" else "application/json")
                self.end_headers(); self.wfile.write(b"ID3test-audio")
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Kokoro)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                service = Speech(Path(directory) / "state.sqlite3")
                service.configure({**DEFAULTS, "base_url": f"http://127.0.0.1:{server.server_port}/v1"})
                self.assertEqual(service.synthesize("Hello"), b"ID3test-audio")
                self.assertEqual(requests[-1][0], "/v1/audio/speech")
                self.assertEqual(requests[-1][1]["input"], "Hello")
                self.assertEqual(requests[-1][1]["model"], "kokoro")
                with patch("agent_coord.speech.MAX_AUDIO", 4), self.assertRaisesRegex(CoordinationError, "oversized"):
                    service.synthesize("Hello")
                Kokoro.mode = "json"
                with self.assertRaisesRegex(CoordinationError, "MP3"): service.synthesize("Hello")
                Kokoro.mode = "redirect"
                with self.assertRaisesRegex(CoordinationError, "HTTP 302"): service.synthesize("Hello")
                service.capacity.acquire(); service.capacity.acquire()
                with self.assertRaisesRegex(CoordinationError, "busy"): service.synthesize("Hello")
                service.capacity.release(); service.capacity.release()
        finally:
            server.shutdown(); server.server_close(); thread.join()


class SpeechHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = remote.RemoteHTTPTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_pairing_csrf_settings_and_mp3_delivery(self):
        f = self.fixture
        self.assertEqual(f.request("GET", "/speech.js")[0], 401)
        self.assertEqual(f.request("GET", "/api/browser/speech/settings")[0], 401)
        cookie = f.pair()
        self.assertEqual(f.request("GET", "/api/browser/speech/settings", cookie=cookie)[0], 200)
        self.assertEqual(f.request("POST", "/api/browser/speech/settings", DEFAULTS, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 403)
        self.assertEqual(f.request("POST", "/api/browser/speech/settings", DEFAULTS, local=True, csrf=f.csrf)[0], 200)
        route = "/api/browser/speech/audio"
        self.assertEqual(f.request("POST", route, {"test": True}, cookie=cookie, origin=f.origin)[0], 403)
        self.assertEqual(f.request("POST", route, {"test": True}, cookie=cookie, origin="https://evil.test", csrf=f.csrf)[0], 403)
        with patch("agent_coord.speech.Speech.synthesize", return_value=b"ID3audio") as synth:
            result = f.request("POST", route, {"test": True}, cookie=cookie, origin=f.origin, csrf=f.csrf)
            self.assertEqual(result[0], 200); self.assertEqual(result[1]["Content-Type"], "audio/mpeg")
            self.assertEqual(result[2], b"ID3audio")
            self.assertIn("media-src 'self' blob:", result[1]["Content-Security-Policy"])
            with patch.object(f.server.browser_sessions, "read", return_value={"thread": {"turns": [{"id": "turn", "status": "completed", "items": [{"type": "agentMessage", "text": "Final answer"}]}]}}):
                self.assertEqual(f.request("POST", route, {"thread_id": "chat", "turn_id": "turn"}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 200)
                synth.assert_called_with("Final answer")
            self.assertEqual(f.request("POST", route, {"text": "arbitrary", "base_url": "https://evil.test"}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 400)
            f.remote.revoke(f.remote.device_id(cookie))
            self.assertEqual(f.request("POST", route, {"test": True}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 401)

    def test_revocation_during_generation_does_not_deliver_audio(self):
        f = self.fixture; cookie = f.pair()
        def generate(text):
            f.remote.revoke(f.remote.device_id(cookie)); return b"ID3private"
        with patch("agent_coord.speech.Speech.synthesize", side_effect=generate):
            self.assertEqual(f.request("POST", "/api/browser/speech/audio", {"test": True}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 401)
