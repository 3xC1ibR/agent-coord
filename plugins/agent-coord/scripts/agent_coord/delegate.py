from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .store import (
    LEASE_MODES,
    ConflictError,
    CoordinationError,
    CoordinationStore,
    normalize_scope,
)

DEFAULT_FLOATING_WIDTH = "90%"
DEFAULT_FLOATING_HEIGHT = "85%"
_PANE_ID = re.compile(r"\bterminal_\d+\b")
_CLIENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def _command_error(label: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return f"{label}: {detail}"


def _json_list(
    command: Sequence[str],
    *,
    cwd: str,
    label: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> list[dict[str, Any]]:
    result = run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoordinationError(_command_error(label, result))
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CoordinationError(f"{label}: command returned invalid JSON.") from exc
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise CoordinationError(f"{label}: command returned an invalid list.")
    return payload


def validate_repository(
    cwd: str,
    *,
    git_executable: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    requested = Path(cwd).expanduser().resolve()
    if not requested.is_dir():
        raise CoordinationError(f"Delegation repository does not exist: {requested}")
    result = run(
        [git_executable, "rev-parse", "--show-toplevel"],
        cwd=str(requested),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise CoordinationError(_command_error("Cannot resolve Git repository", result))
    return str(Path(result.stdout.strip()).resolve())


def validate_ready_bead(
    bead_id: str,
    cwd: str,
    *,
    bd_executable: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    issues = _json_list(
        [bd_executable, "show", bead_id, "--json"],
        cwd=cwd,
        label=f"Cannot read Bead {bead_id}",
        run=run,
    )
    if len(issues) != 1:
        raise CoordinationError(
            f"Bead lookup for {bead_id} returned {len(issues)} rows."
        )
    issue = issues[0]
    if issue.get("status") != "open" or issue.get("assignee"):
        raise CoordinationError(
            f"Bead {bead_id} must be open and unclaimed before delegation."
        )
    ready = _json_list(
        [bd_executable, "ready", "--json"],
        cwd=cwd,
        label="Cannot list ready Beads",
        run=run,
    )
    if not any(item.get("id") == bead_id for item in ready):
        raise CoordinationError(
            f"Bead {bead_id} is not ready; resolve its blockers before delegation."
        )
    return issue


def build_child_prompt(delegation: Mapping[str, Any], *, agent_coord_cli: str) -> str:
    client_label = _CLIENT_LABELS[str(delegation["client"])]
    scopes = "\n".join(f"- {scope}" for scope in delegation["write_scope"])
    lease_mode = str(delegation.get("lease_mode", "write"))
    finish_prefix = (
        f"{agent_coord_cli} delegation finish --delegation-id "
        f"{delegation['delegation_id']} --session-id <your-session-id>"
    )
    if lease_mode == "validation":
        return f"""You are a delegated {client_label} validator launched by Agent Coord.

Delegation ID: {delegation["delegation_id"]}
Parent session: {delegation["parent_session_id"]}
Repository: {delegation["cwd"]}
Bead: {delegation["bead_id"]}
Authorized validation scopes:
{scopes}

Requested validation:
{delegation["instructions"]}

Before validation, follow this sequence:
1. Read all applicable repository instructions and run `bd prime`.
2. Run `bd ready` and `bd show {delegation["bead_id"]}`. Stop if the Bead is not open and ready.
3. Claim the Bead with `bd update {delegation["bead_id"]} --claim`.
4. Use the session ID from the Agent Coord SessionStart hook to run:
   `{agent_coord_cli} begin-work --session-id <your-session-id> --bead {delegation["bead_id"]} --activity validating --lease-mode validation` with one `--scope` argument for each exact scope above.
5. Validate only the requested work. Do not edit repository files or remediate findings. Obey repository validation and git-authority rules.

Record a Beads checkpoint with the commands, verdict, findings, and next action. A failed check is a completed validation result when every required check ran and you reported the failure. Close the Bead when its validation acceptance criteria are complete. Release the declaration with `{agent_coord_cli} end-work --session-id <your-session-id>`, then report with:
`{finish_prefix} --outcome completed --message "Verdict: <pass-or-fail>; Commands: <command, exit status, duration, and concise summary>; Findings: <material findings or none>; Repository changes: <none or unexpected paths>; Details: <Beads note or artifact path>"`

Do not paste full successful logs into the result. If required validation cannot run, add a Beads note with the blocker, release the declaration, then report with:
`{finish_prefix} --outcome failed --message "<exact blocker or incomplete validation>"`

Always run one of the two `delegation finish` commands before your {client_label} process exits. The SessionEnd hook will record an unfinished exit as a failure."""
    return f"""You are a delegated {client_label} worker launched by Agent Coord.

Delegation ID: {delegation["delegation_id"]}
Parent session: {delegation["parent_session_id"]}
Repository: {delegation["cwd"]}
Bead: {delegation["bead_id"]}
Authorized write scopes:
{scopes}

Requested work:
{delegation["instructions"]}

Before editing, follow this sequence:
1. Read all applicable agent instructions for this repository, including AGENTS.md and/or CLAUDE.md files, and run `bd prime`.
2. Run `bd ready` and `bd show {delegation["bead_id"]}`. Stop and report failure if the Bead is no longer open and ready.
3. Claim the Bead with `bd update {delegation["bead_id"]} --claim`.
4. Use the session ID announced by the Agent Coord SessionStart hook to run:
   `{agent_coord_cli} begin-work --session-id <your-session-id> --bead {delegation["bead_id"]}` with one `--scope` argument for each exact scope above.
5. Implement only the requested work and obey repository validation, service-boundary, changelog, documentation, and git-authority rules. Do not commit or push unless the repository instructions or user explicitly authorize it.

At each meaningful boundary, record a Beads checkpoint. If the work succeeds, run the required validation, close the Bead only when its acceptance criteria are genuinely complete, release the declaration with `{agent_coord_cli} end-work --session-id <your-session-id>`, then report with:
`{finish_prefix} --outcome completed --message "Result: <concise outcome>; Changed paths: <paths or none>; Validation: <command, exit status, and concise summary>; Deviations or risks: <details or none>"`

Do not paste full successful logs into the result. If the work cannot complete, add a Beads note with the blocker and next action, release the declaration if active, then report with:
`{finish_prefix} --outcome failed --message "<exact blocker or failure>"`

Always run one of the two `delegation finish` commands before your {client_label} process exits. The SessionEnd hook will record an unfinished exit as a failure."""


def build_zellij_command(
    delegation: Mapping[str, Any],
    *,
    zellij_executable: str,
    client_executable: str,
    env_executable: str,
    agent_coord_cli: str,
    database_path: str,
    pane_name: str,
    floating: bool,
    bypass_hook_trust: bool = False,
    width: str = DEFAULT_FLOATING_WIDTH,
    height: str = DEFAULT_FLOATING_HEIGHT,
) -> list[str]:
    command = [
        zellij_executable,
        "--session",
        str(delegation["zellij_session"]),
        "action",
        "new-pane",
        "--name",
        pane_name,
        "--cwd",
        str(delegation["cwd"]),
        "--no-focus",
    ]
    if floating:
        command.extend(["--floating", "--width", width, "--height", height])
    command.extend(
        [
            "--",
            env_executable,
            f"AGENT_COORD_DELEGATION_ID={delegation['delegation_id']}",
            f"AGENT_COORD_DB={database_path}",
            f"AGENT_COORD_CLIENT={delegation['client']}",
        ]
    )
    command.extend(
        build_client_command(
            delegation,
            client_executable=client_executable,
            agent_coord_cli=agent_coord_cli,
            database_path=database_path,
            bypass_hook_trust=bypass_hook_trust,
        )
    )
    return command


def build_client_command(
    delegation: Mapping[str, Any],
    *,
    client_executable: str,
    agent_coord_cli: str,
    database_path: str,
    bypass_hook_trust: bool | None = None,
) -> list[str]:
    client = str(delegation["client"])
    command = [client_executable]
    if client == "codex":
        command.extend(["--cd", str(delegation["cwd"])])
    command.extend(["--add-dir", str(Path(database_path).parent)])
    model = delegation.get("model")
    if model:
        command.extend(["--model", str(model)])
    reasoning_effort = delegation.get("reasoning_effort")
    if reasoning_effort:
        if client == "codex":
            command.extend(
                [
                    "--config",
                    f"model_reasoning_effort={json.dumps(reasoning_effort)}",
                ]
            )
        else:
            command.extend(["--effort", str(reasoning_effort)])
    if delegation["mode"] == "yolo":
        command.append(
            "--dangerously-bypass-approvals-and-sandbox"
            if client == "codex"
            else "--dangerously-skip-permissions"
        )
    else:
        if client == "codex":
            command.append("--approve-for-me")
        else:
            command.extend(["--permission-mode", "auto"])
    should_bypass_hook_trust = (
        bool(delegation.get("bypass_hook_trust"))
        if bypass_hook_trust is None
        else bypass_hook_trust
    )
    if should_bypass_hook_trust:
        command.append("--dangerously-bypass-hook-trust")
    command.append(build_child_prompt(delegation, agent_coord_cli=agent_coord_cli))
    return command


def _required_executable(name: str, which: Callable[[str], str | None]) -> str:
    executable = which(name)
    if executable is None:
        raise CoordinationError(f"{name} is required for delegation.")
    return executable


def _optional_client_setting(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise CoordinationError(f"{label} must not be empty.")
    return normalized


def delegate_work(
    store: CoordinationStore,
    *,
    parent_session_id: str,
    cwd: str,
    bead_id: str,
    scopes: Iterable[str],
    instructions: str,
    zellij_session: str | None = None,
    pane_name: str | None = None,
    floating: bool = False,
    width: str = DEFAULT_FLOATING_WIDTH,
    height: str = DEFAULT_FLOATING_HEIGHT,
    client: str = "codex",
    yolo: bool = False,
    bypass_hook_trust: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
    lease_mode: str = "write",
    runtime: str | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    target_client = client.strip().lower()
    if target_client not in _CLIENT_LABELS:
        raise CoordinationError("Delegated client must be claude or codex.")
    client_label = _CLIENT_LABELS[target_client]
    parent = store.get_session(parent_session_id)
    if parent["presence"] != "online":
        raise CoordinationError(
            f"Parent session {parent_session_id} must be online before delegation."
        )
    if not instructions.strip():
        raise CoordinationError("Delegated instructions must not be empty.")
    if bypass_hook_trust and target_client != "codex":
        raise CoordinationError("--bypass-hook-trust is only supported for Codex.")
    target_model = _optional_client_setting(model, f"{client_label} model")
    target_reasoning_effort = _optional_client_setting(
        reasoning_effort, f"{client_label} effort"
    )
    target_lease_mode = lease_mode.strip().lower()
    if target_lease_mode not in LEASE_MODES:
        raise CoordinationError("Delegation lease mode must be write or validation.")
    if target_lease_mode == "validation" and yolo:
        raise CoordinationError("Validation-only delegation cannot use --yolo.")
    inferred_runtime = "zellij" if zellij_session or floating else "managed-pty"
    target_runtime = (runtime or inferred_runtime).strip().lower()
    if target_runtime not in {"managed-pty", "zellij"}:
        raise CoordinationError("Delegation runtime must be managed-pty or zellij.")
    if target_runtime == "managed-pty" and (zellij_session or floating):
        raise CoordinationError(
            "Zellij session and floating options require --runtime zellij."
        )

    git_executable = _required_executable("git", which)
    bd_executable = _required_executable("bd", which)
    client_executable = _required_executable(target_client, which)
    zellij_executable = None
    env_executable = None
    if target_runtime == "zellij":
        zellij_executable = _required_executable("zellij", which)
        env_executable = _required_executable("env", which)
    repository = validate_repository(cwd, git_executable=git_executable, run=run)
    normalized_scopes = sorted({normalize_scope(scope, repository) for scope in scopes})
    if not normalized_scopes:
        raise CoordinationError("At least one delegated scope is required.")
    issue = validate_ready_bead(
        bead_id,
        repository,
        bd_executable=bd_executable,
        run=run,
    )
    conflicts = store.proposed_work_conflicts(
        cwd=repository,
        bead_id=bead_id,
        scopes=normalized_scopes,
    )
    if conflicts:
        raise ConflictError(conflicts)
    for active in store.list_delegations(include_terminal=False):
        if active["cwd"] == repository and active["bead_id"] == bead_id:
            raise CoordinationError(
                f"Bead {bead_id} already has active delegation "
                f"{active['delegation_id']}."
            )

    auth_command = (
        [client_executable, "login", "status"]
        if target_client == "codex"
        else [client_executable, "auth", "status", "--json"]
    )
    login = run(
        auth_command,
        check=False,
        capture_output=True,
        text=True,
    )
    if login.returncode != 0:
        raise CoordinationError(
            _command_error(f"{client_label} login is not ready", login)
        )
    if target_client == "claude":
        try:
            auth_status = json.loads(login.stdout)
        except json.JSONDecodeError as exc:
            raise CoordinationError(
                "Claude Code login is not ready: auth status returned invalid JSON."
            ) from exc
        if not isinstance(auth_status, dict) or auth_status.get("loggedIn") is not True:
            raise CoordinationError(
                "Claude Code login is not ready: auth status reports loggedIn=false."
            )
    target_zellij = (
        zellij_session or environ.get("ZELLIJ_SESSION_NAME")
        if target_runtime == "zellij"
        else None
    )
    if target_runtime == "zellij" and not target_zellij:
        raise CoordinationError(
            "A Zellij session is required. Pass --zellij-session or run inside Zellij."
        )
    target_name = pane_name or f"{target_client}-{bead_id}"
    mode = "yolo" if yolo else "reviewed"
    agent_coord_cli = str(Path(__file__).resolve().parents[1] / "agent-coord")

    if dry_run:
        preview = {
            "delegation_id": "<dry-run>",
            "parent_session_id": parent_session_id,
            "client": target_client,
            "cwd": repository,
            "bead_id": bead_id,
            "write_scope": normalized_scopes,
            "instructions": instructions.strip(),
            "name": target_name,
            "status": "dry-run",
            "zellij_session": target_zellij,
            "pane_id": None,
            "runtime_kind": target_runtime,
            "supervisor_pid": None,
            "child_pid": None,
            "output_log_path": None,
            "mode": mode,
            "lease_mode": target_lease_mode,
            "bypass_hook_trust": bypass_hook_trust,
            "model": target_model,
            "reasoning_effort": target_reasoning_effort,
        }
        if target_runtime == "managed-pty":
            from .managed_pty import build_supervisor_command

            command = build_supervisor_command(store, "<dry-run>")
        else:
            assert zellij_executable is not None
            assert env_executable is not None
            command = build_zellij_command(
                preview,
                zellij_executable=zellij_executable,
                client_executable=client_executable,
                env_executable=env_executable,
                agent_coord_cli=agent_coord_cli,
                database_path=str(store.database_path),
                pane_name=target_name,
                floating=floating,
                bypass_hook_trust=bypass_hook_trust,
                width=width,
                height=height,
            )
        return {
            "status": "dry-run",
            "bead": issue,
            "delegation": preview,
            "command": command,
            "client_command": build_client_command(
                preview,
                client_executable=client_executable,
                agent_coord_cli=agent_coord_cli,
                database_path=str(store.database_path),
                bypass_hook_trust=bypass_hook_trust,
            ),
        }

    delegation = store.create_delegation(
        parent_session_id=parent_session_id,
        cwd=repository,
        bead_id=bead_id,
        scopes=normalized_scopes,
        instructions=instructions,
        mode=mode,
        lease_mode=target_lease_mode,
        bypass_hook_trust=bypass_hook_trust,
        model=target_model,
        reasoning_effort=target_reasoning_effort,
        client=target_client,
        runtime_kind=target_runtime,
        name=target_name,
    )
    if target_runtime == "managed-pty":
        from .managed_pty import launch_managed_pty

        return launch_managed_pty(store, delegation["delegation_id"])

    assert zellij_executable is not None
    assert env_executable is not None
    delegation["zellij_session"] = target_zellij
    command = build_zellij_command(
        delegation,
        zellij_executable=zellij_executable,
        client_executable=client_executable,
        env_executable=env_executable,
        agent_coord_cli=agent_coord_cli,
        database_path=str(store.database_path),
        pane_name=target_name,
        floating=floating,
        bypass_hook_trust=bypass_hook_trust,
        width=width,
        height=height,
    )
    result = run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        error = _command_error(f"Cannot launch {client_label} Zellij pane", result)
        store.fail_delegation_launch(delegation["delegation_id"], error)
        raise CoordinationError(error)
    match = _PANE_ID.search(result.stdout)
    if match is None:
        error = "Zellij created a pane but did not return a terminal pane ID."
        try:
            store.fail_delegation_launch(delegation["delegation_id"], error)
        except CoordinationError:
            pass
        raise CoordinationError(error)
    from .managed_pty import output_log_path

    launched = store.mark_delegation_launched(
        delegation["delegation_id"],
        runtime_kind="zellij",
        zellij_session=target_zellij,
        pane_id=match.group(0),
        output_log_path=str(output_log_path(store, delegation["delegation_id"])),
    )
    return {"status": launched["status"], "delegation": launched, "command": command}


def delegate_codex(store: CoordinationStore, **arguments: Any) -> dict[str, Any]:
    """Backward-compatible Python entry point for Codex-only callers."""
    return delegate_work(store, client="codex", **arguments)
