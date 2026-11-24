# External work execution

Ribbon Field can execute assignments from another application through a general
external-work interface. A Stoic adapter runs inside the existing Ribbon Field
runtime. The adapter uses that interface; the execution engine has no dependency
on Stoic concepts or storage.

## Ownership

The source application owns the job definition, schedule, instruction revision,
and requested outcome. It creates one durable run for each occurrence. Ribbon
Field owns the provider session and records the association between that session
and the source run. The source application receives the result and delivers any
user notification.

Do not create a second recurring RF schedule for a job scheduled by Stoic. The
event feed is a notification mechanism. The pending-run API is the recovery
source when events are missed, expired, or unavailable.

## General external-work contract

An assignment contains a source namespace, a request ID, a prompt, and explicit
provider settings. Settings use the ordinary RF session fields: `cwd`, `client`,
`model`, `effort`, and `permission_mode` or the legacy `yolo` flag. An optional
name becomes the conversation title and is preserved by automatic titling.
The conversation can still be renamed explicitly. The runtime validates settings
through the same machinery used for an interactive session. It does not silently replace an
unavailable model or reasoning setting.

An optional `quiet: true` makes the new conversation quiet. It is a
presentation option, not an execution setting. A quiet conversation sends no
desktop, web push or phone notification when a turn finishes. Its results do not
enter Attention; the inbox lists it under Quiet, and under Working while it runs.
Failures, stalls, provider approvals, blocking questions and checkpoints that ask
the user for something still notify and enter Attention. The user can change
the mode later from the thread menu or with `agent-coord thread update
--quiet|--no-quiet`. A routed assignment delivered to a role holder does not
change the holder's mode. A fallback conversation for that assignment is quiet.

The pair `(source, request_id)` identifies an assignment. Repeating the same
request returns its existing receipt. Reusing the pair with different inputs is
an error. Keep this identity stable after a timeout or a lost response.

The Python interface is:

```python
runtime.submit(source, request_id, settings, prompt, name=None, route=None, quiet=False)
runtime.get(source, request_id)
runtime.list(source=None, limit=50)
runtime.cancel(source, request_id)
runtime.resolve(source, request_id, review=None)
```

The authenticated HTTP interface exposes:

- `POST /api/external-work`: submit an assignment.
- `GET /api/external-work`: list receipts.
- `GET /api/external-work/{source}/{request_id}`: read one receipt.
- `POST /api/external-work/{source}/{request_id}/cancel`: request cancellation.
- `POST /api/external-work/{source}/{request_id}/resolve`: reconcile uncertain execution.

HTTP execution requires a separate bearer credential in the owner-only file
`<coordination database path>.external-work-token`. An absent credential disables
this HTTP surface. It does not disable in-process adapters. Never include the
credential in a prompt, a URL, or a committed configuration file.

Provision that credential without printing it:

```sh
agent-coord external-work provision-token
```

Use `agent-coord external-work submit|get|list|cancel|resolve` for authenticated local
execution. Submit takes `--source`, `--request-id`, `--settings` as JSON, an
optional `--name`, an optional `--route` as JSON, an optional `--quiet`, and the prompt (or `-` for stdin). The optional `--url` selects
an RF runtime endpoint. The global `--db` selects the coordination database.

Receipts contain `source`, `request_id`, `state`, `thread_id`, `turn_id`, `result`,
and `error`. States include pending, running, succeeded, failed, cancelled, and
uncertain. A successful result contains final response text and bounded artifact
references. A plain final response is supported. A structured final response can
use `{"text":"...","artifacts":["..."]}`. A success receipt establishes delivery
of the requested result format, not independent verification of every claim in
the result. Source applications can apply their own acceptance checks.

An interrupted or ambiguous provider submission must not automatically start a
second agent. The receipt remains uncertain until execution can be reconciled.
Run `external-work resolve --source S --request-id I` to use saved native
completion evidence for the exact assigned turn. If that evidence is unavailable,
inspect the conversation and external effects first, then supply `--review` JSON
containing the recorded `thread_id`, `turn_id`, and exactly one of `result`
(`text` and `artifacts`) or `error`. Resolution never submits a replacement turn.
Repeated identical resolution is safe. The source receives the resolved result
on the connector's next pass. Uncertain runs remain visible but do not consume
new-execution capacity.
User Stop and runtime update admission rules retain their ordinary meaning.

## Routing an assignment to a role holder

By default every assignment starts a new conversation. An assignment can
instead carry a route, `{"role": "<role name>"}`, naming a
[workspace role](reference.md#supervision-roles-and-succession). Ribbon Field then delivers the prompt as a new
turn in the conversation that holds the role, and that turn's final message is
the result. Routing is optional and additive: an assignment without a route is
stored, launched and reported exactly as before, and its receipt has no
`delivery` field. The route is part of the assignment's identity.

**Workspace.** The role is looked up by name (ignoring case and repeated
spaces) from the assignment's `settings.cwd`, or the runtime's workspace when
`cwd` is absent. The nearest role workspace that contains the directory wins.
Otherwise a single role of that name beneath the directory is used; several
are ambiguous and cause a fallback.

**Resolution.** The holder is resolved at each delivery attempt, never
earlier. The first attempt happens when the assignment is submitted, which for
a Stoic run is immediately after its claim. A run that is still waiting follows
the role if it is transferred, and stops waiting if the role is vacated or its
holder is closed.

**Delivery.** The holder keeps its own provider, model, effort and permissions.
The assignment's settings are not applied to it. Its title and saved original
request do not change. The assignment waits while the holder has a running
turn, an active review, messages the user queued, or an earlier waiting
assignment; a waiting receipt is `pending` with no `thread_id` and
`delivery.outcome` `waiting`. The runtime retries every two seconds. A waiting
assignment can be cancelled and survives a runtime restart without operator
review because nothing was sent. It waits at most two hours from submission
(`ROUTE_WAIT_SECONDS`). If the holder is still busy then, the assignment falls
back to a new conversation and records that the holder stayed busy.

**Fallback.** The ordinary new-conversation path runs with the assignment's
own settings and name when the route cannot be delivered: the role does not
exist, is ambiguous, has no holder, its holder is closed, closing, not an app
conversation of this runtime, refused by the source's holder policy, cannot be
read, or stayed busy for the whole waiting period, or the route has a form this
runtime does not support. The receipt
records `delivery.outcome` `fallback` and the reason. An assignment is never
discarded because of its route.

**Receipt.** A routed receipt adds `delivery`:

```json
{"route": {"role": "outcomes"}, "outcome": "holder", "role_id": "…", "thread_id": "<holder conversation>"}
{"route": {"role": "outcomes"}, "outcome": "waiting", "reason": "Waiting for the role holder's current work to finish.", "thread_id": "<holder conversation>"}
{"route": {"role": "outcomes"}, "outcome": "fallback", "reason": "Role 'outcomes' has no holder."}
```

The existing guarantees hold. The assignment is admitted in one transaction
before the provider is contacted. A provider refusal that proves nothing was
sent returns the assignment to waiting. Any ambiguous submission to the holder
becomes `uncertain` and is never replayed; `resolve` works as for any other
assignment. Cancellation stops the assigned turn only while it is the holder's
active turn. A question the holder asked the user before the assignment does
not fail it; a blocking question asked during the assigned turn does.

In-process sources can pass `holder_policy`, a callable that receives the
holder's session record and raises `CoordinationError` to refuse it.
`runtime.inspect_route(route, directory)` reports whether a route would reach a
holder now, without delivering anything.

## Stoic connection

Stoic job conversations append the occurrence's scheduled date in the Ribbon
Field host's local time zone to the job name. For an unnamed run, Ribbon Field
derives the title from the saved instructions.

Stoic copies each definition's `notify` value into the run snapshot. A run
with `notify: "failures"` is submitted with `quiet: true`, so its conversation
reports only failures and requests that need the user. Any other value, or
none, leaves the conversation ordinary. Stoic still delivers its own result
notification.

The adapter is disabled by default. Its configuration contains:

```json
{
  "enabled": true,
  "base_url": "https://tryblume.ai",
  "token_file": "/absolute/private/path/to/stoic-token",
  "max_concurrent_runs": 2,
  "allowed_roots": ["/opt/projects"],
  "allowed_permission_modes": {
    "codex": ["workspace-write"],
    "claude": ["default"]
  },
  "allowed_models": {},
  "notice_session_id": null
}
```

The default credential reference is `~/.config/stoic/codex-token`. RF reads the
file for each authenticated request so credential rotation does not require
embedding a token in the database. HTTPS is required, except for loopback HTTP
used by local development and integration tests. HTTP redirects are rejected.

RF requires a local execution policy before accepting remote work. Working
directories must resolve beneath an allowed root, and each provider's exact
permission mode must appear in `allowed_permission_modes`. Empty defaults deny
execution. Full access requires adding `full-access` for Codex or
`bypassPermissions` for Claude locally. A provider listed in `allowed_models`
is restricted to those model IDs; omitted providers have no extra model filter.
RF still verifies that every requested model and effort is available.
Policy is checked before claiming a run. A disallowed offer remains pending in
Stoic, and RF reports the local policy error without consuming the occurrence.
Connection status includes the denied count and up to 20 run IDs with reasons.
Discovery cycles through pending pages so denied work cannot hide later offers.

These settings authorize the connected Stoic account to submit work with the
listed permissions. Allowed roots constrain the starting directory; they do not
sandbox a full-access agent. Use a private runner credential and rotate it through
the referenced file. Stoic's current bearer-token authentication is account-wide;
it does not provide runner-specific API scopes.

Inspect or enable the integration through the local CLI:

```sh
agent-coord integration stoic status
agent-coord integration stoic configure --settings '{"enabled":true,"allowed_roots":["/opt/projects"],"allowed_permission_modes":{"codex":["workspace-write"],"claude":["default"]}}'
```

Authenticated `GET /api/browser/integrations/stoic` returns connection status,
the last connection error, and run counts. Authenticated `PATCH` on the same
path changes configuration. These endpoints use RF's local browser control
authentication. Status does not return bearer tokens or run claim tokens.

### Role-routed Stoic runs

A Stoic execution definition can name a role beside its settings:
`{"mode":"external","prompt":"…","settings":{…},"route":{"role":"outcomes"}}`.
Stoic copies the route into each run's snapshot, and the adapter passes it to
the external-work interface when it submits the claimed run. A definition
without a route is unaffected: its runs open a new conversation each time, the
submission call and the published receipt are unchanged.

- **Workspace.** The run's `settings.cwd` selects the workspace in which the
  role name is resolved. The five settings remain required and are checked
  against local policy before the claim, because they define the fallback.
- **Holder policy.** A routed run enters a holder only when local policy would
  let Stoic start that conversation: the holder's directory is beneath an
  allowed root, its provider and permission mode are listed in
  `allowed_permission_modes`, and its model passes `allowed_models`. Otherwise
  the run falls back to a new conversation, so a route cannot widen what the
  connected Stoic account may run.
- **Heartbeat and result.** While the run waits for a busy holder the adapter
  heartbeats without a thread or turn. After delivery it heartbeats with the
  holder's thread and the assigned turn, and publishes that turn's final
  message. The result receipt adds
  `"delivery": {"route": …, "outcome": "holder"|"fallback"|"waiting", "thread_id": …, "reason": …}`,
  which Stoic stores on the run. Claim and receipt identities are unchanged.
- **Capacity.** A run that is only waiting for its holder does not count
  against `max_concurrent_runs`; it counts once its turn has started.
- **Coverage check.** When the adapter starts, and once a day afterwards, it
  reads `GET /api/execution/definitions?routed=true` and resolves every named
  role whose definition's `cwd` is beneath an allowed root. A role that could not receive a run is listed under `uncovered_roles`
  in the connection status. One `action_required` message per uncovered role is
  sent to the conversation named by `notice_session_id`; it is not repeated
  while the same gap remains, including across restarts. When the role becomes
  deliverable the gap is cleared, and a later gap is announced again. A run
  that falls back records and announces its role's gap immediately. Without a
  `notice_session_id` the gap stays in the status and is announced once a
  recipient is configured. A Stoic release without the definition list is
  ignored and checked again the next day.

Disabling the connection stops new claims and submissions. The adapter continues reconciling
already claimed work and publishing completed results. It does not kill active
agents. Cancel a particular run to stop that assignment. Changing the origin or
credential-file reference is rejected while executions need reconciliation or
result publication.

The adapter stores its stable runner identity, event cursor, claim identity,
run snapshot, and exact result publication receipt in the coordination database.
Network requests and provider operations run outside database transactions.

## Stoic protocol

The ordinary Stoic bearer token authenticates these requests:

1. `GET /api/execution/runs?status=pending&limit=100&view=ids` returns run IDs in `{ "runs": [...] }`.
2. `POST /api/execution/runs/{id}/claim` sends `runner_id` and a persisted
   `claim_id`. It returns `run`, `claim_token`, and `lease_expires_at`.
3. `POST /api/execution/runs/{id}/heartbeat` sends `runner_id`, `claim_token`,
   and known `thread_id` and `turn_id`. It returns the authoritative run state.
4. `POST /api/execution/runs/{id}/result?view=receipt` sends the ownership fields, a stable
   `receipt_id`, terminal status, result, and error. Repeated identical receipts
   must be accepted without another notification.

The event feed is filtered to `execution.run.available`. Discovery and result
acknowledgments use compact views so a page of long prompts cannot exceed the
client's response limit. Claims retain the full immutable run snapshot.

The adapter maps RF `succeeded` to Stoic `completed`. Failed, cancelled, and
uncertain execution retain their respective meanings. The run contains the
immutable `id`, `name`, `prompt`, and `settings` needed by the executor, and
`route` when its definition names one. A routed run uses the same envelope and
submission text; only the receiving conversation differs.
RF prepends an occurrence-context JSON object with `run_id`, `source`,
`source_id`, `occurrence_id`, and `scheduled_at` before the unchanged instruction
text. Jobs can therefore use the scheduled date after a delayed start. The
context contains no authentication material. Stoic reserves 2,000 characters
for this envelope within RF's 100,000-character submission limit.
The connector saves the full assembled submission with the claim receipt, so
recovery uses the same text.

A lost claim response repeats the saved claim ID. A lost result response repeats
the saved result receipt. Lease expiration does not authorize reassignment and
blind re-execution. An ownership conflict prevents a new launch and stops a
known active local assignment.

Stoic retains job and review configuration behind its public API. Jobs use the
`job` source; recurring review definitions use the `review` source. The
configuration endpoints are `GET` and `PUT`
`/api/execution/definitions/{source}/{id}`. External configuration provides the
prompt and provider settings; internal configuration preserves the existing
execution path. Existing jobs stay internal until explicitly changed.

The illustrative daily economy brief is not installed automatically. Its daily
time, instructions, provider settings, and notification preferences belong in a
user-selected Stoic job.

## Availability and deployment

The adapter runs with RF's existing runtime and does not require a second
service. The machine must be awake and the runtime must be available. The Stoic
ledger retains work while RF is offline. The source scheduler determines how
missed occurrences are combined or skipped.

Before replacing either Stoic API or scheduler image, run the candidate migration
`python -m src.scripts.migrate_external_execution --check`, then `--apply`, then
require `--check` to exit zero. New ORM mappings require the additive columns to
exist before the new image serves any requests. Rehearse on isolated PostgreSQL
and verify the production backup before this sequence. Then replace the Stoic
API/scheduler images. Deploy the RF runtime, verify its active payload,
and enable the connection. A staged RF application is not proof that the new
backend is active. Keep existing jobs on internal execution until the complete
external path has passed a bounded live validation.

## Verification

`tests/test_external_work_routing.py` and `tests/test_stoic_routing.py` cover
routing: unchanged behavior without a route, delivery to an idle holder, waiting
behind a busy holder, fallback for an unheld or closed role, role transfer,
restart, the holder policy, and the single coverage notice.

The connector tests cover disabled operation, exact settings, restart recovery,
lost claim and result responses, uncertain submissions, cancellation, revoked
ownership, event-cursor expiration, concurrency limits, and credential handling:

```sh
env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID \
  python3 -W error::ResourceWarning -m unittest discover \
  -s tests -p test_stoic_integration.py -v
```

Run the generic external-work tests, authenticated HTTP tests, the complete
repository suite, and the required database contention gate before rollout.

The cross-repository release gate uses Stoic's isolated database fixture, its
real FastAPI routes over loopback HTTP, the real credential-reading client, and
the real RF engine. Only the provider is simulated:

```sh
env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID \
  STOIC_REPOSITORY=/opt/projects/stoic \
  /opt/projects/stoic/.venv/bin/python -m unittest discover \
  -s tests -p test_stoic_execution_contract.py -v
```

This suite is explicitly skipped without `STOIC_REPOSITORY`; running only the
ordinary RF suite is insufficient for a Stoic integration release. Stoic's
separate PostgreSQL concurrency and migration checks are also required.
