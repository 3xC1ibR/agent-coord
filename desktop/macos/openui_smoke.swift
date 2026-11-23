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
    private var portfolioRows: [Int] = []
    private var portfolioValidated = false
    private let startedAt = Date()
    private let portfolio = CommandLine.arguments.count > 2 && CommandLine.arguments[2] == "portfolio"

    func applicationDidFinishLaunching(_ notification: Notification) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "openuiSmoke")
        let driver = portfolio ? """
          let checking = false;
          const verify = (condition, message) => {
            if (!condition) window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'failure', message});
            return condition;
          };
          const activate = element => {
            element.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            element.click();
          };
          const timer = setInterval(async () => {
            const tabs = [...document.querySelectorAll('[role="tab"]')];
            if (checking || tabs.length !== 3) return;
            checking = true;
            clearInterval(timer);
            const rows = [], labels = ['Apify Multi-ATS Jobs', 'Cluster A:', 'Atlassian Forge:'];
            for (let index = 0; index < tabs.length; index++) {
              activate(tabs[index]);
              await new Promise(resolve => setTimeout(resolve, 150));
              let count = document.querySelectorAll('tbody tr').length;
              if (!verify(document.body.innerText.includes(labels[index]), 'Tab ' + index + ' has no expected content: ' + document.body.innerText.slice(-400))) return;
              if (index === 0) {
                const next = document.querySelector('[aria-label="Next page"]');
                if (!verify(next && !next.disabled, 'Portfolio pagination unavailable; visible rows: ' + count)) return;
                next.click();
                await new Promise(resolve => setTimeout(resolve, 150));
                count += document.querySelectorAll('tbody tr').length;
                if (!verify(document.body.innerText.includes('HIPAA X12 EDI 835'), 'Portfolio second page is missing its final candidate.')) return;
                document.querySelector('[aria-label="Previous page"]').click();
              }
              rows.push(count);
            }
            activate(tabs[0]);
            await new Promise(resolve => setTimeout(resolve, 150));
            const button = [...document.querySelectorAll('button')].find(item => item.textContent.startsWith('1. Build & Probe Stream 4:'));
            const valid = JSON.stringify(rows) === '[13,5,9]' && document.body.innerText.includes('87 -> 24 Scored');
            window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'portfolio', valid, rows});
            if (valid && button && !button.disabled) button.click();
          }, 100);
        """ : """
          const timer = setInterval(() => {
            const input = document.querySelector('input');
            const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'Send test response');
            if (!input || !button || button.disabled) return;
            clearInterval(timer);
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'WebKit round trip');
            input.dispatchEvent(new Event('input', {bubbles:true}));
            setTimeout(() => button.click(), 100);
          }, 100);
        """
        configuration.userContentController.addUserScript(WKUserScript(source: """
        document.addEventListener('securitypolicyviolation', event => {
          window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'csp', directive:event.violatedDirective, blocked:event.blockedURI});
        });
        if (location.pathname === '/openui-frame.html') {
          let isolated = false;
          try { void parent.document; } catch { isolated = true; }
          window.webkit.messageHandlers.openuiSmoke.postMessage({kind:'isolation', isolated});
          \(driver)
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
        if kind == "failure" { finish(error: body["message"] as? String ?? "Portfolio content check failed.") }
        if kind == "isolation" { isolated = body["isolated"] as? Bool == true }
        if kind == "portfolio" {
            portfolioValidated = body["valid"] as? Bool == true
            portfolioRows = body["rows"] as? [Int] ?? []
        }
        if kind == "result", message.frameInfo.isMainFrame {
            let event = body["event"] as? [String: Any]
            let state = event?["formState"] as? [String: Any]
            let form = state?["openui_test"] as? [String: Any]
            let note = form?["note"] as? [String: Any]
            submittedNote = note?["value"] as? String
            // The vendor probes eval support and handles its rejection. Keep
            // eval blocked; only blocked asset loads invalidate this check.
            let assetsLoaded = violations.allSatisfy { ($0 as? [String: Any])?["blocked"] as? String == "eval" }
            let portfolioAction = event?["humanFriendlyMessage"] as? String ?? ""
            let contentValid = portfolio
                ? portfolioValidated && portfolioAction.contains("Launch a YOLO builder/operator for Stream 4:")
                : submittedNote == "WebKit round trip"
            finish(error: isolated && assetsLoaded && contentValid
                   ? nil : "Content, action values, sandbox isolation, or CSP validation failed.")
        }
    }

    private func finish(error: String?) {
        guard !finished else { return }
        finished = true
        let report: [String: Any] = ["error": error as Any? ?? NSNull(), "isolated": isolated,
                                      "csp_violations": violations, "action_round_trip": error == nil,
                                      "form_round_trip": !portfolio && error == nil,
                                      "portfolio_rows": portfolioRows,
                                      "duration_ms": Date().timeIntervalSince(startedAt) * 1000,
                                      "submitted_note": submittedNote as Any? ?? NSNull()]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
        fflush(stdout)
        guard CommandLine.arguments.count > 3 else { exit(error == nil ? 0 : 1) }
        webView.takeSnapshot(with: nil) { image, _ in
            if let tiff = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
               let png = bitmap.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: CommandLine.arguments[3]))
            }
            exit(error == nil ? 0 : 1)
        }
    }
}

let application = NSApplication.shared
let smoke = OpenUISmoke()
application.delegate = smoke
application.setActivationPolicy(.accessory)
application.run()
