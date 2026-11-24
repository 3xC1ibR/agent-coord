"""Observe and retire existing signed-predecessor intake without creating one.

Normal operation uses runtime_edge. Only an already recorded legacy service or
interrupted replacement may invoke its exact retained, verified helper. Its
private receipts, authorization and service identity remain predecessor-owned.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from . import desktop_runtime as runtime

def gateway_directory(database):
    path = runtime.runtime_directory(database) / "interaction-gateway"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Interaction gateway directory permissions are unsafe.")
    return path


def _private_record(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Interaction gateway record permissions are unsafe.")
        value = json.loads(stream.read(65537))
    if not isinstance(value, dict):
        raise RuntimeError("Invalid interaction gateway record.")
    return value


def _retained_payload(directory, value):
    if not isinstance(value, str):
        raise RuntimeError("Interaction gateway requires an exact retained payload.")
    path = Path(value)
    if (path != path.resolve() or path.parent != directory.parent / "payloads"
            or not re.fullmatch(r"[0-9a-f]{64}", path.name)):
        raise RuntimeError("Interaction gateway requires an exact retained payload.")
    return path


def _validate_helper(directory, value):
    payload = _retained_payload(directory, value)
    manifest = runtime.read_json(payload / "backend-snapshot.json")
    for name in ("desktop_runtime", "runtime_gateway_lifecycle", "runtime_interaction_gateway"):
        relative = "agent_coord/" + name + ".py"
        if manifest.get(relative) != hashlib.sha256((payload / "backend" / relative).read_bytes()).hexdigest():
            raise RuntimeError("Interaction gateway helper does not match the packaged backend manifest.")
    if runtime.payload_digest(payload) != payload.name or not runtime.compatible(runtime.read_json(payload / "backend.json")):
        raise RuntimeError("Retained interaction gateway payload failed verification.")
    return payload



_HELPER = """import json, sys
from agent_coord import runtime_gateway_lifecycle as gateway
value = json.load(sys.stdin)
operation = value['operation']
if operation == 'status':
    result = gateway.status(value['database'], value['result'])
elif operation == 'retire':
    archive = gateway.retire(value['database'])
    result = str(archive) if archive is not None else None
elif operation == 'activate':
    result = gateway.activate(value['record'], request_id=value['request_id'], payload=value['payload'])
else:
    raise RuntimeError('Unsupported predecessor operation')
print(json.dumps(result), flush=True)
"""


def _helper_payload(database):
    directory = gateway_directory(database)
    record = _private_record(directory / "gateway.json")
    replacement = _private_record(directory.parent / "interaction-gateway-replacement.json")
    if record is None and replacement is None:
        # An archived directory is deliberately outside this active path. Never
        # recreate an engine because only its old receipt archive still exists.
        if any((directory / name).exists() for name in
               ("binding.json", "message-intake.sqlite3", "activation-request.json")):
            raise RuntimeError("Pending predecessor intake has no verified helper identity; preserve its receipts for recovery.")
        return None
    identity = record if record is not None else replacement
    if identity.get("database") != str(Path(database).resolve()):
        raise RuntimeError("Retained interaction service belongs to another database.")
    value = record.get("payload") if record is not None else replacement.get("target")
    return _validate_helper(directory, value)


def _run(database, operation, **values):
    database = str(Path(database).expanduser().resolve())
    payload = _helper_payload(database)
    if payload is None:
        return None
    # The old helper authenticates its existing service and recovers only its
    # recorded authorization. No candidate payload is supplied to status(), so
    # it cannot manufacture a new intake service or replace a staged target.
    command = [runtime.payload_command(payload)[0], "-B", "-u", "-c", _HELPER]
    result = subprocess.run(command,
        input=json.dumps({"database": database, "operation": operation, **values}),
        capture_output=True, text=True, timeout=45,
        env=runtime.payload_environment(payload), cwd=str(Path.home()))
    if result.returncode:
        # Do not expose command credentials or receipt content in diagnostics.
        raise RuntimeError("The retained interaction helper could not confirm its existing intake; preserve the predecessor and retry recovery.")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("The retained interaction helper returned an invalid receipt.") from error


def status(database, result):
    observed = _run(database, "status", result=result)
    return result if observed is None else observed


def attach(database, record, candidate, require_confirmation):
    observed = status(database, runtime.control(record, "status"))
    if observed.get("interaction_url") and not observed.get("interaction_settled"):
        # Finish the already selected predecessor transition before any new
        # candidate publication. Accepted input remains owned by that service.
        return observed
    retire(database)
    return runtime.control(record, "attach", payload=str(candidate), require_confirmation=require_confirmation)


def activate(record, *, request_id, payload, timeout=35):
    existing = _run(record["database"], "activate", record=record, request_id=request_id, payload=payload)
    if existing is None:
        return runtime.control(record, "activate-update", request_id=request_id, payload=payload, timeout=timeout)
    return existing


def retire(database):
    result = _run(database, "retire")
    return Path(result) if result is not None else None


def serve_existing(database, owner_fd, ready_fd):
    payload = _helper_payload(database)
    if payload is None or owner_fd is None or ready_fd is None:
        raise RuntimeError("A new interaction gateway cannot be created; use the stable local runtime listener.")
    command = runtime.payload_command(payload) + ["--runtime-interaction-gateway", "--db", str(Path(database).resolve()),
               "--inherit-owner-fd", str(owner_fd), "--ready-fd", str(ready_fd)]
    for descriptor in (owner_fd, ready_fd):
        os.set_inheritable(descriptor, True)
    os.execve(command[0], command, runtime.payload_environment(payload))
