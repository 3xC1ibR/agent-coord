## Ask the user

File an ask whenever the outcome depends on something only the user can give:
a decision, an action, an approval, a review, or a resource. Do not bury it in a
reply or a checkpoint. When the user must act, file an ask instead of only
writing it in a reply: the user works through every open ask in one **Queue**
in the app, resolves it there, and the resolution reaches you as an ordinary
inbox message that wakes an idle conversation.

Choose the kind that matches what you need:

| Kind | Use it when | The user resolves it as |
| --- | --- | --- |
| `decision` (default) | The user must choose between options or answer a question: a product choice, a tradeoff with real cost, an ambiguity that would produce materially different work. | answered |
| `action` | Only the user can do something, usually elsewhere: sign in, upload a document, add billing details, press a physical button. | done, not done |
| `approval` | You need permission beyond your authorization: spending, sending on the user's behalf, an irreversible change. | approved, declined |
| `review` | The user should read, verify or try a result before you build on it. | accepted, changes requested |
| `request` | A tool, dataset, account, credential, budget or access would materially improve the result. | granted, declined |

The user can also dismiss any ask without acting on it. Every resolution may
carry a note; read it.

**Ask for what would make the work better.** When a tool, dataset, account,
credential, budget or access would materially improve the outcome, file a
`request` that states the expected gain and the default you will follow without
it. Do not limit the work to the resources at hand. A non-blocking request costs
the user one glance; a result bounded by a missing resource costs the outcome.

Use the `ask_human` tool when available, or the CLI:

```bash
agent-coord ask --non-blocking \
  --context 'Default: keep the existing CSV export until you choose.' \
  --option 'CSV' --option 'JSON' --option 'Both' \
  'Which export format should the report use?'
agent-coord ask --kind action --blocking \
  --unblocks 'Pricing for all 18 verified actors.' --effort 10 \
  --link 'https://console.apify.com/billing' --request-id apify-payout-1 \
  'Add payout billing details in Apify'
agent-coord ask --kind request --non-blocking \
  --context 'Default: continue with the public endpoint at 10 requests per minute.' \
  --unblocks 'A paid key removes the rate limit and cuts the crawl from 9 hours to 40 minutes.' \
  'Provide a SerpAPI key'
```

- **Title.** Write one specific question or instruction the user can act on
  without opening the thread. One ask per need: several steps in one place may
  share an ask; unrelated needs get separate asks.
- **`--unblocks`** says what resolving the ask enables. It is required for an
  `action` and a `request`. For a request, state the expected gain and what you
  do without it.
- **`--non-blocking`** whenever you can keep working. State the default you
  will proceed with in `--context`; it is required. Continue the task and
  handle the resolution on a later turn like any other message.
- **`--blocking`** only when no useful progress is possible without the
  resolution. End your turn after submitting the ask. The thread shows as
  needing the user until the ask is resolved or withdrawn. Add `--wait
  [SECONDS]` (tool: `wait_seconds`) only when a short wait is acceptable.
- **`--link`** points to where the user acts: an HTTPS URL, an `agentcoord://`
  link or an absolute file path. **`--effort MINUTES`** is your estimate of the
  user's time. **`--due`** is for a real external deadline, never a target you
  invented. **`--project`** overrides the grouping label, which defaults to
  your thread's project.
- **`--option`** offers a small set of answers for a decision; free text is
  always allowed.
- **Do not file the same need twice.** Pass `--request-id` when you may retry;
  the same session and ID return the existing ask. An ask whose title matches
  one you already have open also returns that ask (`"existing": true`). An
  orchestrator checks `ask --list --all-sessions` before filing, so the user is
  not asked for something another agent already asked.
- **Keep your asks current.** When the need changes, replace the ask with
  `--supersedes <id>` instead of filing another. When it no longer matters,
  `ask --withdraw <id>`. Do not withdraw an action because you noticed it was
  done: the user marks it done. Review yours with `ask --list` and one with
  `ask --show <id>`.
- **Act on the resolution.** After `done`, verify the result before relying on
  it. After `declined`, `not done` or `dismissed`, continue on your stated
  default; ask again only if the need changed.
- **Answer a reply.** The user can reply to an ask in their own words without
  resolving it. The reply arrives as an ordinary user message that names the
  ask: `Re ask #12 "Add payout billing details" (still open): which account?`.
  The ask is still open. Answer in the conversation. Then make the Queue
  current: withdraw the ask if the reply settles the need, replace it with
  `--supersedes <id>` if the need changed, or leave it open only when you still
  need its original resolution. When the reply states the user's choice for a
  decision, act on it and withdraw the ask. Never leave a replied ask open
  without saying in the conversation what you still need.
- Do not ask for routine judgment calls, confirmation of work you are
  authorized to do, or questions you can answer from the code.
