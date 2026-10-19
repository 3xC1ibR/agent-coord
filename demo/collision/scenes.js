/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * Hook A (?hook=collision): Stop Claude Code and Codex from overwriting each other's files.
 * Lease-based write scopes and file-conflict locks for parallel coding agents in one workspace. */
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
    {at: 0, end: 8.8, l1: "Stop Claude Code and Codex", l2: "from overwriting each other's files."},
    {at: 9.0, end: 18.8, l1: "Declare the smallest write scope.", l2: "Before touching a single line."},
    {at: 19.0, end: 27.8, l1: "Overlapping edits stop at the lock.", l2: "Before silent stomping happens."},
    {at: 28.0, end: OUTRO, l1: "Clean handoff in one workspace.", l2: "Zero worktree sprawl."},
  ];

  const subheads = [
    {at: 0.4, end: 8.6, text: "Running two agents in the same repo causes silent file stomping, broken tests, and git index.lock wars."},
    {at: 9.2, end: 18.6, text: "Lease-based write scopes and file-conflict locks for parallel coding agents—in one workspace, without worktree sprawl."},
    {at: 19.2, end: 27.6, text: "Instead of overwriting Claude's in-flight edits, Codex sees the active lease owner and switches to uncontested test files."},
    {at: 28.2, end: 39.4, text: "When Claude finishes and runs end-work, the lease releases cleanly and both agents pass validation together."},
  ];

  const wireL = $("wire-left"), wireR = $("wire-right"), arc = $("peer-arc");
  const lenL = wireL.getTotalLength(), lenR = wireR.getTotalLength(), lenArc = arc.getTotalLength();
  $("wire-left-active").style.strokeDasharray = `${lenL}`;
  $("wire-right-active").style.strokeDasharray = `${lenR}`;
  $("peer-arc-active").style.strokeDasharray = `${lenArc}`;

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

    const cardIntro = progress(t, 0.8, .6);
    show($("workspace-pill"), cardIntro);
    show($("agent-claude"), cardIntro);
    show($("agent-codex"), cardIntro);
    show($("file-card"), cardIntro);
    $("agent-claude").style.transform = `translateY(${(1 - cardIntro) * 16}px)`;
    $("agent-codex").style.transform = `translateY(${(1 - cardIntro) * 16}px)`;

    // Commands & scopes
    let cmdClaude = "", cmdCodex = "";
    let scopeClaude = "", scopeCodex = "";
    let statusClaude = {text: "Editing checkout.py…", cls: ""}, statusCodex = {text: "Editing checkout.py…", cls: ""};

    if (t < 9.0) {
      cmdClaude = "$ claude · Refactor Stripe capture";
      cmdCodex = "$ codex · Add webhook retry logic";
      scopeClaude = `<span class="scope-pill blocked">No write scope declared</span>`;
      scopeCodex = `<span class="scope-pill blocked">No write scope declared</span>`;
      if (t >= 4.2) {
        statusClaude = {text: "Lines 42–43 overwritten!", cls: "conflict"};
        statusCodex = {text: "fatal: .git/index.lock exists", cls: "conflict"};
      }
    } else if (t < 19.0) {
      cmdClaude = typeCmd("agent-coord begin-work --scope checkout.py", t, 9.6, 1.8);
      cmdCodex = "$ codex · Ready in acme-api";
      if (t >= 11.8) {
        scopeClaude = `<span class="scope-pill">lease: src/billing/checkout.py</span>`;
        statusClaude = {text: "Lease active · editing safely", cls: "done"};
      } else {
        scopeClaude = `<span class="scope-pill">Requesting scope…</span>`;
        statusClaude = {text: "Declaring write scope…", cls: ""};
      }
      scopeCodex = `<span class="scope-pill" style="--tone:#65755e;--tint:#ecefe6">idle · watching scopes</span>`;
      statusCodex = {text: "Sees active lease on checkout.py", cls: ""};
    } else if (t < 28.0) {
      cmdClaude = "agent-coord begin-work --scope checkout.py";
      scopeClaude = `<span class="scope-pill">lease: src/billing/checkout.py</span>`;
      statusClaude = {text: "Protected · edits undisturbed", cls: "done"};
      if (t < 23.6) {
        cmdCodex = typeCmd("agent-coord begin-work --scope checkout.py", t, 19.4, 1.4);
        if (t >= 21.0) {
          scopeCodex = `<span class="scope-pill blocked">CONFLICT: owned by Claude Code</span>`;
          statusCodex = {text: "Blocked before overwrite ✓", cls: "warn"};
        } else {
          scopeCodex = `<span class="scope-pill">Checking lease…</span>`;
          statusCodex = {text: "Requesting checkout.py…", cls: ""};
        }
      } else {
        cmdCodex = typeCmd("agent-coord begin-work --scope tests/", t, 23.6, 1.4);
        scopeCodex = `<span class="scope-pill">lease: tests/test_checkout.py</span>`;
        statusCodex = {text: "Parallel scope · writing tests ✓", cls: "done"};
      }
    } else {
      cmdClaude = t < 31.2 ? typeCmd("agent-coord end-work", t, 28.4, 1.0) : "agent-coord end-work";
      scopeClaude = t >= 29.8 ? `<span class="scope-pill" style="--tone:#3f7062;--tint:#e4eee8">scope released ✓</span>` : `<span class="scope-pill">releasing lease…</span>`;
      statusClaude = {text: "Committed · 0 conflicts ✓", cls: "done"};
      cmdCodex = "$ pytest tests/test_checkout.py";
      scopeCodex = `<span class="scope-pill">lease: tests/test_checkout.py</span>`;
      statusCodex = {text: "28 passed in 0.4s ✓", cls: "done"};
    }

    $("cmd-claude").textContent = cmdClaude;
    $("cmd-codex").textContent = cmdCodex;
    $("scope-claude").innerHTML = scopeClaude;
    $("scope-codex").innerHTML = scopeCodex;
    $("status-claude").textContent = statusClaude.text;
    $("status-claude").className = "agent-status " + statusClaude.cls;
    $("status-codex").textContent = statusCodex.text;
    $("status-codex").className = "agent-status " + statusCodex.cls;

    // Orbit rings
    $("ring-claude").style.transform = `rotate(${t * 55}deg)`;
    $("ring-codex").style.transform = `rotate(${t * 55 + 90}deg)`;

    // File card state
    const badge = $("lease-badge"), l1El = $("line-1"), l2El = $("line-2"), l3El = $("line-3");
    const l1Txt = $("line-1-txt"), l2Txt = $("line-2-txt"), l3Txt = $("line-3-txt"), footer = $("file-footer");
    l1El.className = "code-line"; l2El.className = "code-line"; l3El.className = "code-line";
    $("file-card").style.borderColor = "#dfe3d8";

    if (t < 4.2) {
      badge.textContent = "UNPROTECTED";
      badge.style.background = "#ecefe6"; badge.style.color = "#65755e";
      l1El.className = "code-line claude-write";
      l1Txt.textContent = "  intent = stripe.capture(cart.id)";
      l2Txt.textContent = "  return verify_capture(intent.id)";
      l3Txt.textContent = "  # Claude writing lines 42–43…";
      footer.textContent = "Both agents writing simultaneously…";
      footer.style.color = "#7f8d7a";
    } else if (t < 9.0) {
      badge.textContent = "OVERWRITTEN ✕";
      badge.style.background = "#fbeae7"; badge.style.color = "#a64436";
      $("file-card").style.borderColor = "#d97a6c";
      l1El.className = "code-line codex-stomp";
      l2El.className = "code-line codex-stomp";
      l3El.className = "code-line codex-stomp";
      l1Txt.textContent = "  intent = retry_webhook(cart) # stomp";
      l2Txt.textContent = "  raise CollisionError('broken tests')";
      l3Txt.textContent = "  # fatal: .git/index.lock exists";
      footer.textContent = "Silent file stomping · broken test suite";
      footer.style.color = "#a64436";
    } else if (t < 19.0) {
      badge.textContent = "LEASE: CLAUDE CODE";
      badge.style.background = "#f1eadd"; badge.style.color = "#9c7949";
      $("file-card").style.borderColor = "#9c7949";
      l1El.className = "code-line claude-write";
      l2El.className = "code-line claude-write";
      l1Txt.textContent = "  intent = stripe.capture(cart.id)";
      l2Txt.textContent = "  return verify_capture(intent.id)";
      l3Txt.textContent = "  # lease_mode: write · conflict-locked";
      footer.textContent = "Exclusive write scope locked to Claude Code";
      footer.style.color = "#9c7949";
    } else if (t < 28.0) {
      badge.textContent = "LOCKED · CLAUDE OWNS";
      badge.style.background = "#f1eadd"; badge.style.color = "#9c7949";
      $("file-card").style.borderColor = t < 23.4 ? "#b58a3c" : "#9c7949";
      l1El.className = "code-line claude-write";
      l2El.className = "code-line claude-write";
      l1Txt.textContent = "  intent = stripe.capture(cart.id)";
      l2Txt.textContent = "  return verify_capture(intent.id)";
      l3Txt.textContent = t < 23.4 ? "  # blocked: Codex write rejected" : "  # Codex scoped to test_checkout.py";
      footer.textContent = t < 23.4 ? "Conflict lock blocks overlapping write" : "Parallel progress in one repo · zero collisions";
      footer.style.color = t < 23.4 ? "#a87f32" : "#3f7062";
    } else {
      badge.textContent = "RELEASED · CLEAN ✓";
      badge.style.background = "#e4eee8"; badge.style.color = "#3f7062";
      $("file-card").style.borderColor = "#3f7062";
      l1El.className = "code-line clean-done";
      l2El.className = "code-line clean-done";
      l3El.className = "code-line clean-done";
      l1Txt.textContent = "  intent = stripe.capture(cart.id)";
      l2Txt.textContent = "  return verify_capture(intent.id)";
      l3Txt.textContent = "  # 28/28 tests green · index clean";
      footer.textContent = "Handoff complete · zero worktrees created";
      footer.style.color = "#3f7062";
    }

    // Packets & wires
    show($("wire-left"), cardIntro);
    show($("wire-right"), cardIntro);

    // Left packet (Claude -> File: stomp at 2.2-3.8, lease lock at 10.8-11.8)
    let pL = -1, colL = "#9c7949";
    if (t >= 2.2 && t <= 3.8) pL = (t - 2.2) / 1.6;
    else if (t >= 10.8 && t <= 11.8) pL = (t - 10.8) / 1.0;
    if (pL >= 0) {
      const f = smooth(pL), pt = wireL.getPointAtLength(f * lenL);
      $("pkt-left").setAttribute("cx", pt.x); $("pkt-left").setAttribute("cy", pt.y);
      $("pkt-left").setAttribute("fill", colL);
      $("wire-left-active").setAttribute("stroke", colL);
      $("wire-left-active").style.strokeDashoffset = `${lenL * (1 - f)}`;
      show($("pkt-left"), Math.sin(Math.PI * f));
      show($("wire-left-active"), Math.sin(Math.PI * f));
    } else {
      show($("pkt-left"), 0); show($("wire-left-active"), 0);
    }

    // Right packet (Codex -> File: stomp at 3.0-4.2, blocked bounce at 20.0-21.2)
    let pR = -1, colR = "#3f7062";
    if (t >= 3.0 && t <= 4.2) { pR = (t - 3.0) / 1.2; colR = "#b84a39"; }
    else if (t >= 20.0 && t <= 21.2) {
      const raw = (t - 20.0) / 1.2;
      pR = raw < 0.7 ? raw / 0.7 : 1 - (raw - 0.7) / 0.3 * 0.45;
      colR = "#b58a3c";
    }
    if (pR >= 0) {
      const f = smooth(pR), pt = wireR.getPointAtLength(f * lenR);
      $("pkt-right").setAttribute("cx", pt.x); $("pkt-right").setAttribute("cy", pt.y);
      $("pkt-right").setAttribute("fill", colR);
      $("wire-right-active").setAttribute("stroke", colR);
      $("wire-right-active").style.strokeDashoffset = `${lenR * (1 - f)}`;
      show($("pkt-right"), 0.95);
      show($("wire-right-active"), 0.85);
    } else {
      show($("pkt-right"), 0); show($("wire-right-active"), 0);
    }

    // Shield ring pulse when conflict is blocked
    const shieldOn = fade(t, 20.7, 23.2, .3);
    show($("shield-ring"), shieldOn);
    $("shield-ring").style.transformOrigin = "540px 560px";
    $("shield-ring").style.transform = `scale(${0.92 + 0.12 * progress(t, 20.7, .6)}) rotate(${t * 25}deg)`;

    // Peer handoff arc (Claude -> Codex at 30.2-35.0)
    const peerOn = fade(t, 30.0, 36.5, .35);
    show($("peer-arc"), peerOn);
    const peerFlight = (t - 30.2) / 1.1;
    if (peerFlight >= 0 && peerFlight <= 1) {
      const f = smooth(peerFlight), pt = arc.getPointAtLength(f * lenArc);
      $("pkt-peer").setAttribute("cx", pt.x); $("pkt-peer").setAttribute("cy", pt.y);
      $("pkt-peer").setAttribute("fill", "#9c7949");
      $("peer-arc-active").setAttribute("stroke", "#9c7949");
      $("peer-arc-active").style.strokeDashoffset = `${lenArc * (1 - f)}`;
      show($("pkt-peer"), Math.sin(Math.PI * f));
      show($("peer-arc-active"), Math.sin(Math.PI * f));
    } else {
      show($("pkt-peer"), 0); show($("peer-arc-active"), 0);
    }

    const pillOn = fade(t, 30.8, 36.8, .35);
    $("peer-pill").textContent = "→  checkout.py released · ready for your retry tests";
    show($("peer-pill"), pillOn);

    // Bottom banner
    let bannerText = "", bannerColor = "#3f7062";
    if (t >= 4.2 && t < 8.8) {
      bannerText = "Without scope locks: silent file overwrite + .git/index.lock collision";
      bannerColor = "#b84a39";
    } else if (t >= 11.8 && t < 18.8) {
      bannerText = "agent-coord begin-work --scope src/billing/checkout.py · lease active";
      bannerColor = "#9c7949";
    } else if (t >= 20.8 && t < 27.8) {
      bannerText = t < 23.6
        ? "Conflict detected: src/billing/checkout.py is leased by Claude Code"
        : "Codex locks tests/test_checkout.py · both agents run in one worktree";
      bannerColor = t < 23.6 ? "#a87f32" : "#3f7062";
    } else if (t >= 29.8 && t < OUTRO) {
      bannerText = "agent-coord end-work · lease released · 28 tests green · 0 git worktrees";
      bannerColor = "#3f7062";
    }
    show($("bottom-banner"), bannerText ? 1 : 0);
    $("banner-text").textContent = bannerText;
    $("banner-dot").style.background = bannerColor;

    window.__sceneState = {
      time: t,
      headline: h ? `${h.l1} ${h.l2}` : "",
      subhead: sub?.text || null,
      leaseBadge: badge.textContent,
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
      {t: 4.2, kind: "pop", pitch: 330, gain: .18},
      {t: 9.0, kind: "whoosh", length: .45},
      {t: 9.6, kind: "click"},
      {t: 11.8, kind: "pop", pitch: 783.99, gain: .18},
      {t: 19.0, kind: "whoosh", length: .45},
      {t: 19.4, kind: "click"},
      {t: 20.8, kind: "pop", pitch: 440, gain: .2},
      {t: 23.6, kind: "click"},
      {t: 25.0, kind: "pop", pitch: 880, gain: .18},
      {t: 28.0, kind: "mark", name: "break"},
      {t: 28.4, kind: "click"},
      {t: 29.8, kind: "pop", pitch: 987.77, gain: .16},
      {t: 31.0, kind: "pop", pitch: 1046.5, gain: .2},
      {t: 32.0, kind: "mark", name: "groove"},
      {t: 32.0, kind: "accent"},
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
