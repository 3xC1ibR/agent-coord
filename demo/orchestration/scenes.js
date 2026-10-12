/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * One OpenAI-powered dispatcher assembles a four-agent team across providers,
 * the agents coordinate with each other, one result is reported back, and two
 * follow-ups return to the same agents with their context intact.
 * Kimi is shown for illustration only; it is not a Ribbon Field provider. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const smooth = n => { const x = clamp(n); return x * x * (3 - 2 * x); };
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .4) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const show = (el, o) => { el.style.opacity = String(clamp(o)); el.style.visibility = o <= 0 ? "hidden" : "visible"; };
  const current = (list, t) => { let found = null; for (const item of list) if (t >= item.at) found = item; return found; };

  const ids = ["ui", "api", "tests", "release"], names = ["Checkout UI", "Payments API", "Test suite", "Release"];
  const xs = [150, 410, 670, 930], tones = ["#4f7a8a", "#9c7949", "#847099", "#3f7062"];
  const fan = ids.map((_, i) => 4.8 + .55 * i), arrival = fan.map(f => f + .95);
  const OUTRO = 44.2, DURATION = 49;

  const headings = [
    {at: 0, end: 4.4, html: "One request.<br><span>One dispatcher.</span>"},
    {at: 4.5, end: 11.1, html: "One team.<br><span>Four different models.</span>"},
    {at: 11.2, end: 25.8, html: "Working in parallel.<br><span>Talking to each other.</span>"},
    {at: 25.9, end: 29.9, html: "One coherent result.<br><span>Reported back to you.</span>"},
    {at: 30.0, end: OUTRO, html: "Follow-ups find<br><span>the agent that remembers.</span>"},
  ];
  const requests = [
    {at: 1.3, send: 3.3, end: 7.2, to: -1, text: "Ship the new checkout flow by Friday."},
    {at: 30.4, send: 32.0, end: 38.4, to: 1, text: "Add Apple Pay to checkout."},
    {at: 38.6, send: 40.2, end: 44.0, to: 3, text: "Roll the next release out gradually."},
  ];
  const captions = [
    {at: 7.5, end: 11.0, text: "One OpenAI model launches Gemini, Anthropic, Kimi, and another OpenAI model."},
    {at: 12.0, end: 16.6, text: "Two agents reach for the same file. They settle it between themselves."},
    {at: 16.9, end: 19.4, text: "Inside its own conversation, an agent can spin up short-lived subagents."},
    {at: 19.5, end: 21.7, text: "Subagents finish and vanish. App agents stay."},
    {at: 21.9, end: 25.2, text: "Release waits for everyone, then ships."},
  ];
  const report = {at: 26.0, end: 29.6};
  const statuses = [
    [{at: arrival[0], text: "Starting…"}, {at: 8.0, text: "Building the screens…"}, {at: 11.8, text: "Editing order schema…", kind: "conflict"},
     {at: 16.2, text: "Uses the API's fields ✓"}, {at: 21.2, text: "Ready ✓", kind: "done"}, {at: 36.7, text: "Adding the button…"}, {at: 37.7, text: "Button added ✓", kind: "done"}],
    [{at: arrival[1], text: "Starting…"}, {at: 8.2, text: "Writing the endpoints…"}, {at: 11.8, text: "Editing order schema…", kind: "conflict"},
     {at: 16.2, text: "Owns order schema ✓"}, {at: 16.9, text: "3 subagents at work…"}, {at: 19.3, text: "Edge cases covered ✓"}, {at: 21.6, text: "Ready ✓", kind: "done"},
     {at: 33.4, text: "Adding Apple Pay…"}, {at: 37.9, text: "Apple Pay added ✓", kind: "done"}],
    [{at: arrival[2], text: "Starting…"}, {at: 8.4, text: "Writing the tests…"}, {at: 19.6, text: "48 passing ✓", kind: "done"}, {at: 38.2, text: "52 passing ✓", kind: "done"}],
    [{at: arrival[3], text: "Starting…"}, {at: 8.6, text: "Waiting for green…", kind: "wait"}, {at: 20.2, text: "Waiting for all three…", kind: "wait"},
     {at: 22.4, text: "Deploying…", kind: "deploy"}, {at: 24.4, text: "Live ✓", kind: "done"}, {at: 41.6, text: "Staging the rollout…"}, {at: 43.2, text: "Staged rollout ready ✓", kind: "done"}],
  ];
  const assignments = [
    [{at: arrival[0], text: "Checkout screens"}, {at: 37.7, text: "+ Apple Pay button"}],
    [{at: arrival[1], text: "Payment endpoints"}, {at: 37.9, text: "+ Apple Pay"}],
    [{at: arrival[2], text: "Checkout tests"}, {at: 38.2, text: "+ Apple Pay tests"}],
    [{at: arrival[3], text: "Ship by Friday"}, {at: 43.2, text: "Gradual rollouts"}],
  ];
  const pills = [
    {at: 12.8, from: 1, to: 0, text: "I'll own the order schema."},
    {at: 14.9, from: 0, to: 1, text: "Using your field names."},
    {at: 20.0, from: 2, to: 3, text: "All green."},
    {at: 35.8, from: 1, to: 0, text: "New button, same fields."},
  ];
  const subagents = {at: 16.9, end: 19.2, agent: 1};
  const deploy = {at: 22.4, end: 24.2};
  const reportPacket = {at: 24.8, end: 25.7};
  const FLIGHT = .9, PILL_IN = .45, PILL_OUT = 1.9;

  const svg = (name, attrs) => { const e = document.createElementNS("http://www.w3.org/2000/svg", name); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; };
  const routes = xs.map((x, i) => {
    const d = `M 540 586 C 540 640, ${x} 616, ${x} 684`;
    const group = svg("g", {}), line = svg("path", {d, class: "route"}), active = svg("path", {d, class: "active-route", stroke: tones[i]});
    const dot = svg("circle", {r: 6, fill: tones[i]});
    group.append(line, active); $("links").append(group); $("packets").append(dot);
    return {group, line, active, dot, length: line.getTotalLength()};
  });
  const arcs = {};
  for (const [a, b] of [[0, 1], [2, 3]]) {
    const d = `M ${xs[a] + 72} 744 C ${xs[a] + 72} 600, ${xs[b] - 72} 600, ${xs[b] - 72} 744`;
    const group = svg("g", {}), track = svg("path", {d, class: "peer"}), active = svg("path", {d, class: "peer-active"});
    const dot = svg("circle", {r: 6});
    group.append(track, active); $("peers").append(group); $("packets").append(dot);
    arcs[`${a}-${b}`] = {group, track, active, dot, length: track.getTotalLength(), center: (xs[a] + xs[b]) / 2};
  }
  const pillEls = pills.map(p => {
    const el = document.createElement("div"); el.className = "pill"; el.textContent = p.text;
    const key = `${Math.min(p.from, p.to)}-${Math.max(p.from, p.to)}`;
    el.style.left = arcs[key].center + "px"; $("pills").append(el); return el;
  });

  function render(time) {
    const t = Math.max(0, Math.min(DURATION - .001, time));
    const outro = progress(t, OUTRO, .7);
    show($("brand"), 1 - outro); show($("story"), 1 - outro); show($("endcard"), outro);
    $("endcard").style.transform = `translateY(${(1 - outro) * 18}px)`;

    const heading = headings.find(h => t >= h.at && t < h.end);
    $("headline").innerHTML = heading?.html || "";
    show($("headline"), heading ? fade(t, heading.at, heading.end, .35) : 0);
    $("headline").style.transform = `translateY(${heading ? (1 - progress(t, heading.at, .5)) * 10 : 0}px)`;

    const request = requests.find(r => t >= r.at && t < r.end);
    $("request-text").textContent = request?.text || "";
    show($("request"), request ? fade(t, request.at, request.end, .4) : 0);
    $("request").style.transform = `translateY(${request ? (1 - progress(t, request.at, .55)) * 14 : 0}px)`;

    const caption = captions.find(c => t >= c.at && t < c.end);
    $("caption").textContent = caption?.text || "";
    show($("caption"), caption ? fade(t, caption.at, caption.end, .35) : 0);

    const reportIn = fade(t, report.at, report.end, .45);
    show($("report"), reportIn);
    $("report").style.transform = `translateY(${(1 - progress(t, report.at, .6)) * 14}px)`;

    show($("hub"), progress(t, .9, .6));
    const pulse = Math.max(...requests.map(r => fade(t, r.send - .1, r.send + .6, .22)), fade(t, reportPacket.end - .1, reportPacket.end + .6, .22));
    $("hub").querySelector(".hub-mark").style.transform = `scale(${1 + pulse * .07})`;
    show($("input-link"), request ? fade(t, request.at + .45, request.end, .4) : 0);
    const inputFlight = request ? clamp((t - request.send + .55) / .5) : 0;
    $("input-packet").setAttribute("cy", 392 + 38 * inputFlight);
    show($("input-packet"), Math.sin(inputFlight * Math.PI));

    const state = {time: t, headline: heading ? $("headline").textContent : "", request: request?.text || null, target: request && request.to >= 0 ? ids[request.to] : request ? "all" : null,
      caption: caption?.text || null, report: reportIn > .5, agents: [], status: {}, assignment: {}, arrival: {}, pills: [], subagents: false, endcard: outro >= 1};

    ids.forEach((id, i) => {
      const agent = $(id), intro = progress(t, arrival[i], .65);
      show(agent, intro);
      agent.style.transform = `translateY(${(1 - intro) * 18}px) scale(${.94 + .06 * intro})`;
      if (intro > 0) state.agents.push(id);
      const follow = requests.find(r => r.to === i), followArrive = follow ? follow.send + 1.1 : Infinity;
      const label = agent.querySelector(".arrival");
      const labelOn = t >= followArrive ? fade(t, followArrive, followArrive + 2.6, .25) : fade(t, arrival[i], arrival[i] + 2.2, .25);
      const longLabel = t >= followArrive;
      label.textContent = longLabel ? "Same agent · same context" : "New agent";
      label.className = "arrival" + (longLabel && i === 3 ? " edge-right" : longLabel && i === 0 ? " edge-left" : "");
      show(label, labelOn);
      if (labelOn > .5) state.arrival[id] = label.textContent;

      const status = current(statuses[i], t), kind = status?.kind || "work";
      const statusEl = agent.querySelector(".status");
      statusEl.textContent = status?.text || "";
      statusEl.className = "status " + kind;
      show(statusEl, status ? progress(t, status.at, .3) : 0);
      if (status) state.status[id] = status.text;
      const ring = agent.querySelector(".orbit-progress");
      const ringStyles = {
        done: {dash: "415 0", opacity: .45, rotate: 0, stroke: ""},
        conflict: {dash: "6 12", opacity: .9, rotate: t * 30, stroke: "#b58a3c"},
        wait: {dash: "52 363", opacity: .4, rotate: t * 22 + i * 110, stroke: ""},
        deploy: {dash: "100 315", opacity: .85, rotate: t * 160, stroke: ""},
        work: {dash: "52 363", opacity: .7, rotate: t * 64 + i * 110, stroke: ""},
      }[kind];
      ring.style.strokeDasharray = ringStyles.dash; ring.style.opacity = String(ringStyles.opacity);
      ring.style.transform = `rotate(${ringStyles.rotate}deg)`; ring.style.stroke = ringStyles.stroke;

      const [initial, revision] = assignments[i];
      agent.querySelector(".initial").textContent = initial.text;
      agent.querySelector(".revision").textContent = revision.text;
      const changed = progress(t, revision.at, .6);
      show(agent.querySelector(".initial"), 1 - changed);
      show(agent.querySelector(".revision"), changed);
      agent.querySelector(".revision").style.transform = `translateY(${(1 - changed) * 10}px)`;
      state.assignment[id] = changed >= .5 ? revision.text : initial.text;

      const bar = agent.querySelector(".bar");
      if (bar) { show(bar, fade(t, deploy.at, deploy.end + .9, .25)); bar.firstElementChild.style.width = `${clamp((t - deploy.at) / (deploy.end - deploy.at)) * 100}%`; }
      agent.querySelectorAll(".sub").forEach((dotEl, k) => {
        const on = subagents.agent === i ? fade(t, subagents.at, subagents.end, .4) : 0;
        show(dotEl, on * .9);
        dotEl.style.transform = `rotate(${t * 95 + k * 120}deg) translate(72px) scale(${.7 + .3 * on})`;
        if (on > .5) state.subagents = true;
      });

      const {group, active, dot, line, length} = routes[i];
      show(group, progress(t, fan[i], .3));
      let flight = -1, inFlight = false, arriveAt = 0, reverse = false;
      const fanP = (t - fan[i]) / .95;
      if (fanP >= 0 && fanP <= 1.5) { flight = clamp(fanP); arriveAt = arrival[i]; }
      if (follow && t >= follow.send && t <= follow.send + 1.6) { flight = clamp((t - follow.send) / 1.1); arriveAt = follow.send + 1.1; }
      if (i === 3 && t >= reportPacket.at && t <= reportPacket.end + .5) { flight = clamp((t - reportPacket.at) / (reportPacket.end - reportPacket.at)); arriveAt = reportPacket.end; reverse = true; }
      inFlight = flight >= 0 && t < arriveAt + .4;
      active.style.strokeDasharray = `${length}`;
      active.style.strokeDashoffset = `${(reverse ? -1 : 1) * length * (1 - Math.max(0, flight))}`;
      show(active, inFlight ? 1 - progress(t, arriveAt, .4) : 0);
      const position = line.getPointAtLength((reverse ? 1 - Math.max(0, flight) : Math.max(0, flight)) * length);
      dot.setAttribute("cx", position.x); dot.setAttribute("cy", position.y);
      show(dot, inFlight ? Math.sin(Math.PI * clamp(flight)) : 0);
    });

    Object.values(arcs).forEach(arc => { show(arc.group, 0); show(arc.dot, 0); });
    pills.forEach((pill, k) => {
      const key = `${Math.min(pill.from, pill.to)}-${Math.max(pill.from, pill.to)}`, arc = arcs[key];
      const forward = pill.from < pill.to, p = (t - pill.at) / FLIGHT, tone = tones[pill.from];
      const on = fade(t, pill.at + PILL_IN, pill.at + PILL_OUT + .4, .3);
      show(pillEls[k], on);
      pillEls[k].style.transform = `translateX(-50%) translateY(${(1 - progress(t, pill.at + PILL_IN, .4)) * 8}px)`;
      if (on > .5) state.pills.push(pill.text);
      if (p < 0 || t > pill.at + PILL_OUT + .7) return;
      const flight = smooth(p);
      show(arc.group, fade(t, pill.at - .2, pill.at + PILL_OUT + .7, .3));
      arc.active.setAttribute("stroke", tone); arc.dot.setAttribute("fill", tone);
      arc.active.style.strokeDasharray = `${arc.length}`;
      arc.active.style.strokeDashoffset = `${(forward ? 1 : -1) * arc.length * (1 - flight)}`;
      show(arc.active, p <= 1.4 ? 1 - progress(p, 1, .4) : 0);
      const pos = arc.track.getPointAtLength((forward ? flight : 1 - flight) * arc.length);
      arc.dot.setAttribute("cx", pos.x); arc.dot.setAttribute("cy", pos.y);
      show(arc.dot, p <= 1 ? Math.sin(Math.PI * flight) : 0);
    });
    window.__sceneState = state;
  }
  window.__render = t => { render(t); return true; };
  window.__duration = () => DURATION;
  window.__cues = () => [];
  window.__ready = Promise.all([document.fonts.ready, ...Array.from(document.images, im => im.decode())]).then(() => { render(0); return true; });
})();
