#!/usr/bin/env python3
"""Validate and publish a macOS release to R2 using the AWS CLI's S3 API.

No third-party Python packages are required. Credentials come from the AWS CLI
environment. Without --publish this only validates local artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request


ARCHITECTURES = ("arm64", "x86_64")
BUCKET = "ribbon-field-downloads"
DOWNLOAD_URL = "https://downloads.ribbonfield.com"
ACCOUNT_ID = "af320d6747244dea99edc8895d3d00f4"


def digest(stream) -> str:
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return digest(stream)


def release_files(directory: Path, version: str, commit: str) -> tuple[list[Path], dict]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Version must be numeric X.Y.Z")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Source commit must be a full Git SHA")
    files = []
    release = {"version": version, "source_commit": commit, "downloads": {}}
    for arch in ARCHITECTURES:
        name = f"Ribbon-Field-{version}-{arch}.dmg"
        image = directory / name
        metadata = directory / (name + ".json")
        checksum = directory / (name + ".sha256")
        notary = directory / (name + ".notary.json")
        validation = directory / (name + ".validation.json")
        provenance = directory / (name + ".provenance.json")
        for path in (image, metadata, checksum, notary, validation, provenance):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing or unsafe release artifact: {path.name}")
        manifest = json.loads(metadata.read_text())
        if (manifest.get("version") != version or manifest.get("architecture") != arch
                or manifest.get("notarized") is not True
                or not manifest.get("signing_identity", "").startswith("Developer ID Application:")):
            raise ValueError(f"Not a signed, notarized {arch} release: {name}")
        sha256 = file_digest(image)
        if (manifest.get("sha256") != sha256
                or checksum.read_text().strip() != f"{sha256}  {name}"):
            raise ValueError(f"Checksum mismatch: {name}")
        if json.loads(notary.read_text()).get("status") != "Accepted":
            raise ValueError(f"Notarization was not accepted: {name}")
        report = json.loads(validation.read_text())
        if (report.get("architecture") != arch
                or Path(report.get("disk_image", "")).name != name
                or any(report.get(key) is not True for key in ("relocated", "backend", "cli"))):
            raise ValueError(f"Distribution validation is incomplete: {name}")
        source = json.loads(provenance.read_text())
        if source.get("source_commit") != commit or source.get("version") != version:
            raise ValueError(f"Source provenance mismatch: {name}")
        files.extend((image, checksum, metadata, provenance))
        release["downloads"][arch] = {
            "url": f"{DOWNLOAD_URL}/releases/{version}/{name}",
            "sha256": sha256,
            "size": image.stat().st_size,
        }
    return files, release


class R2:
    def __init__(self):
        self.command = ["aws", "--endpoint-url", f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
                        "--region", "auto", "--no-cli-pager", "s3api"]

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.command + list(args), capture_output=True, text=True,
                              timeout=600, env={**os.environ, "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
                                                "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"})

    def exists_identical(self, key: str, path: Path) -> bool:
        result = self.run("head-object", "--bucket", BUCKET, "--key", key)
        if result.returncode:
            if "(404)" in result.stderr or "(NoSuchKey)" in result.stderr:
                return False
            raise RuntimeError(f"Cannot inspect {key}: {result.stderr}")
        with tempfile.TemporaryDirectory(prefix="ribbon-release-check-") as directory:
            downloaded = Path(directory) / "object"
            result = self.run("get-object", "--bucket", BUCKET, "--key", key, str(downloaded))
            if result.returncode:
                raise RuntimeError(f"Cannot read existing {key}: {result.stderr}")
            if file_digest(downloaded) != file_digest(path):
                raise ValueError(f"Refusing to replace published bytes: {key}; use a new version")
        return True

    def put(self, key: str, path: Path) -> None:
        content_type = ("application/x-apple-diskimage" if path.suffix == ".dmg" else
                        "application/json" if path.suffix == ".json" else "text/plain")
        result = self.run("put-object", "--bucket", BUCKET, "--key", key, "--body", str(path),
                          "--if-none-match", "*", "--content-type", content_type,
                          "--cache-control", "public, max-age=31536000, immutable")
        if result.returncode:
            raise RuntimeError(f"Upload failed for {key}: {result.stderr}")


def verify_download(key: str, path: Path) -> None:
    with urllib.request.urlopen(f"{DOWNLOAD_URL}/{key}", timeout=120) as response:
        if digest(response) != file_digest(path):
            raise ValueError(f"Public download checksum mismatch: {key}")


def publish(files: list[Path], release: dict, r2: R2) -> None:
    prefix = f"releases/{release['version']}/"
    with tempfile.TemporaryDirectory(prefix="ribbon-release-") as directory:
        manifest = Path(directory) / "release.json"
        manifest.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
        # Detect all conflicts before uploading anything. If a previous attempt
        # stopped partway through, identical objects can be safely reused.
        objects = [(prefix + path.name, path) for path in [*files, manifest]]
        existing = {key: r2.exists_identical(key, path) for key, path in objects}
        # release.json is a completion marker: upload it only after every public
        # artifact has been downloaded again and its checksum verified.
        for key, path in objects:
            if not existing[key]:
                r2.put(key, path)
            verify_download(key, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--publish", action="store_true", help="Upload verified files to the public R2 bucket")
    args = parser.parse_args()
    files, release = release_files(args.directory, args.version, args.commit)
    if args.publish:
        publish(files, release, R2())
    print(json.dumps({"published": args.publish, **release}, indent=2))


if __name__ == "__main__":
    main()
