/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * Hook B (?hook=amnesia): 9 terminal tabs. 4 Claude sessions. Zero idea which agent is waiting on you.
 * A native macOS mission control that tracks live agent state, structured checkpoints, and exact next actions across every repo. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const back = n => { const x = clamp(n), c = 1.4; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .4) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const mix = (a, b, p) => a + (b - a) * p;
  const show = (el, o) => { el.style.opacity = String(clamp(o)); el.style.visibility = o <= 0 ? "hidden" : "visible"; };

  const OUTRO = 39.8, DURATION = 45;

  const headings = [
    {at: 0, end: 8.8, l1: "9 terminal tabs. 4 Claude sessions.", l2: "Zero idea which agent is waiting on you."},
    {at: 9.0, end: 18.8, l1: "Stop running git status in every pane.", l2: "See live checkpoints across every repo."},
    {at: 19.0, end: 28.8, l1: "Two agents need you right now.", l2: "Exact next actions, front and center."},
    {at: 29.0, end: OUTRO, l1: "Approve in seconds. Archive the done.", l2: "Your morning triage, solved."},
  ];

  const subheads = [
    {at: 0.4, end: 8.6, text: "9 iTerm & tmux panes across 3 repos means 20 minutes every morning scrolling up to remember who is blocked."},
    {at: 9.2, end: 18.6, text: "A native macOS mission control that tracks live agent state, structured checkpoints, and exact next actions."},
    {at: 19.2, end: 28.6, text: "Attention queue surfaces next_step.actor = user immediately—distinguishing blocked approvals from running work."},
    {at: 29.2, end: 39.4, text: "Unblock the two waiting sessions, roll up finished threads, and keep autonomous agents moving."},
  ];

  // 9 panes: 4 Claude sessions, 3 Codex sessions, 2 Gemini sessions across acme-api, acme-web, acme-mobile
  const PANES = [
    {
      id: "tmux · pane 1", repo: "acme-api", provider: "Claude", tone: "#9c7949",
      rawTitle: "zsh · claude (scrollback +412 lines)", rawLine: "? Allow bash: alembic upgrade [y/n]",
      title: "Billing schema migration", phase: "validation",
      summary: "Migration SQL verified on local Postgres.",
      nextUser: true, nextText: "user · Approve production migration run",
      badgeNow: "NEEDS YOU · APPROVAL", badgeResolved: "APPROVED · DEPLOYING ✓"
    },
    {
      id: "tmux · pane 2", repo: "acme-web", provider: "Codex", tone: "#3f7062",
      rawTitle: "zsh · codex · git status", rawLine: "M src/checkout.js  M tests/checkout.js",
      title: "Checkout session flow", phase: "implementation",
      summary: "Running 42 integration tests for checkout.",
      nextUser: false, nextText: "agent · Finish test suite",
      badgeNow: "WORKING · IMPLEMENTATION", badgeResolved: "WORKING · 42/42 PASSING"
    },
    {
      id: "tmux · pane 3", repo: "acme-mobile", provider: "Claude", tone: "#9c7949",
      rawTitle: "zsh · claude (idle 6h)", rawLine: "Waiting on APNs token TTL choice...",
      title: "iOS push token rotation", phase: "planning",
      summary: "Compared 7-day vs 30-day APNs token TTLs.",
      nextUser: true, nextText: "user · Choose 7d or 30d APNs TTL",
      badgeNow: "NEEDS YOU · DECISION", badgeResolved: "30D CHOSEN · IMPLEMENTING ✓"
    },
    {
      id: "iTerm · tab 4", repo: "acme-api", provider: "Codex", tone: "#3f7062",
      rawTitle: "zsh · codex (scrollback +890 lines)", rawLine: "$ pytest test_webhooks.py · 18 passed",
      title: "Stripe webhook idempotency", phase: "finished",
      summary: "Delivered duplicate-event lock & 18 tests.",
      nextUser: false, nextText: "null · Completed",
      done: true, badgeNow: "FINISHED · READY TO ARCHIVE", badgeResolved: "ARCHIVED · ROLLED UP ✓"
    },
    {
      id: "iTerm · tab 5", repo: "acme-web", provider: "Claude", tone: "#9c7949",
      rawTitle: "zsh · claude · git diff", rawLine: "+ const copy = hooks[slug] || fallback;",
      title: "Landing page hero copy", phase: "implementation",
      summary: "Updating dynamic hero copy for 3 hooks.",
      nextUser: false, nextText: "agent · Verify viewport bounds",
      badgeNow: "WORKING · IMPLEMENTATION", badgeResolved: "WORKING · IMPLEMENTATION"
    },
    {
      id: "iTerm · tab 6", repo: "acme-mobile", provider: "Gemini", tone: "#4f7a8a",
      rawTitle: "zsh · antigravity · tailscale", rawLine: "Paired with iPhone 17 over Tailscale",
      title: "Tailscale remote pairing", phase: "finished",
      summary: "Verified QR pairing and byte-range video.",
      nextUser: false, nextText: "null · Completed",
      done: true, badgeNow: "FINISHED · READY TO ARCHIVE", badgeResolved: "ARCHIVED · ROLLED UP ✓"
    },
    {
      id: "tmux · pane 7", repo: "acme-api", provider: "Claude", tone: "#9c7949",
      rawTitle: "zsh · claude · git status", rawLine: "nothing to commit, working tree clean",
      title: "Rate limiter benchmark", phase: "investigation",
      summary: "Measured p99 token bucket at 0.14ms.",
      nextUser: false, nextText: "null · Findings recorded",
      done: true, badgeNow: "LATER · INVESTIGATION", badgeResolved: "LATER · SAVED"
    },
    {
      id: "tmux · pane 8", repo: "acme-web", provider: "Codex", tone: "#3f7062",
      rawTitle: "zsh · codex · npm run check", rawLine: "Checking S3 + CloudFront invalidation...",
      title: "CDN cache headers", phase: "deployment",
      summary: "Validating immutable headers on staging.",
      nextUser: false, nextText: "agent · Run smoke check",
      badgeNow: "WORKING · DEPLOYMENT", badgeResolved: "WORKING · DEPLOYMENT"
    },
    {
      id: "iTerm · tab 9", repo: "acme-mobile", provider: "Gemini", tone: "#4f7a8a",
      rawTitle: "zsh · antigravity (scrollback)", rawLine: "Rendering 390px mobile preview...",
      title: "Voice composer waveform", phase: "finished",
      summary: "Delivered 60fps canvas bar visualizer.",
      nextUser: false, nextText: "null · Completed",
      done: true, badgeNow: "FINISHED · READY TO ARCHIVE", badgeResolved: "ARCHIVED · ROLLED UP ✓"
    }
  ];

  // Grid positions:
  // Chaotic 3x3 grid (t < 9.5)
  const gridPos = i => ({
    x: 60 + (i % 3) * 328,
    y: 382 + Math.floor(i / 3) * 162
  });

  // Organized lanes (t >= 9.5):
  // Top row (NOW · Needs You): panes 0 and 2 centered/highlighted at y = 424
  // Bottom rows (WORKING & LATER): remaining 7 panes in 2 rows (4 on row 1, 3 on row 2) or 3x2 compact
  const sortedTargets = [
    {x: 60, y: 424},    // pane 0 (Needs You #1)
    {x: 60, y: 658},    // pane 1 (Working)
    {x: 388, y: 424},   // pane 2 (Needs You #2)
    {x: 716, y: 658},   // pane 3 (Finished)
    {x: 388, y: 658},   // pane 4 (Working)
    {x: 716, y: 740},   // pane 5 (Finished - stacked/rolled)
    {x: 60, y: 740},    // pane 6 (Later)
    {x: 388, y: 740},   // pane 7 (Working)
    {x: 716, y: 424},   // pane 8 -> summary / rolled up card in top-right or bottom-right
  ];
  // Let's make all 9 cards cleanly non-overlapping in the sorted view OR clearly placed:
  // Wait: if pane-card height is 142px (or 102px when compact in sorted mode), let's compute exact non-overlapping coordinates!
  // Let's check:
  // Row 0 (NOW lane, y = 418, height = 148): Pane 0 at x=60, Pane 2 at x=388.
  // Row 1 (WORKING lane, y = 652, height = 104): Pane 1 at x=60, Pane 4 at x=388, Pane 7 at x=716.
  // Row 2 (LATER & FINISHED lane, y = 766, height = 104): Pane 6 at x=60, Pane 3 at x=388, Pane 5 at x=716 (and Pane 8 rolls into Finished).

  const gridEl = $("grid");
  const cards = PANES.map((p, i) => {
    const el = document.createElement("div");
    el.className = "pane-card";
    el.innerHTML = `
      <div class="pane-top">
        <span class="pane-id"></span>
        <span class="chip" style="--tone:${p.tone}">${p.provider}</span>
      </div>
      <div class="pane-title"></div>
      <div class="pane-line"></div>
      <span class="pane-badge"></span>
    `;
    gridEl.append(el);
    return {
      el,
      idEl: el.querySelector(".pane-id"),
      titleEl: el.querySelector(".pane-title"),
      lineEl: el.querySelector(".pane-line"),
      badgeEl: el.querySelector(".pane-badge"),
      data: p,
      i
    };
  });

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

    const intro = progress(t, 0.5, .6);
    show($("top-bar"), intro);

    const sortP = progress(t, 9.6, 1.1);
    const resolved1 = t >= 23.6;
    const resolved2 = t >= 28.0;
    const archived = t >= 31.0;

    let counterTxt = "9 ungrouped terminal panes · 4 Claude · 3 Codex · 2 Gemini";
    if (t >= 9.6 && t < 23.6) counterTxt = "2 Need You Now · 3 Working · 4 Finished / Later";
    else if (t >= 23.6 && t < 28.0) counterTxt = "1 Needs You Now · 4 Working · 4 Finished / Later";
    else if (t >= 28.0) counterTxt = "0 Waiting on You · 5 Working · 4 Rolled Up ✓";
    $("triage-counter").textContent = counterTxt;

    show($("lane-now"), sortP);
    show($("lane-run"), sortP);

    // Lens inspection (19.2 - 28.4)
    const lens1 = t >= 19.2 && t < 23.8;
    const lens2 = t >= 24.0 && t < 28.6;
    const lensActive = lens1 || lens2;
    const lensOpen = lens1 ? fade(t, 19.2, 23.8, .35) : lens2 ? fade(t, 24.0, 28.6, .35) : 0;

    cards.forEach((c, i) => {
      const p = c.data;
      const g0 = gridPos(i);
      let gx = g0.x, gy = g0.y, gh = 142, opacity = intro;

      if (i === 0) { gx = mix(g0.x, 60, sortP); gy = mix(g0.y, 418, sortP); gh = mix(142, 160, sortP); }
      else if (i === 2) { gx = mix(g0.x, 388, sortP); gy = mix(g0.y, 418, sortP); gh = mix(142, 160, sortP); }
      else if (i === 8) {
        // 9th pane sits next to NOW or rolls into finished stack at x=716, y=418
        gx = mix(g0.x, 716, sortP); gy = mix(g0.y, 418, sortP); gh = mix(142, 160, sortP);
      } else if (i === 1) { gx = mix(g0.x, 60, sortP); gy = mix(g0.y, 650, sortP); gh = mix(142, 104, sortP); }
      else if (i === 4) { gx = mix(g0.x, 388, sortP); gy = mix(g0.y, 650, sortP); gh = mix(142, 104, sortP); }
      else if (i === 7) { gx = mix(g0.x, 716, sortP); gy = mix(g0.y, 650, sortP); gh = mix(142, 104, sortP); }
      else if (i === 6) { gx = mix(g0.x, 60, sortP); gy = mix(g0.y, 764, sortP); gh = mix(142, 104, sortP); }
      else if (i === 3) { gx = mix(g0.x, 388, sortP); gy = mix(g0.y, 764, sortP); gh = mix(142, 104, sortP); }
      else if (i === 5) { gx = mix(g0.x, 716, sortP); gy = mix(g0.y, 764, sortP); gh = mix(142, 104, sortP); }

      c.el.style.left = `${Math.round(gx)}px`;
      c.el.style.top = `${Math.round(gy)}px`;
      c.el.style.height = `${Math.round(gh)}px`;

      if (t < 9.4) {
        c.el.className = "pane-card";
        c.idEl.textContent = `${p.id} · ${p.repo}`;
        c.titleEl.textContent = p.rawTitle;
        c.lineEl.textContent = p.rawLine;
        const qPulse = 0.55 + 0.45 * Math.sin(t * 5 + i);
        c.badgeEl.textContent = "STATUS UNKNOWN · ? ";
        c.badgeEl.style.background = `rgba(235, 232, 220, ${qPulse})`;
        c.badgeEl.style.color = "#7a6843";
      } else {
        c.idEl.textContent = `${p.repo} · ${p.phase}`;
        c.titleEl.textContent = p.title;
        if (i === 8) {
          c.lineEl.textContent = archived ? "3 finished threads rolled up cleanly" : "Completed · no user action needed";
          c.badgeEl.textContent = archived ? "ROLLED UP · 3 DONE ✓" : "FINISHED · NO ACTION NEEDED";
          c.badgeEl.style.background = "#e4eee8"; c.badgeEl.style.color = "#3f7062";
          c.el.className = "pane-card done-card";
        } else if (p.nextUser) {
          const isRes = (i === 0 && resolved1) || (i === 2 && resolved2);
          c.el.className = isRes ? "pane-card" : "pane-card needs-user";
          c.lineEl.textContent = isRes ? "Unblocked · agent continuing work" : p.nextText;
          c.badgeEl.textContent = isRes ? p.badgeResolved : p.badgeNow;
          c.badgeEl.style.background = isRes ? "#e4eee8" : "#faecd2";
          c.badgeEl.style.color = isRes ? "#3f7062" : "#9c6d1e";
        } else {
          c.el.className = p.done ? "pane-card done-card" : "pane-card";
          c.lineEl.textContent = p.summary;
          c.badgeEl.textContent = (p.done && archived) ? p.badgeResolved : p.badgeNow;
          c.badgeEl.style.background = p.done ? "#ecefe6" : "#e4eee8";
          c.badgeEl.style.color = p.done ? "#65755e" : "#3f7062";
        }
      }

      const dim = lensOpen * ((lens1 && i === 0) || (lens2 && i === 2) ? 0 : 0.65);
      show(c.el, opacity * (1 - dim));
    });

    // Render Lens
    const lensEl = $("lens");
    if (lensActive) {
      const targetPane = lens1 ? PANES[0] : PANES[2];
      const approved = lens1 ? t >= 22.0 : t >= 26.8;
      $("lens-repo").textContent = `${targetPane.repo} · structured checkpoint`;
      $("lens-title").textContent = targetPane.title;
      $("lens-chip").textContent = `${targetPane.provider} Code`;
      $("lens-chip").style.setProperty("--tone", targetPane.tone);
      $("ckpt-phase").textContent = approved ? "implementation (resumed)" : targetPane.phase;
      $("ckpt-summary").textContent = targetPane.summary;
      $("ckpt-next").textContent = approved ? "agent · Executing approved step" : targetPane.nextText;
      $("lens-action").textContent = approved
        ? (lens1 ? "✓ Approved migration · agent resumed immediately" : "✓ Selected 30-day APNs TTL · agent resumed")
        : (lens1 ? "Waiting on you: Approve alembic upgrade head" : "Waiting on you: Choose 7d or 30d APNs TTL");
      $("lens-action").style.background = approved ? "#e4eee8" : "#faecd2";
      $("lens-action").style.color = approved ? "#2f5e4e" : "#94661a";
      lensEl.style.borderColor = approved ? "#3f7062" : "#c89e4c";
      const sc = mix(0.88, 1, back(lensOpen));
      lensEl.style.transform = `scale(${sc})`;
      show(lensEl, lensOpen);
    } else {
      show(lensEl, 0);
    }

    // Bottom banner
    let bannerText = "", bannerColor = "#3f7062";
    if (t >= 1.2 && t < 8.8) {
      bannerText = "Without checkpoints: 9 raw terminals · 20 minutes of git status & scrollback";
      bannerColor = "#b84a39";
    } else if (t >= 10.2 && t < 18.8) {
      bannerText = "Structured checkpoints group every session by Attention: Now · Working · Later";
      bannerColor = "#3f7062";
    } else if (t >= 19.2 && t < 28.8) {
      bannerText = "next_step.actor = user pinpoints the exact 2 sessions blocked on your decision";
      bannerColor = "#a87f32";
    } else if (t >= 29.0 && t < OUTRO) {
      bannerText = "Both blockers cleared · finished threads rolled up · 0 wasted scrollback";
      bannerColor = "#3f7062";
    }
    show($("bottom-banner"), bannerText ? 1 : 0);
    $("banner-text").textContent = bannerText;
    $("banner-dot").style.background = bannerColor;

    window.__sceneState = {
      time: t,
      headline: h ? `${h.l1} ${h.l2}` : "",
      subhead: sub?.text || null,
      counter: counterTxt,
      lensActive,
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
      {t: 9.0, kind: "whoosh", length: .5},
      {t: 9.8, kind: "pop", pitch: 659.25, gain: .15},
      {t: 10.1, kind: "pop", pitch: 783.99, gain: .15},
      {t: 10.4, kind: "pop", pitch: 987.77, gain: .16},
      {t: 19.2, kind: "whoosh", length: .45},
      {t: 22.0, kind: "click"},
      {t: 22.1, kind: "pop", pitch: 880, gain: .18},
      {t: 24.0, kind: "whoosh", length: .45},
      {t: 26.8, kind: "click"},
      {t: 26.9, kind: "pop", pitch: 1046.5, gain: .2},
      {t: 29.0, kind: "mark", name: "break"},
      {t: 31.0, kind: "accent"},
      {t: 32.0, kind: "mark", name: "groove"},
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
