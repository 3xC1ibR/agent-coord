"""Portable runtime, code signing, and disk-image helpers for build.py."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
MACHO_MAGIC = {bytes.fromhex(value) for value in
               ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def runtime_spec(architecture: str) -> dict:
    manifest = json.loads((HERE / "python-runtime.json").read_text())
    target = manifest["targets"][architecture]
    filename = (f"cpython-{manifest['version']}+{manifest['release']}-"
                f"{target['triple']}-install_only_stripped.tar.gz")
    return {**target, "version": manifest["version"], "release": manifest["release"],
            "filename": filename,
            "url": f"{manifest['source']}/releases/download/{manifest['release']}/{filename}"}


def verified_archive(spec: dict, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["filename"]
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as download:
            temporary = Path(download.name)
            try:
                with urllib.request.urlopen(spec["url"], timeout=60) as response:
                    shutil.copyfileobj(response, download)
                download.close()
                verify_hash(temporary, spec["sha256"])
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    verify_hash(archive, spec["sha256"])
    return archive


def verify_hash(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError(f"Python runtime checksum mismatch: {path}")


def extract_runtime(archive: Path, destination: Path) -> None:
    # Validate paths and links before extracting, including on Python 3.10
    # builders that do not provide tarfile's data filter.
    with tempfile.TemporaryDirectory(prefix="ribbon-runtime-") as scratch:
        root = Path(scratch).resolve()
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                path = (root / member.name).resolve()
                if not path.is_relative_to(root / "python"):
                    raise ValueError(f"Unsafe runtime archive path: {member.name}")
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise ValueError(f"Unsupported runtime archive entry: {member.name}")
                if member.issym():
                    link = (path.parent / member.linkname).resolve()
                    if not link.is_relative_to(root / "python"):
                        raise ValueError(f"Unsafe runtime archive link: {member.name}")
                source.extract(member, root)
        shutil.copytree(root / "python", destination, symlinks=True)


def bundle_runtime(resources: Path, architecture: str, cache: Path) -> dict:
    spec = runtime_spec(architecture)
    extract_runtime(verified_archive(spec, cache), resources / "runtime")
    # install_only archives omit the bundled native dependencies' license texts.
    # Retrieve those and their provenance from the matching full distribution.
    full = {**spec, "sha256": spec["full_sha256"],
            "filename": spec["filename"].replace("install_only_stripped.tar.gz", "pgo+lto-full.tar.zst"),
            "url": spec["url"].replace("install_only_stripped.tar.gz", "pgo+lto-full.tar.zst")}
    archive = verified_archive(full, cache)
    with tempfile.TemporaryDirectory(prefix="ribbon-licenses-") as scratch:
        subprocess.run(["/usr/bin/tar", "-xf", str(archive), "-C", scratch,
                        "python/licenses", "python/PYTHON.json"], check=True)
        shutil.copytree(Path(scratch) / "python/licenses", resources / "runtime-licenses")
        shutil.copyfile(Path(scratch) / "python/PYTHON.json", resources / "runtime-licenses/PYTHON.json")
    (resources / "python-runtime.json").write_text(json.dumps(spec, indent=2) + "\n")
    return spec


def signing_command(identity: str) -> list[str]:
    args = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        args += ["--options", "runtime", "--timestamp"]
    # Ad hoc signatures have no team identity and cannot satisfy hardened
    # library validation when Python loads its bundled extension modules.
    return args


def sign_app(app: Path, identity: str) -> None:
    # Sign nested native code before sealing the app. --deep is for verification,
    # not signing: Python and wheel dylibs must each have our team identity.
    for path in sorted(app.rglob("*"), key=lambda p: (-len(p.parts), str(p))):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            native = stream.read(4) in MACHO_MAGIC
        if native:
            subprocess.run(signing_command(identity) + [str(path)], check=True, capture_output=True)
    subprocess.run(signing_command(identity) + [str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def notarize(artifact: Path, profile: str) -> None:
    result = subprocess.run(["xcrun", "notarytool", "submit", str(artifact),
                             "--keychain-profile", profile, "--wait", "--output-format", "json"],
                            check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    artifact.with_suffix(artifact.suffix + ".notary.json").write_text(json.dumps(report, indent=2) + "\n")
    if report.get("status") != "Accepted":
        raise ValueError(f"Notarization was not accepted: {report}")


def staple(artifact: Path) -> None:
    subprocess.run(["xcrun", "stapler", "staple", str(artifact)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(artifact)], check=True)


def create_dmg(app: Path, output: Path, *, identity: str, profile: str | None,
               version: str, architecture: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise ValueError(f"Refusing to overwrite an existing release: {output}")
    with tempfile.TemporaryDirectory(prefix="ribbon-dmg-") as scratch:
        stage = Path(scratch) / "contents"
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        note = ("Ribbon Field\n\n"
                "Drag Ribbon Field.app to Applications, then open it from Applications.\n"
                "Requires macOS 12 or later. Python and the app backend are included.\n"
                "Install and sign in to Codex CLI or Claude Code to start agent conversations.\n"
                "No repository checkout, Xcode, or separate Python installation is needed.\n")
        if not profile:
            note += "\nTEST BUILD: This image is not notarized for public distribution.\n"
        (stage / "Read Me.txt").write_text(note)
        staged_image = Path(scratch) / output.name
        subprocess.run(["hdiutil", "create", "-volname", "Ribbon Field", "-srcfolder", str(stage),
                        "-format", "UDZO", "-fs", "HFS+", str(staged_image)], check=True)
        if identity != "-":
            subprocess.run(signing_command(identity) + [str(staged_image)], check=True)
        if profile:
            notarize(staged_image, profile)
            staple(staged_image)
            subprocess.run(["spctl", "--assess", "--type", "open", "--context",
                            "context:primary-signature", "--verbose=2", str(staged_image)], check=True)
            shutil.copyfile(staged_image.with_suffix(".dmg.notary.json"),
                            output.with_suffix(".dmg.notary.json"))
        shutil.copyfile(staged_image, output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".dmg.sha256").write_text(f"{digest}  {output.name}\n")
    output.with_suffix(".dmg.json").write_text(json.dumps({
        "version": version, "architecture": architecture, "minimum_macos": "12.0",
        "sha256": digest, "signing_identity": identity, "notarized": bool(profile),
        "python": runtime_spec(architecture),
    }, indent=2) + "\n")
