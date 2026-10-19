/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * Hook C (?hook=delegation): Let Codex review Claude's implementation—without copy-pasting between terminals.
 * Direct agent-to-agent messaging, delegation, and shared work-thread state for multi-agent engineering workflows. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const smooth = n => { const x = clamp(n); return x * x * (3 - 2 * x); };
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .4) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const show = (el, o) => { el.style.opacity = String(clamp(o)); el.style.visibility = o <= 0 ? "hidden" : "visible"; };

  const OUTRO = 39.8, DURATION = 45;

  const headings = [
    {at: 0, end: 8.8, l1: "Let Codex review Claude's implementation—", l2: "without copy-pasting between terminals."},
    {at: 9.0, end: 18.8, l1: "Direct agent-to-agent delegation.", l2: "Shared work-thread state & scope handoff."},
    {at: 19.0, end: 28.8, l1: "Codex audits the diff and replies.", l2: "Findings arrive directly in Claude's inbox."},
    {at: 29.0, end: OUTRO, l1: "Claude patches the race condition.", l2: "Codex signs off · zero terminal juggling."},
  ];

  const subheads = [
    {at: 0.4, end: 8.6, text: "Stop manually copy-pasting plans and diffs from Claude Code into Codex (or vice versa) across terminal panes."},
    {at: 9.2, end: 18.6, text: "Direct agent-to-agent messaging, delegation, and shared work-thread state for multi-agent engineering workflows."},
    {at: 19.2, end: 28.6, text: "Codex inspects src/auth/pkce.py in the same workspace and sends a structured reply with exact line numbers."},
    {at: 29.2, end: 39.4, text: "Claude applies the atomic mutex fix, Codex verifies 34/34 tests green, and the shared checkpoint marks finished."},
  ];

  const arcTop = $("arc-top"), arcBot = $("arc-bot");
  const lenTop = arcTop.getTotalLength(), lenBot = arcBot.getTotalLength();
  $("arc-top-active").style.strokeDasharray = `${lenTop}`;
  $("arc-bot-active").style.strokeDasharray = `${lenBot}`;

  function typeCmd(text, t, start, dur = 1.4) {
    if (t < start) return "";
    const p = clamp((t - start) / dur);
    const s = text.slice(0, Math.floor(p * text.length));
    return p < 1 && Math.floor(t * 4) % 2 === 0 ? s + "▋" : s;
  }

  function render(time) {
    const t = Math.max(0, Math.min(DURATION - .001, time));
    const outro = progress(t, OUTRO, .7);
    show($("brand"), 1 - outro);
    show($("story"), 1 - outro);
    show($("endcard"), outro);
    $("endcard").style.transform = `translateY(${(1 - outro) * 18}px)`;

    const h = headings.find(x => t >= x.at && t < x.end);
    const l1 = $("headline").querySelector(".l1"), l2 = $("headline").querySelector(".l2");
    l1.textContent = h?.l1 || ""; l2.textContent = h?.l2 || "";
    show(l1, h ? fade(t, h.at, h.end, .35) : 0);
    show(l2, h ? fade(t, h.at + .12, h.end, .35) : 0);
    l1.style.transform = `translateY(${h ? (1 - progress(t, h.at, .45)) * 10 : 0}px)`;
    l2.style.transform = `translateY(${h ? (1 - progress(t, h.at + .12, .45)) * 10 : 0}px)`;

    const sub = subheads.find(x => t >= x.at && t < x.end);
    $("subhead").textContent = sub?.text || "";
    show($("subhead"), sub ? fade(t, sub.at, sub.end, .35) : 0);

    const intro = progress(t, 0.6, .6);
    show($("thread-bar"), intro);
    show($("col-claude"), intro);
    show($("col-codex"), intro);
    $("col-claude").style.transform = `translateY(${(1 - intro) * 16}px)`;
    $("col-codex").style.transform = `translateY(${(1 - intro) * 16}px)`;

    $("ring-claude").style.transform = `rotate(${t * 50}deg)`;
    $("ring-codex").style.transform = `rotate(${t * 50 + 120}deg)`;

    // Thread phase pill
    let phaseTxt = "phase: implementation";
    if (t >= 9.0 && t < 19.0) phaseTxt = "phase: orchestrating";
    else if (t >= 19.0 && t < 29.0) phaseTxt = "phase: validation";
    else if (t >= 29.0 && t < 34.5) phaseTxt = "phase: implementation";
    else if (t >= 34.5) phaseTxt = "phase: finished ✓";
    $("thread-phase").textContent = phaseTxt;

    // Ghost clipboard (manual copy-paste pain in 1.2 - 8.6)
    const ghostOn = fade(t, 1.4, 8.6, .35);
    show($("clipboard-ghost"), ghostOn);
    $("ghost-tag").textContent = t < 4.6 ? "⌘C / ⌘V · Terminal Copy-Paste" : "Context Truncated · 410 lines lost";
    $("ghost-txt").textContent = t < 4.6
      ? "Copying 380-line diff from Claude pane into Codex prompt…"
      : "Missing file paths & test state · manual relay breaks flow";

    // Claude & Codex columns content
    let cliClaude = "", cliCodex = "";
    let artClaudeTitle = "", artClaudeBody = "", artCodexTitle = "", artCodexBody = "";
    let stClaude = {text: "", cls: ""}, stCodex = {text: "", cls: ""};

    if (t < 9.0) {
      cliClaude = "$ claude · Implemented src/auth/pkce.py";
      artClaudeTitle = "src/auth/pkce.py (unreviewed)";
      artClaudeBody = "def rotate_refresh_token(user_id):\n  tok = db.fetch_token(user_id)\n  return issue_pair(tok.family_id)";
      stClaude = {text: "Waiting for human to copy diff…", cls: "bad"};

      cliCodex = "$ codex · Pasted raw snippet";
      artCodexTitle = "Prompt buffer (missing context)";
      artCodexBody = "> \"Here is Claude's code, is it safe?\"\n! Missing token_store.py & test suite\n! Cannot verify concurrency locks";
      stCodex = {text: "Blind to repo state & thread history", cls: "bad"};
    } else if (t < 19.0) {
      cliClaude = typeCmd("agent-coord delegate --client codex --role validation", t, 9.4, 1.6);
      artClaudeTitle = "Handoff · scope & checkpoint shared";
      artClaudeBody = "target: src/auth/pkce.py\ntests: tests/test_pkce.py\nreply_required: true";
      stClaude = {text: t >= 12.2 ? "Delegated review to Codex ✓" : "Dispatching validation request…", cls: "done"};

      cliCodex = t >= 12.2 ? "$ agent-coord inbox --unread" : "$ codex · Managed specialist ready";
      artCodexTitle = t >= 12.2 ? "Inbox message #418 (from Claude)" : "Awaiting specialist routing…";
      artCodexBody = t >= 12.2
        ? "Review PKCE token rotation for\nconcurrent refresh race conditions.\nShared thread: acme-auth"
        : "Connected to shared SQLite coordination DB";
      stCodex = {text: t >= 12.2 ? "Inspecting src/auth/pkce.py…" : "Idle · listening for handoff", cls: "done"};
    } else if (t < 29.0) {
      cliClaude = "$ agent-coord inbox --unread";
      artClaudeTitle = t >= 23.6 ? "Reply received from Codex (#419)" : "src/auth/pkce.py";
      artClaudeBody = t >= 23.6
        ? "Finding: Line 58 lacks row lock.\nTwo parallel requests can mint\nduplicate refresh tokens."
        : "Waiting for Codex validation verdict…\nScope released for inspection";
      stClaude = {text: t >= 23.6 ? "Actionable finding in inbox !" : "Awaiting specialist verdict…", cls: t >= 23.6 ? "warn" : ""};

      cliCodex = typeCmd("agent-coord reply --message-id 418 'Race on L58'", t, 19.6, 1.6);
      artCodexTitle = "Validation Verdict · src/auth/pkce.py";
      artCodexBody = "✕ Line 58: unlocked SELECT before rotation\n→ Fix: wrap family_id in SELECT FOR UPDATE\n✓ Automatically acks message #418";
      stCodex = {text: "Sent structured review to Claude ✓", cls: "warn"};
    } else {
      cliClaude = typeCmd("agent-coord send --to codex 'Patched row lock'", t, 29.4, 1.4);
      artClaudeTitle = "src/auth/pkce.py · patched ✓";
      artClaudeBody = "with db.lock_family(tok.family_id):\n  verify_not_revoked(tok.id)\n  return issue_pair(tok.family_id)";
      stClaude = {text: "Race fixed · 34/34 tests passing ✓", cls: "done"};

      cliCodex = "$ pytest tests/test_pkce.py -q";
      artCodexTitle = "Re-verification · Codex sign-off";
      artCodexBody = t >= 33.4
        ? "✓ Parallel refresh race test passed\n✓ 34 passed in 0.38s\n✓ Checkpoint: finished (next_step: null)"
        : "Running concurrent refresh stress test…\nVerifying SELECT FOR UPDATE lock…";
      stCodex = {text: t >= 33.4 ? "Approved & verified · LGTM ✓" : "Verifying Claude's patch…", cls: "done"};
    }

    $("cli-claude").textContent = cliClaude;
    $("art-claude-title").textContent = artClaudeTitle;
    $("art-claude-body").textContent = artClaudeBody;
    $("status-claude").textContent = stClaude.text;
    $("status-claude").className = "col-status " + stClaude.cls;

    $("cli-codex").textContent = cliCodex;
    $("art-codex-title").textContent = artCodexTitle;
    $("art-codex-body").textContent = artCodexBody;
    $("status-codex").textContent = stCodex.text;
    $("status-codex").className = "col-status " + stCodex.cls;

    // Network arcs and message pills
    const topArcVis = fade(t, 10.6, 18.6, .35) + fade(t, 30.6, 38.6, .35);
    const botArcVis = fade(t, 20.8, 28.6, .35) + fade(t, 33.0, 38.6, .35);
    show($("arc-top"), clamp(topArcVis));
    show($("arc-bot"), clamp(botArcVis));

    // Top packet (Claude -> Codex at 11.0-12.2 and 31.0-32.1)
    let fTop = -1;
    if (t >= 11.0 && t <= 12.2) fTop = (t - 11.0) / 1.2;
    else if (t >= 31.0 && t <= 32.1) fTop = (t - 31.0) / 1.1;
    if (fTop >= 0) {
      const s = smooth(fTop), pt = arcTop.getPointAtLength(s * lenTop);
      $("pkt-top").setAttribute("cx", pt.x); $("pkt-top").setAttribute("cy", pt.y);
      $("pkt-top").setAttribute("fill", "#9c7949");
      $("arc-top-active").setAttribute("stroke", "#9c7949");
      $("arc-top-active").style.strokeDashoffset = `${lenTop * (1 - s)}`;
      show($("pkt-top"), Math.sin(Math.PI * s));
      show($("arc-top-active"), Math.sin(Math.PI * s));
    } else {
      show($("pkt-top"), 0); show($("arc-top-active"), 0);
    }

    // Bottom packet (Codex -> Claude at 21.4-22.6 and 33.2-34.3)
    let fBot = -1;
    if (t >= 21.4 && t <= 22.6) fBot = (t - 21.4) / 1.2;
    else if (t >= 33.2 && t <= 34.3) fBot = (t - 33.2) / 1.1;
    if (fBot >= 0) {
      const s = smooth(fBot), pt = arcBot.getPointAtLength(s * lenBot);
      $("pkt-bot").setAttribute("cx", pt.x); $("pkt-bot").setAttribute("cy", pt.y);
      $("pkt-bot").setAttribute("fill", "#3f7062");
      $("arc-bot-active").setAttribute("stroke", "#3f7062");
      $("arc-bot-active").style.strokeDashoffset = `${lenBot * (1 - s)}`;
      show($("pkt-bot"), Math.sin(Math.PI * s));
      show($("arc-bot-active"), Math.sin(Math.PI * s));
    } else {
      show($("pkt-bot"), 0); show($("arc-bot-active"), 0);
    }

    // Floating pills
    let pillTopTxt = "", pillTopOn = 0;
    if (t >= 11.4 && t < 18.4) {
      pillTopTxt = "Claude → Codex · Review src/auth/pkce.py (reply_required=true)";
      pillTopOn = fade(t, 11.4, 18.4, .3);
    } else if (t >= 31.2 && t < 38.4) {
      pillTopTxt = "Claude → Codex · Added SELECT FOR UPDATE lock on line 58";
      pillTopOn = fade(t, 31.2, 38.4, .3);
    }
    $("pill-top").textContent = pillTopTxt;
    show($("pill-top"), pillTopOn);

    let pillBotTxt = "", pillBotOn = 0;
    if (t >= 21.8 && t < 28.4) {
      pillBotTxt = "Codex → Claude · Line 58 race condition: lock token family before issue";
      pillBotOn = fade(t, 21.8, 28.4, .3);
    } else if (t >= 33.4 && t < 38.6) {
      pillBotTxt = "Codex → Claude · Verified · 34/34 tests green · LGTM ✓";
      pillBotOn = fade(t, 33.4, 38.6, .3);
    }
    $("pill-bot").textContent = pillBotTxt;
    show($("pill-bot"), pillBotOn);

    // Bottom banner
    let bannerText = "", bannerColor = "#3f7062";
    if (t >= 1.2 && t < 8.8) {
      bannerText = "Without Agent Coord: manual copy-paste between terminals loses repo context";
      bannerColor = "#b84a39";
    } else if (t >= 9.6 && t < 18.8) {
      bannerText = "Specialist routing: Claude delegates validation directly to Codex with shared state";
      bannerColor = "#9c7949";
    } else if (t >= 19.6 && t < 28.8) {
      bannerText = "agent-coord reply --message-id 418 delivers findings straight to Claude's inbox";
      bannerColor = "#a87f32";
    } else if (t >= 29.4 && t < OUTRO) {
      bannerText = "Closed-loop multi-model engineering · implemented by Claude, verified by Codex ✓";
      bannerColor = "#3f7062";
    }
    show($("bottom-banner"), bannerText ? 1 : 0);
    $("banner-text").textContent = bannerText;
    $("banner-dot").style.background = bannerColor;

    window.__sceneState = {
      time: t,
      headline: h ? `${h.l1} ${h.l2}` : "",
      subhead: sub?.text || null,
      phase: phaseTxt,
      pillTop: pillTopTxt || null,
      pillBot: pillBotTxt || null,
      banner: bannerText || null,
      endcard: outro >= 1
    };
  }

  function cues() {
    return [
      {t: 0.0, kind: "mark", name: "intro"},
      {t: 2.0, kind: "riser", length: 2.0},
      {t: 4.0, kind: "impact", gain: .85},
      {t: 4.0, kind: "mark", name: "groove"},
      {t: 9.0, kind: "whoosh", length: .45},
      {t: 9.4, kind: "click"},
      {t: 12.2, kind: "pop", pitch: 783.99, gain: .18},
      {t: 19.0, kind: "whoosh", length: .45},
      {t: 19.6, kind: "click"},
      {t: 22.6, kind: "pop", pitch: 659.25, gain: .18},
      {t: 29.0, kind: "mark", name: "break"},
      {t: 29.4, kind: "click"},
      {t: 32.0, kind: "mark", name: "groove"},
      {t: 32.1, kind: "pop", pitch: 880, gain: .18},
      {t: 34.3, kind: "pop", pitch: 1046.5, gain: .2},
      {t: 34.5, kind: "accent"},
      {t: 37.8, kind: "riser", length: 2.0},
      {t: OUTRO, kind: "impact", gain: 1.0},
      {t: OUTRO, kind: "mark", name: "outro"}
    ];
  }

  window.__render = t => { render(t); return true; };
  window.__duration = () => DURATION;
  window.__cues = cues;
  window.__ready = Promise.all([document.fonts.ready, ...Array.from(document.images, im => im.decode())]).then(() => { render(0); return true; });
})();
