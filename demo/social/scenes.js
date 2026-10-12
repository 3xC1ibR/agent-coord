/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * The six illustrated requests use three persistent agent identities. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .4) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const show = (el, opacity) => { el.style.opacity = String(opacity); el.style.visibility = opacity <= 0 ? "hidden" : "visible"; };
  const names = ["Chat", "Snake", "Pong"], ids = names.map(n => n.toLowerCase());
  const colors = ["#3f7062", "#847099", "#9c7949"], xs = [210, 540, 870];
  const requests = [
    {at: 1.4, send: 3.2, end: 5.9, to: 0, text: "Build a chat app.", create: true},
    {at: 6.0, send: 7.8, end: 10.1, to: 1, text: "Build a Snake game.", create: true},
    {at: 10.2, send: 12.0, end: 14.6, to: 2, text: "Build a Pong game.", create: true},
    {at: 18.0, send: 20.0, end: 23.6, to: 1, text: "Make Snake work on my phone.", create: false},
    {at: 23.7, send: 25.7, end: 29.0, to: 0, text: "Give the chat app a dark theme.", create: false},
    {at: 29.1, send: 31.1, end: 34.5, to: 2, text: "Add a rematch button to Pong.", create: false},
  ];
  const arrival = r => r.send + 1.1;
  const headings = [
    {at: 0, end: 14.6, html: "One conversation.<br><span>Room for every idea.</span>"},
    {at: 14.7, end: 17.7, html: "Three projects.<br><span>Moving in parallel.</span>"},
    {at: 17.8, end: 34.5, html: "New request.<br><span>The same agent.</span>"},
  ];
  const svg = (name, attrs) => { const e = document.createElementNS("http://www.w3.org/2000/svg", name); Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); return e; };
  const routes = xs.map((x, i) => {
    const d = `M 540 596 C 540 636, ${x} 610, ${x} 692`;
    const group = svg("g", {}), line = svg("path", {d, class: "route"}), active = svg("path", {d, class: "active-route", stroke: colors[i]});
    const dot = svg("circle", {r: 6, fill: colors[i]});
    group.append(line, active); $("links").append(group); $("packets").append(dot);
    return {group, line, active, dot, length: line.getTotalLength()};
  });
  function render(time) {
    const t = Math.max(0, Math.min(38.999, time));
    const outro = progress(t, 34.5, .7);
    show($("brand"), (1 - outro));
    show($("story"), 1 - outro);
    show($("endcard"), outro);
    $("endcard").style.transform = `translateY(${(1-outro)*18}px)`;
    const heading = headings.find(h => t >= h.at && t < h.end);
    $("headline").innerHTML = heading?.html || "";
    show($("headline"), heading ? fade(t, heading.at, heading.end, .35) : 0);
    $("headline").style.transform = `translateY(${heading ? (1 - progress(t, heading.at, .5)) * 10 : 0}px)`;
    const request = requests.find(r => t >= r.at && t < r.end);
    $("request-text").textContent = request?.text || "";
    show($("request"), request ? fade(t, request.at, request.end, .4) : 0);
    $("request").style.transform = `translateY(${request ? (1 - progress(t, request.at, .55)) * 14 : 0}px)`;
    show($("hub"), progress(t, .65, .6));
    const pulse = request ? fade(t, request.send - .1, request.send + .6, .22) : 0;
    $("hub").querySelector(".hub-mark").style.transform = `scale(${1 + pulse * .065})`;
    show($("input-link"), request ? fade(t, request.at+.45, request.end, .4) : 0);
    const inputFlight = request ? clamp((t-request.send+.55)/.5) : 0;
    $("input-packet").setAttribute("cy", 405 + 56*inputFlight);
    show($("input-packet"), Math.sin(inputFlight*Math.PI));
    $("explanation").textContent = "Each agent keeps its own conversation.";
    show($("explanation"), fade(t, 14.8, 17.6, .35));
    ids.forEach((id, i) => {
      const agent = $(id), birth = arrival(requests[i]), intro = progress(t, birth, .65);
      const own = requests.findLast(r => r.to === i && t >= r.send);
      const followup = requests.find(r => r.to === i && !r.create);
      const changed = progress(t, arrival(followup) + .5, .6);
      const received = own ? fade(t, arrival(own), arrival(own) + 2.5, .25) : 0;
      show(agent, intro);
      agent.style.transform = `translateY(${(1-intro)*18}px) scale(${.94+.06*intro})`;
      const label = agent.querySelector(".arrival");
      label.textContent = own?.create ? "New agent" : "Same agent";
      show(label, received);
      const orbit = agent.querySelector(".orbit-progress");
      orbit.style.transform = `rotate(${t*64 + i*110}deg)`;
      orbit.style.strokeDasharray = changed >= 1 ? "415 0" : "52 363";
      orbit.style.opacity = String(changed >= 1 ? .45 : .7);
      show(agent.querySelector(".initial"), 1-changed);
      agent.querySelector(".initial").textContent = own && !own.create && t >= arrival(own) ? "Updating…" : "Building…";
      show(agent.querySelector(".revision"), changed);
      agent.querySelector(".revision").style.transform = `translateY(${(1-changed)*10}px)`;
      agent.querySelector(".status").textContent = changed >= 1 ? "✓ Updated" : "";
      const {group, active, dot, line, length} = routes[i];
      show(group, progress(t, requests[i].send, .3));
      const flight = request?.to === i ? clamp((t-request.send)/1.1) : -1;
      const inFlight = request?.to === i && t >= request.send && t < arrival(request) + .4;
      active.style.strokeDasharray = `${length}`;
      active.style.strokeDashoffset = `${length * (1-Math.max(0,flight))}`;
      show(active, inFlight ? 1-progress(t, arrival(request), .4) : 0);
      const position = line.getPointAtLength(Math.max(0, flight)*length);
      dot.setAttribute("cx", position.x); dot.setAttribute("cy",position.y);
      show(dot, inFlight ? Math.sin(Math.PI*clamp(flight)) : 0);
    });
    window.__sceneState = {time:t, agents:ids.filter((_,i)=>t>=arrival(requests[i])),
      request:request?.text || null, target:request ? ids[request.to] : null,
      updated:ids.filter((_,i)=>t>=arrival(requests.find(r=>r.to===i&&!r.create))+1.1), endcard:outro>=1};
  }
  window.__render = t => {render(t); return true;};
  window.__duration = () => 39;
  window.__cues = () => [];
  window.__ready = Promise.all([document.fonts.ready, ...Array.from(document.images, im => im.decode())]).then(() => {render(0); return true;});
})();
