/* Conceptual motion study, not product UI or actual provider activity.
 * Every property is derived from t so parallel rendering and scrubbing agree. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = x => Math.min(1, Math.max(0, x));
  const ease = x => 1 - Math.pow(1 - x, 3);
  const progress = (t, start, duration) => ease(clamp((t - start) / duration));
  const smooth = x => x * x * (3 - 2 * x);
  const mix = (a, b, p) => a + (b - a) * p;
  const fade = (t, start, end, duration = .35) => progress(t, start, duration) * (1 - progress(t, end - duration, duration));
  const visible = (el, value) => {el.style.opacity = String(clamp(value)); el.style.visibility = value <= 0 ? "hidden" : "visible";};
  const keys = ["chat", "snake", "pong"], tones = ["#778a72", "#8271a6", "#ad8153"];
  const centers = [480, 960, 1440];
  const prompts = [
    {at: 1.0, send: 3.4, target: 0, text: "Build a minimalist chat app.", creation: true},
    {at: 6.0, send: 8.3, target: 1, text: "Build a Snake game.", creation: true},
    {at: 11.0, send: 13.3, target: 2, text: "Build a two-player Pong game.", creation: true},
    {at: 19.0, send: 21.9, target: 1, text: "Make Snake work on my phone.", creation: false},
    {at: 25.0, send: 27.8, target: 0, text: "Give the chat app a dark theme.", creation: false},
    {at: 31.0, send: 33.8, target: 2, text: "Add a rematch button to Pong.", creation: false},
  ];
  const arrived = prompts.map(p => p.send + .85);
  const revisions = [29.1, 23.2, 35.1];
  const headings = [
    {at: 0, html: "Three ideas.<br><span>One place to start.</span>"},
    {at: 15.5, html: "Each idea gets an agent.<br><span>All three get to work.</span>"},
    {at: 18.2, html: "Keep the requests coming.<br><span>They find the right agent.</span>"},
    {at: 37.3, html: "New requests. Familiar agents.<br><span>The context stays with them.</span>"},
  ];
  const cards = keys.map(id => $(id));
  const svg = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
    return el;
  };
  const routes = centers.map((x, i) => {
    const group = svg("g", {}), path = svg("path", {class: "route"}), active = svg("path", {class: "route-active", stroke: tones[i]});
    const point = svg("circle", {r: 5, fill: tones[i]});
    group.append(path, active); $("routes").append(group); $("packets").append(point);
    return {group, path, active, point};
  });
  const snakeNodes = Array.from({length: 7}, (_, i) => {
    const el = svg("rect", {width: 17, height: 17, rx: 5, fill: i ? "#a799bb" : "#8271a6"});
    $("snake-body").append(el); return el;
  });
  function curve(x, y, p) {
    const q = 1 - p;
    return {x: q*q*q*960 + 3*q*q*p*960 + 3*q*p*p*x + p*p*p*x,
      y: q*q*q*y + 3*q*q*p*(y-76) + 3*q*p*p*751 + p*p*p*671};
  }
  function render(time) {
    const t = Math.max(0, Math.min(41.999, time));
    const expand = progress(t, 3.45, .9), composerY = mix(454, 803, expand);
    const headingIndex = headings.findLastIndex(h => t >= h.at);
    const currentHeading = headings[headingIndex];
    $("headline").innerHTML = currentHeading.html;
    const titleIntro = progress(t, currentHeading.at, .5);
    $("headline").style.transform = `translateY(${(1 - titleIntro) * 8}px)`;
    visible($("headline"), titleIntro);
    $("dispatcher").style.top = composerY + "px";
    visible($("dispatcher"), progress(t, .2, .5));
    const activeIndex = prompts.findLastIndex(p => t >= p.at), request = prompts[activeIndex];
    const sent = request && t >= request.send, delta = request ? t - request.send : -100;
    const routeLabel = $("route-label"), input = $("prompt");
    input.className = ""; routeLabel.textContent = "";
    if (!request) {input.textContent = "What would you like to build?"; input.className = "placeholder";}
    else if (t >= 37.3) {input.textContent = "What’s next?"; input.className = "placeholder"; routeLabel.textContent = "3 agents · context intact";}
    else {
      const typingDuration = request.creation ? 1.2 : 1.7;
      const length = sent ? request.text.length : Math.floor(clamp((t - request.at) / typingDuration) * request.text.length);
      input.textContent = request.text.slice(0, length);
      if (!sent && Math.floor(t * 2.2) % 2 === 0) {const caret = document.createElement("span"); caret.className = "caret"; input.append(caret);}
      if (sent) routeLabel.textContent = delta < .85 ? (request.creation ? "Starting an agent…" : "Routing…") :
        (request.creation ? "Created " : "Sent to ") + ["Chat", "Snake", "Pong"][request.target];
    }
    $("send").style.transform = `scale(${1 - .09 * fade(t, request?.send || 100, (request?.send || 100) + .25, .08)})`;
    $("send").style.backgroundColor = sent ? "#e9ede4" : "#dbe3d2";
    const initialWork = ["Building the conversation.", "Building the game loop.", "Building two-player mode."];
    const updating = ["Applying the dark theme.", "Adding swipe controls.", "Adding one more round."];
    const updated = ["Dark theme applied.", "Swipe controls added.", "Ready for a rematch."];
    cards.forEach((card, i) => {
      const birth = arrived[i], intro = progress(t, birth - .15, .7), revision = progress(t, revisions[i], .75);
      const ownRequest = prompts.findLast(p => p.target === i && t >= p.send);
      const pulse = ownRequest ? fade(t, ownRequest.send + .55, ownRequest.send + 2.35, .35) : 0;
      visible(card, intro);
      card.style.transform = `translateY(${(1 - intro) * 26 - pulse * 3}px) scale(${mix(.965, 1, intro)})`;
      card.style.borderColor = pulse > .01 ? tones[i] : "#dcded8";
      const isUpdating = ownRequest && !ownRequest.creation && t < revisions[i] + .75;
      card.querySelector(".agent-status span").textContent = revision >= 1 ? "Ready" : isUpdating ? "Updating" : "Working";
      card.querySelector(".agent-status i").style.opacity = revision >= 1 ? "1" : String(.5 + .5 * (Math.sin(t * 3 + i) + 1) / 2);
      card.querySelector(".work-text").textContent = revision >= 1 ? updated[i] : isUpdating ? updating[i] : initialWork[i];
      const glyph = card.querySelector(".work-glyph"); glyph.classList.toggle("ready", revision >= 1);
      glyph.style.transform = revision >= 1 ? "none" : `rotate(${t * 125}deg)`;
      const arrival = card.querySelector(".arrival");
      arrival.textContent = ownRequest?.creation ? "New agent" : "Same agent. Same context.";
      visible(arrival, ownRequest ? fade(t, ownRequest.send + .85, ownRequest.send + 3, .25) : 0);
      // Each card's preview gradually appears as its first work gets underway.
      visible(card.querySelector(".preview"), progress(t, birth + .2, 1));
      const {group, path, active, point} = routes[i];
      const d = `M 960 ${composerY} C 960 ${composerY - 76}, ${centers[i]} 751, ${centers[i]} 671`;
      path.setAttribute("d", d); active.setAttribute("d", d);
      const routeStart = prompts[i].send + .15;
      visible(group, progress(t, routeStart, .3) * expand);
      let flight = -1;
      if (request?.target === i && delta >= .12 && delta <= 1.05) flight = smooth(clamp((delta - .12) / .85));
      const length = active.getTotalLength();
      active.style.strokeDasharray = `${length}`;
      active.style.strokeDashoffset = `${length * (1 - Math.max(0, flight))}`;
      visible(active, flight >= 0 ? 1 - progress(delta, .86, .2) : 0);
      const xy = curve(centers[i], composerY, Math.max(0, flight));
      point.setAttribute("cx", xy.x); point.setAttribute("cy", xy.y);
      visible(point, flight >= 0 ? Math.sin(clamp(flight) * Math.PI) : 0);
    });
    const chatRevision = progress(t, revisions[0], .75);
    visible(document.querySelector(".dark-app"), chatRevision);
    visible(document.querySelector(".light-app"), 1 - chatRevision);
    const mobile = progress(t, revisions[1], .85), board = document.querySelector(".game-board");
    Object.assign(board.style, {left: mix(20, 56, mobile) + "px", top: mix(8, -7, mobile) + "px", width: mix(340, 142, mobile) + "px",
      height: mix(201, 229, mobile) + "px", borderColor: mobile > 0 ? "#bfb6cd" : "transparent", borderRadius: mix(10, 21, mobile) + "px",
      boxShadow: `0 8px 16px rgba(60,40,80,${mobile * .06})`});
    visible(document.querySelector(".phone-home"), mobile);
    document.querySelector(".snake-grid").setAttribute("viewBox", `${35 * mobile} ${-10 * mobile} ${320 - 95 * mobile} ${170 + 70 * mobile}`);
    visible(document.querySelector(".swipe"), progress(t, revisions[1] + .6, .5));
    document.querySelector(".swipe svg").style.transform = `translateX(${Math.sin((t - revisions[1]) * 2.5) * 7}px)`;
    // A looping snake path moves continuously, including while another agent receives a request.
    const step = Math.floor(Math.max(0, t - 9) * 3);
    const trail = [];
    for (let x = 3; x <= 10; x++) trail.push([x, 2]);
    for (let y = 3; y <= 5; y++) trail.push([10, y]);
    for (let x = 9; x >= 3; x--) trail.push([x, 5]);
    for (let y = 4; y >= 3; y--) trail.push([3, y]);
    snakeNodes.forEach((node, i) => {
      const cell = trail[(step - i + 100 * trail.length) % trail.length];
      node.setAttribute("x", cell[0] * 20 + 1.5); node.setAttribute("y", cell[1] * 20 + 1.5);
    });
    const pongRevision = progress(t, revisions[2], .6);
    visible(document.querySelector(".rematch"), pongRevision);
    const ball = document.querySelector(".ball"), phase = Math.max(0, t - 14) * 1.2;
    ball.style.left = (26 + (1 - Math.abs((phase % 2) - 1)) * 287) + "px";
    ball.style.top = (85 + Math.sin(phase * 1.8) * 47) + "px";
    document.querySelector(".paddle.left").style.top = (75 + Math.sin(phase * 1.8 + .4) * 44) + "px";
    document.querySelector(".paddle.right").style.top = (75 + Math.sin(phase * 1.8 - .4) * 44) + "px";
    const context = $("context-note");
    context.textContent = t >= 37.3 ? "One place to ask. A familiar agent for every idea." :
      t >= 18.2 ? "Follow-ups return to the agents already doing the work." : "";
    visible(context, t >= 37.3 ? progress(t, 37.3, .5) : progress(t, 18.2, .5));
    // Readable scene state for artifact validation, without launching real work.
    window.__sceneState = {time: t, agents: keys.filter((_, i) => t >= arrived[i]),
      completedRevisions: keys.filter((_, i) => t >= revisions[i] + .85), request: activeIndex, target: request ? keys[request.target] : null};
  }
  window.__render = t => {render(t); return true;};
  window.__duration = () => 42;
  window.__cues = () => [];
  window.__ready = document.fonts.ready.then(() => {render(0); return true;});
})();
