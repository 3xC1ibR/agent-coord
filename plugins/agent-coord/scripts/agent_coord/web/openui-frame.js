/* Runs only in an opaque-origin sandbox. No RF APIs, storage, or tool provider. */
(() => {
  "use strict";
  // Some saved responses use layout-library containers and a shorthand
  // for tabs. Adapt those calls to the chat catalog without evaluating source
  // or changing strings, comments, component references, or other signatures.
  function normalizeSource(source) {
    const tokens = /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/|[A-Za-z_$][\w$]*|[^\s]/g;
    const stack = [], edits = [];
    let previous;
    for (const match of source.matchAll(tokens)) {
      const token = match[0], start = match.index;
      if (/^(?:\/\/|#|\/\*)/.test(token)) continue;
      if (token === '"' || token === "'" || (token === "/" && source[start + 1] === "*")) return source;
      if (token === "(" || token === "[" || token === "{") {
        stack.push({token, name: token === "(" && !previous?.qualified ? previous?.text : null,
          nameStart: previous?.start, args: [start + 1]});
      } else if (token === "," && stack.at(-1)?.token === "(") {
        stack.at(-1).args.push(start + 1);
      } else if (token === ")" || token === "]" || token === "}") {
        const call = stack.pop();
        if (!call || call.token !== ({")": "(", "]": "[", "}": "{"})[token]) return source;
        if (token === ")") {
          const args = call.args.map((offset, index) => source.slice(offset, index + 1 < call.args.length ? call.args[index + 1] - 1 : start).trim());
          if (call.name === "Stack" && args.length === 1 && args[0]) {
            edits.push({start: call.nameStart, end: call.nameStart + 5, text: "Card"});
          } else if (call.name === "Grid" && args.length === 2 && /^\d+$/.test(args[1])) {
            edits.push({start: call.nameStart, end: call.nameStart + 4, text: "Card"},
              {start: call.args[1] - 1, end: start, text: ""});
          } else if (call.name === "TabItem" && args.length === 2 && args[1].startsWith("[")) {
            edits.push({start: call.args[1], end: call.args[1], text: args[0] + "\n,"});
          }
        }
      }
      previous = {text: token, start, qualified: previous?.text === "@" || previous?.text === "."};
    }
    if (stack.length) return source;
    const parts = [];
    let offset = 0;
    for (const edit of edits.sort((a, b) => a.start - b.start)) {
      parts.push(source.slice(offset, edit.start), edit.text);
      offset = edit.end;
    }
    parts.push(source.slice(offset));
    return parts.join("");
  }
  function renderError(errors) {
    const failures = errors.filter(error => error.severity !== "warning");
    if (!failures.length) return null;
    return "OpenUI: " + failures.map(error => (error.statementId ? error.statementId + ": " : "") + error.message).join("; ").slice(0, 280);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {normalizeSource, renderError};
    return;
  }
  let port, renderer, config, formState = {}, busy = false;
  const send = value => port?.postMessage(value);
  function render() {
    const {React, createRoot, Renderer, openuiChatLibrary} = window.__OpenUI;
    renderer ||= createRoot(document.getElementById("root"));
    document.documentElement.dataset.theme = config.theme;
    document.documentElement.dataset.openuiTheme = config.theme;
    document.documentElement.classList.toggle("dark", config.theme === "dark");
    const disabled = config.disabled || busy;
    document.body.inert = disabled;
    document.body.setAttribute("aria-disabled", String(disabled));
    renderer.render(React.createElement(Renderer, {
      response: normalizeSource(config.response), library: openuiChatLibrary, isStreaming: disabled,
      initialState: formState, publishObservability: false,
      onStateUpdate: state => { formState = state; send({type: "state", state}); },
      onAction: event => {
        if (disabled) return;
        busy = true;
        send({type: "action", event});
        render();
      },
      onError: errors => {
        const message = renderError(errors);
        send(message ? {type: "error", message} : {type: "rendered"});
      },
    }));
  }
  window.addEventListener("message", event => {
    if (event.source !== parent || port || event.data?.type !== "rf-openui-init" || !event.ports[0]) return;
    port = event.ports[0];
    port.onmessage = ({data}) => {
      if (data.type === "render") {
        config = data; formState = data.state || formState;
        busy = false;
        try { render(); } catch { send({type: "error", message: "OpenUI could not load. Its source is available below."}); }
      } else if (data.type === "settled") { busy = false; render(); }
    };
    port.start();
    send({type: "ready"});
  });
  // UI links are display-only; all agent interactions use the action callback.
  document.addEventListener("click", event => { if (event.target.closest("a")) event.preventDefault(); }, true);
  let lastHeight = 0;
  new ResizeObserver(() => {
    const height = Math.ceil(document.body.getBoundingClientRect().height);
    if (height !== lastHeight) { lastHeight = height; send({type: "height", height}); }
  }).observe(document.body);
})();
