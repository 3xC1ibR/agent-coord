"use strict";

// Transient popups share the same reading and interaction window. Inline form
// errors and requests that need a decision keep their own lifetimes.
function createTransientNotice(element, view, onDismiss = () => { element.hidden = true; }) {
  let timer, active = false, hovered = false, focused = false, duration = 6000;
  const pause = () => { view.clearTimeout(timer); };
  const dismiss = () => {
    active = false; pause(); onDismiss();
  };
  const resume = () => {
    pause();
    if (active && !hovered && !focused) timer = view.setTimeout(dismiss, duration);
  };
  element.addEventListener("pointerenter", event => {
    if (event.pointerType === "touch") return;
    hovered = true; pause();
  });
  element.addEventListener("pointerleave", () => { hovered = false; resume(); });
  element.addEventListener("focusin", () => { focused = true; pause(); });
  element.addEventListener("focusout", event => { focused = element.contains(event.relatedTarget); resume(); });
  return {
    show(milliseconds = 6000) {
      duration = milliseconds; active = true; element.hidden = false;
      focused = element.contains(element.ownerDocument?.activeElement);
      resume();
    },
    dismiss,
  };
}

if (typeof module !== "undefined") module.exports = createTransientNotice;
