"""Kokoro speech transport. Only the Mac may configure the local provider."""
from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .store import CoordinationError

DEFAULTS = {"base_url": "http://127.0.0.1:8880/v1", "voice": "af_heart", "speed": 1.0}
MAX_AUDIO = 32 * 1024 * 1024
MAX_VOICES = 1024 * 1024
MAX_TEXT = 24000


def validate_settings(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise CoordinationError("Speech settings require a Kokoro address, voice, and speed.")
    url, voice, speed = value["base_url"], value["voice"], value["speed"]
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not parsed or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"
                or not parsed.port or not (parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback)):
            raise ValueError()
    except ValueError as exc:
        raise CoordinationError("Use Kokoro on this Mac, for example http://127.0.0.1:8880/v1.") from exc
    if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_+.-]{1,120}", voice):
        raise CoordinationError("Enter a Kokoro voice name, for example af_heart.")
    if type(speed) not in {int, float} or not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise CoordinationError("Speech speed must be between 0.5 and 2.")
    return {"base_url": url.rstrip("/"), "voice": voice, "speed": speed}


def spoken_text(turn: dict) -> str:
    if turn.get("status") != "completed":
        raise CoordinationError("Only completed replies can be read aloud.")
    messages = [item for item in turn.get("items", []) if item.get("type") == "agentMessage"]
    finals = [item for item in messages if item.get("phase") == "final_answer"]
    # Claude has no phase field. Its last assistant message is the final reply.
    chosen = finals or [item for item in messages if item.get("phase") in {None, ""}][-1:]
    text = "\n\n".join(str(item.get("text") or "") for item in chosen)
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1[^\n]*$", " Code block omitted. ", text)
    text = re.sub(r"!\[([^\]]*)\]\([^\n]*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\n]*?\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+)", "", text)
    text = re.sub(r"[`*_~]", "", text)
    text = " ".join(text.split())
    if not text:
        raise CoordinationError("This turn has no completed reply to read aloud.")
    if len(text) > MAX_TEXT:
        raise CoordinationError("This reply is too long to read aloud (24,000 characters maximum).")
    return text


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Speech:
    def __init__(self, database_path):
        self.path = Path(database_path).with_suffix(".speech.json")
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(2)

    def settings(self) -> dict:
        with self.lock:
            try:
                return validate_settings(json.loads(self.path.read_text()))
            except FileNotFoundError:
                return dict(DEFAULTS)
            except (OSError, ValueError) as exc:
                raise CoordinationError("Cannot read speech settings on this Mac.") from exc

    def configure(self, value: dict) -> dict:
        value = validate_settings(value)
        with self.lock:
            temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                    json.dump(value, file)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return value

    def voices(self) -> list[dict]:
        request = Request(self.settings()["base_url"] + "/audio/voices",
                          headers={"Accept": "application/json"})
        try:
            with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=10) as response:
                data = response.read(MAX_VOICES + 1)
                if len(data) > MAX_VOICES:
                    raise CoordinationError("Kokoro returned an oversized voice list.")
            payload = json.loads(data)
            entries = payload.get("voices") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise ValueError()
            voices = {}
            for entry in entries:
                voice = entry if isinstance(entry, str) else entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_+.-]{1,120}", voice):
                    raise ValueError()
                name = entry.get("name") if isinstance(entry, dict) else voice
                voices[voice] = {"id": voice, "name": name if isinstance(name, str) and 0 < len(name) <= 200 else voice}
            if not voices:
                raise CoordinationError("Kokoro has no available voices.")
            return sorted(voices.values(), key=lambda voice: (voice["name"].casefold(), voice["id"]))
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise CoordinationError(f"Kokoro could not list voices (HTTP {code}). Check the server on your Mac.") from None
        except (URLError, OSError, TimeoutError) as exc:
            raise CoordinationError("Cannot load Kokoro voices. Start it on your Mac and check Speech settings.") from exc
        except ValueError as exc:
            raise CoordinationError("Kokoro returned an invalid voice list.") from exc

    def synthesize(self, text: str) -> bytes:
        if not text or len(text) > MAX_TEXT:
            raise CoordinationError("Speech text is empty or too long.")
        if not self.capacity.acquire(blocking=False):
            raise CoordinationError("Kokoro is busy. Try speech again shortly.")
        try:
            settings = self.settings()
            request = Request(settings["base_url"] + "/audio/speech", data=json.dumps({
                "model": "kokoro", "input": text, "voice": settings["voice"],
                "speed": settings["speed"], "response_format": "mp3",
            }).encode(), headers={"Content-Type": "application/json", "Accept": "audio/mpeg"})
            # Never route conversation text through an environment proxy or redirect.
            with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=90) as response:
                if response.headers.get_content_type() not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                    raise CoordinationError("Kokoro did not return MP3 audio.")
                data = response.read(MAX_AUDIO + 1)
                if not data or len(data) > MAX_AUDIO:
                    raise CoordinationError("Kokoro returned empty or oversized audio.")
                return data
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise CoordinationError(f"Kokoro rejected speech (HTTP {code}). Check the voice and server on your Mac.") from None
        except (URLError, OSError, TimeoutError) as exc:
            raise CoordinationError("Cannot reach Kokoro. Start it on your Mac and check Speech settings.") from exc
        finally:
            self.capacity.release()
