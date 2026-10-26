import AppKit
import WebKit

// A disposable native WebKit host. It neither opens the user's RF database nor
// starts a provider. The page serves the real renderer with the real HTTP CSP.
final class OpenUISmoke: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    private var webView: WKWebView!
    private var window: NSWindow!
    private var violations: [Any] = []
    private var isolated = false
    private var finished = false
    private var submittedNote: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "openuiSmoke")
        configuration.userContentController.addUserScript(WKUserScript(source: """
        document.addEventListener('securitypolicyviolation', event => {
          window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'csp', directive:event.violatedDirective, blocked:event.blockedURI});
        });
        if (location.pathname === '/openui-frame.html') {
          let isolated = false;
          try { void parent.document; } catch { isolated = true; }
          window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'isolation', isolated});
          const timer = setInterval(() => {
            const input = document.querySelector('input');
            const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'Send test response');
            if (!input || !button || button.disabled) return;
            clearInterval(timer);
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'WebKit round trip');
            input.dispatchEvent(new Event('input', {bubbles:true}));
            setTimeout(() => button.click(), 100);
          }, 100);
        }
        """, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 500), configuration: configuration)
        window = NSWindow(contentRect: webView.frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "OpenUI native regression check"
        window.contentView = webView
        window.orderFront(nil)
        webView.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) {
            self.finish(error: "OpenUI did not render and submit in WKWebView.")
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let kind = body["kind"] as? String else { return }
        if kind == "csp" { violations.append(body) }
        if kind == "isolation" { isolated = body["isolated"] as? Bool == true }
        if kind == "result", message.frameInfo.isMainFrame {
            let event = body["event"] as? [String: Any]
            let state = event?["formState"] as? [String: Any]
            let form = state?["openui_test"] as? [String: Any]
            let note = form?["note"] as? [String: Any]
            submittedNote = note?["value"] as? String
            // The vendor probes eval support and handles its rejection. Keep
            // eval blocked; only blocked asset loads invalidate this check.
            let assetsLoaded = violations.allSatisfy { ($0 as? [String: Any])?["blocked"] as? String == "eval" }
            finish(error: isolated && assetsLoaded && submittedNote == "WebKit round trip"
                   ? nil : "Form values, sandbox isolation, or CSP validation failed.")
        }
    }

    private func finish(error: String?) {
        guard !finished else { return }
        finished = true
        let report: [String: Any] = ["error": error as Any? ?? NSNull(), "isolated": isolated,
                                      "csp_violations": violations, "form_round_trip": error == nil,
                                      "submitted_note": submittedNote as Any? ?? NSNull()]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
        fflush(stdout)
        exit(error == nil ? 0 : 1)
    }
}

let application = NSApplication.shared
let smoke = OpenUISmoke()
application.delegate = smoke
application.setActivationPolicy(.accessory)
application.run()
