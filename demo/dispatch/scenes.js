/* An edit of actual frontend captures. Only timing, cursor, typed text, and
 * editorial highlights are composed here; cards and controls come from the app.
 * All frames are pure functions of time, including backwards seeks. */
"use strict";
(() => {
  const {el, Frame, Cursor, seg, ease, lerp, clamp01} = Engine;
  const shot = name => "out/shots/" + name + ".png";
  const prompts = [
    "Build a minimalist chat app.",
    "Build a Snake game.",
    "Build a two-player Pong game.",
    "Make Snake work with swipe controls on my phone.",
    "Give the chat app a dark theme.",
    "Add a rematch button to Pong.",
  ];
  const targets = ["chat", "snake", "pong", "snake", "chat", "pong"];
  const starts = [3, 8, 13, 21, 27, 33];
  const sends = starts.map((start, i) => start + (i === 3 ? 3 : 2.35));
  const names = ["00-ready", ...prompts.flatMap((_, i) => [`prompt-${i}`, `routing-${i}`, `sent-${i}`]), "snake-conversation", "routing-history"];
  // Preload every image so frame-parallel encoding never captures a decode gap.
  const preload = el("div"); preload.hidden = true;
  for (const name of names) { const img = el("img"); img.src = shot(name); preload.append(img); }
  document.body.append(preload);
  let frame, cursor, heading, footnote, step, typing, highlight, flight, handoff;
  function rect(node, box) {
    Object.assign(node.style, {left: box.x + "px", top: box.y + "px", width: box.width + "px", height: box.height + "px"});
  }
  function opacity(node, amount) { node.style.opacity = String(clamp01(amount)); node.style.display = amount <= 0 ? "none" : ""; }
  Engine.addScene({start: 0, duration: 50, cls: "dispatch-demo",
    mount(root) {
      heading = el("h1", "demo-heading"); root.append(heading);
      footnote = el("div", "demo-footnote", "Scripted preview · Real Ribbon Field UI"); root.append(footnote);
      step = el("div", "demo-step"); root.append(step);
      frame = new Frame(root, {src: shot("00-ready"), pageW: 1440, pageH: 740, fit: 7 / 6, title: "Ribbon Field"});
      frame.place({x: 120, y: 108});
      typing = el("div", "demo-type"); frame.overlay.append(typing);
      highlight = el("div", "demo-highlight"); frame.overlay.append(highlight);
      flight = el("div", "demo-flight", "↗"); frame.overlay.append(flight);
      handoff = el("div", "demo-handoff"); frame.overlay.append(handoff);
      cursor = new Cursor(root);
    },
    update(t) {
      let active = -1;
      for (let i = 0; i < starts.length; i++) if (t >= starts[i]) active = i;
      let name = "00-ready";
      if (active >= 0) name = t < sends[active] ? (active ? `sent-${active - 1}` : "00-ready") :
        t < sends[active] + .65 ? `routing-${active}` : `sent-${active}`;
      if (t >= 39 && t < 45) name = "snake-conversation";
      if (t >= 45) name = "sent-5";
      frame.swap(shot(name));
      frame.place({x: 120, y: 108 + (1 - seg(t, .15, .7)) * 12, o: seg(t, .15, .65)});
      heading.textContent = t < 18 ? "Three ideas. One place to send them." : t < 21 ? "Three agents, working in parallel." :
        t < 39 ? "New request. The right conversation." : t < 45 ? "Each agent keeps its context." : "Keep the context. Keep going.";
      step.textContent = t < 3 ? "Dispatcher enabled" : t < 18 ? "Create · Chat / Snake / Pong" :
        t < 21 ? "3 app agents working" : t < 39 ? "Route · Reuse the same agents" : t < 45 ? "Follow up directly, too" : "1 dispatcher · 3 persistent app agents";
      opacity(heading, seg(t, 0, .5)); opacity(footnote, 1); opacity(step, seg(t, .3, .5));
      for (const item of [typing, highlight, flight, handoff]) opacity(item, 0);
      cursor.set(-100, -100, 0); cursor.ripple(-1);
      if (t >= 38.2 && t < 39) {
        const box = BOXES["sent-5"].snake, end = frame.project(box.x + box.width / 2, box.y + 24);
        const start = frame.project(1230, 655), move = seg(t, 38.2, .5, ease.inOutCubic);
        cursor.set(lerp(start.x, end.x, move), lerp(start.y, end.y, move));
        cursor.ripple(t - 38.85, end.x, end.y);
        return;
      }
      if (active < 0 || t >= 39) return;
      const i = active, at = starts[i], sent = sends[i], dt = t - sent;
      const input = BOXES[`prompt-${i}`].message, send = BOXES[`prompt-${i}`].send;
      if (t < sent) {
        Object.assign(typing.style, BOXES.typography);
        rect(typing, input); opacity(typing, 1);
        const length = Math.floor(clamp01((t - at - .15) / (i === 3 ? 2 : 1.4)) * prompts[i].length);
        typing.replaceChildren(document.createTextNode(prompts[i].slice(0, length)));
        if (Math.floor(t * 2) % 2 === 0) typing.append(el("span", "typing-caret"));
      }
      const end = frame.project(send.x + send.width / 2, send.y + send.height / 2);
      const begin = frame.project(input.x + 160, input.y + 26);
      const move = seg(t, sent - .55, .4, ease.inOutCubic);
      cursor.set(lerp(begin.x, end.x, move), lerp(begin.y, end.y, move), t > sent - .7 && dt < .8 ? 1 : 0);
      cursor.ripple(dt, end.x, end.y);
      const target = BOXES[`sent-${i}`][targets[i]];
      if (target && dt >= .65 && dt < 2.5) {
        rect(highlight, {x: target.x - 3, y: target.y - 3, width: target.width + 6, height: target.height + 6});
        opacity(highlight, seg(dt, .65, .2) * (1 - seg(dt, 2.05, .45)));
        handoff.textContent = i < 3 ? "New app agent" : "Existing app agent";
        Object.assign(handoff.style, {left: target.x + 12 + "px", top: target.y + target.height + 10 + "px"});
        opacity(handoff, seg(dt, .8, .2) * (1 - seg(dt, 2.05, .45)));
      }
      if (target && dt >= .25 && dt < .95) {
        const p = seg(dt, .25, .7, ease.inOutCubic);
        const x = lerp(send.x + send.width / 2, target.x + target.width / 2, p);
        const y = lerp(send.y, target.y + target.height / 2, p) - Math.sin(p * Math.PI) * 48;
        Object.assign(flight.style, {left: x - 15 + "px", top: y - 15 + "px"});
        opacity(flight, Math.sin(p * Math.PI));
      }
    }
  });
})();
