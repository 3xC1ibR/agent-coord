/* Runs only in an opaque-origin sandbox. No RF APIs, storage, or tool provider. */
(() => {
  "use strict";
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
      response: config.response, library: openuiChatLibrary, isStreaming: disabled,
      initialState: formState, publishObservability: false,
      onStateUpdate: state => { formState = state; send({type: "state", state}); },
      onAction: event => {
        if (disabled) return;
        busy = true;
        send({type: "action", event});
        render();
      },
      onError: errors => {
        if (errors.some(error => error.severity !== "warning")) send({type: "error", message: "This OpenUI block could not render completely. Open its source for details."});
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
