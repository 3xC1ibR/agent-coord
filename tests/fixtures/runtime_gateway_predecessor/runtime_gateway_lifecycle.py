"""Attach the desktop to a single private interaction gateway during a signed upgrade."""
from contextlib import closing
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

from . import desktop_runtime as runtime, runtime_update


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


def _port(url):
    parsed = urlsplit(url)
    if not (parsed.scheme == "http" and parsed.port and parsed.netloc == f"127.0.0.1:{parsed.port}"
            and url == f"http://127.0.0.1:{parsed.port}/"):
        raise RuntimeError("Invalid interaction gateway origin.")
    return parsed.port


class GatewayControlRejected(RuntimeError):
    """The authenticated gateway explicitly declined a control operation."""


def _request(record, route, value=None, *, timeout=2):
    # A control credential may be sent only to this exact loopback origin.
    # HTTPConnection neither follows redirects nor inherits proxy settings.
    with closing(http.client.HTTPConnection("127.0.0.1", _port(record["url"]), timeout=timeout)) as connection:
        headers = {"Authorization": "Bearer " + record["control_token"], "Content-Type": "application/json"}
        connection.request("GET" if value is None else "POST", "/_runtime/" + route,
                     None if value is None else json.dumps(value).encode(), headers)
        response = connection.getresponse()
        if response.status in (400, 403, 404, 409, 413, 422):
            raise GatewayControlRejected(f"Interaction gateway {route} returned HTTP {response.status}.")
        if response.status != 200:
            raise RuntimeError(f"Interaction gateway {route} returned HTTP {response.status}.")
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Interaction gateway response is too large.")
    result = json.loads(raw)
    if not isinstance(result, dict) or any(result.get(key) != record[key] for key in ("instance", "database", "payload")):
        raise RuntimeError("Interaction gateway response belongs to another instance.")
    return result


def _retained_payload(directory, value):
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


def _read_identity(directory, database):
    record = _private_record(directory / "gateway.json")
    binding = _private_record(directory / "binding.json")
    if not record:
        if binding:
            raise RuntimeError("Previously bound interaction gateway discovery is missing.")
        return None, None
    if record.get("database") != database or not all(isinstance(record.get(k), str) and record[k]
            for k in ("url", "payload", "instance", "control_token")):
        raise RuntimeError("Interaction gateway discovery does not match this database.")
    _retained_payload(directory, record["payload"])
    _port(record["url"])
    pin = {"database": database, "payload": record["payload"], "url": record["url"],
           "credential_hash": hashlib.sha256(record["control_token"].encode()).hexdigest()}
    if binding and binding != pin:
        raise RuntimeError("Interaction gateway origin or credential changed before retirement.")
    identity = _request(record, "identity")
    if identity.get("url") != record["url"] or type(identity.get("settled")) is not bool:
        raise RuntimeError("Invalid interaction gateway identity.")
    if binding is None:
        runtime.atomic_json(directory / "binding.json", pin)
    return record, identity


def serve(database, directory, owner_fd, ready_fd):
    if owner_fd is None or ready_fd is None:
        raise RuntimeError("Interaction gateway startup requires an ownership reservation and readiness pipe.")
    try:
        owner = os.fstat(owner_fd)
        saved = (directory / "owner.lock").stat()
        if (owner.st_dev, owner.st_ino) != (saved.st_dev, saved.st_ino):
            raise RuntimeError("Invalid interaction gateway ownership reservation.")
        import fcntl
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from . import runtime_interaction_gateway
        stopped = threading.Event()
        recovery = threading.Thread(target=_recover_authorization, args=(database, directory, stopped),
                                    name="gateway-activation-recovery", daemon=True)
        recovery.start()
        try:
            return runtime_interaction_gateway.serve(database, directory, ready_fd)
        finally:
            stopped.set()
            recovery.join(timeout=1)
    finally:
        os.close(owner_fd)


def _start(database, directory, payload):
    try:
        owner = runtime.lock_file(directory / "owner.lock")
    except BlockingIOError:
        raise RuntimeError("The interaction gateway owner is unavailable. No second owner was started.") from None
    try:
        payload = _validate_helper(directory, payload)
        read, write = os.pipe()
        try:
            command = runtime.payload_command(payload) + ["--runtime-interaction-gateway", "--db", database,
                       "--ready-fd", str(write), "--inherit-owner-fd", str(owner)]
            log_fd = os.open(directory / "gateway.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(log_fd, "ab") as log:
                child = subprocess.Popen(command, pass_fds=(owner, write), env=runtime.payload_environment(payload),
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                threading.Thread(target=child.wait, daemon=True).start()
            os.close(write)
            write = None
            if not select.select([read], [], [], 20)[0] or os.read(read, 64) != b"ready\n":
                raise RuntimeError("The interaction gateway did not confirm readiness. Check its private gateway.log.")
        finally:
            if write is not None:
                os.close(write)
            os.close(read)
    finally:
        os.close(owner)


def _stop_settled(directory, record, identity):
    if not identity["settled"]:
        raise RuntimeError("Interaction gateway has unfinished intake.")
    receipt = _request(record, "retire", {})
    if receipt.get("retired") is not True:
        raise RuntimeError("The preceding interaction gateway did not confirm retirement.")
    deadline = time.monotonic() + 20
    while not runtime.owner_available(directory):
        if time.monotonic() >= deadline:
            raise RuntimeError("The retired interaction gateway still owns its service.")
        time.sleep(.05)


def retire(database):
    """Retire an authenticated settled service and preserve its complete receipts."""
    database = str(Path(database).resolve())
    directory = gateway_directory(database)
    guard = runtime.lock_file(directory.parent / "interaction-gateway-launch.lock", blocking=True)
    try:
        _finish_replacement(directory, database)
        record, identity = _read_identity(directory, database)
        if not record:
            return None
        _stop_settled(directory, record, identity)
        archive = directory.with_name("interaction-gateway-retired-" + uuid.uuid4().hex)
        os.rename(directory, archive)
        return archive
    finally:
        os.close(guard)


def _replace_settled(directory, record, identity, candidate):
    # Verify the replacement before asking the previous service to exit.
    _validate_helper(directory, candidate)
    _stop_settled(directory, record, identity)
    # Preserve receipts and the original browser origin. A durable replacement
    # record covers a crash between archiving the directory and seeding the new one.
    archive = directory.with_name("interaction-gateway-retired-" + uuid.uuid4().hex)
    intent = directory.parent / "interaction-gateway-replacement.json"
    runtime.atomic_json(intent, {"archive": archive.name, "database": record["database"], "target": str(candidate)})
    _finish_replacement(directory, record["database"])


def _finish_replacement(directory, database):
    intent = directory.parent / "interaction-gateway-replacement.json"
    replacement = _private_record(intent)
    if not replacement:
        return
    if replacement.get("database") != database or not re.fullmatch(r"interaction-gateway-retired-[0-9a-f]{32}", replacement["archive"]):
        raise RuntimeError("Invalid recorded gateway replacement.")
    target = _retained_payload(directory, replacement["target"])
    archive = directory.parent / replacement["archive"]
    if not archive.exists():
        if not runtime.owner_available(directory):
            raise RuntimeError("Gateway retirement is still in progress.")
        os.rename(directory, archive)
    previous = _private_record(archive / "gateway.json")
    if previous is None or previous.get("database") != database:
        raise RuntimeError("Retired interaction origin is unavailable.")
    directory.mkdir(mode=0o700, exist_ok=True)
    runtime.atomic_json(directory / "gateway.json", {**previous, "payload": str(target), "settled": False})
    intent.unlink()


def _ensure(database, payload=None):
    database = str(Path(database).resolve())
    directory = gateway_directory(database)
    if not directory.exists():
        if payload is None and not (directory.parent / "interaction-gateway-replacement.json").exists():
            return None, None
        directory.mkdir(mode=0o700, exist_ok=True)
    guard = runtime.lock_file(directory.parent / "interaction-gateway-launch.lock", blocking=True)
    try:
        _finish_replacement(directory, database)
        existing = _private_record(directory / "gateway.json")
        if runtime.owner_available(directory):
            if existing is None and _private_record(directory / "binding.json") is not None:
                raise RuntimeError("Cannot recover the bound interaction gateway without its original discovery.")
            if existing is None and payload is None:
                return None, None
            if existing and existing.get("database") != database:
                raise RuntimeError("Retained interaction gateway belongs to a different database.")
            _start(database, directory, existing["payload"] if existing else payload)
        record, identity = _read_identity(directory, database)
        if record is None:
            raise RuntimeError("Interaction gateway has an owner without authenticated discovery.")
        released = (_private_record(directory / "activation-request.json") or {}).get("phase") == "released"
        if payload is not None and (record["payload"] != str(payload) or released and identity["settled"]):
            # A later explicit staging may follow a rejected attempt. Archive
            # its final receipts, keep the same origin and start with a new
            # authorization namespace, even for the same candidate.
            _replace_settled(directory, record, identity, payload)
            _start(database, directory, payload)
            record, identity = _read_identity(directory, database)
        return record, identity
    finally:
        os.close(guard)


def status(database, result, *, candidate=None):
    record, identity = _ensure(database, candidate)
    if record is None:
        return result
    intent = _private_record(gateway_directory(database) / "activation-request.json") or {}
    return {**result, "interaction_url": record["url"], "interaction_payload": record["payload"],
            "interaction_settled": identity["settled"], "interaction_request_id": intent.get("request_id")}


def attach(database, record, candidate, require_confirmation):
    directory = runtime.runtime_directory(database)
    update = runtime_update.read(directory)
    if update and update["phase"] not in runtime_update.TERMINAL:
        return status(database, runtime.control(record, "status"))
    if str(candidate) != record["payload"]:
        authorization = _private_record(directory / "update-authorization.json")
        if (authorization and authorization.get("source_instance") == record["instance"] and
                authorization.get("runtime_version") == runtime.read_json(candidate / "backend.json")["runtime_version"]):
            raise RuntimeError("A prior installer authorization would activate on attach. Its authorization remains pending.")
    result = runtime.control(record, "attach", payload=str(candidate), require_confirmation=require_confirmation)
    eligible = result.get("pending_confirmation") is True and result.get("pending") == str(candidate)
    return status(database, result, candidate=candidate if eligible else None)


_IDENTITY = ("request_id", "target", "source_instance")


def _matching_update(directory, values):
    update = runtime_update.read(directory)
    if update and update["id"] == values["request_id"]:
        if update["target"] != values["target"] or update.get("source_instance", values["source_instance"]) != values["source_instance"]:
            raise RuntimeError("This activation identity belongs to a different transition.")
        return update
    return None


def _release_preparation(path, intent, gateway, values, timeout):
    released = _request(gateway, "release-preparation", values, timeout=timeout)
    if released.get("released") is not True or any(released.get(k) != v for k, v in values.items()):
        raise RuntimeError("The gateway did not confirm release of the rejected preparation.")
    runtime.atomic_json(path, {**intent, "phase": "released"})


def _submit_authorization(record, directory, values, gateway, timeout, *, recovering=False):
    intent_path = directory / "interaction-gateway/activation-request.json"
    intent = _private_record(intent_path) if gateway else None
    if recovering and intent is None:
        raise RuntimeError("No stored authorization remains to recover.")
    if intent and any(intent.get(k) != v for k, v in values.items()):
        raise RuntimeError("Another gateway activation request is pending. Observe its original identity.")
    update = _matching_update(directory, values)
    if update:
        if intent and intent.get("phase") != "recorded":
            runtime.atomic_json(intent_path, {**intent, "phase": "recorded"})
        return {"instance": record["instance"], "database": record["database"], "update": update}
    previous = runtime_update.read(directory)
    if previous and previous["phase"] not in runtime_update.TERMINAL:
        raise RuntimeError("Another runtime update is already in progress.")
    if gateway:
        if gateway["payload"] != values["target"] or record["instance"] != values["source_instance"]:
            raise RuntimeError("The interaction gateway source or staged candidate changed.")
        if intent and intent.get("phase") in {"recorded", "released"}:
            raise RuntimeError("The original activation request already has a final disposition.")
        if intent and "previous_update_id" in intent and intent["previous_update_id"] != (previous or {}).get("id"):
            raise RuntimeError("A different update superseded the state preceding this authorization.")
        if intent and intent.get("phase") == "releasing":
            _release_preparation(intent_path, intent, gateway, values, timeout)
            raise GatewayControlRejected("The gateway released its rejected preparation.")
        active = runtime.control(record, "status", timeout=timeout)
        if (active.get("pending") != values["target"] or active.get("pending_confirmation") is not True
                or active.get("paused") or active.get("instance", record["instance"]) != values["source_instance"]):
            raise RuntimeError("The candidate is no longer held by the original serving coordinator.")
        if not intent:
            # Only an explicit activate call can create this durable authorization.
            # Discovery, attach and gateway startup never select a new request ID.
            intent = {**values, "phase": "authorized", "previous_update_id": (previous or {}).get("id")}
            runtime.atomic_json(intent_path, intent)
        try:
            prepared = _request(gateway, "prepare", values, timeout=timeout)
            if any(prepared.get(k) != v for k, v in values.items()):
                raise RuntimeError("The interaction gateway returned a different preparation identity.")
            if prepared.get("prepared") is False:
                raise GatewayControlRejected("The interaction gateway rejected this preparation.")
            if prepared.get("prepared") is not True:
                raise RuntimeError("The interaction gateway did not confirm this activation fence.")
        except GatewayControlRejected:
            intent = {**intent, "phase": "releasing"}
            runtime.atomic_json(intent_path, intent)
            _release_preparation(intent_path, intent, gateway, values, timeout)
            raise
    try:
        result = runtime.control(record, "activate-update", payload=values["target"], request_id=values["request_id"], timeout=timeout)
    except (OSError, ValueError, RuntimeError):
        update = _matching_update(directory, values)
        if not update:
            raise
        result = {"instance": record["instance"], "database": record["database"], "update": update}
    if intent:
        if (result.get("update") or {}).get("id") != values["request_id"] or not _matching_update(directory, values):
            raise RuntimeError("The original activation has not reached a confirmed durable outcome.")
        runtime.atomic_json(intent_path, {**intent, "phase": "recorded"})
    return result


def activate(record, *, request_id, payload, timeout=35):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", request_id):
        raise RuntimeError("Invalid backend activation request identity.")
    database = record["database"]
    directory = runtime.runtime_directory(database)
    guard = runtime.lock_file(directory / "interaction-activation.lock", blocking=True)
    try:
        values = {"request_id": request_id, "target": payload, "source_instance": record["instance"]}
        # A late query to the replacement can observe its predecessor's receipt.
        update = runtime_update.read(directory)
        if update and update["id"] == request_id:
            if update["target"] != payload:
                raise RuntimeError("This activation request was already used for a different target.")
            return {"instance": record["instance"], "database": database, "update": update}
        gateway, _ = _ensure(database)
        return _submit_authorization(record, directory, values, gateway, timeout)
    finally:
        os.close(guard)


def _recover_authorization(database, directory, stopped):
    """The detached gateway owns recovery even if the authorizing helper exits."""
    intent_path = directory / "activation-request.json"
    delay = .5
    last_error = None
    while not stopped.wait(delay):
        try:
            intent = _private_record(intent_path)
            if not intent:
                continue
            if intent.get("phase") in {"recorded", "released"}:
                return
            guard = runtime.lock_file(directory.parent / "interaction-activation.lock")
            try:
                intent = _private_record(intent_path)
                if not intent:
                    continue
                if intent.get("phase") in {"recorded", "released"}:
                    return
                values = {key: intent[key] for key in _IDENTITY}
                if _matching_update(directory.parent, values):
                    runtime.atomic_json(intent_path, {**intent, "phase": "recorded"})
                    return
                source = runtime.discover(database, timeout=2)
                if not source or source["instance"] != values["source_instance"]:
                    raise RuntimeError("Waiting for the originally authorized source.")
                gateway, _ = _read_identity(directory, database)
                if gateway:
                    _submit_authorization(source, directory.parent, values, gateway, 35, recovering=True)
            finally:
                os.close(guard)
        except BlockingIOError:
            continue  # The explicit activation helper still owns this request.
        except (OSError, http.client.HTTPException, ValueError, RuntimeError, TypeError, KeyError) as error:
            if str(error) != last_error:
                logging.getLogger(__name__).warning("Interaction activation remains pending: %s", error)
                last_error = str(error)
            # Retain ambiguous results. The next attempt checks the durable
            # coordinator receipt first and can use only the original identity.
            delay = min(5, delay * 2)
