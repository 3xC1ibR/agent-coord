"""Pinned Sparkle framework and signed appcast packaging (no Python dependencies)."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
DOWNLOAD_URL = "https://downloads.ribbonfield.com"
KEYCHAIN_ACCOUNT = "ribbon-field"
RUNTIME_KEYS = ("runtime_protocol", "runtime_version", "ui_protocol_min", "ui_protocol_max",
                "runtime_schema_min", "runtime_schema_max")
ET.register_namespace("sparkle", SPARKLE_NS)


def public_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Sparkle public key must be base64 Ed25519") from exc
    if len(decoded) != 32:
        raise ValueError("Sparkle public key must contain 32 bytes")
    return decoded


def https_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Sparkle requires an HTTPS URL without credentials or fragments")
    return value


def configuration(feed: str | None, key: str | None, *, standalone: bool, identity: str) -> dict:
    if not feed and not key:
        return {}
    if not feed or not key:
        raise ValueError("Sparkle requires both --update-feed and --update-public-key")
    if not standalone or not identity.startswith("Developer ID Application:"):
        raise ValueError("Sparkle requires a standalone Developer ID signed build")
    https_url(feed)
    public_key(key)
    return {"SUFeedURL": feed, "SUPublicEDKey": key, "SUEnableAutomaticChecks": True,
            "SUAutomaticallyUpdate": False, "SURequireSignedFeed": True,
            "SUVerifyUpdateBeforeExtraction": True, "SUSignedFeedFailureExpirationInterval": 0}


def runtime_metadata(version: str, build: str) -> dict:
    # Build, runtime handshake and signed appcast share the runtime's contract.
    scripts = HERE.parents[1] / "plugins/agent-coord/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from agent_coord.desktop_runtime import compatibility_metadata
    value = compatibility_metadata(f"{version}+{build}")
    validate_runtime(value)
    return value


def validate_runtime(value: dict) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("runtime_version"), str) or not value["runtime_version"]:
        raise ValueError("Missing runtime version")
    for key in set(RUNTIME_KEYS) - {"runtime_version"}:
        if type(value.get(key)) is not int or value[key] < 1:
            raise ValueError(f"Invalid runtime metadata: {key}")
    for prefix in ("ui_protocol", "runtime_schema"):
        if value[prefix + "_min"] > value[prefix + "_max"]:
            raise ValueError(f"Invalid runtime range: {prefix}")


def bundle_runtime_metadata(app: Path) -> dict:
    """Refuse drift before signing/publishing artifacts from the same build."""
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    backend = json.loads((app / "Contents/Resources/backend.json").read_text())
    expected = runtime_metadata(info["CFBundleShortVersionString"], info["CFBundleVersion"])
    for source in (info, backend):
        actual = {key: source.get(key) for key in RUNTIME_KEYS}
        validate_runtime(actual)
        if actual != expected:
            raise ValueError("Bundle runtime compatibility differs from the runtime contract")
    return expected


def distribution(cache: Path) -> Path:
    from distribution import verified_archive
    spec = json.loads((HERE / "sparkle-runtime.json").read_text())
    archive = verified_archive(spec, cache)
    # Re-extract verified bytes each time; never trust an unverified unpacked cache.
    destination = cache / ("Sparkle-" + spec["version"])
    with tempfile.TemporaryDirectory(prefix="sparkle-", dir=cache) as scratch:
        root = Path(scratch).resolve()
        with tarfile.open(archive, "r:xz") as source:
            for member in source.getmembers():
                path = (root / member.name).resolve()
                if not path.is_relative_to(root) or not (member.isfile() or member.isdir() or member.issym()):
                    raise ValueError("Unsafe Sparkle archive path")
                if member.issym() and not (path.parent / member.linkname).resolve().is_relative_to(root):
                    raise ValueError("Unsafe Sparkle archive link")
                source.extract(member, root, filter="data")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(root, destination, symlinks=True)
    return destination


def embed(app: Path, architecture: str, cache: Path) -> Path:
    source = distribution(cache)
    framework = source / "Sparkle.framework"
    info = plistlib.loads((framework / "Resources/Info.plist").read_bytes())
    spec = json.loads((HERE / "sparkle-runtime.json").read_text())
    if info["CFBundleShortVersionString"] != spec["version"] or info["LSMinimumSystemVersion"] != "12.0":
        raise ValueError("Unexpected Sparkle framework version or deployment target")
    subprocess.run(["lipo", str(framework / "Sparkle"), "-verify_arch", architecture], check=True)
    target = app / "Contents/Frameworks/Sparkle.framework"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(framework, target, symlinks=True)
    resources = app / "Contents/Resources"
    shutil.copyfile(source / "LICENSE", resources / "Sparkle-LICENSE")
    shutil.copyfile(HERE / "sparkle-runtime.json", resources / "sparkle-runtime.json")
    return source


def verify_signature(path: Path, signature: str, key: str) -> None:
    """Verify with OS crypto, including against the configured public key."""
    raw_key = public_key(key)
    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except ValueError as exc:
        raise ValueError("Invalid Sparkle signature") from exc
    if len(raw_signature) != 64:
        raise ValueError("Invalid Sparkle signature length")
    with tempfile.TemporaryDirectory(prefix="sparkle-verify-") as directory:
        root = Path(directory)
        signature_file = root / "signature"
        signature_file.write_bytes(raw_signature)
        key_file = root / "key"
        if sys.platform == "darwin":
            key_file.write_bytes(raw_key)
            script = root / "verify.swift"
            script.write_text('''import Foundation
import CryptoKit
let args = CommandLine.arguments
let key = try Curve25519.Signing.PublicKey(rawRepresentation: Data(contentsOf: URL(fileURLWithPath: args[1])))
let signature = try Data(contentsOf: URL(fileURLWithPath: args[2]))
let bytes = try Data(contentsOf: URL(fileURLWithPath: args[3]), options: .mappedIfSafe)
exit(key.isValidSignature(signature, for: bytes) ? 0 : 1)
''')
            command = ["xcrun", "swift", str(script), str(key_file), str(signature_file), str(path)]
        else:
            # RFC 8410 SubjectPublicKeyInfo prefix for an Ed25519 raw public key.
            key_file.write_bytes(bytes.fromhex("302a300506032b6570032100") + raw_key)
            command = ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key_file),
                       "-keyform", "DER", "-rawin", "-in", str(path), "-sigfile", str(signature_file)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Sparkle signature verification failed: {path.name}")


def verify_appcast(path: Path, key: str) -> ET.Element:
    # Sparkle 2.10.1 SPUExtractSignedFeed.m signs all bytes before the final
    # signature comment. Require a single terminal block; reject unsigned XML.
    raw = path.read_bytes()
    prefix = b"<!-- sparkle-signatures:\n"
    if raw.count(prefix) != 1:
        raise ValueError("Missing or ambiguous signed appcast")
    content, block = raw.split(prefix)
    match = re.fullmatch(rb"edSignature: ([A-Za-z0-9+/=]+)\nlength: ([0-9]+)\n-->\s*", block)
    if not match or int(match[2]) != len(content):
        raise ValueError("Invalid signed appcast length or signature block")
    with tempfile.TemporaryDirectory(prefix="sparkle-feed-") as directory:
        unsigned = Path(directory) / "content.xml"
        unsigned.write_bytes(content)
        verify_signature(unsigned, match[1].decode("ascii"), key)
    return ET.fromstring(content)


def sign_file(tool: Path, path: Path, key_file: Path | None) -> str:
    command = [str(tool), "-p"]
    if key_file:
        if key_file.is_symlink() or not key_file.is_file() or key_file.stat().st_mode & 0o077:
            raise ValueError("Sparkle private key file must be a private regular file (mode 600)")
        command += ["--ed-key-file", str(key_file)]
    else:
        command += ["--account", KEYCHAIN_ACCOUNT]
    command.append(str(path))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        # Tool diagnostics may contain credential material. Never include them.
        raise ValueError("Sparkle signing failed; configure the private key file or signing Keychain")
    return result.stdout.strip()


def create_appcast(path: Path, *, version: str, build: str, architecture: str,
                   url: str, size: int, signature: str, runtime: dict) -> None:
    validate_runtime(runtime)
    https_url(url)
    if architecture not in ("arm64", "x86_64") or not re.fullmatch(r"[1-9][0-9]*", build):
        raise ValueError("Invalid update architecture or build number")
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Ribbon Field"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Ribbon Field {version}"
    for name, value in (("version", build), ("shortVersionString", version), ("minimumSystemVersion", "12.0")):
        ET.SubElement(item, f"{{{SPARKLE_NS}}}{name}").text = value
    ET.SubElement(item, "ribbonRuntime").text = json.dumps(runtime, sort_keys=True, separators=(",", ":"))
    ET.SubElement(item, "ribbonArchitecture").text = architecture
    ET.SubElement(item, "enclosure", {"url": url, "length": str(size), "type": "application/zip",
                                     f"{{{SPARKLE_NS}}}edSignature": signature})
    ET.indent(rss)
    path.write_bytes(ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n")


def package_update(app: Path, output: Path, *, tools: Path, key_file: Path | None,
                   architecture: str, channel: str) -> dict:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    version, build = info["CFBundleShortVersionString"], info["CFBundleVersion"]
    expected_feed = f"{DOWNLOAD_URL}/updates/{channel}/{architecture}.xml"
    if info.get("SUFeedURL") != expected_feed:
        raise ValueError(f"Release update feed must be {expected_feed}")
    if output.exists() or output.is_symlink():
        raise ValueError("Refusing to overwrite an existing update archive")
    runtime = bundle_runtime_metadata(app)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    from distribution import run_stapler
    run_stapler("validate", app)
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output)], check=True)
    tool = tools / "bin/sign_update"
    signature = sign_file(tool, output, key_file)
    verify_signature(output, signature, info["SUPublicEDKey"])
    feed = output.with_suffix(".xml")
    url = f"{DOWNLOAD_URL}/releases/{version}/{output.name}"
    create_appcast(feed, version=version, build=build, architecture=architecture, url=url,
                   size=output.stat().st_size, signature=signature, runtime=runtime)
    sign_file(tool, feed, key_file)
    verify_appcast(feed, info["SUPublicEDKey"])
    # Verify with Sparkle's native feed parser/signature implementation.
    command = [str(tool), "--verify"] + (["--ed-key-file", str(key_file)] if key_file else ["--account", KEYCHAIN_ACCOUNT]) + [str(feed)]
    if subprocess.run(command, capture_output=True).returncode:
        raise ValueError("Sparkle appcast signature verification failed")
    metadata = {"version": version, "build_number": build, "architecture": architecture,
                "channel": channel, "feed_url": expected_feed, "public_key": info["SUPublicEDKey"],
                "runtime": runtime, "signature": signature, "size": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "appcast_sha256": hashlib.sha256(feed.read_bytes()).hexdigest()}
    output.with_suffix(".zip.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Fetch/verify the pinned framework and signing tools")
    parser.add_argument("--cache", type=Path, default=HERE.parents[1] / "build/macos/sparkle-cache")
    args = parser.parse_args()
    if args.fetch:
        print(distribution(args.cache))
