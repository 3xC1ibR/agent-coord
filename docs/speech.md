# Read replies aloud

Ribbon Field can automatically read completed chat replies using Kokoro on your
Mac. The app owns speech generation and playback; agents need no instructions,
tools, or prompt changes. Both Codex and Claude chats are supported.

## Set up a new installation

1. Install and start [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI#readme)
   on the Mac using its documented CPU setup. Kokoro and its voice models are
   a separate service; Ribbon Field does not bundle or download them.
2. Open **Speech settings** in Ribbon Field’s sidebar. Enter the local Kokoro
   API address, normally `http://127.0.0.1:8880/v1`. Choose a voice from the
   available-voices dropdown (the default is `af_heart`) and a speed, then choose
   **Test voice** to save and check playback. If you change the address, choose
   **Save** to load that server's voices. **Refresh voices** retries the list
   after starting Kokoro; your saved selection remains available while it is offline.
3. Open a chat's thread context side panel and select **Read replies aloud**.
   On mobile, open **⋯ → Thread context and links** to reach the same control.
   New completed replies are read automatically on that device while Ribbon
   Field remains open. Existing history is not read when you turn speech on.
   Select it again to turn it off.

The integration ships with the app and requires no helper scripts, development
checkout, Python SDK, or machine-specific filesystem paths. Settings are saved
privately beside Ribbon Field’s state database. This release supports a local
Kokoro-FastAPI service; hosted providers and API keys are not supported.

## Listen on your phone

Use the existing [paired Tailscale connection](remote-access.md), open a chat,
and enable **Read replies aloud** in its thread context panel on the phone.
The Mac generates the audio and
Ribbon Field sends it through the authenticated connection. The phone does not
need Kokoro installed or a second network route. Configure the provider on the
Mac; paired phones can view settings and test their own playback.

Speech preferences are per chat and per browser/device. Safari and the native
iOS app have separate storage. The Mac must remain awake and run both Ribbon
Field and Kokoro. Browser restrictions may require **Play** before audio can
start; this never loses the generated reply. Background/locked-phone automatic
playback is not guaranteed by this release.

## Playback behavior

- Read the final answer after a successful turn; skip commentary, tools,
  reasoning, failed turns, and interrupted turns. For Claude history without
  message phases, use the last assistant message.
- Remove Markdown formatting, link destinations, and fenced code blocks from
  spoken text. Replies above 24,000 spoken characters show an explicit error.
- Queue replies from enabled chats, including when another chat is selected.
  Stop speech cancels pending playback; turning a chat off also cancels its queue.
- Share playback claims between same-origin tabs with the browser Web Locks API.
  Other devices have independent preferences and playback.
- Show generation/connectivity errors with a retry control. Reloading does not
  replay old conversation history. Audio is held in memory and discarded after
  playback or cancellation; it is not saved to conversation history.

Only the Mac can change the provider address. It must be a loopback HTTP(S)
address with an explicit port and `/v1` path. Redirects and environment proxies
are disabled. Audio requests retain normal pairing, origin, and CSRF checks;
the server checks pairing again after generation. At most two requests may
generate concurrently, with a 90-second socket timeout and a 32 MiB audio limit.

## Validation

The implementation passed the full 713-test Python suite (one skip), the
381-test JavaScript suite, and plugin validators. Focused coverage includes
pairing/CSRF, revocation during generation, redirect rejection, completed-reply
selection, duplicate suppression, autoplay recovery, and cancellation while
waiting for another tab.

Fresh installed-plugin browser checks exercised configuration, a valid MP3
sample, automatic final-only requests, reload persistence without replay,
provider-error retry, and desktop/390px mobile layouts. The mobile speech
controls measure 44px and the page has no horizontal overflow. These checks used
an isolated fake provider and MP3 service; real Kokoro synthesis and physical
iPhone/background playback were not exercised.
