(() => {
  'use strict';
  const story = document.querySelector('.scroll-story');
  const scene = document.querySelector('.scene');
  const copies = [...document.querySelectorAll('.story-copy')];
  const request = document.querySelector('.request-card');
  const nodes = Object.fromEntries([...document.querySelectorAll('[data-node]')].map(el => [el.dataset.node, el]));
  const paths = Object.fromEntries([...document.querySelectorAll('.route')].map(el => [el.id, { el, length: el.getTotalLength() }]));
  const pulse = document.querySelector('#task-pulse');
  const apiPulse = document.querySelector('#api-pulse');
  const reply = document.querySelector('#reply-pulse');
  const leadNote = document.querySelector('.lead-note');
  const exchange = document.querySelector('.agent-exchange');
  const verification = document.querySelector('.verification-note');
  const decision = document.querySelector('.decision-card');
  const progress = document.querySelector('.story-progress div');
  const chapterNumber = document.querySelector('#chapter-number');
  const announcement = document.querySelector('#story-announcement');
  const scrollLabel = document.querySelector('#scroll-label');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const statuses = Object.fromEntries(Object.keys(nodes).map(name => [name, document.querySelector(`#${name}-status`)]));
  const teams = ['web', 'api', 'mobile'];
  const recruits = {
    ui: [2.75, 3.15], a11y: [3.05, 3.5],
    data: [2.85, 3.25], access: [3.15, 3.6],
    sync: [3.0, 3.4], verifier: [4.95, 5.35],
  };
  const initialStatuses = Object.fromEntries(Object.entries(statuses).map(([name, el]) => [name, el.textContent]));
  const lastChapter = copies.length - 1;
  document.querySelector('#chapter-total').textContent = String(copies.length).padStart(2, '0');
  let frame = 0;
  let activeChapter = -1;
  const clamp = value => Math.max(0, Math.min(1, value));
  const smooth = (start, end, value) => { const x = clamp((value - start) / (end - start)); return x * x * (3 - 2 * x); };
  const between = (value, enter, full, leave, gone) => smooth(enter, full, value) * (1 - smooth(leave, gone, value));
  const opacity = (el, value) => {
    el.style.opacity = value.toFixed(4);
    el.setAttribute('aria-hidden', value < .05 ? 'true' : 'false');
  };
  function drawPath(name, amount, alpha = 1) {
    const { el, length } = paths[name];
    el.style.strokeDasharray = `${length} ${length}`;
    el.style.strokeDashoffset = String(length * (1 - clamp(amount)));
    opacity(el, amount > 0 ? alpha : 0);
  }
  function movePulse(el, name, fraction, visibility) {
    const { el: path, length } = paths[name];
    const point = path.getPointAtLength(length * clamp(fraction));
    el.setAttribute('cx', point.x);
    el.setAttribute('cy', point.y);
    opacity(el, visibility);
  }
  function reveal(el, amount, disappear = 0) {
    const alpha = amount * (1 - disappear);
    opacity(el, alpha);
    el.style.transform = `translate(-50%,calc(-50% + ${(1 - amount) * (reducedMotion.matches ? 0 : 22)}px))`;
    el.setAttribute('aria-hidden', alpha < 0.1 ? 'true' : 'false');
  }
  function setStatus(name, value) {
    if (statuses[name].textContent !== value) statuses[name].textContent = value;
  }
  function setPath(name, d) {
    const path = paths[name];
    if (path.el.getAttribute('d') === d) return;
    path.el.setAttribute('d', d);
    path.length = path.el.getTotalLength();
  }
  function updateConnections() {
    // Use the rendered card edges so the tree stays connected at every size.
    const box = scene.getBoundingClientRect();
    const x = value => Math.round((value - box.left) * 620 / box.width * 100) / 100;
    const y = value => Math.round((value - box.top) * 640 / box.height * 100) / 100;
    const positions = Object.fromEntries(Object.entries(nodes).map(([name, el]) => {
      const rect = el.getBoundingClientRect();
      return [name, { left: x(rect.left), right: x(rect.right), top: y(rect.top), bottom: y(rect.bottom), cx: x(rect.left + rect.width / 2), cy: y(rect.top + rect.height / 2) }];
    }));
    Object.entries(paths).forEach(([name, { el }]) => {
      if (!el.dataset.from) return;
      const from = positions[el.dataset.from];
      const to = positions[el.dataset.to];
      if (el.dataset.nested) {
        const rail = to.left - 12;
        const bend = from.bottom + 16;
        setPath(name, `M${from.cx} ${from.bottom} C${from.cx} ${bend} ${rail} ${bend} ${rail} ${bend + 16} L${rail} ${to.cy - 10} Q${rail} ${to.cy} ${to.left} ${to.cy}`);
      } else {
        const middle = (from.bottom + to.top) / 2;
        setPath(name, `M${from.cx} ${from.bottom} C${from.cx} ${middle} ${to.cx} ${middle} ${to.cx} ${to.top}`);
      }
    });
    const { lead, api, mobile, verifier } = positions;
    const messageY = Math.max(api.bottom, mobile.bottom) + 35;
    setPath('message-route', `M${mobile.cx} ${mobile.bottom} C${mobile.cx} ${messageY} ${api.cx} ${messageY} ${api.cx} ${api.bottom}`);
    setPath('return-route', `M${verifier.right} ${verifier.cy} C618 ${verifier.cy} 618 ${lead.cy} ${lead.right} ${lead.cy}`);
  }
  function render() {
    frame = 0;
    const rect = story.getBoundingClientRect();
    const distance = Math.max(1, story.offsetHeight - innerHeight);
    const p = clamp(-rect.top / distance);
    const raw = p * (copies.length - .2);
    const chapter = Math.min(lastChapter, Math.floor(raw));
    // Each reduced-motion state shows the complete scene without animated travel.
    const t = reducedMotion.matches ? [0, 1.35, 2.35, 3.6, 4.35, 5.65, 6.5][chapter] : raw;
    const done = smooth(5.95, 6.4, t);
    const sceneFade = 1 - done;
    const arrival = smooth(.2, 1.0, t);
    const expanded = smooth(2.45, 3.05, t);
    const talking = between(t, 3.8, 4.05, 4.55, 4.85);
    const nestedAlpha = sceneFade * (1 - talking);

    copies.forEach((copy, index) => {
      const local = raw - Math.floor(raw);
      const outgoing = reducedMotion.matches ? 0 : smooth(.65, .82, local);
      const incoming = reducedMotion.matches ? 0 : smooth(.82, 1, local);
      let alpha = index === chapter ? (chapter === lastChapter ? 1 : 1 - outgoing) : index === chapter + 1 ? incoming : 0;
      if (reducedMotion.matches) alpha = index === chapter ? 1 : 0;
      opacity(copy, alpha);
      const shift = reducedMotion.matches ? 0 : index === chapter ? -18 * outgoing : 18 * (1 - incoming);
      copy.style.transform = `translateY(${shift}px)`;
      copy.classList.toggle('is-current', alpha > .5);
      copy.setAttribute('aria-hidden', alpha > .5 ? 'false' : 'true');
      copy.inert = alpha <= .5;
    });

    request.style.top = `${40 + 18 * arrival}%`;
    request.style.transform = `translate(-50%,-50%) rotate(${-5 * (1 - arrival)}deg) scale(${1 - .58 * arrival})`;
    opacity(request, 1 - smooth(.85, 1.15, t));
    const compact = innerWidth <= 760 && innerHeight <= 700;
    const initialLeadTop = compact ? 26.3 : 30.6;
    const expandedLeadTop = compact ? 14.2 : innerWidth > 760 && innerWidth <= 1050 ? 14.5 : 17.2;
    const leadTop = initialLeadTop + (expandedLeadTop - initialLeadTop) * expanded;
    nodes.lead.style.top = `${58 - (58 - leadTop) * smooth(1.1, 1.8, t)}%`;
    reveal(nodes.lead, smooth(.8, 1.2, t), done);
    reveal(nodes.web, smooth(1.65, 2.15, t), done);
    reveal(nodes.api, smooth(1.75, 2.25, t), done);
    reveal(nodes.mobile, smooth(1.85, 2.35, t), done);
    teams.forEach(name => { nodes[name].style.top = `${55.2 - 14.6 * expanded}%`; });
    Object.entries(recruits).forEach(([name, [start, end]]) => {
      reveal(nodes[name], smooth(start, end, t), 1 - nestedAlpha);
      nodes[name].classList.toggle('passed', t >= 5.5);
      setStatus(name, t >= 5.5 ? 'Checks passed' : initialStatuses[name]);
    });
    nodes.api.classList.toggle('is-speaking', talking > .5);
    nodes.mobile.classList.toggle('is-speaking', talking > .5);

    setStatus('lead', t < 1.6 ? 'Planning the work' : t < 5.5 ? 'Coordinating the team' : 'Gathering the result');
    setStatus('web', t < 2.75 ? 'Building collections' : t < 3.8 ? 'Calls 2 agents' : 'Web ready');
    setStatus('api', t < 2.85 ? 'Adding shared access' : t < 3.8 ? 'Calls 2 agents' : t < 4.3 ? 'Keeping compatibility' : 'Older apps supported');
    setStatus('mobile', t < 3 ? 'Connecting the app' : t < 3.8 ? 'Calls a sync agent' : t < 4.9 ? 'Coordinating release' : t < 5.5 ? 'Calls a verifier' : 'Mobile verified');
    updateConnections();

    drawPath('request-route', smooth(.4, .95, t), between(t, .4, .6, 1.05, 1.3) * .65);
    drawPath('web-route', smooth(1.4, 2.05, t), sceneFade * .85);
    drawPath('api-route', smooth(1.5, 2.15, t), sceneFade * .85);
    drawPath('mobile-route', smooth(1.6, 2.25, t), sceneFade * .85);
    Object.entries(recruits).forEach(([name, [start, end]]) => {
      drawPath(`${name}-route`, smooth(start - .1, end, t), nestedAlpha * .75);
    });
    drawPath('message-route', smooth(3.8, 4.05, t), talking);
    drawPath('return-route', smooth(5.5, 6.12, t), sceneFade * .5);

    opacity(leadNote, between(t, 1.65, 1.85, 2.1, 2.4));
    opacity(exchange, talking);
    exchange.style.transform = `translateY(${reducedMotion.matches ? 0 : 12 * (1 - talking)}px)`;
    opacity(exchange.querySelector('.reply'), smooth(4.1, 4.35, t));
    opacity(verification, between(t, 5.5, 5.65, 5.95, 6.4));

    opacity(pulse, 0);
    opacity(apiPulse, 0);
    opacity(reply, 0);
    if (!reducedMotion.matches) {
      if (t > 1.4 && t < 2.05) movePulse(pulse, 'web-route', (t - 1.4) / .65, 1);
      if (t > 1.5 && t < 2.15) movePulse(apiPulse, 'api-route', (t - 1.5) / .65, 1);
      if (t > 1.6 && t < 2.25) movePulse(reply, 'mobile-route', (t - 1.6) / .65, 1);
      if (t > 2.65 && t < 3.1) movePulse(pulse, 'ui-route', (t - 2.65) / .45, 1);
      if (t > 2.75 && t < 3.2) movePulse(apiPulse, 'data-route', (t - 2.75) / .45, 1);
      if (t > 2.95 && t < 3.4) movePulse(reply, 'sync-route', (t - 2.95) / .45, 1);
      if (t > 3.15 && t < 3.5) movePulse(pulse, 'a11y-route', (t - 3.15) / .35, 1);
      if (t > 3.25 && t < 3.6) movePulse(apiPulse, 'access-route', (t - 3.25) / .35, 1);
      if (t > 3.85 && t < 4.1) movePulse(pulse, 'message-route', (t - 3.85) / .25, 1);
      if (t > 4.15 && t < 4.45) movePulse(reply, 'message-route', 1 - (t - 4.15) / .3, 1);
      if (t > 4.85 && t < 5.35) movePulse(pulse, 'verifier-route', (t - 4.85) / .5, 1);
      if (t > 5.5 && t < 6.12) movePulse(reply, 'return-route', (t - 5.5) / .62, sceneFade);
    }

    const ready = smooth(5.95, 6.4, t);
    opacity(decision, ready);
    decision.style.transform = `translate(-50%,calc(-50% + ${reducedMotion.matches ? 0 : 30 * (1 - ready)}px)) scale(${.95 + .05 * ready})`;
    decision.style.pointerEvents = ready > .9 ? 'auto' : 'none';
    decision.inert = ready <= .9;
    decision.setAttribute('aria-hidden', ready > .9 ? 'false' : 'true');
    progress.style.transform = `scaleX(${p})`;
    scene.dataset.chapter = chapter;
    scene.dataset.progress = p.toFixed(3);
    chapterNumber.textContent = String(chapter + 1).padStart(2, '0');
    scrollLabel.textContent = chapter === lastChapter ? 'The work comes back to you.' : 'A little scroll. A bigger picture.';
    if (chapter !== activeChapter) {
      activeChapter = chapter;
      announcement.textContent = copies[chapter].querySelector('h1,h2').textContent;
    }
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(render); }
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  reducedMotion.addEventListener('change', schedule);
  document.querySelector('.review-button').addEventListener('click', () => {
    document.querySelector('.review-feedback').hidden = false;
    document.querySelector('.review-button').textContent = 'Example reviewed ✓';
  });
  render();
})();
