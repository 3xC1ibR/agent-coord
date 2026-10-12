"use strict";

// Shared overview feedback. Menus live outside the scrolling columns so they
// remain reachable on narrow cards and at the bottom of the viewport.
function createThreadActions(doc, win) {
  let activeMenu = null;
  const notices = new Map(), flights = new Set();
  const make = (tag, text, className) => {
    const element = doc.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function closeMenu(focus = false) {
    if (!activeMenu) return;
    const {trigger, panel} = activeMenu;
    panel.remove(); trigger.setAttribute("aria-expanded", "false"); activeMenu = null;
    if (focus && trigger.isConnected) trigger.focus();
  }
  doc.addEventListener("pointerdown", event => {
    if (activeMenu && !activeMenu.panel.contains(event.target) && !activeMenu.trigger.contains(event.target)) closeMenu();
  });
  doc.addEventListener("keydown", event => {
    if (event.key === "Escape" && activeMenu) { event.preventDefault(); closeMenu(true); }
  });
  function cancelMotion() { for (const stop of flights) stop(); flights.clear(); }
  win.addEventListener("scroll", () => { closeMenu(); cancelMotion(); }, {capture: true, passive: true});
  win.addEventListener("resize", () => { closeMenu(); cancelMotion(); });

  function menu(title, items, disabled = false) {
    const trigger = make("button", "⋮", "card-more");
    trigger.type = "button"; trigger.disabled = disabled;
    trigger.title = "Thread options";
    trigger.setAttribute("aria-label", "Thread options · " + title);
    trigger.setAttribute("aria-haspopup", "menu"); trigger.setAttribute("aria-expanded", "false");
    trigger.onclick = event => showMenu(trigger, title, items, event);
    return trigger;
  }
  function showMenu(trigger, title, items, event) {
    event.stopPropagation();
    const same = activeMenu?.trigger === trigger; closeMenu(); if (same) return;
    const panel = make("div", "", "thread-action-menu"); panel.setAttribute("role", "menu");
    panel.setAttribute("aria-label", "Thread options · " + title);
    for (const item of items) {
      const button = make("button", item.label); button.type = "button";
      button.setAttribute("role", "menuitem");
      button.onclick = event => { event.stopPropagation(); closeMenu(true); item.run(); };
      panel.append(button);
    }
    panel.onkeydown = event => {
      const buttons = [...panel.children], index = buttons.indexOf(doc.activeElement);
      let next;
      if (event.key === "ArrowDown") next = (index + 1) % buttons.length;
      if (event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = buttons.length - 1;
      if (next !== undefined) { event.preventDefault(); buttons[next].focus(); }
      if (event.key === "Tab") closeMenu(true);
    };
    doc.body.append(panel);
    const rect = trigger.getBoundingClientRect(), size = panel.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(rect.right - size.width, win.innerWidth - size.width - 8)) + "px";
    panel.style.top = Math.max(8, rect.bottom + size.height + 8 > win.innerHeight
      ? rect.top - size.height - 4 : rect.bottom + 4) + "px";
    activeMenu = {trigger, panel}; trigger.setAttribute("aria-expanded", "true"); panel.firstChild.focus();
  }

  function notify(key, message, undo) {
    notices.get(key)?.();
    let region = doc.getElementById("thread-feedback");
    if (!region) {
      region = make("div", "", "thread-feedback"); region.id = "thread-feedback";
      region.setAttribute("aria-label", "Thread actions"); doc.body.append(region);
    }
    const notice = make("div", "", "thread-notice"), copy = make("span", message);
    copy.setAttribute("role", "status"); notice.append(copy);
    let timer, hovered = false, focused = false, pending = false, removed = false;
    const pause = () => win.clearTimeout(timer);
    const remove = () => {
      if (removed) return;
      removed = true; pause(); notice.remove(); notices.delete(key);
    };
    const resume = () => {
      pause();
      if (!removed && !hovered && !focused && !pending) timer = win.setTimeout(remove, undo ? 10000 : 6000);
    };
    notice.addEventListener("pointerenter", () => { hovered = true; pause(); });
    notice.addEventListener("pointerleave", () => { hovered = false; resume(); });
    notice.addEventListener("focusin", () => { focused = true; pause(); });
    notice.addEventListener("focusout", event => { focused = notice.contains(event.relatedTarget); resume(); });
    if (undo) {
      const button = make("button", "Undo", "quiet"); button.type = "button";
      button.onclick = async () => {
        if (pending || removed) return;
        pending = true; pause(); button.disabled = true;
        try { await undo(); remove(); }
        catch (error) { copy.textContent = error.message + " Try Undo again."; button.disabled = false; }
        finally { pending = false; resume(); }
      };
      notice.append(button);
    }
    const dismiss = make("button", "×", "quiet notice-dismiss"); dismiss.type = "button";
    dismiss.setAttribute("aria-label", "Dismiss notification"); dismiss.onclick = remove;
    notice.append(dismiss); region.append(notice); notices.set(key, remove); resume();
  }

  function captureClose(id) {
    const focused = doc.activeElement?.closest("#overview [data-card-thread]");
    const card = focused?.dataset.cardThread === id ? focused
      : [...doc.querySelectorAll("#overview [data-card-thread]")].find(el => el.dataset.cardThread === id);
    if (!card) return null;
    const rect = card.getBoundingClientRect();
    if (!rect.width || rect.bottom < 0 || rect.top > win.innerHeight) return null;
    return {copy: card.cloneNode(true), rect};
  }
  function closed(snapshot) {
    const tab = doc.getElementById("show-closed");
    if (!tab) return;
    tab.classList.add("thread-destination");
    win.setTimeout(() => tab.classList.remove("thread-destination"), 1000);
    if (!snapshot || win.matchMedia("(prefers-reduced-motion: reduce)").matches || !snapshot.copy.animate) return;
    const {copy, rect} = snapshot, target = tab.getBoundingClientRect();
    if (!target.width) return;
    copy.setAttribute("aria-hidden", "true"); copy.inert = true;
    for (const element of [copy, ...copy.querySelectorAll("[id]")]) element.removeAttribute("id");
    Object.assign(copy.style, {position: "fixed", left: rect.left + "px", top: rect.top + "px",
      width: rect.width + "px", height: rect.height + "px", margin: "0", pointerEvents: "none", zIndex: "1000"});
    doc.body.append(copy);
    const dx = target.left + target.width / 2 - rect.left - rect.width / 2;
    const dy = target.top + target.height / 2 - rect.top - rect.height / 2;
    const animation = copy.animate([{transform: "translate(0, 0) scale(1)", opacity: .8},
      {transform: `translate(${dx}px, ${dy}px) scale(.08)`, opacity: 0}],
    {duration: 320, easing: "ease-in", fill: "forwards"});
    const stop = () => { animation.cancel(); copy.remove(); flights.delete(stop); };
    flights.add(stop); animation.finished.then(stop, stop);
  }
  return {menu, showMenu, closeMenu, notify, captureClose, closed};
}

if (typeof module !== "undefined") module.exports = createThreadActions;
