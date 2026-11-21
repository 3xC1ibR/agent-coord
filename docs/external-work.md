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
name labels the conversation. The runtime validates the settings through the
same machinery used for an interactive session. It does not silently replace an
unavailable model or reasoning setting.

The pair `(source, request_id)` identifies an assignment. Repeating the same
request returns its existing receipt. Reusing the pair with different inputs is
an error. Keep this identity stable after a timeout or a lost response.

The Python interface is:

```python
runtime.submit(source, request_id, settings, prompt, name=None)
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
optional `--name`, and the prompt (or `-` for stdin). The optional `--url` selects
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

## Stoic connection

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
  "allowed_models": {}
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
immutable `id`, `name`, `prompt`, and `settings` needed by the executor.
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
