"""Local desktop interaction service retained across a coordinator replacement.

Only the existing coordinator may control providers. During its offline snapshot
this service reads committed conversation state and journals ordinary composer
messages in a separate private database. Only the authenticated terminal update
outcome permits delivery to the next serving coordinator.
"""
from __future__ import annotations

from collections import deque
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import desktop_runtime as runtime
from .provider_transport import MAX_BATCH_BYTES
from .image_inputs import MAX_MESSAGE_BODY_BYTES
from .runtime_message_intake import IntakeFull, OrdinaryComposerReceiver, RuntimeMessageIntake, TransportNotAttempted, canonical_receipt
from .runtime_saved_views import SavedRuntimeViews
from .store import CoordinationError

BROWSER = "/api/browser/"
CACHEABLE = frozenset({"organization", "views", "desktop-projects", "workspaces"})
QUEUE_STATES = {"prepared", "draining", "source_draining"}
TEMPORARY = (OSError, http.client.HTTPException, sqlite3.Error, RuntimeError, ValueError)


def _private_json(path):
    if not path.exists():
        return {}
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise PermissionError("Unsafe private runtime record")
    return runtime.read_json(path)


def _connection(address, timeout):
    url = urlsplit(address)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Invalid local coordinator URL")
    return http.client.HTTPConnection("127.0.0.1", url.port, timeout=timeout)


def _exchange(address, method, path, body=None, *, headers=None, timeout=10):
    with contextlib.closing(_connection(address, timeout)) as connection:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()


class InteractionGateway:
    def __init__(self, database, directory, payload):
        self.database = Path(database).resolve()
        self.directory = Path(directory)
        self.payload = str(Path(payload).resolve())
        self.runtime_directory = runtime.runtime_directory(self.database)
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.upstream_streams = set()
        self.composer_waiters = {}
        self.writers = 0
        self.intake_writers = 0
        self.source_deliveries = 0
        self.draining_threads = {}
        self.last_dispatch = {}
        self.delivery_retry_after = {}
        self.delivery_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rf-update-input")
        self.revision = 0
        self.watch_secret = secrets.token_bytes(32)
        self.receipt_changes = deque(maxlen=256)
        self.fence = _private_json(self.directory / "transition.json")
        self.config = _private_json(self.directory / "browser-config.json")
        source_attachment = _private_json(self.directory / "source-authority.json")
        self.source = source_attachment.get("authority", {})
        self.prior_stop = source_attachment.get("previous_stop")
        self.terminal = _private_json(self.directory / "terminal-authority.json")
        self.intake = RuntimeMessageIntake(self.directory)
        self.views = None
        self.backend = None
        self.opened_at = None
        self.receiver = None
        self.admission_probe = False
        self.cache = {}
        self.instance = secrets.token_hex(16)
        self.url = None
        self.visit_origin = None
        self.visit_token = None
        self.token = None
        self.control_token = None

    def _authority(self, record):
        if not record or record.get("database") != str(self.database):
            raise CoordinationError("The coordinator database changed.")
        fields = ("instance", "payload", "url")
        if not all(isinstance(record.get(k), str) and record[k] for k in fields):
            raise CoordinationError("The coordinator identity is incomplete.")
        file = self.database.stat()
        return {**{k: record[k] for k in fields}, "database": str(self.database),
                "device": file.st_dev, "inode": file.st_ino}

    def _require_work(self, authority=None):
        proof = _private_json(self.runtime_directory / "stop-proof.json")
        instances = {p.get("instance") for p in (self.source, self.terminal, authority or {})}
        instances.add(self.fence.get("source_instance"))
        instances.discard(None)
        update = runtime_update_record(self.runtime_directory) if self.fence else {}
        matching_update = bool(self.fence and update.get("id") == self.fence.get("request_id"))
        # A candidate can be stopped after commit, before this helper ever
        # learns its instance. A newly recorded Stop for this DB supersedes
        # a pending transfer. A retained proof from before staging does not.
        unobserved_receiver_stop = (matching_update and not self.terminal and proof.get("instance")
                                    and proof.get("database") == str(self.database)
                                    and proof.get("instance") != self.prior_stop)
        if proof.get("instance") in instances or matching_update and update.get("cancelled") or unobserved_receiver_stop:
            raise CoordinationError("Work has been stopped")

    def _check_database(self, authority):
        file = self.database.stat()
        if (not authority or authority.get("database") != str(self.database)
                or (authority.get("device"), authority.get("inode")) != (file.st_dev, file.st_ino)):
            raise CoordinationError("The receiving database changed.")

    def _require_named_owner(self, authority):
        # Negative evidence from discovery does not authenticate another owner.
        # Confirmation of a receiver still requires the runtime control handshake.
        record = _private_json(self.runtime_directory / "discovery.json")
        if not authority or any(record.get(k) != authority[k] for k in ("instance", "database", "url")):
            raise CoordinationError("The receiving coordinator changed.")

    def _legacy_receipt(self, thread, body):
        authority = self._destination()
        self._check_database(authority)
        result = canonical_receipt(self.database, thread, body)
        self._check_database(authority)
        return result

    def _destination(self):
        if (self.fence.get("state") in {"draining", "settled"} and not self.fence.get("released")):
            return self.terminal
        return self.source

    def verify_origin(self, backend, *, require_admission=False):
        authority = self._destination()
        self._before_transport(authority)
        if require_admission:
            # Stop pauses admission before the old coordinator exits and
            # before its durable stop proof is available. Use one authenticated
            # serving-status call on the asynchronous delivery path.
            record = _private_json(self.runtime_directory / "discovery.json")
            action = "transfer-admission" if self.admission_probe else "status"
            status = runtime.control(record, action, timeout=3)
            if (status.get("paused") or status.get("status") != "serving"
                    or any(status.get(k) != authority[k] for k in ("url", "payload", "instance", "database"))):
                raise CoordinationError("The receiving coordinator changed or work is paused.")
            record = {**record, "payload": status["payload"]}
        else:
            record = runtime.discover(self.database, timeout=2)
        if (self._authority(record) != authority or not backend or
                (backend[0], backend[2], backend[3]) != (authority["url"], authority["instance"], authority["payload"])):
            raise CoordinationError("The receiving coordinator changed.")

    def _verify_delivery(self, thread, body):
        self.verify_origin(self.backend, require_admission=True)

    def _before_transport(self, authority):
        # A local refusal here proves no HTTP request has been made even if
        # the ownership preflight succeeded just before the durable claim.
        try:
            if self._destination() != authority:
                raise CoordinationError("The receiving coordinator changed.")
            self._require_work(authority)
            self._check_database(authority)
            self._require_named_owner(authority)
        except (OSError, ValueError, RuntimeError) as error:
            raise TransportNotAttempted(str(error)) from error

    def connect_backend(self, *, terminal=False):
        record = runtime.discover(self.database, timeout=1)
        if not record:
            raise RuntimeError("The coordinator is reconnecting")
        authority = self._authority(record)
        status = runtime.control(record, "status", timeout=3)
        if (any(status.get(k) != authority[k] for k in ("instance", "payload", "url", "database"))
                or status.get("status") != "serving" or status.get("paused")):
            raise CoordinationError("The authenticated coordinator is unavailable or changed.")
        if terminal:
            if self.source.get("database") != str(self.database) or (self.source["instance"], self.source["payload"]) != (
                    self.fence.get("source_instance"), self.fence.get("previous")):
                raise CoordinationError("The transition has no matching source authority.")
            update = status.get("update") or {}
            if (any(update.get(k) != value for k, value in (
                    ("id", self.fence["request_id"]), ("source_instance", self.source["instance"]),
                    ("target", self.fence["target"]), ("previous", self.source["payload"]),
                    ("database", str(self.database)))) or update.get("cancelled") or status.get("paused")
                    or status.get("status") != "serving" or update.get("requires_review")):
                raise RuntimeError("Waiting for an authenticated terminal update")
            if update.get("phase") == "committed":
                if status["payload"] != self.fence["target"]:
                    raise RuntimeError("The committed payload does not match the update")
            elif update.get("phase") not in {"rolled-back", "failed"} or status["payload"] != self.fence["previous"]:
                raise RuntimeError("The previous coordinator has not resumed")
        pin = self.terminal if terminal else self.source
        if pin and pin != authority:
            raise CoordinationError("The pinned coordinator or database changed.")
        if not terminal and not pin and (self.config or self.fence or self.intake.pending_threads()):
            raise CoordinationError("An existing transition has no verified source authority.")
        self._require_work(authority)
        admission_probe = status.get("transfer_admission_probe") == 1
        status, _, data = _exchange(record["url"], "GET", BROWSER + "config", timeout=2)
        if status != 200:
            raise RuntimeError("The coordinator configuration is unavailable")
        config = json.loads(data)
        token = config.pop("token")
        if not isinstance(token, str) or not token or config.get("remote") is not False:
            raise RuntimeError("The coordinator did not return a local browser configuration")
        if self._authority(runtime.discover(self.database, timeout=2)) != authority:
            raise CoordinationError("The coordinator changed during attachment.")
        if not pin:
            if terminal:
                runtime.atomic_json(self.directory / "terminal-authority.json", authority)
            else:
                self.prior_stop = _private_json(self.runtime_directory / "stop-proof.json").get("instance")
                runtime.atomic_json(self.directory / "source-authority.json",
                                    {"authority":authority, "previous_stop":self.prior_stop})
        if terminal:
            self.terminal = authority
        else:
            self.source = authority
        runtime.atomic_json(self.directory / "browser-config.json", config)
        receiver = None
        if config.get("ordinaryTransferReceipts") is True:
            def forward(thread, body):
                self._before_transport(authority)
                headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token,
                           "Authorization": "Bearer " + record["control_token"],
                           "X-Ribbon-Receiver-Instance": record["instance"], "X-Ribbon-Receiver-Payload": record["payload"],
                           "X-Ribbon-Receiver-Database": f"{receiver.device}:{receiver.inode}"}
                status, _, _ = _exchange(record["url"], "POST", BROWSER + "sessions/" + quote(thread, safe="") +
                                         "/update-input", json.dumps(body).encode(), headers=headers, timeout=15)
                if status != 200:
                    raise RuntimeError("The receiving coordinator did not confirm the submission")
            receiver = OrdinaryComposerReceiver(self.database, record["instance"], record["payload"], forward)
        views = SavedRuntimeViews(self.database, config.get("workspaceRoot"), current_directory=config.get("cwd"))
        with self.condition:
            previous_views = self.views
            self.backend = (record["url"], token, record["instance"], record["payload"])
            self.config = config
            self.views = views
            self.receiver = receiver
            self.admission_probe = admission_probe
        if previous_views is not None:
            previous_views.close()

    def _save_fence(self, fence):
        runtime.atomic_json(self.directory / "transition.json", fence)
        self.fence = fence
        if fence["state"] == "prepared":
            # End only the old HTTP subscription. The client reconnects with
            # its last complete provider cursor to this same stable origin.
            for stream in self.upstream_streams:
                with contextlib.suppress(OSError):
                    stream.shutdown(socket.SHUT_RDWR)
        self.receipt_completed()

    def identity(self):
        with self.condition:
            return dict(instance=self.instance, database=str(self.database), payload=self.payload,
                        url=self.url, settled=self.fence.get("state") == "settled")

    def browser_config(self):
        with contextlib.closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            event, completion = db.execute("""SELECT
                (SELECT COALESCE(MAX(sequence),0) FROM coordinator_events),
                (SELECT COALESCE(MAX(id),0) FROM turn_completions)""").fetchone()
        return {**self.config, "token": self.token, "eventCursor": event, "completionCursor": completion,
                "interactionContinuity": True, "conversationSnapshotControls": True,
                "visitTransport": ({"origin": self.visit_origin, "token": self.visit_token}
                                  if self.visit_origin else None)}

    def _source_status(self, wanted):
        record = runtime.discover(self.database, timeout=2)
        if (not record or self._authority(record) != self.source or
                record["instance"] != wanted["source_instance"] or wanted["target"] != self.payload):
            raise CoordinationError("The update source or target changed")
        self._require_work(self.source)
        status = runtime.control(record, "status", timeout=5)
        if status.get("instance") != wanted["source_instance"] or status.get("paused"):
            raise CoordinationError("The original coordinator has not resumed")
        return status

    def prepare(self, request):
        wanted = {k: request[k] for k in ("request_id", "target", "source_instance")}
        if not isinstance(wanted["request_id"], str) or not 1 <= len(wanted["request_id"]) <= 200:
            raise ValueError("Invalid update identity")
        current = self._source_status(wanted) if not self.fence else None
        with self.condition:
            if self.fence and any(self.fence.get(k) != v for k, v in wanted.items()):
                raise CoordinationError("Another transition owns this endpoint")
            if self.fence and self.fence["state"] != "prepared":
                raise CoordinationError("This preparation has already concluded")
            if not self.fence:
                if (not current or current.get("pending") != self.payload or not current.get("pending_confirmation")
                        or not self.backend or current["instance"] != self.backend[2]):
                    raise CoordinationError("The expected pending update is unavailable")
                self._save_fence(dict(wanted, previous=current["payload"], state="prepared"))
        # The listener continues accepting and displaying work during this
        # wait. Only legacy operations that have already reached the original
        # source must finish; all other intake can transfer to the successor.
        limit = time.monotonic() + 25
        while True:
            with self.condition:
                forwarding = self.writers or self.source_deliveries
            if not forwarding:
                self._check_database(self.source)
                self._require_work(self.source)
                known_legacy = self.intake.reconcile_legacy(self.database, lookup=self._legacy_receipt)
                with contextlib.closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    pending = {row[0] for row in db.execute("""SELECT DISTINCT state FROM browser_submission_receipts
                        WHERE created_at >= ? AND state IN ('accepted','uncertain')""", (self.opened_at,))}
                if not pending and known_legacy:
                    return {**self.identity(), **wanted, "prepared": True}
                if "uncertain" in pending:
                    raise CoordinationError("The source has an unconfirmed submission that cannot be replayed.")
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Waiting for the original coordinator to confirm forwarded work")
            with self.condition:
                self.condition.wait(min(.2, remaining))

    def release_preparation(self, request):
        wanted = {k: request[k] for k in ("request_id", "target", "source_instance")}
        status = self._source_status(wanted)
        with self.condition:
            if self.fence and any(self.fence.get(k) != v for k, v in wanted.items()):
                raise CoordinationError("Another transition owns this endpoint")
            update = runtime_update_record(self.runtime_directory)
            for observation in (update, status.get("update") or {}):
                if observation.get("id") == wanted["request_id"] or (
                        observation and observation.get("phase") not in {"committed", "failed", "rolled-back"}):
                    raise CoordinationError("An admitted update owns this endpoint")
            if self.fence:
                if self.fence.get("previous") != status.get("payload") or status.get("status") != "serving":
                    raise CoordinationError("The original coordinator has not resumed")
                if self.fence["state"] == "prepared":
                    self._save_fence({**self.fence, "state": "source_draining", "released": True})
                elif not self.fence.get("released"):
                    raise CoordinationError("This update has already been admitted")
            return {**self.identity(), **wanted, "released": True}

    @contextlib.contextmanager
    def composer_order(self, thread):
        # Sequence complete authenticated requests as soon as they arrive.
        # Never hold the condition across validation, SQLite or provider I/O.
        ticket = object()
        with self.condition:
            waiting = self.composer_waiters.setdefault(thread, deque())
            waiting.append(ticket)
        try:
            with self.condition:
                self.condition.wait_for(lambda: waiting[0] is ticket or self.stop.is_set())
                if self.stop.is_set():
                    raise CoordinationError("Work has been stopped")
            yield
        finally:
            with self.condition:
                waiting.remove(ticket)
                if not waiting:
                    del self.composer_waiters[thread]
                self.condition.notify_all()

    def serving_input(self, thread, body):
        receiver = self.receiver
        self.verify_origin(self.backend, require_admission=True)
        self.views.authorize_composer(thread, body, receiver_owns_turn=True)
        submission = self.intake.accept(thread, body, serving_authority=receiver.authority)
        waiting = submission is None
        if waiting:
            # An untargeted ordinary message can wait in FIFO order while the
            # earlier native start obtains its turn ID. A supplied target
            # still needs the saved-turn check before accepting deferred work.
            if "expectedTurnId" in body:
                return None
            self._before_transport(self.terminal)
            submission = self.intake.accept(thread, body)
        self.receipt_completed(thread)
        if waiting:
            return 202, {"transferSubmission": submission}
        try:
            self.intake.drain_to(thread, receiver, preflight=self._verify_delivery)
        except TEMPORARY:
            # The journal already accepted the request. A transient failure
            # cannot convert its receipt into a definitive client rejection.
            return 202, {"transferSubmission": self.intake.get(thread, body["requestId"])}
        finally:
            self.receipt_completed(thread)
        return self.submission_response(thread, body, self.intake.get(thread, body["requestId"]))

    def submission_response(self, thread, body, submission):
        authority = self.intake.serving_authority(submission["sequence"])
        if authority:
            receiver = self.receiver
            if not receiver or receiver.authority != authority:
                raise CoordinationError("The receiving coordinator changed.")
            self.verify_origin(self.backend, require_admission=True)
            if submission["state"] == "failed":
                raise CoordinationError("The receiving coordinator declined this submission. Refresh before sending.")
            if submission["state"] == "committed":
                result = receiver.completed_result(thread, body)
                self._before_transport(self.terminal)
                return 200, result
        return 202, {"transferSubmission": submission}

    @contextlib.contextmanager
    def mutation(self):
        with self.condition:
            direct = self.fence.get("state") not in QUEUE_STATES
            if direct:
                self.writers += 1
            else:
                self.intake_writers += 1
        try:
            yield direct
        finally:
            with self.condition:
                if direct:
                    self.writers -= 1
                else:
                    self.intake_writers -= 1
                self.condition.notify_all()

    def thread_record(self, thread):
        # Local authorization plus the same workspace boundary as the real API.
        if self.views is None:
            raise CoordinationError("The saved workspace has not been verified")
        return self.views.authorize_thread(thread)

    def staged_source(self):
        return not self.fence and self.backend and self.receiver is None

    def accept(self, thread, body, *, staged=False):
        with self.condition:
            if self.fence.get("state") not in QUEUE_STATES and not (staged and self.staged_source()):
                raise CoordinationError("The handover just completed. Submit this draft on the serving backend.")
            self.intake_writers += 1
        try:
            self._require_work()
            # A rollback can replace the file during the authorized cutover.
            # Its terminal identity is pinned before delivery to the successor.
            if self.fence.get("state") != "prepared":
                self._check_database(self._destination())
                self._require_named_owner(self._destination())
            self.views.authorize_composer(thread, body)
            return self.intake.accept(thread, body)
        finally:
            with self.condition:
                self.intake_writers -= 1
                self.receipt_completed(thread)

    def legacy_forward(self, thread_id, body):
        address, token, instance, payload = self.backend
        authority = self._destination()
        try:
            self.views.authorize_composer(thread_id, body)
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            raise TransportNotAttempted(str(error)) from error
        self._before_transport(authority)
        if (address, instance, payload) != (authority["url"], authority["instance"], authority["payload"]):
            raise TransportNotAttempted("The source coordinator changed")
        status, _, _ = _exchange(address, "POST", BROWSER + "sessions/" + quote(thread_id, safe="") + "/messages",
                                 json.dumps(body).encode(), headers={"Content-Type": "application/json",
                                 "X-Agent-Coord-Token": token}, timeout=30)
        if status != 200:
            raise RuntimeError("The coordinator did not confirm the forwarded input")

    def drain_staged_source(self):
        with self.condition:
            if not self.staged_source():
                return
            self.source_deliveries += 1
        try:
            for thread in self.intake.pending_threads():
                with self.condition:
                    if not self.staged_source():
                        break
                result = self.intake.drain_one(thread, self._legacy_receipt,
                                              self.legacy_forward, preflight=self._verify_delivery)
                if result and result["state"] in {"committed", "failed"}:
                    self.receipt_completed(thread)
        finally:
            with self.condition:
                self.source_deliveries -= 1
                self.condition.notify_all()

    def watch_key(self, thread):
        return hmac.digest(self.watch_secret, thread.encode(), "sha256")[:16].hex()

    def receipt_completed(self, thread=None):
        with self.condition:
            self.revision += 1
            self.receipt_changes.append((self.revision, self.watch_key(thread) if thread else None))
            self.condition.notify_all()

    def receipt_signal(self, previous, current):
        signal = {"revision": current}
        with self.condition:
            if previous is None or previous < 0 or current <= previous:
                return signal
            keys = {key for revision, key in self.receipt_changes if previous < revision <= current}
            if (self.receipt_changes and previous >= self.receipt_changes[0][0] - 1
                    and keys and None not in keys):
                signal["watchKeys"] = sorted(keys)
        return signal

    def resume(self):
        if self.staged_source():
            self.drain_staged_source()
            return
        if self.fence.get("state") == "prepared":
            update = runtime_update_record(self.runtime_directory)
            if update.get("id") != self.fence["request_id"] or update.get("phase") not in {"committed", "rolled-back", "failed"}:
                return
            self.connect_backend(terminal=True)
            with self.condition:
                self._save_fence({**self.fence, "state": "draining"})
        if self.fence.get("state") not in {"draining", "source_draining"}:
            return
        if self.backend is None:
            self.connect_backend(terminal=self.fence["state"] == "draining")
        if self.fence["state"] == "source_draining" and self.backend[2:] != (self.fence["source_instance"], self.fence["previous"]):
            raise CoordinationError("The release receiver changed")
        for thread, future in list(self.draining_threads.items()):
            if future.done():
                del self.draining_threads[thread]
                try:
                    result = future.result()
                except TEMPORARY:
                    self.delivery_retry_after[thread] = time.monotonic() + .5
                    continue
                if result and result["state"] in {"queued", "uncertain"}:
                    self.delivery_retry_after[thread] = time.monotonic() + .5
                if not self.receiver and result and result["state"] in {"committed", "failed"}:
                    self.receipt_completed(thread)
        if len(self.draining_threads) >= 4:
            return
        pending = self.intake.pending_threads()
        now = time.monotonic()
        self.delivery_retry_after = {thread: retry for thread, retry in self.delivery_retry_after.items()
                                     if retry > now}
        self.last_dispatch = {thread: self.last_dispatch[thread] for thread in pending if thread in self.last_dispatch}
        for thread in sorted(pending, key=lambda thread: self.last_dispatch.get(thread, 0)):
            if len(self.draining_threads) >= 4:
                break
            if thread in self.draining_threads or thread in self.delivery_retry_after:
                continue
            if self.receiver:
                job = self.delivery_pool.submit(self.intake.drain_batch, thread, self.receiver,
                    preflight=self._verify_delivery, completed=lambda thread=thread: self.receipt_completed(thread),
                    stopped=self.stop.is_set)
            else:
                job = self.delivery_pool.submit(self.intake.drain_one, thread,
                    self._legacy_receipt, self.legacy_forward,
                    preflight=self._verify_delivery)
            self.draining_threads[thread] = job
            self.last_dispatch[thread] = time.monotonic()
        with self.condition:
            if not self.draining_threads and not self.intake_writers and not self.intake.pending_threads():
                self._save_fence({**self.fence, "state": "settled"})

    def monitor(self):
        while not self.stop.wait(.02 if self.fence.get("state") in {"draining", "source_draining"} else .2):
            try:
                self.resume()
            except TEMPORARY:
                self.stop.wait(.5)


def runtime_update_record(directory):
    from .runtime_update import read
    return read(directory) or {}


def _static(path):
    from .ui import _WEB_ROOT, _workspace_html
    if path == "/":
        return _workspace_html(), "text/html; charset=utf-8"
    relative = path[1:]
    if not relative or any(part in {".", "..", ""} or part.startswith(".") for part in relative.split("/")):
        return None
    asset = (_WEB_ROOT / relative).resolve()
    if (not asset.is_relative_to(_WEB_ROOT.resolve()) or not asset.is_file()
            or asset.suffix not in {".js", ".css", ".png", ".webmanifest"}
            and relative != "openui-frame.html"):
        return None
    return asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or "application/octet-stream"


def _serving_candidate(gateway):
    if gateway.fence.get("state") != "draining" or gateway.fence.get("released"):
        return False
    receiver = gateway.receiver
    return bool(receiver and gateway.backend and
                gateway.backend[2:] == (receiver.instance, receiver.payload) ==
                (gateway.terminal.get("instance"), gateway.terminal.get("payload")) and
                receiver.payload == gateway.payload)


def _provider_and_receipt_events(gateway):
    return gateway.staged_source() or _serving_candidate(gateway)


class GatewayServer(ThreadingHTTPServer):
    request_queue_size = 64


class _ProviderEventFrames:
    """Keep incomplete SSE frames private so a receipt can be sent between events."""

    line_end = re.compile(rb"\r\n|[\r\n]")
    block_size = 64 * 1024
    # The provider transport's maximum encoded batch bounds per-connection
    # temporary storage. Even larger complete frames can use ordinary streaming.
    spool_limit = MAX_BATCH_BYTES

    def __init__(self, pending, output, active):
        self.pending = pending
        self.output = output
        self.active = active
        self.at_line_start = True
        self.deferred_cr = b""
        self.streaming = False

    def append(self, data, *, complete):
        if not complete and not self.streaming and self.pending.tell() + len(data) <= self.spool_limit:
            self.pending.write(data)
            return
        if self.pending.tell():
            self.pending.seek(0)
            while block := self.pending.read(self.block_size):
                if not self.active():
                    raise ConnectionAbortedError("The provider event source changed.")
                self.output.write(block)
            self.pending.seek(0)
            self.pending.truncate()
        if data:
            self.output.write(data)
        self.streaming = not complete
        if complete:
            self.output.flush()

    def receive(self, chunk):
        eof = chunk is None
        chunk = self.deferred_cr + (chunk or b"")
        self.deferred_cr = b""
        if not eof and chunk.endswith(b"\r"):
            chunk, self.deferred_cr = chunk[:-1], b"\r"
        consumed = offset = 0
        for terminator in self.line_end.finditer(chunk):
            blank = self.at_line_start and offset == terminator.start()
            self.at_line_start = True
            offset = terminator.end()
            if blank:
                self.append(chunk[consumed:offset], complete=True)
                consumed = offset
        if offset < len(chunk):
            self.at_line_start = False
        self.append(chunk[consumed:], complete=False)
        return bool(consumed) or self.streaming


class GatewayHandler(BaseHTTPRequestHandler):
    @property
    def gateway(self):
        return self.server.gateway

    def log_message(self, *_):
        pass  # The path may contain a private search or thread identifier.

    def local(self):
        return (self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]
                and not any(k.lower().startswith(("forwarded", "x-forwarded-", "tailscale-")) for k in self.headers)
                and self.path.startswith("/") and not self.path.startswith("//")
                and not any(c in self.path for c in ("\r", "\n", "\\", "#"))
                and len(self.path) < 16384)

    def answer(self, status, data, mime="application/json; charset=utf-8", headers=None):
        from .ui import browser_content_policy
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        fields = browser_content_policy(self.path, status, mime, self.gateway.url.rstrip("/"), self.local())
        # The app page may send only visit observations to this exact loopback
        # origin. Keep the sandbox and preview policies intact.
        if self.gateway.visit_origin and mime.startswith("text/html") and urlsplit(self.path).path == "/":
            fields["Content-Security-Policy"] = fields["Content-Security-Policy"].replace(
                "connect-src 'self';", f"connect-src 'self' {self.gateway.visit_origin}{BROWSER}activity/events;")
        fields["Cache-Control"] = "no-store"
        fields.update(headers or {})
        for name, value in fields.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value):
        self.answer(status, json.dumps(value, separators=(",", ":")).encode())

    def sse_headers(self):
        self.response_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def changed_submissions(self, query):
        if set(query) - {"thread", "after"} or len(query.get("thread", ())) != 1 or len(query.get("after", ["0"])) != 1:
            return self.json(400, {"error": "Select one conversation and one receipt cursor"})
        thread = query["thread"][0]
        self.gateway.thread_record(thread)
        after = int(query.get("after", ["0"])[0])
        if not 0 <= after <= 2**63-1:
            raise ValueError("Invalid submission cursor")
        cursor, records = self.gateway.intake.thread_changes(thread, after)
        self.json(200, dict(threadId=thread, watchKey=self.gateway.watch_key(thread), cursor=cursor, submissions=records, more=len(records) == 16))

    def signal_receipts(self, revision, previous=None):
        # Receipt events share the stream without advancing the provider cursor.
        signal = self.gateway.receipt_signal(previous, revision)
        data = json.dumps(signal, separators=(",", ":"))
        self.wfile.write(f"event: update-submission-change\ndata: {data}\n\n".encode())
        self.wfile.flush()

    @contextlib.contextmanager
    def provider_chunks(self, source):
        """A single backpressured worker can be interrupted before or after upstream headers."""
        gateway = self.gateway
        address = source[0]
        headers = {"Last-Event-ID": self.headers["Last-Event-ID"]} if self.headers.get("Last-Event-ID") else {}
        chunks = queue.Queue(maxsize=1)
        stopped = threading.Event()
        upstream_socket = None

        def enqueue(chunk):
            while not stopped.is_set():
                try:
                    chunks.put(chunk, timeout=.1)
                    with gateway.condition:
                        gateway.condition.notify_all()
                    return
                except queue.Full:
                    continue

        def read_upstream():
            nonlocal upstream_socket
            try:
                gateway.verify_origin(source)
                with contextlib.closing(_connection(address, 2)) as connection:
                    connection.connect()
                    with gateway.condition:
                        if stopped.is_set() or gateway.stop.is_set() or gateway.backend != source or not _provider_and_receipt_events(gateway):
                            return
                        upstream_socket = connection.sock
                        gateway.upstream_streams.add(upstream_socket)
                    connection.request("GET", self.path, headers=headers)
                    # Shutdown from the handler or the cutover unblocks even a
                    # source stalled before headers or halfway through a chunk.
                    upstream_socket.settimeout(None)
                    with connection.getresponse() as response:
                        if response.status != 200 or response.headers.get_content_type() != "text/event-stream":
                            return
                        while not stopped.is_set():
                            chunk = response.read1(_ProviderEventFrames.block_size)
                            if not chunk:
                                return
                            enqueue(chunk)
            except (OSError, http.client.HTTPException, RuntimeError):
                pass
            finally:
                with gateway.condition:
                    gateway.upstream_streams.discard(upstream_socket)
                enqueue(None)

        reader = threading.Thread(target=read_upstream, name="rf-staged-event-reader")
        reader.start()
        try:
            yield chunks
        finally:
            stopped.set()
            with gateway.condition:
                if upstream_socket is not None:
                    with contextlib.suppress(OSError):
                        upstream_socket.shutdown(socket.SHUT_RDWR)
            reader.join()

    def staged_provider_events(self):
        """One EventSource carries provider frames and content-free intake notifications."""
        gateway = self.gateway
        self.connection.settimeout(3)
        self.sse_headers()
        self.wfile.write(b"retry: 250\n\n")
        revision = gateway.revision
        self.signal_receipts(revision)
        last_output = time.monotonic()
        source = gateway.backend

        def active():
            return not gateway.stop.is_set() and gateway.backend == source and _provider_and_receipt_events(gateway)

        with tempfile.SpooledTemporaryFile(max_size=2 * _ProviderEventFrames.block_size, dir=gateway.directory) as pending:
            frames = _ProviderEventFrames(pending, self.wfile, active)
            with self.provider_chunks(source) as chunks:
                while active():
                    current = gateway.revision
                    if not frames.streaming:
                        if current != revision:
                            self.signal_receipts(current, revision)
                            revision = current
                            last_output = time.monotonic()
                        elif time.monotonic() - last_output >= 5:
                            self.wfile.write(b": connected\n\n")
                            self.wfile.flush()
                            last_output = time.monotonic()
                    # Both kinds of input wake this reader. Waiting only for a
                    # provider frame imposed an extra polling interval on an
                    # already committed user receipt in another window.
                    wait_seconds = 5 if frames.streaming else max(0, 5 - (time.monotonic() - last_output))
                    if frames.streaming and current != revision:
                        wait_seconds = .15
                    with gateway.condition:
                        gateway.condition.wait_for(lambda: not active() or not chunks.empty()
                            or gateway.revision != current, timeout=wait_seconds)
                    if not active():
                        return
                    try:
                        chunk = chunks.get_nowait()
                    except queue.Empty:
                        # Beyond the disk bound, a partial frame has crossed the
                        # wire. A stalled transfer cannot carry another event.
                        # EventSource reconnects at its last completed cursor;
                        # the new stream publishes its receipt signal first.
                        if frames.streaming and gateway.revision != revision:
                            return
                        continue
                    if frames.receive(chunk):
                        last_output = time.monotonic()
                    if chunk is None:
                        return

    def provider_events(self):
        gateway = self.gateway
        if _provider_and_receipt_events(gateway):
            gateway.verify_origin(gateway.backend)
            return self.staged_provider_events()
        if gateway.fence.get("state") in QUEUE_STATES or not gateway.backend:
            self.sse_headers()
            self.wfile.write(b"retry: 250\n\n")
            revision = -1
            while not gateway.stop.is_set():
                with gateway.condition:
                    gateway.condition.wait_for(lambda: gateway.revision != revision or gateway.stop.is_set(), timeout=5)
                    current = gateway.revision
                    observing = gateway.fence.get("state") in QUEUE_STATES and not _provider_and_receipt_events(gateway)
                if current != revision:
                    self.signal_receipts(current, revision)
                    revision = current
                else:
                    self.wfile.write(b": coordinator update\n\n")
                    self.wfile.flush()
                if not observing:
                    break
            return
        backend = gateway.backend
        gateway.verify_origin(backend)
        with contextlib.closing(_connection(backend[0], 10)) as conn:
            headers = {"Last-Event-ID": self.headers["Last-Event-ID"]} if self.headers.get("Last-Event-ID") else {}
            conn.request("GET", self.path, headers=headers)
            upstream = conn.sock
            response = conn.getresponse()
            if response.status != 200:
                self.answer(response.status, response.read())
                return
            self.sse_headers()
            with gateway.condition:
                if gateway.fence.get("state") in QUEUE_STATES:
                    self.wfile.write(b"retry: 250\n\n")
                    self.wfile.flush()
                    return
                gateway.upstream_streams.add(upstream)
            try:
                while not gateway.stop.is_set() and gateway.fence.get("state") not in QUEUE_STATES:
                    chunk = response.read1(65536)
                    if not chunk:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                with gateway.condition:
                    gateway.upstream_streams.discard(upstream)

    def relay(self, method, body=None):
        gateway = self.gateway
        if not gateway.backend:
            raise CoordinationError("The coordinator is reconnecting")
        backend = gateway.backend
        gateway.verify_origin(backend)
        address, token, _, _ = backend
        headers = {key: self.headers[key] for key in
                   ("X-Agent-Coord-History-View", "X-Agent-Coord-Thread-Controls", "If-None-Match")
                   if self.headers.get(key)}
        if method == "GET" and self.headers.get("X-Agent-Coord-Token") == gateway.token:
            headers["X-Agent-Coord-Token"] = token
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-Agent-Coord-Token": token,
                            "Origin": address.rstrip("/")})
        status, fields, raw = _exchange(address, method, self.path, body, headers=headers, timeout=30)
        path = urlsplit(self.path).path
        if method == "GET" and status == 200 and path.startswith(BROWSER):
            route = path[len(BROWSER):]
            if route in CACHEABLE and not urlsplit(self.path).query and len(raw) <= 4 * 1024 * 1024:
                gateway.cache[route] = raw
            if route.startswith("sessions/") and "/" not in route[len("sessions/"):]:
                records = gateway.intake.visible(unquote(route[len("sessions/"):]))
                receiver_ready = _serving_candidate(gateway)
                if records:
                    result = json.loads(raw)
                    result.update(transferSubmissions=records, transferReadSource="serving")
                    if receiver_ready:
                        result["acceptsUntargetedInput"] = True
                    return self.json(status, result)
                fields["X-Ribbon-Transfer-Read-Source"] = "serving"
                if receiver_ready:
                    fields["X-Ribbon-Untargeted-Input"] = "1"
        useful = {k: v for k, v in fields.items() if k in {"ETag", "Server-Timing", "Cache-Control",
                                                          "X-Ribbon-Transfer-Read-Source", "X-Ribbon-Untargeted-Input"}}
        self.answer(status, raw, fields.get("Content-Type", "application/json"), useful)

    def dispatch_get(self, parsed):
        gateway = self.gateway
        if parsed.path == "/_runtime/identity":
            if not self.control_authorized():
                return self.json(403, {"error": "Runtime authorization required"})
            return self.json(200, gateway.identity())
        asset = _static(parsed.path)
        if asset is not None:
            raw, mime = asset
            cache = {}
            if parse_qs(parsed.query).get("v") == [hashlib.sha256(raw).hexdigest()]:
                cache["Cache-Control"] = "private, max-age=31536000, immutable"
            return self.answer(200, raw, mime, cache)
        if parsed.path == BROWSER + "config":
            return self.json(200, gateway.browser_config())
        if parsed.path == BROWSER + "update-submissions":
            return self.changed_submissions(parse_qs(parsed.query, keep_blank_values=True))
        if parsed.path == BROWSER + "events":
            return self.provider_events()
        if parsed.path.startswith("/_runtime/"):
            return self.json(404, {"error": "Unknown control"})
        route = parsed.path.removeprefix(BROWSER)
        offline = gateway.fence.get("state") == "prepared"
        # Legacy coordinators can spend seconds servicing ordinary thread
        # reads or indexing a search on their single foreground writer. Use
        # the same committed snapshot reader before and during their handover.
        staged_legacy = gateway.staged_source()
        if not offline and not staged_legacy:
            return self.relay("GET")
        if not parsed.path.startswith(BROWSER):
            if not offline:
                return self.relay("GET")
            return self.json(503, {"error": "This action will be available when the coordinator resumes."})
        try:
            saved = gateway.views.get(route, parse_qs(parsed.query, keep_blank_values=True),
                    recent=self.headers.get("X-Agent-Coord-History-View") == "recent",
                    thread_controls=self.headers.get("X-Agent-Coord-Thread-Controls") == "1")
        except KeyError:
            if not offline:
                return self.relay("GET")
            if route in gateway.cache and not parsed.query:
                return self.answer(200, gateway.cache[route])
            return self.json(503, {"error": "This view needs the serving coordinator."})
        if route.startswith("sessions/"):
            saved.update(transferSubmissions=gateway.intake.visible(unquote(route[9:])),
                         transferReadSource="saved", deferReadAcknowledgment=offline)
        return self.json(200, saved)

    def control_authorized(self):
        return secrets.compare_digest(self.headers.get("Authorization", "").encode(),
                                      ("Bearer " + self.gateway.control_token).encode())

    def post(self, parsed, *, browser_token=None):
        gateway = self.gateway
        control = parsed.path.startswith("/_runtime/")
        permitted = (self.control_authorized() if control else
                     secrets.compare_digest(self.headers.get("X-Agent-Coord-Token", "").encode(),
                                            (browser_token or gateway.token).encode()))
        if (not permitted or self.headers.get("Origin") not in (None, gateway.url.rstrip("/"))
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            return self.json(403, {"error": "Reload Ribbon Field before making changes."})
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            return self.json(415, {"error": "Send a JSON request"})
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdecimal():
            return self.json(400, {"error": "A fixed request length is required"})
        size = int(lengths[0])
        if not 0 < size <= (4096 if control else MAX_MESSAGE_BODY_BYTES):
            return self.json(413, {"error": "Request body is empty or too large"})
        self.connection.settimeout(10)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete request")
        if control:
            body = json.loads(raw)
            if parsed.path == "/_runtime/prepare":
                return self.json(200, gateway.prepare(body))
            if parsed.path == "/_runtime/release-preparation":
                return self.json(200, gateway.release_preparation(body))
            if parsed.path == "/_runtime/retire" and gateway.identity()["settled"]:
                self.json(200, {**gateway.identity(), "retired": True})
                gateway.stop.set()
                return
            return self.json(409, {"error": "The transition remains open"})
        if not parsed.path.startswith((BROWSER, "/api/remote/")):
            return self.json(404, {"error": "Unknown interaction route"})
        parts = parsed.path.split("/")
        composer = len(parts) == 6 and parts[1:4] == ["api", "browser", "sessions"] and parts[5] == "messages"
        thread = unquote(parts[4]) if composer else None
        order = gateway.composer_order(thread) if composer else contextlib.nullcontext()
        with order, gateway.mutation() as direct:
            if composer:
                body = json.loads(raw)
                existing = gateway.intake.existing(thread, body, gateway.thread_record)
                if existing:
                    return self.json(*gateway.submission_response(thread, body, existing))
                if _serving_candidate(gateway):
                    response = gateway.serving_input(thread, body)
                    if response:
                        return self.json(*response)
            if composer and (not direct or gateway.staged_source()):
                if direct:
                    # Alternate authorities (review, dispatch and slash commands)
                    # stay on their normal source route during passive staging.
                    try:
                        gateway.views.authorize_composer(thread, body)
                    except CoordinationError:
                        return self.relay("POST", raw)
                result = gateway.accept(thread, body, staged=direct)
                return self.json(202, {"transferSubmission": result})
            if parsed.path == BROWSER + "workspaces/open" and (not direct or gateway.staged_source()):
                try:
                    return self.json(200, gateway.views.open_workspace(json.loads(raw)))
                except CoordinationError:
                    if not direct:
                        raise
            if direct:
                return self.relay("POST", raw)
            return self.json(503, {"code": "coordinator_offline", "error": "This action needs the serving coordinator. Your change was not submitted."})

    def handle_interaction(self):
        try:
            if not self.local():
                return self.json(403, {"error": "Invalid local origin"})
            parsed = urlsplit(self.path)
            if self.command == "POST":
                return self.post(parsed)
            if (self.headers.get("Sec-Fetch-Site") == "cross-site"
                    and parsed.path not in {"/openui-frame.js", "/vendor/openui/openui-bundle.min.js",
                                            "/vendor/openui/openui-styles.css"}):
                return self.json(403, {"error": "Invalid local origin"})
            return self.dispatch_get(parsed)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (IntakeFull, CoordinationError) as exc:
            self.json(503 if isinstance(exc, IntakeFull) else 409, {"error": str(exc)})
        except (KeyError, ValueError, TypeError):
            self.json(400, {"error": "Invalid interaction request"})
        except (OSError, sqlite3.Error, http.client.HTTPException, RuntimeError):
            if getattr(self, "response_started", False):
                self.close_connection = True
                return
            self.json(503, {"error": "The coordinator is reconnecting. Check the conversation before sending again."})

    do_GET = do_POST = handle_interaction



class VisitHandler(GatewayHandler):
    """A dedicated browser connection pool for synchronous, owner-confirmed visit writes."""

    def permitted_origin(self):
        return bool(self.gateway.url) and self.local() and self.headers.get_all("Origin") == [self.gateway.url.rstrip("/")]

    def end_headers(self):
        if self.permitted_origin():
            self.send_header("Access-Control-Allow-Origin", self.gateway.url.rstrip("/"))
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        headers = {name.strip().lower() for name in self.headers.get("Access-Control-Request-Headers", "").split(",")}
        if (not self.permitted_origin() or self.path != BROWSER + "activity/events"
                or self.headers.get("Access-Control-Request-Method") != "POST"
                or headers != {"content-type", "x-agent-coord-token"}):
            return self.json(403, {"error": "Visit reporting requires the local app."})
        self.answer(204, b"", headers={"Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": "Content-Type, X-Agent-Coord-Token",
                    "Access-Control-Max-Age": "300"})

    def post(self, parsed, **kwargs):
        if not self.permitted_origin() or self.path != BROWSER + "activity/events":
            return self.json(403, {"error": "Visit reporting requires the local app."})
        return super().post(parsed, browser_token=self.gateway.visit_token)

    def do_GET(self):
        self.json(404, {"error": "Unknown visit reporting route"})


@contextlib.contextmanager
def visit_listener(gateway, port=0):
    with GatewayServer(("127.0.0.1", port), VisitHandler) as server:
        server.gateway = gateway
        gateway.visit_origin = f"http://127.0.0.1:{server.server_port}"
        gateway.visit_token = hmac.new(gateway.control_token.encode(),
            ("visit-reporting:" + gateway.visit_origin).encode(), hashlib.sha256).hexdigest()
        worker = threading.Thread(target=server.serve_forever, name="rf-visit-reporting", daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown()
            worker.join()

def serve(database, directory, ready_fd):
    directory = Path(directory)
    payload = Path(__file__).resolve().parents[2]
    gateway = InteractionGateway(database, directory, payload)
    previous = _private_json(directory / "gateway.json")
    if previous and (previous["database"] != str(gateway.database) or previous["payload"] != gateway.payload):
        raise RuntimeError("A different signed transition owns this endpoint")
    if not gateway.config or gateway.fence.get("state") != "prepared":
        gateway.connect_backend(terminal=gateway.fence.get("state") in {"draining", "settled"} and not gateway.fence.get("released"))
    else:
        gateway.views = SavedRuntimeViews(database, gateway.config.get("workspaceRoot"), current_directory=gateway.config.get("cwd"))
    gateway.opened_at = previous.get("opened_at") or time.time()
    gateway.control_token = previous.get("control_token") or secrets.token_urlsafe(32)
    gateway.token = previous.get("browser_token") or secrets.token_urlsafe(32)
    port = urlsplit(previous["url"]).port if previous else 0
    visit_port = urlsplit(previous["visit_origin"]).port if previous.get("visit_origin") else 0
    with GatewayServer(("127.0.0.1", port), GatewayHandler) as server, contextlib.ExitStack() as listeners:
        server.gateway = gateway
        server.timeout = .5
        gateway.url = f"http://127.0.0.1:{server.server_port}/"
        listeners.enter_context(visit_listener(gateway, visit_port))
        runtime.atomic_json(directory / "gateway.json", {**gateway.identity(), "control_token": gateway.control_token,
                                                         "browser_token": gateway.token, "opened_at": gateway.opened_at,
                                                         "visit_origin": gateway.visit_origin})
        monitor = threading.Thread(target=gateway.monitor, name="rf-update-delivery", daemon=True)
        monitor.start()
        try:
            os.write(ready_fd, b"ready\n")
            os.close(ready_fd)
            while not gateway.stop.is_set():
                server.handle_request()
        finally:
            gateway.stop.set()
            with gateway.condition:
                gateway.condition.notify_all()
            monitor.join(32)
            gateway.delivery_pool.shutdown(wait=True, cancel_futures=True)
            if gateway.views is not None:
                gateway.views.close()
