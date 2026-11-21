"""Stoic execution adapter hosted by the Ribbon Field runtime.

The remote run ledger owns scheduling. This adapter owns only durable delivery
receipts and delegates all provider execution to ExternalWorkRuntime.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .store import CoordinationError
from .codex_app_server import BrowserBusyError

DEFAULT_ORIGIN = "https://tryblume.ai"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "uncertain"}


class StoicRequestError(CoordinationError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StoicClient:
    """Bounded authenticated requests; credentials never enter saved receipts."""
    def __init__(self, base_url, token_file):
        self.base_url = base_url
        self.token_file = token_file
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, body=None):
        try:
            token = Path(self.token_file).expanduser().read_text().strip()
        except OSError:
            raise StoicRequestError("Stoic credential file is unavailable.") from None
        if not token or len(token) > 16384 or "\n" in token or "\r" in token:
            raise StoicRequestError("Stoic credential file is invalid.")
        request = Request(self.base_url + path,
                          data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                          method=method, headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=10) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise StoicRequestError(f"Stoic returned HTTP {status}.", status) from None
        except (OSError, URLError):
            raise StoicRequestError("Stoic is unreachable; delivery will be retried.") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise StoicRequestError("Stoic response exceeds the supported size.")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise StoicRequestError("Stoic returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise StoicRequestError("Stoic returned an invalid response object.")
        return result


class StoicIntegration:
    def __init__(self, external_work, *, client_factory=StoicClient):
        self.external_work = external_work
        self.sessions = external_work.sessions
        self.store = self.sessions.store
        self.client_factory = client_factory
        self.stopped = threading.Event()
        self.worker = None
        self.processing = threading.Lock()
        self.next_scan = 0.0
        self.last_error = None
        self.last_connected_at = None
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS stoic_connection (
                    id INTEGER PRIMARY KEY CHECK(id=1), connection_id TEXT NOT NULL,
                    runner_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    base_url TEXT NOT NULL, token_file TEXT NOT NULL, cursor TEXT,
                    updated_at REAL NOT NULL, max_concurrent_runs INTEGER NOT NULL DEFAULT 2
                );
                CREATE TABLE IF NOT EXISTS stoic_execution_bindings (
                    run_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
                    claim_token TEXT, run_json TEXT, state TEXT NOT NULL,
                    receipt_json TEXT, error TEXT, updated_at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(stoic_connection)")}
            if "max_concurrent_runs" not in columns:
                db.execute("ALTER TABLE stoic_connection ADD COLUMN max_concurrent_runs INTEGER NOT NULL DEFAULT 2")
            if "policy_json" not in columns:
                db.execute("ALTER TABLE stoic_connection ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}'")
            db.execute("""INSERT OR IGNORE INTO stoic_connection
                (id,connection_id,runner_id,enabled,base_url,token_file,updated_at)
                VALUES (1,?,?,0,?,?,?)""", (uuid.uuid4().hex, "ribbon-field-" + uuid.uuid4().hex,
                DEFAULT_ORIGIN, str(Path("~/.config/stoic/codex-token").expanduser()), self.store.clock()))

    def _configuration(self):
        with self.store.reading(), self.store._connection() as db:
            config = dict(db.execute("SELECT * FROM stoic_connection WHERE id=1").fetchone())
        config.update(json.loads(config.pop("policy_json")))
        return config

    def status(self):
        config = self._configuration()
        with self.store.reading(), self.store._connection() as db:
            counts = {row[0]: row[1] for row in db.execute(
                "SELECT state,COUNT(*) FROM stoic_execution_bindings GROUP BY state")}
        return {"enabled": bool(config["enabled"]), "base_url": config["base_url"],
                "token_file": config["token_file"], "runner_id": config["runner_id"],
                "max_concurrent_runs": config["max_concurrent_runs"],
                "allowed_roots": config.get("allowed_roots", []),
                "allowed_permission_modes": config.get("allowed_permission_modes", {}),
                "allowed_models": config.get("allowed_models", {}),
                "credential_configured": Path(config["token_file"]).is_file(),
                "last_connected_at": self.last_connected_at, "last_error": self.last_error,
                "runs": counts}

    def configure(self, body):
        policy_keys = {"allowed_roots", "allowed_permission_modes", "allowed_models"}
        if not isinstance(body, dict) or set(body) - {"enabled", "base_url", "token_file", "max_concurrent_runs"} - policy_keys:
            raise CoordinationError("Unknown Stoic connection setting.")
        if "enabled" in body and not isinstance(body["enabled"], bool):
            raise CoordinationError("Stoic enabled must be a boolean.")
        if "max_concurrent_runs" in body and (type(body["max_concurrent_runs"]) is not int
                or not 1 <= body["max_concurrent_runs"] <= 16):
            raise CoordinationError("Stoic max_concurrent_runs must be an integer from 1 to 16.")
        policy_changes = {}
        if "allowed_roots" in body:
            roots = body["allowed_roots"]
            if (not isinstance(roots, list) or len(roots) > 32 or any(
                    not isinstance(root, str) or not root or len(root) > 4096
                    or not Path(root).expanduser().is_absolute() for root in roots)):
                raise CoordinationError("Stoic allowed_roots must be a list of absolute directory paths.")
            policy_changes["allowed_roots"] = list(dict.fromkeys(str(Path(root).expanduser().resolve()) for root in roots))
        for key in ("allowed_permission_modes", "allowed_models"):
            if key not in body:
                continue
            value = body[key]
            if (not isinstance(value, dict) or len(value) > 16 or any(
                    not isinstance(client, str) or not client or len(client) > 200
                    or not isinstance(entries, list) or len(entries) > 100
                    or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 200 for entry in entries)
                    for client, entries in value.items())):
                raise CoordinationError(f"Stoic {key} must map provider names to lists of exact identifiers.")
            policy_changes[key] = value
        # Configuration never changes underneath an in-flight authenticated request.
        if not self.processing.acquire(blocking=False):
            raise CoordinationError("Stoic is synchronizing. Retry the connection change shortly.")
        try:
            config = self._configuration()
            origin = body.get("base_url", config["base_url"])
            if not isinstance(origin, str):
                raise CoordinationError("Stoic base_url must be an HTTPS origin.")
            parsed = urlsplit(origin)
            local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if (not parsed.hostname or (parsed.scheme != "https" and not local_http)
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"}):
                raise CoordinationError("Stoic base_url must be an HTTPS origin (HTTP is allowed for localhost).")
            origin = origin.rstrip("/")
            token_file = body.get("token_file", config["token_file"])
            if not isinstance(token_file, str) or not token_file.strip() or len(token_file) > 4096:
                raise CoordinationError("Specify a Stoic credential file path.")
            token_file = str(Path(token_file).expanduser().resolve())
            changed_identity = origin != config["base_url"] or token_file != config["token_file"]
            with self.store._connection() as db:
                active = db.execute("SELECT 1 FROM stoic_execution_bindings WHERE state != 'published' LIMIT 1").fetchone()
                if changed_identity and active:
                    raise CoordinationError("Finish or reconcile existing Stoic runs before changing the connection.")
                if changed_identity:
                    db.execute("DELETE FROM stoic_execution_bindings")
                    db.execute("UPDATE stoic_connection SET connection_id=?,cursor=NULL WHERE id=1", (uuid.uuid4().hex,))
                db.execute("UPDATE stoic_connection SET enabled=?,base_url=?,token_file=?,updated_at=? WHERE id=1",
                           (int(body.get("enabled", bool(config["enabled"]))), origin, token_file, self.store.clock()))
                if "max_concurrent_runs" in body:
                    db.execute("UPDATE stoic_connection SET max_concurrent_runs=? WHERE id=1", (body["max_concurrent_runs"],))
                policy = {key: config[key] for key in policy_keys if key in config}
                policy.update(policy_changes)
                db.execute("UPDATE stoic_connection SET policy_json=? WHERE id=1", (json.dumps(policy),))
            self.next_scan = 0
            self.last_error = None
        finally:
            self.processing.release()
        return self.status()

    def _bindings(self):
        with self.store.reading(), self.store._connection() as db:
            rows = []
            # Give recovery and new work their own bounded pages so neither a
            # large pending queue nor historical uncertainty can starve them.
            for predicate in ("state NOT IN ('published','conflict','claiming','uncertain')",
                              "state='claiming'", "state='uncertain'"):
                rows.extend(dict(row) for row in db.execute(
                    "SELECT * FROM stoic_execution_bindings WHERE " + predicate + " ORDER BY updated_at LIMIT 100"))
            return rows

    def _save(self, run_id, **values):
        allowed = {"claim_token", "run_json", "state", "receipt_json", "error"}
        if set(values) - allowed:
            raise ValueError("Invalid binding field")
        values["updated_at"] = self.store.clock()
        with self.store.database_operation("stoic.binding", priority="background"), self.store._connection() as db:
            db.execute("UPDATE stoic_execution_bindings SET " + ",".join(key + "=?" for key in values)
                       + " WHERE run_id=?", (*values.values(), run_id))

    def _stage(self, run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 200:
            raise StoicRequestError("Stoic returned an invalid run identifier.")
        with self.store.database_operation("stoic.stage", priority="background"), self.store._connection() as db:
            db.execute("""INSERT OR IGNORE INTO stoic_execution_bindings
                (run_id,claim_id,state,updated_at) VALUES (?,?,'claiming',?)""",
                (run_id, uuid.uuid4().hex, self.store.clock()))

    def _run_path(self, run_id):
        return "/api/execution/runs/" + quote(run_id, safe="")

    def _authorize_settings(self, config, settings):
        cwd = settings.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).expanduser().is_absolute():
            raise CoordinationError("Stoic execution requires an absolute working directory.")
        resolved = Path(cwd).expanduser().resolve()
        if not any(resolved.is_relative_to(Path(root)) for root in config.get("allowed_roots", [])):
            raise CoordinationError("The working directory is outside the local Stoic execution policy.")
        client = settings.get("client")
        if settings.get("permission_mode") not in config.get("allowed_permission_modes", {}).get(client, []):
            raise CoordinationError("The provider or permission mode is not allowed by the local Stoic execution policy.")
        models = config.get("allowed_models", {})
        if client in models and settings.get("model") not in models[client]:
            raise CoordinationError("The model is not allowed by the local Stoic execution policy.")

    @staticmethod
    def _prompt(run):
        if "scheduled_at" not in run:
            return run["prompt"]
        context = {"run_id": run["id"], **{key: run.get(key) for key in ("source", "source_id", "occurrence_id", "scheduled_at")}}
        return ("Stoic execution context (immutable occurrence):\n" + json.dumps(context, sort_keys=True)
                + "\n\nInstructions:\n" + run["prompt"])

    def _synchronize(self, client, config, binding):
        run_id = binding["run_id"]
        path = self._run_path(run_id)
        source = "stoic:" + config["connection_id"]
        if binding["state"] == "claiming":
            claimed = client.request("POST", path + "/claim", {
                "runner_id": config["runner_id"], "claim_id": binding["claim_id"]})
            run, claim_token = claimed.get("run"), claimed.get("claim_token")
            if (not isinstance(run, dict) or run.get("id") != run_id
                    or not isinstance(run.get("prompt"), str) or not run["prompt"].strip()
                    or not isinstance(run.get("settings"), dict)
                    or not isinstance(claim_token, str) or not claim_token):
                raise StoicRequestError("Stoic returned an invalid claim receipt.")
            binding.update(claim_token=claim_token, run_json=json.dumps(run), state="claimed")
            self._save(run_id, claim_token=claim_token, run_json=binding["run_json"], state="claimed")
        auth = {"runner_id": config["runner_id"], "claim_token": binding["claim_token"]}
        if binding["receipt_json"]:
            receipt = json.loads(binding["receipt_json"])
            client.request("POST", path + "/result?view=receipt", receipt)
            self._save(run_id, state="uncertain" if receipt["status"] == "uncertain" else "published",
                       receipt_json=None if receipt["status"] == "uncertain" else binding["receipt_json"], error=None)
            return
        local = self.external_work.get(source, run_id)
        if binding["state"] == "uncertain" and (local is None or local["state"] == "uncertain"):
            self._save(run_id, state="uncertain")
            return
        # Renew/check ownership before any new provider submission. A rejected
        # heartbeat must never turn a previously owned run into a fresh launch.
        heartbeat = client.request("POST", path + "/heartbeat", {
            **auth, "thread_id": local.get("thread_id") if local else None,
            "turn_id": local.get("turn_id") if local else None})
        remote_status = heartbeat.get("status")
        if remote_status in {"completed", "failed"}:
            if local and local["state"] not in TERMINAL_STATES:
                self.external_work.cancel(source, run_id)
            self._save(run_id, state="published", error=None)
            return
        if remote_status == "cancelled":
            if local:
                self.external_work.cancel(source, run_id)
                local = self.external_work.get(source, run_id)
            else:
                local = {"state": "cancelled", "result": None, "error": "Cancelled before launch."}
        elif remote_status == "uncertain" and local is None:
            # The engine reserves its durable row before contacting a provider.
            # No row is positive evidence that this runtime never started it.
            local = {"state": "failed", "result": None,
                     "error": "Execution did not start before its lease expired."}
        elif remote_status not in {"running", "uncertain"}:
            raise StoicRequestError("Stoic returned an invalid execution state.")
        elif local is None:
            if self.stopped.is_set() or not config["enabled"]:
                return
            run = json.loads(binding["run_json"])
            try:
                self._authorize_settings(config, run["settings"])
                local = self.external_work.submit(source, run_id, run["settings"], self._prompt(run), name=run.get("name"))
            except BrowserBusyError:
                # Maintenance refuses work before durable admission. Retain
                # ownership and retry after the runtime resumes.
                local = self.external_work.get(source, run_id)
                if local is None:
                    return
            except CoordinationError as exc:
                # A persisted submission may have reached the provider even if
                # its reply was lost. The engine is authoritative about that.
                local = self.external_work.get(source, run_id)
                if local is None:
                    local = {"state": "failed", "result": None, "error": str(exc)[:1000]}
        state = local["state"]
        if state in TERMINAL_STATES:
            status = "completed" if state == "succeeded" else state
            receipt = {**auth, "receipt_id": "result:" + binding["claim_id"] + ":" + status,
                       "status": status,
                       "result": local.get("result"), "error": local.get("error")}
            # Save the exact terminal bytes before network publication. A lost
            # response reuses the receipt, including after process restart.
            self._save(run_id, receipt_json=json.dumps(receipt), state="reporting")
            client.request("POST", path + "/result?view=receipt", receipt)
            self._save(run_id, state="uncertain" if state == "uncertain" else "published",
                       receipt_json=None if state == "uncertain" else json.dumps(receipt), error=None)
        elif binding["state"] != "submitted":
            self._save(run_id, state="submitted", error=None)

    def process_once(self, *, wait=False):
        if self.stopped.is_set() or not self.processing.acquire(blocking=False):
            return
        try:
            config = self._configuration()
            bindings = self._bindings()
            if not config["enabled"] and not any(b["state"] != "claiming" for b in bindings):
                return
            client = self.client_factory(config["base_url"], config["token_file"])
            failures = []
            active_count = sum(b["state"] not in {"claiming", "uncertain"} for b in bindings)
            for binding in bindings:
                if self.stopped.is_set():
                    return
                if binding["state"] == "claiming":
                    admission = getattr(self.sessions, "admission", None)
                    if (not config["enabled"] or active_count >= config["max_concurrent_runs"]
                            or (admission is not None and admission.paused)):
                        continue
                    active_count += 1
                try:
                    self._synchronize(client, config, binding)
                except Exception as exc:
                    error = str(exc) if isinstance(exc, StoicRequestError) else "Stoic run synchronization failed; inspect its execution."
                    try:
                        if isinstance(exc, StoicRequestError) and exc.status in {404, 409, 410, 412}:
                            local = self.external_work.get("stoic:" + config["connection_id"], binding["run_id"])
                            if local and local["state"] not in TERMINAL_STATES:
                                self.external_work.cancel("stoic:" + config["connection_id"], binding["run_id"])
                            self._save(binding["run_id"], state="conflict", error=error)
                        else:
                            self._save(binding["run_id"], error=error)
                    except Exception:
                        # One broken binding must not prevent other owners from
                        # renewing leases or publishing already completed work.
                        error = "Stoic run recovery failed; inspect its execution."
                    failures.append(error)
            if not config["enabled"]:
                self.last_error = failures[0] if failures else None
                return
            now = self.store.clock()
            if now >= self.next_scan:
                page = client.request("GET", "/api/execution/runs?" + urlencode({"status": "pending", "limit": 100, "view": "ids"}))
                rows = page.get("runs")
                if not isinstance(rows, list):
                    raise StoicRequestError("Stoic returned an invalid pending-run list.")
                for run in rows:
                    self._stage(run.get("id") if isinstance(run, dict) else None)
                self.next_scan = now + 30
            # The durable pending-run scan is authoritative. Feed events only
            # reduce latency, and cursor expiry never discards pending work.
            query = {"after": config["cursor"] or "latest", "wait": 5 if wait else 0, "limit": 100,
                     "types": "execution.run.available"}
            try:
                feed = client.request("GET", "/api/event-feed?" + urlencode(query))
                cursor = feed.get("next_cursor")
                if not isinstance(cursor, str) or len(cursor) > 2000:
                    raise StoicRequestError("Stoic returned an invalid event cursor.")
                if feed.get("events") or feed.get("has_more"):
                    self.next_scan = 0
                with self.store.database_operation("stoic.cursor", priority="background"), self.store._connection() as db:
                    db.execute("UPDATE stoic_connection SET cursor=? WHERE id=1", (cursor,))
            except StoicRequestError as exc:
                if exc.status in {400, 404, 410, 422}:
                    self.next_scan = 0
                    with self.store._connection() as db:
                        db.execute("UPDATE stoic_connection SET cursor=NULL WHERE id=1")
                else:
                    raise
            self.last_error = failures[0] if failures else None
            self.last_connected_at = self.store.clock()
        except StoicRequestError as exc:
            self.last_error = str(exc)
        finally:
            self.processing.release()

    def start(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, name="stoic-integration", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.process_once(wait=True)
            except Exception:
                # Provider errors must not disclose credentials or response
                # contents, or silently kill synchronization for every job.
                self.last_error = "Stoic synchronization failed; inspect the run before retrying."
            self.stopped.wait(30 if self.last_error else 5)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            # Provider calls already admitted must finish before the owning
            # server closes BrowserSessions and its persistence workers.
            self.worker.join()


def start_connector(external_work):
    connector = StoicIntegration(external_work)
    connector.start()
    return connector
