"""Install and authenticate Google's official Antigravity ACP runtime.

Runtime layout and launch environment were checked against T3 Code's
antigravityRelease.ts and antigravityAuthSupport.ts (2026-10-11).
No Google credentials are read or copied by Ribbon Field.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .store import CoordinationError

VERSION = "1.3.0"
REGISTRY = "https://github.com/agentclientprotocol/registry/blob/main/antigravity-acp/agent.json"
# Official Google archives, SHA-256 and uncompressed member sizes, pinned to 1.3.0.
RELEASES = {
    ("Darwin", "arm64"): ("macos", "darwin-arm64", "7cd97045f7b4fe81175a107cdf16f9c51484e3c78a5162cae415338bb6aa5b88", 111456962, 278535456, 118611392),
    ("Darwin", "x86_64"): ("macos", "darwin-x86_64", "bb23956b89984bf5d354af2c3725e6c57f0cc1b7228e77a0e91c9c2bc1d47646", 117245544, 282840688, 124175392),
    ("Linux", "x86_64"): ("linux", "linux-x86_64", "9fb60956af0a9d76220a4db91ca9ac88e2a2372ad68f985ab5fceace6b825b96", 333727150, 926533965, 130388040),
    ("Linux", "arm64"): ("linux", "linux-arm64", "500b0bc0fb858e88f4df404d4cedf80bf9298c178291e39e383d6c50b111cbdf", 321690363, 930848992, 123224968),
    ("Windows", "x86_64"): ("windows", "windows-x86_64", "65215e0688681fa3116e048a9eab27ef53af1bbd6f3da3f1c52bd4911d8b17f9", 124509787, 81437336, 145548952),
    ("Windows", "arm64"): ("windows", "windows-arm64", "4a0f469720e9beb9438a979f543fdbfad5022ebe0992c052c590bd78b3144ca3", 124654803, 85893472, 135640216),
}


def provider_directory(store):
    return store.database_path.parent / "providers" / "antigravity"


def executable_names():
    return ("agy_acp_server.exe", "localharness_external.exe") if os.name == "nt" else ("agy_acp_server.par", "localharness_external")


def resolve_runtime(store):
    executable, harness = executable_names()
    configured = os.environ.get("AGENT_COORD_ANTIGRAVITY_BIN")
    managed = provider_directory(store) / "runtime" / VERSION / executable
    candidate = configured or (str(managed) if managed.is_file() else shutil.which(executable))
    if not candidate:
        raise CoordinationError("Antigravity is not installed. Run agent-coord antigravity install, then agent-coord antigravity login on this machine.")
    binary = Path(candidate).expanduser().resolve()
    helper = binary.with_name(harness)
    if not all(p.is_file() and os.access(p, os.X_OK) for p in (binary, helper)):
        raise CoordinationError("Antigravity needs an executable ACP server and matching localharness_external in the same directory. Run agent-coord antigravity install or set AGENT_COORD_ANTIGRAVITY_BIN.")
    return binary, helper


def install_runtime(store):
    machine = {"aarch64": "arm64", "AMD64": "x86_64"}.get(platform.machine(), platform.machine())
    release = RELEASES.get((platform.system(), machine))
    if not release:
        raise CoordinationError("No managed Antigravity runtime is available for this platform. See " + REGISTRY)
    folder, target, digest, size, binary_size, harness_size = release
    root = provider_directory(store) / "runtime"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / VERSION
    names = dict(zip(executable_names(), (binary_size, harness_size)))
    if destination.exists():
        if all((destination / name).is_file() and (destination / name).stat().st_size == count for name, count in names.items()):
            return {"status": "installed", "version": VERSION, "path": str(destination / executable_names()[0])}
        raise CoordinationError(f"The Antigravity installation at {destination} is incomplete. Move it aside and retry installation.")
    if shutil.disk_usage(root).free < size + binary_size + harness_size + 512 * 1024 * 1024:
        raise CoordinationError("Not enough free disk space to install Antigravity.")
    url = f"https://dl.google.com/agy-extensions/releases/{folder}/agy-acp-server-{VERSION}-{target}.zip"
    try:
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as temporary:
            staging = Path(temporary)
            archive = staging / "download.zip"
            checksum, received = hashlib.sha256(), 0
            with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > size:
                        raise CoordinationError("Antigravity download exceeds the pinned release size.")
                    checksum.update(chunk)
                    output.write(chunk)
            if received != size or checksum.hexdigest() != digest:
                raise CoordinationError("Antigravity download failed its SHA-256 or size check; nothing was installed.")
            pair = staging / "runtime"
            pair.mkdir(mode=0o700)
            with zipfile.ZipFile(archive) as source:
                members = source.infolist()
                if len(members) != 2 or {m.filename for m in members} != set(names):
                    raise CoordinationError("Antigravity archive contains unexpected members.")
                for member in members:
                    kind = (member.external_attr >> 16) & 0o170000
                    if member.file_size != names[member.filename] or kind not in (0, 0o100000) or member.flag_bits & 1:
                        raise CoordinationError("Antigravity archive contains an unsafe or incorrectly sized member.")
                    path = pair / member.filename
                    with source.open(member) as input_file, path.open("xb") as output:
                        shutil.copyfileobj(input_file, output)
                    path.chmod(0o755)
            # Publish the complete pair atomically; never replace an in-use version.
            pair.rename(destination)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        raise CoordinationError(f"Could not install Antigravity: {exc}") from exc
    return {"status": "installed", "version": VERSION, "path": str(destination / executable_names()[0])}


def run_setup(store, operation):
    from .antigravity import AntigravityConnection
    if operation == "install":
        return install_runtime(store)
    if operation == "status":
        try:
            binary, _ = resolve_runtime(store)
        except CoordinationError as exc:
            return {"installed": False, "message": str(exc)}
        return {"installed": True, "path": str(binary),
                "saved_login": (provider_directory(store) / "profile/antigravity-acp/acp_token.json").is_file()}
    import sys
    import webbrowser
    def authorize(url):
        print("Complete Google sign-in in your browser:\n" + url, file=sys.stderr, flush=True)
        webbrowser.open(url)
    connection = AntigravityConnection(store, lambda event: None,
        {"cwd": str(provider_directory(store)), "authorize": authorize})
    try:
        connection.initialize()
        connection.request("authenticate", {"methodId": "oauth-personal"}, timeout=300)
        # Authentication alone does not establish subscription/account access.
        session = connection.request("session/new", {"cwd": str(provider_directory(store)), "mcpServers": []})
        return {"status": "authenticated", "models": model_catalog(session)}
    finally:
        connection.close()


def model_catalog(session):
    option = next((item for item in session.get("configOptions", []) if item.get("id") == "model" or item.get("category") == "model"), {})
    values = []
    for entry in option.get("options", []):
        values.extend([entry] if "value" in entry else entry.get("options", []))
    return [{"model": entry["value"], "displayName": entry.get("name", entry["value"]),
             "isDefault": entry["value"] == option.get("currentValue"),
             "supportedReasoningEfforts": [], "defaultReasoningEffort": None} for entry in values]
