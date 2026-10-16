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
  function render() {
    frame = 0;
    const rect = story.getBoundingClientRect();
    const distance = Math.max(1, story.offsetHeight - innerHeight);
    const p = clamp(-rect.top / distance);
    const raw = p * 5.8;
    const chapter = Math.min(5, Math.floor(raw));
    // Reduced motion preserves all six narrative states without moving elements.
    const t = reducedMotion.matches ? [0, 1.35, 2.5, 3.35, 4.65, 5.5][chapter] : raw;
    const done = smooth(4.9, 5.4, t);
    const sceneFade = 1 - done;
    const arrival = smooth(.2, 1.0, t);

    copies.forEach((copy, index) => {
      const local = raw - Math.floor(raw);
      const outgoing = reducedMotion.matches ? 0 : smooth(.65, .82, local);
      const incoming = reducedMotion.matches ? 0 : smooth(.82, 1, local);
      let alpha = index === chapter ? (chapter === 5 ? 1 : 1 - outgoing) : index === chapter + 1 ? incoming : 0;
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
    const leadTop = innerWidth <= 760 && innerHeight <= 700 ? 26.3 : 30.6;
    nodes.lead.style.top = `${58 - (58 - leadTop) * smooth(1.1, 1.8, t)}%`;
    reveal(nodes.lead, smooth(.8, 1.2, t), done);
    reveal(nodes.web, smooth(1.65, 2.15, t), done);
    reveal(nodes.api, smooth(1.75, 2.25, t), done);
    reveal(nodes.mobile, smooth(1.85, 2.35, t), done);
    reveal(nodes.verifier, smooth(3.75, 4.22, t), done);
    nodes.verifier.classList.toggle('passed', t >= 4.45);

    drawPath('request-route', smooth(.4, .95, t), between(t, .4, .6, 1.05, 1.3) * .65);
    drawPath('web-route', smooth(1.4, 2.05, t), sceneFade * .85);
    drawPath('api-route', smooth(1.5, 2.15, t), sceneFade * .85);
    drawPath('mobile-route', smooth(1.6, 2.25, t), sceneFade * .85);
    drawPath('message-route', smooth(2.7, 3.0, t), between(t, 2.6, 2.85, 3.6, 3.85));
    drawPath('verifier-route', smooth(3.6, 4.1, t), sceneFade * .85);
    drawPath('return-route', smooth(4.45, 5.02, t), sceneFade * .5);

    opacity(leadNote, between(t, 1.65, 1.85, 2.1, 2.4));
    const talking = between(t, 2.7, 3.05, 3.55, 3.9);
    opacity(exchange, talking);
    exchange.style.transform = `translateY(${reducedMotion.matches ? 0 : 12 * (1 - talking)}px)`;
    opacity(exchange.querySelector('.reply'), smooth(3.05, 3.35, t));
    opacity(verification, between(t, 4.45, 4.65, 4.9, 5.3));

    opacity(pulse, 0);
    opacity(apiPulse, 0);
    opacity(reply, 0);
    if (!reducedMotion.matches) {
      if (t > 1.4 && t < 2.05) movePulse(pulse, 'web-route', (t - 1.4) / .65, 1);
      if (t > 1.5 && t < 2.15) movePulse(apiPulse, 'api-route', (t - 1.5) / .65, 1);
      if (t > 1.6 && t < 2.25) movePulse(reply, 'mobile-route', (t - 1.6) / .65, 1);
      if (t > 2.75 && t < 3.03) movePulse(pulse, 'message-route', (t - 2.75) / .28, 1);
      if (t > 3.08 && t < 3.38) movePulse(reply, 'message-route', 1 - (t - 3.08) / .3, 1);
      if (t > 3.6 && t < 4.1) movePulse(pulse, 'verifier-route', (t - 3.6) / .5, 1);
      if (t > 4.45 && t < 5.02) movePulse(reply, 'return-route', (t - 4.45) / .57, sceneFade);
    }

    statuses.lead.textContent = t < 1.6 ? 'Planning the work' : t < 4.4 ? 'Coordinating the team' : 'Gathering the result';
    statuses.web.textContent = t < 3.3 ? 'Building collections' : 'Web ready';
    statuses.api.textContent = t < 3.25 ? 'Adding shared access' : 'Older apps supported';
    statuses.mobile.textContent = t < 3.6 ? 'Connecting the app' : t < 4.45 ? 'Requesting a check' : 'Mobile verified';
    statuses.verifier.textContent = t < 4.45 ? 'Checking both apps' : 'Checks passed';

    const ready = smooth(4.95, 5.4, t);
    opacity(decision, ready);
    decision.style.transform = `translate(-50%,calc(-50% + ${reducedMotion.matches ? 0 : 30 * (1 - ready)}px)) scale(${.95 + .05 * ready})`;
    decision.style.pointerEvents = ready > .9 ? 'auto' : 'none';
    decision.inert = ready <= .9;
    decision.setAttribute('aria-hidden', ready > .9 ? 'false' : 'true');
    progress.style.transform = `scaleX(${p})`;
    scene.dataset.chapter = chapter;
    scene.dataset.progress = p.toFixed(3);
    chapterNumber.textContent = String(chapter + 1).padStart(2, '0');
    scrollLabel.textContent = chapter === 5 ? 'The work comes back to you.' : 'A little scroll. A bigger picture.';
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
