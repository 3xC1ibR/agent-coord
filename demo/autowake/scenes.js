/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * Tiny Auto-Wake Demo (30s): Agents don't have to sleep-poll every time—auto wake with `agent-coord job start --wake-self`. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const smooth = n => { const x = clamp(n); return x * x * (3 - 2 * x); };
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .35) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const show = (el, o) => { el.style.opacity = String(clamp(o)); el.style.visibility = o <= 0 ? "hidden" : "visible"; };

  const OUTRO = 25.4, DURATION = 30;

  const headings = [
    {at: 0, end: 6.8, l1: "Stop burning turns on sleep 30.", l2: "Let background jobs wake your agent."},
    {at: 7.0, end: 15.8, l1: "Start a watcher with --wake-self.", l2: "Your agent ends its turn & rests idle."},
    {at: 16.0, end: OUTRO, l1: "Build finishes. Agent auto-wakes.", l2: "Zero polling loops. Instant continuation."},
  ];

  const subheads = [
    {at: 0.3, end: 6.6, text: "Waiting on CI or macOS notarization by running sleep 30 && gh run view wastes context and model turns."},
    {at: 7.2, end: 15.6, text: "agent-coord job start --wake-self runs the watcher in a detached supervisor while the agent sleeps at 0 token cost."},
    {at: 16.2, end: 25.0, text: "When the job completes, subscription_matched fires and automatically wakes the conversation to continue."},
  ];

  const arcTop = $("arc-top"), arcBot = $("arc-bot");
  const lenTop = arcTop.getTotalLength(), lenBot = arcBot.getTotalLength();
  $("arc-top-active").style.strokeDasharray = `${lenTop}`;
  $("arc-bot-active").style.strokeDasharray = `${lenBot}`;

  function typeCmd(text, t, start, dur = 1.2) {
    if (t < start) return "";
    const p = clamp((t - start) / dur);
    const s = text.slice(0, Math.floor(p * text.length));
    return p < 1 && Math.floor(t * 4) % 2 === 0 ? s + "▋" : s;
  }

  function render(time) {
    const t = Math.max(0, Math.min(DURATION - .001, time));
    const outro = progress(t, OUTRO, .65);
    show($("brand"), 1 - outro);
    show($("story"), 1 - outro);
    show($("endcard"), outro);
    $("endcard").style.transform = `translateY(${(1 - outro) * 18}px)`;

    const h = headings.find(x => t >= x.at && t < x.end);
    const l1 = $("headline").querySelector(".l1"), l2 = $("headline").querySelector(".l2");
    l1.textContent = h?.l1 || ""; l2.textContent = h?.l2 || "";
    show(l1, h ? fade(t, h.at, h.end, .3) : 0);
    show(l2, h ? fade(t, h.at + .1, h.end, .3) : 0);
    l1.style.transform = `translateY(${h ? (1 - progress(t, h.at, .4)) * 10 : 0}px)`;
    l2.style.transform = `translateY(${h ? (1 - progress(t, h.at + .1, .4)) * 10 : 0}px)`;

    const sub = subheads.find(x => t >= x.at && t < x.end);
    $("subhead").textContent = sub?.text || "";
    show($("subhead"), sub ? fade(t, sub.at, sub.end, .3) : 0);

    const intro = progress(t, 0.4, .5);
    show($("top-pill"), intro);
    show($("card-agent"), intro);
    show($("card-job"), intro);

    // Orbit rings: agent stops spinning when idle (10.5 - 17.6)!
    const agentSpin = t < 10.5 ? t * 65 : t < 17.6 ? 10.5 * 65 : (10.5 * 65) + (t - 17.6) * 65;
    $("ring-agent").style.transform = `rotate(${agentSpin}deg)`;
    $("ring-agent").style.opacity = (t >= 10.5 && t < 17.6) ? "0.2" : "0.85";
    $("ring-job").style.transform = `rotate(${t * 85}deg)`;
    $("ring-job").style.opacity = t >= 7.6 ? "0.9" : "0.2";

    let aTag = "", jTag = "";
    let cliAgent = "", cliJob = "";
    let logATitle = "", logABody = "", logJTitle = "", logJBody = "";
    let stAgent = {text: "", cls: ""}, stJob = {text: "", cls: ""};

    if (t < 7.0) {
      aTag = "POLLING LOOP · WASTING TURNS";
      cliAgent = "$ sleep 30 && gh run view 89412";
      logATitle = "Repeated sleep-polling every 30s";
      logABody = t < 3.6
        ? "> sleep 30 … still in_progress\n> sleep 30 … still in_progress\n! Burning model turns just to wait"
        : "> sleep 30 … Apple notarization retry\n> sleep 30 … still running\n! Context cluttered with poll output";
      stAgent = {text: "Stuck sleeping in a loop…", cls: "bad"};

      jTag = "NOT CONFIGURED";
      cliJob = "# No detached watcher active";
      logJTitle = "External CI · macOS DMG build";
      logJBody = "• Intel DMG: notarized ✓\n• Apple Silicon DMG: retrying network…\n• Duration: 14m 20s";
      stJob = {text: "No wake subscription attached", cls: ""};
    } else if (t < 16.0) {
      aTag = t >= 10.5 ? "IDLE · TURN ENDED · 0 TOKENS" : "REGISTERING --WAKE-SELF";
      cliAgent = typeCmd("agent-coord job start --wake-self -- gh run watch", t, 7.2, 1.4);
      logATitle = t >= 10.5 ? "Turn ended · waiting for wake event" : "Subscription fa3deffa saved";
      logABody = t >= 10.5
        ? "✓ Watcher detached to background\n✓ Agent turn ended cleanly\n✓ 0 sleep calls · 0 tokens burned"
        : "target: job 5a4460cf\nmode: all · wake_self: true\ncontinue: 'Verify DMG & report'";
      stAgent = {text: t >= 10.5 ? "Sleeping peacefully · no polling ✓" : "Detaching job watcher…", cls: "done"};

      jTag = "SUPERVISOR WATCHING · 0 TOKENS";
      cliJob = "gh run watch 89412 --exit-status";
      logJTitle = "Job #5a4460cf · Detached PTY";
      logJBody = t < 13.0
        ? "• Intel DMG: passed ✓\n• Apple Silicon: retrying notarization…\n• Watching exit status outside LLM…"
        : "• Intel DMG: passed ✓\n• Apple Silicon: notarized & stapled ✓\n• Exit code 0 · firing wake event!";
      stJob = {text: t < 13.0 ? "Polling externally (0 model turns)" : "Build finished · waking agent!", cls: t < 13.0 ? "done" : "warn"};
    } else {
      aTag = "AUTO-WOKEN · TURN RESUMED ⚡";
      cliAgent = t >= 18.2 ? "$ agent-coord ack --message-id 904" : "# Waking from subscription_matched…";
      logATitle = "Wake Event · macOS DMG release";
      logABody = `{"event": "subscription_matched",\n "subscription_id": "fa3deffa...",\n "outcomes": [{"kind": "job", "exit": 0}]}`;
      stAgent = {text: t >= 18.2 ? "Resumed instantly · DMG verified ✓" : "Auto-woken on completion ⚡", cls: "done"};

      jTag = "COMPLETED · EXIT 0 ✓";
      cliJob = "gh run watch 89412 --exit-status";
      logJTitle = "Job #5a4460cf · Finished";
      logJBody = "✓ Intel & Apple Silicon DMGs signed\n✓ Wake delivered to Release Agent\n✓ Detached watcher exited cleanly";
      stJob = {text: "Triggered auto-wake ✓", cls: "done"};
    }

    $("agent-state-tag").textContent = aTag;
    $("cli-agent").textContent = cliAgent;
    $("log-agent-title").textContent = logATitle;
    $("log-agent-body").textContent = logABody;
    $("status-agent").textContent = stAgent.text;
    $("status-agent").className = "col-status " + stAgent.cls;

    $("job-state-tag").textContent = jTag;
    $("cli-job").textContent = cliJob;
    $("log-job-title").textContent = logJTitle;
    $("log-job-body").textContent = logJBody;
    $("status-job").textContent = stJob.text;
    $("status-job").className = "col-status " + stJob.cls;

    // Network arcs
    show($("arc-top"), fade(t, 8.2, 15.6, .35));
    show($("arc-bot"), fade(t, 16.0, 24.8, .35));

    // Top packet: Agent -> Watcher at 8.6 - 9.8
    const fTop = (t - 8.6) / 1.2;
    if (fTop >= 0 && fTop <= 1) {
      const s = smooth(fTop), pt = arcTop.getPointAtLength(s * lenTop);
      $("pkt-top").setAttribute("cx", pt.x); $("pkt-top").setAttribute("cy", pt.y);
      $("pkt-top").setAttribute("fill", "#3f7062");
      $("arc-top-active").setAttribute("stroke", "#3f7062");
      $("arc-top-active").style.strokeDashoffset = `${lenTop * (1 - s)}`;
      show($("pkt-top"), Math.sin(Math.PI * s));
      show($("arc-top-active"), Math.sin(Math.PI * s));
    } else {
      show($("pkt-top"), 0); show($("arc-top-active"), 0);
    }

    // Bottom packet: Watcher -> Agent auto-wake at 16.2 - 17.4
    const fBot = (t - 16.2) / 1.2;
    if (fBot >= 0 && fBot <= 1) {
      const s = smooth(fBot), pt = arcBot.getPointAtLength(s * lenBot);
      $("pkt-bot").setAttribute("cx", pt.x); $("pkt-bot").setAttribute("cy", pt.y);
      $("pkt-bot").setAttribute("fill", "#4f7a8a");
      $("arc-bot-active").setAttribute("stroke", "#4f7a8a");
      $("arc-bot-active").style.strokeDashoffset = `${lenBot * (1 - s)}`;
      show($("pkt-bot"), Math.sin(Math.PI * s));
      show($("arc-bot-active"), Math.sin(Math.PI * s));
    } else {
      show($("pkt-bot"), 0); show($("arc-bot-active"), 0);
    }

    // Pills
    const pTopOn = fade(t, 9.0, 15.4, .3);
    $("pill-top").textContent = "job start --wake-self · subscription saved before launch";
    show($("pill-top"), pTopOn);

    const pBotOn = fade(t, 16.6, 24.8, .3);
    $("pill-bot").textContent = "⚡ subscription_matched · wakes agent with job outcome";
    show($("pill-bot"), pBotOn);

    // Bottom banner
    let bannerText = "", bannerColor = "#3f7062";
    if (t >= 0.8 && t < 6.8) {
      bannerText = "Without auto-wake: agent loops on sleep 30 && gh run view, wasting turns";
      bannerColor = "#b84a39";
    } else if (t >= 7.4 && t < 15.8) {
      bannerText = "The agent-coord job start --wake-self watcher runs while the thread rests idle";
      bannerColor = "#4f7a8a";
    } else if (t >= 16.2 && t < OUTRO) {
      bannerText = "subscription_matched wakes the thread automatically the moment CI finishes ✓";
      bannerColor = "#3f7062";
    }
    show($("bottom-banner"), bannerText ? 1 : 0);
    $("banner-text").textContent = bannerText;
    $("banner-dot").style.background = bannerColor;

    window.__sceneState = {
      time: t,
      headline: h ? `${h.l1} ${h.l2}` : "",
      subhead: sub?.text || null,
      agentState: aTag,
      jobState: jTag,
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
      {t: 7.0, kind: "whoosh", length: .45},
      {t: 7.2, kind: "click"},
      {t: 9.8, kind: "pop", pitch: 783.99, gain: .18},
      {t: 10.5, kind: "mark", name: "break"},
      {t: 14.0, kind: "riser", length: 2.0},
      {t: 16.0, kind: "impact", gain: .9},
      {t: 16.0, kind: "mark", name: "groove"},
      {t: 17.4, kind: "pop", pitch: 1046.5, gain: .22},
      {t: 18.2, kind: "accent"},
      {t: 23.4, kind: "riser", length: 2.0},
      {t: OUTRO, kind: "impact", gain: 1.0},
      {t: OUTRO, kind: "mark", name: "outro"}
    ];
  }

  window.__render = t => { render(t); return true; };
  window.__duration = () => DURATION;
  window.__cues = cues;
  window.__ready = Promise.all([document.fonts.ready, ...Array.from(document.images, im => im.decode())]).then(() => { render(0); return true; });
})();
