# Antigravity conversation sync

Ribbon Field sends opted-in Antigravity prompt/final-reply pairs to Stoic's
existing `POST /api/codex/turns` API. Stoic's normal message search and KES
ingestion apply to imported messages. No additional service is installed.

Start a user prompt with `use Stoic`, `please use Stoic`, `/stoic`, `$stoic`,
or `/stoic on`. The opt-in prompt and subsequent completed turns are included.
Earlier history is excluded. `/stoic off`, `$stoic off`, or `stop using Stoic`
disables sync and discards all queued turns. An upload already in flight may
finish. Automatic coordination messages cannot enable or disable sync.

Provide an agent credential through `STOIC_API_TOKEN`, `STOIC_API_TOKEN_FILE`,
or `~/.config/stoic/codex-token` (mode 0600), in that order. The adapter verifies
the agent identity before importing. It never places credentials in prompts,
saved state or diagnostic output, and does not forward credentials on redirects.
`STOIC_API_URL` defaults to `https://tryblume.ai`.

Consent survives a runtime restart and resume of the same Ribbon Field thread.
New threads and forks require their own opt-in. The Ribbon Field thread and
turn IDs are the import keys; provider process IDs are not. Repeated uploads
therefore deduplicate even if an acknowledgment is lost. Changing token subjects
does not send the former account's queue to the new account.

Local state is private JSON at
`~/.local/state/stoic-antigravity-sync/<sha256-of-thread-ID>/state.json`.
`enabled`, `pending`, `last_error`, `last_synced_at`, `last_turn_id` and
`conversation_id` distinguish consent, pending work and acknowledged delivery.
`STOIC_ANTIGRAVITY_SYNC_STATE_DIR` overrides the directory for isolated tests.
Uploads run outside provider and database locks, and failed uploads remain
queued for the next completed turn. No polling daemon is used. An off command
does not wait for an in-flight network request.

Only user text and the final assistant text are imported. Tool traces,
injected coordination/skill instructions, failed or interrupted turns, and
images are excluded. Image-only prompts are not imported. The existing Codex
and Claude Code hooks remain responsible for their own providers.

Existing threads, including Rook, use the same path as new threads: after the
updated backend is active, submit a fresh opt-in in that thread. Nothing
automatically replays the earlier transcript or inherits historical consent.
Plugin refresh alone does not activate code in an already running backend.
