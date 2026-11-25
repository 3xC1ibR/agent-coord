from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = ROOT / "desktop/macos"
IOS_DIR = ROOT / "mobile/ios/RibbonField"

MACOS_PRODUCTION_SOURCES = [
    MACOS_DIR / "App.swift",
    MACOS_DIR / "WorkspaceFiles.swift",
    MACOS_DIR / "FileBrowser.swift",
    MACOS_DIR / "RuntimeHost.swift",
    MACOS_DIR / "Updater.swift",
    MACOS_DIR / "WindowRestoration.swift",
    MACOS_DIR / "IntegratedTitlebar.swift",
]

MACOS_SMOKE_SOURCES = [
    MACOS_DIR / "App.swift",
    MACOS_DIR / "SmokeHarness.swift",
    MACOS_DIR / "WorkspaceFiles.swift",
    MACOS_DIR / "FileBrowser.swift",
    MACOS_DIR / "RuntimeHost.swift",
    MACOS_DIR / "Updater.swift",
    MACOS_DIR / "WindowRestoration.swift",
    MACOS_DIR / "IntegratedTitlebar.swift",
]


def _ios_simulator_sdk() -> tuple[str, dict[str, str]] | None:
    candidates = [dict(os.environ)]
    xcode_dev = Path("/Applications/Xcode.app/Contents/Developer")
    if xcode_dev.is_dir():
        candidates.append({**os.environ, "DEVELOPER_DIR": str(xcode_dev)})
    for env in candidates:
        probe = subprocess.run(
            ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"],
            capture_output=True,
            text=True,
            env=env,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip(), env
    return None


def _strip_preprocessor_blocks(source: str, flag: str) -> str:
    """Return the active Swift lines when `flag` is NOT defined."""
    output: list[str] = []
    stack: list[bool] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#if "):
            cond = stripped[4:].strip()
            parent_active = all(stack)
            if cond == flag:
                stack.append(False)
            else:
                stack.append(parent_active)
            continue
        if stripped == "#else":
            if stack:
                parent_active = all(stack[:-1])
                stack[-1] = parent_active and not stack[-1]
            continue
        if stripped == "#endif":
            if stack:
                stack.pop()
            continue
        if all(stack):
            output.append(line)
    return "\n".join(output)


class NativeShellsTests(unittest.TestCase):
    def test_macos_smoke_harness_split_and_production_isolation(self):
        app_source = (MACOS_DIR / "App.swift").read_text(encoding="utf-8")
        smoke_source = (MACOS_DIR / "SmokeHarness.swift").read_text(encoding="utf-8")

        self.assertLess(len(app_source.splitlines()), 1650, "Smoke automation must be extracted out of App.swift")
        self.assertGreater(len(smoke_source.splitlines()), 1950, "SmokeHarness.swift must hold the extracted smoke suite")
        self.assertIn("#if RIBBON_SMOKE_TESTS", smoke_source)
        self.assertIn("extension AppDelegate", smoke_source)
        for entrypoint in (
            "func runSmoke(",
            "func smokeWindowRestoration(",
            "func smokeOriginChange(",
            "func smokeTitlebar(",
            "func smokeDetach(",
            "func finishSmoke(",
            "func prepareOfflineSmokeFixtures(",
        ):
            self.assertIn(entrypoint, smoke_source)
            self.assertNotIn(entrypoint, app_source)

        production_only = _strip_preprocessor_blocks(app_source, "RIBBON_SMOKE_TESTS")
        self.assertNotIn("smoke", production_only.lower(), "Zero smoke references may remain in production App.swift lines")

    def test_macos_swiftc_typechecks_both_production_and_smoke_configurations(self):
        prod = subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-swift-version",
                "5",
                "-target",
                "arm64-apple-macosx12.0",
                "-typecheck",
                *(str(path) for path in MACOS_PRODUCTION_SOURCES),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(prod.returncode, 0, f"Production swiftc typecheck failed:\n{prod.stderr}")

        smoke = subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-swift-version",
                "5",
                "-DRIBBON_SMOKE_TESTS",
                "-target",
                "arm64-apple-macosx12.0",
                "-typecheck",
                *(str(path) for path in MACOS_SMOKE_SOURCES),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, f"Smoke-enabled swiftc typecheck failed:\n{smoke.stderr}")

    def test_macos_instant_quit_never_blocks_on_busy_or_multi_second_timeouts(self):
        app_source = (MACOS_DIR / "App.swift").read_text(encoding="utf-8")
        self.assertNotIn("A message or image is still being accepted", app_source)
        self.assertNotIn("A window is not responding while saving drafts", app_source)
        self.assertNotIn("windowStateBusy", app_source)
        self.assertIn("windowPersistenceQueue.sync", app_source)
        match = re.search(r"quitExportDeadlineSeconds:\s*TimeInterval\s*=\s*([0-9.]+)", app_source)
        self.assertIsNotNone(match, "Expected explicit bounded quitExportDeadlineSeconds constant")
        deadline = float(match.group(1))
        self.assertLessEqual(deadline, 0.06, "Quit export deadline must be <= 60 ms")

    def test_ios_swiftc_typecheck_and_resilience_contracts(self):
        connection_source = (IOS_DIR / "ConnectionModel.swift").read_text(encoding="utf-8")
        pairing_source = (IOS_DIR / "PairingClient.swift").read_text(encoding="utf-8")

        self.assertIn("view.allowsBackForwardNavigationGestures = true", connection_source)
        self.assertNotIn("allowsBackForwardNavigationGestures = false", connection_source)
        self.assertIn('static let service = "com.agentcoord.ios.pairingCookie"', pairing_source)
        self.assertIn("SecItemAdd", pairing_source)
        self.assertIn("SecItemCopyMatching", pairing_source)
        self.assertIn("SecItemDelete", pairing_source)
        self.assertIn("PairingCookieKeychain.save(", connection_source)
        self.assertIn("PairingCookieKeychain.clear()", connection_source)
        self.assertIn("restorePairingCookieIfNeeded(", connection_source)
        self.assertIn("state.config.token = config.token", connection_source)
        self.assertIn("agent-coord-native-reconnect", connection_source)
        self.assertIn("agentCoordReconnectEvents", connection_source)
        self.assertIn("agent-coord.draft:", connection_source)
        self.assertIn("agentCoordRestoreDraftBackup", connection_source)
        self.assertNotIn("state.config.token !== config.token", connection_source)

        sdk_info = _ios_simulator_sdk()
        if sdk_info is not None:
            sdk_path, env = sdk_info
            ios_sources = sorted(str(path) for path in IOS_DIR.glob("*.swift"))
            check = subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    "-sdk",
                    sdk_path,
                    "-target",
                    "arm64-apple-ios16.4-simulator",
                    "-typecheck",
                    *ios_sources,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(check.returncode, 0, f"iOS Simulator swiftc typecheck failed:\n{check.stderr}")

    def test_ios_keychain_cookie_persistence_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="ribbon-keychain-test-") as tmp:
            harness = Path(tmp) / "KeychainRunner.swift"
            binary = Path(tmp) / "keychain_test"
            harness.write_text(
                """
import Foundation

@main
struct KeychainRunner {
    static func main() throws {
        let service = "com.agentcoord.ios.pairingCookie.test." + UUID().uuidString
        defer { PairingCookieKeychain.clear(service: service) }

        let server = try ServerAddress("https://mac.tail.ts.net")
        let otherServer = try ServerAddress("https://other.tail.ts.net")
        let header = "__Host-agent-coord-device=durable-token-123; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600"
        guard let cookie = HTTPCookie.cookies(withResponseHeaderFields: ["Set-Cookie": header], for: server.url).first else {
            fputs("Failed to parse test cookie\\n", stderr)
            exit(1)
        }
        guard PairingCookieKeychain.save(cookie, for: server, service: service) else {
            fputs("Keychain save failed\\n", stderr)
            exit(1)
        }
        guard let restored = PairingCookieKeychain.load(for: server, service: service) else {
            fputs("Keychain load returned nil\\n", stderr)
            exit(1)
        }
        guard restored.name == "__Host-agent-coord-device",
              restored.value == "durable-token-123",
              restored.domain == "mac.tail.ts.net",
              restored.path == "/",
              restored.isSecure,
              restored.isHTTPOnly else {
            fputs("Restored cookie properties mismatch\\n", stderr)
            exit(1)
        }
        // Foreign origin must never receive the cookie, and invalid origin lookup clears mismatched state safely
        // only when origin mismatches; test expired timestamp rejection first on a second isolated service.
        let expService = service + ".expired"
        defer { PairingCookieKeychain.clear(service: expService) }
        _ = PairingCookieKeychain.save(cookie, for: server, service: expService)
        let expiredLookup = PairingCookieKeychain.load(for: server, now: Date().addingTimeInterval(7200), service: expService)
        guard expiredLookup == nil else {
            fputs("Expired cookie should not be returned\\n", stderr)
            exit(1)
        }
        let wrongOrigin = PairingCookieKeychain.load(for: otherServer, service: service)
        guard wrongOrigin == nil else {
            fputs("Mismatched server origin must not load cookie\\n", stderr)
            exit(1)
        }
        guard PairingCookieKeychain.clear(service: service),
              PairingCookieKeychain.load(for: server, service: service) == nil else {
            fputs("Keychain clear did not delete cookie\\n", stderr)
            exit(1)
        }
    }
}
""",
                encoding="utf-8",
            )
            compiled = subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    "-swift-version",
                    "5",
                    "-target",
                    "arm64-apple-macosx13.0",
                    str(IOS_DIR / "ConnectionPolicy.swift"),
                    str(IOS_DIR / "PairingClient.swift"),
                    str(harness),
                    "-o",
                    str(binary),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, f"Failed to compile Keychain harness:\n{compiled.stderr}")
            executed = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(executed.returncode, 0, f"Keychain round-trip failed:\n{executed.stderr}")

    def test_ios_user_script_draft_backup_and_event_source_tracking(self):
        connection_source = (IOS_DIR / "ConnectionModel.swift").read_text(encoding="utf-8")
        match = re.search(r'static let userScriptSource = """\n(.*?)\n    """', connection_source, re.DOTALL)
        self.assertIsNotNone(match, "Could not extract ConnectionModel.userScriptSource")
        user_script = match.group(1)

        node_script = """
const assert = require("node:assert/strict");
const vm = require("node:vm");
const userScript = process.env.IOS_USER_SCRIPT;

const storage = new Map();
const localStorage = {
  getItem: k => storage.get(k) ?? null,
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: k => storage.delete(k),
};

const listeners = new Map();
const winListeners = new Map();
const messageField = {
  id: "message",
  value: "",
  dispatchEvent(ev) {
    ev.target = messageField;
    for (const fn of listeners.get(ev.type) || []) fn(ev);
  },
};
const headChildren = [];
const document = {
  head: { appendChild: el => headChildren.push(el) },
  getElementById: id => id === "message" ? messageField : null,
  createElement: () => ({ id: "", textContent: "" }),
  addEventListener: (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]),
};

class FakeEventSource {
  constructor(url) { this.url = url; this.readyState = 1; }
}

const context = {
  URL,
  Event: class { constructor(type, opts = {}) { this.type = type; this.bubbles = !!opts.bubbles; this.isTrusted = !!opts.isTrusted; } },
  EventSource: FakeEventSource,
  localStorage,
  document,
  location: { href: "https://mac.tail.ts.net/#thread-42", origin: "https://mac.tail.ts.net", hash: "#thread-42" },
  state: { selected: "thread-42", drafts: new Map() },
  setTimeout: fn => { fn(); return 1; },
  setInterval: () => 1,
  clearInterval: () => {},
  addEventListener: (type, fn) => winListeners.set(type, [...(winListeners.get(type) || []), fn]),
};
context.window = context;

vm.runInContext(userScript, vm.createContext(context));

// 1. EventSource for /api/browser/events is captured on window.agentCoordNativeEvents.
const es = new context.EventSource("/api/browser/events?after=0");
assert.equal(context.agentCoordNativeEvents, es);

// 2. Typing in #message mirrors per-thread draft into localStorage under agent-coord.draft:<thread_id>.
messageField.value = "Unsent mobile draft across Wi-Fi handoff";
for (const fn of listeners.get("input") || []) fn({ type: "input", target: messageField, isTrusted: true });
assert.equal(localStorage.getItem("agent-coord.draft:thread-42"), "Unsent mobile draft across Wi-Fi handoff");

// 3. Simulated WebContent process termination + page reload restores the draft into both state.drafts and #message.
messageField.value = "";
context.state.drafts.clear();
context.window = context;
const freshContext = {
  ...context,
  window: null,
};
freshContext.window = freshContext;
listeners.clear();
winListeners.clear();
vm.runInContext(userScript, vm.createContext(freshContext));
assert.equal(freshContext.agentCoordRestoreDraftBackup(), true);
assert.equal(messageField.value, "Unsent mobile draft across Wi-Fi handoff");
assert.equal(freshContext.state.drafts.get("thread-42"), "Unsent mobile draft across Wi-Fi handoff");

// 4. Submitting #composer after clearing #message removes the backup key so sent text never resurrects.
messageField.value = "";
for (const fn of listeners.get("submit") || []) fn({ type: "submit", target: { id: "composer" } });
assert.equal(localStorage.getItem("agent-coord.draft:thread-42"), null);
"""
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            env={**os.environ, "IOS_USER_SCRIPT": user_script},
        )
        self.assertEqual(proc.returncode, 0, f"iOS WKUserScript verification failed:\n{proc.stderr}")

    def test_ios_check_connection_silent_token_refresh_and_sse_reconnect(self):
        connection_source = (IOS_DIR / "ConnectionModel.swift").read_text(encoding="utf-8")
        match = re.search(
            r'let value = try await webView\.callAsyncJavaScript\("""\n(.*?)\n\s*""", arguments:',
            connection_source,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Could not extract checkConnection JS body")
        check_js = match.group(1)

        node_script = """
const assert = require("node:assert/strict");
const vm = require("node:vm");
const checkJS = process.env.IOS_CHECK_JS;

(async () => {
  // Scenario A: Closed EventSource + rotated CSRF token with classic connect() fallback.
  let connectCalls = 0;
  let closedOld = false;
  let receivedEvents = [];
  const state = { config: { token: "old-csrf", eventCursor: 99, completionCursor: 50 }, drafts: new Map([["t1", "unsent"]]) };
  const contextA = {
    AbortController,
    CustomEvent: class {
      constructor(type, init = {}) {
        this.type = type;
        this.detail = init.detail;
        this.cancelable = !!init.cancelable;
        this.defaultPrevented = false;
      }
      preventDefault() { this.defaultPrevented = true; }
    },
    setTimeout,
    clearTimeout,
    state,
    connect: () => { connectCalls += 1; },
    fetch: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ token: "new-csrf", eventCursor: 12, completionCursor: 7 }),
    }),
    agentCoordNativeEvents: { readyState: 2, close() { closedOld = true; } },
    dispatchEvent(ev) { receivedEvents.push(ev); return !ev.defaultPrevented; },
  };
  contextA.window = contextA;
  const runCheckA = vm.runInContext(`(async () => { ${checkJS} })`, vm.createContext(contextA));
  const resultA = await runCheckA();
  assert.equal(resultA.status, 200);
  assert.equal(resultA.reconnected, true);
  assert.equal(resultA.reload, undefined);
  assert.equal(state.config.token, "new-csrf");
  assert.equal(state.config.eventCursor, 12);
  assert.equal(state.config.completionCursor, 7);
  assert.equal(closedOld, true);
  assert.equal(connectCalls, 1);
  assert.equal(receivedEvents.length, 1);
  assert.equal(receivedEvents[0].type, "agent-coord-native-reconnect");
  assert.equal(receivedEvents[0].detail.token, "new-csrf");
  assert.equal(state.drafts.get("t1"), "unsent");

  // Scenario B: Bundled app exposing window.agentCoordReconnectEvents(config, {closed}).
  let hookCalls = [];
  const contextB = {
    ...contextA,
    agentCoordReconnectEvents: (cfg, meta) => hookCalls.push({ cfg, meta }),
  };
  contextB.window = contextB;
  const runCheckB = vm.runInContext(`(async () => { ${checkJS} })`, vm.createContext(contextB));
  const resultB = await runCheckB();
  assert.equal(resultB.status, 200);
  assert.equal(resultB.reconnected, true);
  assert.equal(hookCalls.length, 1);
  assert.equal(hookCalls[0].cfg.token, "new-csrf");
  assert.equal(hookCalls[0].meta.closed, true);
})().catch(err => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            env={**os.environ, "IOS_CHECK_JS": check_js},
        )
        self.assertEqual(proc.returncode, 0, f"checkConnection JS verification failed:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
