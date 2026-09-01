from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .store import WAKEABLE_ACTIVITIES, CoordinationError, CoordinationStore
from .zellij_wake import WAKE_PROMPT, ZellijClient, ZellijCommandError

DEFAULT_POLL_SECONDS = 0.25
DEFAULT_LOG_LIMIT_BYTES = 1024 * 1024
DEFAULT_LOG_RETAIN_BYTES = 768 * 1024
DEFAULT_TERMINAL_GRACE_SECONDS = 3.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class _TerminalScreen:
    """Render the useful text state of the fixed-size managed terminal."""

    def __init__(self, *, rows: int = 40, columns: int = 120) -> None:
        self.rows = rows
        self.columns = columns
        self.lines = [[" "] * columns for _ in range(rows)]
        self.history: list[str] = []
        self.row = 0
        self.column = 0
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.saved_cursor = (0, 0)

    def _blank_line(self) -> list[str]:
        return [" "] * self.columns

    def _line_text(self, line: list[str]) -> str:
        return "".join(line).rstrip()

    def _scroll_up(self, count: int = 1) -> None:
        for _ in range(max(0, count)):
            removed = self.lines.pop(self.scroll_top)
            self.lines.insert(self.scroll_bottom, self._blank_line())
            if self.scroll_top == 0:
                self.history.append(self._line_text(removed))
                self.history = self.history[-1000:]

    def _scroll_down(self, count: int = 1) -> None:
        for _ in range(max(0, count)):
            self.lines.pop(self.scroll_bottom)
            self.lines.insert(self.scroll_top, self._blank_line())

    def _line_feed(self) -> None:
        self.column = 0
        if self.row == self.scroll_bottom:
            self._scroll_up()
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _reverse_index(self) -> None:
        if self.row == self.scroll_top:
            self._scroll_down()
        else:
            self.row = max(0, self.row - 1)

    @staticmethod
    def _parameters(value: str) -> list[int | None]:
        value = value.lstrip("?<=>!")
        if not value:
            return []
        parameters: list[int | None] = []
        for part in value.split(";"):
            try:
                parameters.append(int(part.split(":", 1)[0]) if part else None)
            except ValueError:
                parameters.append(None)
        return parameters

    @staticmethod
    def _parameter(parameters: list[int | None], index: int, default: int = 1) -> int:
        if index >= len(parameters) or parameters[index] in {None, 0}:
            return default
        return int(parameters[index])

    def _erase_display(self, mode: int) -> None:
        if mode in {2, 3}:
            self.lines = [self._blank_line() for _ in range(self.rows)]
            if mode == 3:
                self.history.clear()
            return
        if mode == 0:
            self.lines[self.row][self.column :] = [" "] * (self.columns - self.column)
            for row in range(self.row + 1, self.rows):
                self.lines[row] = self._blank_line()
            return
        if mode == 1:
            for row in range(self.row):
                self.lines[row] = self._blank_line()
            self.lines[self.row][: self.column + 1] = [" "] * (self.column + 1)

    def _erase_line(self, mode: int) -> None:
        if mode == 0:
            self.lines[self.row][self.column :] = [" "] * (self.columns - self.column)
        elif mode == 1:
            self.lines[self.row][: self.column + 1] = [" "] * (self.column + 1)
        elif mode == 2:
            self.lines[self.row] = self._blank_line()

    def _control_sequence(self, value: str, command: str) -> None:
        parameters = self._parameters(value)
        amount = self._parameter(parameters, 0)
        if command == "A":
            self.row = max(self.scroll_top, self.row - amount)
        elif command in {"B", "e"}:
            self.row = min(self.scroll_bottom, self.row + amount)
        elif command in {"C", "a"}:
            self.column = min(self.columns - 1, self.column + amount)
        elif command == "D":
            self.column = max(0, self.column - amount)
        elif command == "E":
            self.row = min(self.scroll_bottom, self.row + amount)
            self.column = 0
        elif command == "F":
            self.row = max(self.scroll_top, self.row - amount)
            self.column = 0
        elif command in {"G", "`"}:
            self.column = min(self.columns - 1, amount - 1)
        elif command in {"H", "f"}:
            self.row = min(self.rows - 1, self._parameter(parameters, 0) - 1)
            self.column = min(self.columns - 1, self._parameter(parameters, 1) - 1)
        elif command == "d":
            self.row = min(self.rows - 1, amount - 1)
        elif command == "J":
            self._erase_display(parameters[0] or 0 if parameters else 0)
        elif command == "K":
            self._erase_line(parameters[0] or 0 if parameters else 0)
        elif command == "L" and self.scroll_top <= self.row <= self.scroll_bottom:
            for _ in range(amount):
                self.lines.pop(self.scroll_bottom)
                self.lines.insert(self.row, self._blank_line())
        elif command == "M" and self.scroll_top <= self.row <= self.scroll_bottom:
            for _ in range(amount):
                self.lines.pop(self.row)
                self.lines.insert(self.scroll_bottom, self._blank_line())
        elif command == "P":
            line = self.lines[self.row]
            del line[self.column : self.column + amount]
            line.extend([" "] * amount)
        elif command == "@":
            line = self.lines[self.row]
            line[self.column : self.column] = [" "] * amount
            del line[self.columns :]
        elif command == "X":
            end = min(self.columns, self.column + amount)
            self.lines[self.row][self.column : end] = [" "] * (end - self.column)
        elif command == "S":
            self._scroll_up(amount)
        elif command == "T":
            self._scroll_down(amount)
        elif command == "r":
            top = self._parameter(parameters, 0) - 1
            bottom = self._parameter(parameters, 1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.scroll_top, self.scroll_bottom = top, bottom
            else:
                self.scroll_top, self.scroll_bottom = 0, self.rows - 1
            self.row = self.column = 0
        elif command == "s":
            self.saved_cursor = (self.row, self.column)
        elif command == "u":
            self.row, self.column = self.saved_cursor

    def _put(self, character: str) -> None:
        if unicodedata.combining(character):
            if self.column:
                self.lines[self.row][self.column - 1] += character
            return
        width = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if self.column >= self.columns:
            self._line_feed()
        self.lines[self.row][self.column] = character
        if width == 2 and self.column + 1 < self.columns:
            self.lines[self.row][self.column + 1] = ""
        self.column += width

    def feed(self, text: str) -> None:
        index = 0
        while index < len(text):
            character = text[index]
            if character == "\x1b":
                if index + 1 >= len(text):
                    break
                kind = text[index + 1]
                if kind == "[":
                    end = index + 2
                    while end < len(text) and not "@" <= text[end] <= "~":
                        end += 1
                    if end >= len(text):
                        break
                    self._control_sequence(text[index + 2 : end], text[end])
                    index = end + 1
                    continue
                if kind in "]P_X^":
                    end = index + 2
                    while end < len(text):
                        if text[end] == "\x07":
                            end += 1
                            break
                        if text[end : end + 2] == "\x1b\\":
                            end += 2
                            break
                        end += 1
                    index = end
                    continue
                if kind in {"D", "E"}:
                    self._line_feed()
                elif kind == "M":
                    self._reverse_index()
                elif kind == "7":
                    self.saved_cursor = (self.row, self.column)
                elif kind == "8":
                    self.row, self.column = self.saved_cursor
                index += 2
                continue
            if character == "\r":
                self.column = 0
            elif character == "\n":
                self._line_feed()
            elif character == "\b":
                self.column = max(0, self.column - 1)
            elif character == "\t":
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif character >= " ":
                self._put(character)
            index += 1

    def render(self) -> str:
        lines = self.history + [self._line_text(line) for line in self.lines]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def runtime_directory(store: CoordinationStore, delegation_id: str) -> Path:
    if not _SAFE_ID.fullmatch(delegation_id):
        raise CoordinationError("Delegation ID is not safe for a runtime path.")
    root = store.database_path.parent / "delegations"
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise CoordinationError("Refusing to use a delegations symlink.")
    directory = root / delegation_id
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise CoordinationError("Refusing to use a delegation runtime symlink.")
    return directory


def output_log_path(store: CoordinationStore, delegation_id: str) -> Path:
    return runtime_directory(store, delegation_id) / "output.log"


def supervisor_log_path(store: CoordinationStore, delegation_id: str) -> Path:
    return runtime_directory(store, delegation_id) / "supervisor.log"


def _append_bounded_log(
    path: Path,
    data: bytes,
    *,
    limit_bytes: int = DEFAULT_LOG_LIMIT_BYTES,
    retain_bytes: int = DEFAULT_LOG_RETAIN_BYTES,
) -> None:
    if not data:
        return
    if limit_bytes <= 0 or not 0 < retain_bytes <= limit_bytes:
        raise CoordinationError("Managed PTY log bounds are invalid.")
    if path.exists() and path.is_symlink():
        raise CoordinationError(
            "Refusing to write managed PTY output through a symlink."
        )
    with path.open("ab") as log_file:
        log_file.write(data)
    size = path.stat().st_size
    if size <= limit_bytes:
        return
    with path.open("rb") as log_file:
        log_file.seek(max(0, size - retain_bytes))
        tail = log_file.read()
    with path.open("wb") as log_file:
        log_file.write(b"[older managed PTY output truncated]\n")
        log_file.write(tail)


def read_output_tail(path: str | Path | None, *, max_bytes: int = 64 * 1024) -> str:
    if path is None or max_bytes <= 0:
        return ""
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        return ""
    size = candidate.stat().st_size
    with candidate.open("rb") as log_file:
        log_file.seek(max(0, size - DEFAULT_LOG_LIMIT_BYTES))
        data = log_file.read()
    text = data.decode("utf-8", errors="replace")
    terminal = _TerminalScreen()
    terminal.feed(text)
    rendered = terminal.render()
    encoded = rendered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return rendered
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _replace_output_snapshot(path: Path, output: str) -> None:
    if path.exists() and path.is_symlink():
        raise CoordinationError(
            "Refusing to write delegation output through a symlink."
        )
    data = output.encode("utf-8")[-DEFAULT_LOG_RETAIN_BYTES:]
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix="output-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(data)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def read_delegation_output(
    store: CoordinationStore, delegation_id: str, *, max_bytes: int = 64 * 1024
) -> str:
    delegation = store.get_delegation(delegation_id)
    if not _SAFE_ID.fullmatch(delegation_id):
        raise CoordinationError("Delegation ID is not safe for a runtime path.")
    root = store.database_path.parent / "delegations"
    if root.exists() and root.is_symlink():
        raise CoordinationError("Refusing to read through a delegations symlink.")
    expected = (root / delegation_id / "output.log").resolve()
    stored = delegation["output_log_path"]
    if stored and Path(stored).resolve() != expected:
        raise CoordinationError(
            "Delegation output path does not match its runtime directory."
        )
    if delegation["runtime_kind"] == "managed-pty":
        if not stored:
            return ""
        return read_output_tail(expected, max_bytes=max_bytes)
    if delegation["runtime_kind"] != "zellij":
        return ""

    cached = read_output_tail(expected, max_bytes=max_bytes)
    should_capture = (
        delegation["status"]
        in {
            "launching",
            "launched",
            "attached",
        }
        or not cached
    )
    executable = shutil.which("zellij") if should_capture else None
    if executable and delegation["zellij_session"] and delegation["pane_id"]:
        client = ZellijClient(
            session_name=str(delegation["zellij_session"]),
            pane_id=str(delegation["pane_id"]),
            executable=executable,
        )
        try:
            screen = client.dump_screen()
        except (OSError, ZellijCommandError):
            pass
        else:
            if screen.strip():
                snapshot = output_log_path(store, delegation_id)
                _replace_output_snapshot(snapshot, screen)
                return read_output_tail(snapshot, max_bytes=max_bytes)
    return read_output_tail(expected, max_bytes=max_bytes)


def build_supervisor_command(store: CoordinationStore, delegation_id: str) -> list[str]:
    wrapper = Path(__file__).resolve().parents[1] / "agent-coord"
    return [
        sys.executable,
        str(wrapper),
        "--db",
        str(store.database_path),
        "delegation",
        "supervise",
        "--delegation-id",
        delegation_id,
    ]


def launch_managed_pty(
    store: CoordinationStore,
    delegation_id: str,
    *,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> dict[str, Any]:
    command = build_supervisor_command(store, delegation_id)
    output_path = output_log_path(store, delegation_id)
    supervisor_path = supervisor_log_path(store, delegation_id)
    try:
        with supervisor_path.open("ab") as supervisor_log:
            process = popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=supervisor_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        error = f"Cannot start managed PTY supervisor: {exc}"
        store.fail_delegation_launch(delegation_id, error)
        raise CoordinationError(error) from exc
    try:
        launched = store.mark_delegation_launched(
            delegation_id,
            runtime_kind="managed-pty",
            supervisor_pid=process.pid,
            output_log_path=str(output_path),
        )
    except Exception:
        process.terminate()
        raise
    return {
        "status": launched["status"],
        "delegation": launched,
        "command": command,
    }


class ManagedPTYWakeWatcher:
    def __init__(
        self,
        store: CoordinationStore,
        session_id: str,
        *,
        write: Callable[[int, bytes], int] = os.write,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.write = write

    def run_once(self, master_fd: int) -> dict[str, Any]:
        try:
            target = self.store.get_wake_target(self.session_id)
        except CoordinationError:
            return {"status": "disabled", "session_id": self.session_id}
        if not target["enabled"] or target["transport"] != "managed-pty":
            return {"status": "disabled", "session_id": self.session_id}
        session = self.store.get_session(self.session_id)
        if session["presence"] == "offline":
            return {"status": "offline", "session_id": self.session_id}
        message_ids = self.store.pending_wake_message_ids(self.session_id)
        if not message_ids:
            return {"status": "waiting", "session_id": self.session_id}
        if session["turn_active"] or session["activity"] not in WAKEABLE_ACTIVITIES:
            self.store.record_wake_check(self.session_id)
            return {
                "status": "busy",
                "session_id": self.session_id,
                "message_ids": message_ids,
            }
        claimed = self.store.claim_wake_messages(self.session_id, message_ids)
        if not claimed:
            return {"status": "raced", "session_id": self.session_id}
        try:
            self.write(master_fd, (WAKE_PROMPT + "\r").encode())
        except OSError as exc:
            detail = f"Managed PTY wake failed: {exc}"
            self.store.complete_wake_attempts(
                self.session_id, claimed, outcome="failed", detail=detail
            )
            self.store.record_wake_check(self.session_id, error=detail)
            return {
                "status": "failed",
                "session_id": self.session_id,
                "message_ids": claimed,
                "error": detail,
            }
        self.store.complete_wake_attempts(
            self.session_id,
            claimed,
            outcome="sent",
            detail=WAKE_PROMPT,
        )
        self.store.record_wake_check(self.session_id, woke=True)
        return {
            "status": "woke",
            "session_id": self.session_id,
            "message_ids": claimed,
        }


def _set_controlling_terminal() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _set_terminal_size(fd: int, *, rows: int = 40, columns: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _kill_process_group(child_pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(child_pid, sig)
    except ProcessLookupError:
        pass


def _drain_master(master_fd: int, path: Path) -> bool:
    try:
        data = os.read(master_fd, 64 * 1024)
    except BlockingIOError:
        return True
    except OSError as exc:
        if exc.errno == errno.EIO:
            return False
        raise
    if not data:
        return False
    _append_bounded_log(path, data)
    return True


def _lock_path(store: CoordinationStore, delegation_id: str) -> Path:
    digest = hashlib.sha256(delegation_id.encode()).hexdigest()[:16]
    return runtime_directory(store, delegation_id) / f"supervisor-{digest}.lock"


@contextmanager
def _supervisor_lock(store: CoordinationStore, delegation_id: str):
    path = _lock_path(store, delegation_id)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoordinationError(
                f"A managed PTY supervisor already owns delegation {delegation_id}."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def supervise_managed_pty(
    store: CoordinationStore,
    delegation_id: str,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    terminal_grace_seconds: float = DEFAULT_TERMINAL_GRACE_SECONDS,
    client_command: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if poll_interval_seconds <= 0:
        raise CoordinationError("Managed PTY poll interval must be positive.")
    delegation = store.get_delegation(delegation_id)
    if delegation["runtime_kind"] != "managed-pty":
        raise CoordinationError(
            f"Delegation {delegation_id} is not a managed PTY delegation."
        )
    if client_command is None:
        from .delegate import build_client_command

        executable = shutil.which(str(delegation["client"]))
        if executable is None:
            raise CoordinationError(
                f"{delegation['client']} is required for managed PTY delegation."
            )
        client_command = build_client_command(
            delegation,
            client_executable=executable,
            agent_coord_cli=str(Path(__file__).resolve().parents[1] / "agent-coord"),
            database_path=str(store.database_path),
        )
    output_path = output_log_path(store, delegation_id)
    child_environment = dict(os.environ if environment is None else environment)
    child_environment.update(
        {
            "AGENT_COORD_DELEGATION_ID": delegation_id,
            "AGENT_COORD_DB": str(store.database_path),
            "AGENT_COORD_CLIENT": str(delegation["client"]),
            "AGENT_COORD_MANAGED_PTY": "1",
            "TERM": child_environment.get("TERM") or "xterm-256color",
        }
    )

    with _supervisor_lock(store, delegation_id):
        master_fd, slave_fd = pty.openpty()
        child: subprocess.Popen[bytes] | None = None
        child_session_id: str | None = None
        watcher: ManagedPTYWakeWatcher | None = None
        terminal_since: float | None = None
        eof_sent = False
        try:
            _set_terminal_size(slave_fd)
            child = popen(
                list(client_command),
                cwd=str(delegation["cwd"]),
                env=child_environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_set_controlling_terminal,
            )
            os.close(slave_fd)
            slave_fd = -1
            os.set_blocking(master_fd, False)
            store.set_delegation_child_process(
                delegation_id,
                supervisor_pid=os.getpid(),
                child_pid=child.pid,
                output_log_path=str(output_path),
            )
            while child.poll() is None:
                readable, _, _ = select.select(
                    [master_fd], [], [], poll_interval_seconds
                )
                if readable and not _drain_master(master_fd, output_path):
                    break
                delegation = store.get_delegation(delegation_id)
                attached = delegation["child_session_id"]
                if attached and attached != child_session_id:
                    child_session_id = str(attached)
                    store.register_wake_target(
                        session_id=child_session_id,
                        transport="managed-pty",
                        endpoint={
                            "delegation_id": delegation_id,
                            "supervisor_pid": os.getpid(),
                            "child_pid": child.pid,
                        },
                        watcher_pid=os.getpid(),
                    )
                    watcher = ManagedPTYWakeWatcher(store, child_session_id)
                if watcher is not None and delegation["status"] not in {
                    "completed",
                    "failed",
                }:
                    watcher.run_once(master_fd)
                if delegation["status"] in {"completed", "failed"}:
                    terminal_since = terminal_since or monotonic()
                    inactive = True
                    if child_session_id:
                        try:
                            inactive = not store.get_session(child_session_id)[
                                "turn_active"
                            ]
                        except CoordinationError:
                            inactive = True
                    if inactive and not eof_sent:
                        try:
                            os.write(master_fd, b"\x04")
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                        eof_sent = True
                    if monotonic() - terminal_since >= terminal_grace_seconds:
                        _kill_process_group(child.pid, signal.SIGTERM)
            if child.poll() is None:
                try:
                    child.wait(timeout=terminal_grace_seconds)
                except subprocess.TimeoutExpired:
                    _kill_process_group(child.pid, signal.SIGTERM)
                    child.wait(timeout=terminal_grace_seconds)
            exit_code = int(child.returncode or 0)
            while _drain_master(master_fd, output_path):
                readable, _, _ = select.select([master_fd], [], [], 0)
                if not readable:
                    break
            result = store.record_delegation_runtime_exit(
                delegation_id, exit_code=exit_code
            )
            if child_session_id:
                store.disable_wake(child_session_id)
                try:
                    if store.get_session(child_session_id)["presence"] != "offline":
                        store.end_session(child_session_id)
                except CoordinationError:
                    pass
            return result
        finally:
            if child is not None and child.poll() is None:
                _kill_process_group(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=terminal_grace_seconds)
                except subprocess.TimeoutExpired:
                    _kill_process_group(child.pid, signal.SIGKILL)
                    child.wait()
            if slave_fd >= 0:
                os.close(slave_fd)
            os.close(master_fd)
