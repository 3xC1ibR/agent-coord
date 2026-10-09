import SwiftUI
import WebKit

@MainActor
final class ConnectionModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    @Published private(set) var server: ServerAddress?
    @Published private(set) var webView: WKWebView?
    @Published var showingConnection = false
    @Published var message: String?
    @Published var busy = false
    @Published var needsReload = false
    @Published var externalURL: URL?
    @Published var incomingInvitation: PairingInvitation?
    let notifications = PushNotifications.shared
    private var notificationDestination: NotificationDestination?
    private let defaults: UserDefaults
    private let store = WKWebsiteDataStore.default()
    private let addressKey = "agentcoord.serverOrigin"
    private let clearingKey = "agentcoord.disconnectPending"
    private var hasPage = false
    private var terminated = false
    private var checking = false
    private var started = false

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        super.init()
        notifications.onOpen = { [weak self] destination in
            guard let self, self.server == destination.server else { return }
            self.notificationDestination = destination
            Task { await self.openPendingNotification() }
        }
    }

    func start() async {
        guard !started else { return }
        started = true
        busy = true
        if defaults.bool(forKey: clearingKey) || defaults.string(forKey: addressKey) == nil {
            await clearWebsiteData()
            defaults.removeObject(forKey: clearingKey)
        }
        if let saved = defaults.string(forKey: addressKey), let address = try? ServerAddress(saved) {
            server = address
            createWebView()
            loadHome()
            await notifications.connect(server: address, store: store)
        } else {
            defaults.removeObject(forKey: addressKey)
            await clearWebsiteData()
            showingConnection = true
        }
        busy = false
    }

    func pair(text: String, name: String) async {
        guard !busy else { return }
        busy = true
        message = nil
        defer { busy = false }
        let restoreNotifications = notifications.requested
        do {
            let invitation = try incomingInvitation ?? PairingInvitation(text)
            incomingInvitation = nil
            if let server, server != invitation.server { throw ConnectionError.differentServer }
            let deviceName = name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard (1...80).contains(deviceName.count) else {
                message = "Give this device a name of 1–80 characters."
                return
            }
            if restoreNotifications { await notifications.disable() }
            let cookie = try await PairingClient().redeem(invitation, name: deviceName)
            // Start WebKit before installing its cookie; an unused store can defer this callback.
            if webView == nil { createWebView() }
            await store.httpCookieStore.setCookie(cookie)
            server = invitation.server
            defaults.set(invitation.server.url.absoluteString, forKey: addressKey)
            showingConnection = false
            // Re-pairing the same origin preserves a loaded conversation and its draft.
            if webView == nil { createWebView() }
            if !hasPage || terminated { loadHome() } else { requireReload() }
            await notifications.connect(server: invitation.server, store: store)
        } catch let error as ConnectionError {
            message = error.localizedDescription
        } catch {
            // URLSession errors can contain request details: never display or log raw errors.
            message = "Could not reach your Mac. Check Tailscale, remote access, and the Mac’s connection, then use a fresh pairing link."
        }
        if restoreNotifications { await notifications.restorePreference() }
    }

    func disconnect() async {
        guard !busy else { return }
        busy = true
        // Durable marker makes an interrupted deletion resume at next launch.
        defaults.set(true, forKey: clearingKey)
        defaults.removeObject(forKey: addressKey)
        let removedNotifications = await notifications.disconnect()
        notificationDestination = nil
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView?.uiDelegate = nil
        webView = nil
        server = nil
        incomingInvitation = nil
        externalURL = nil
        hasPage = false
        terminated = false
        needsReload = false
        await clearWebsiteData()
        defaults.removeObject(forKey: clearingKey)
        message = removedNotifications ? nil : "Disconnected. Your Mac could not confirm notification removal; revoke this device in Remote access on your Mac."
        showingConnection = true
        busy = false
    }

    private func clearWebsiteData() async {
        // This app has one connection; erase cookies, caches, local storage and service workers.
        await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
        for cookie in await store.httpCookieStore.allCookies() {
            await store.httpCookieStore.deleteCookie(cookie)
        }
    }

    private func createWebView() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        // No script message bridge exposes native privileges to server content.
        let script = """
        (() => {
          const OriginalEventSource = window.EventSource;
          if (OriginalEventSource) window.EventSource = class extends OriginalEventSource {
            constructor(url, options) {
              super(url, options);
              const address = new URL(url, location.href);
              if (address.origin === location.origin && address.pathname === '/api/browser/events')
                window.agentCoordNativeEvents = this;
            }
          };
          const install = () => {
            if (!document.head || document.getElementById('native-notification-style')) return;
            const style = document.createElement('style'); style.id = 'native-notification-style';
            style.textContent = '#notifications, #notifications + button, #notifications ~ small { display:none !important; }';
            document.head.appendChild(style);
          };
          install(); document.addEventListener('DOMContentLoaded', install, {once:true});
        })();
        """
        configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = false
        view.isOpaque = false
        view.backgroundColor = .systemBackground
        webView = view
    }

    private func loadHome() {
        guard let server else { return }
        message = nil
        terminated = false
        needsReload = false
        hasPage = false
        webView?.load(URLRequest(url: server.url))
    }

    private func requireReload() {
        needsReload = true
        message = "Your connection is restored, but this page needs to reload before you continue. Copy any unsent text first."
    }

    func reloadPage() {
        needsReload = false
        message = nil
        hasPage = false
        terminated = false
        webView?.reload() // Keeps the current thread URL; explicitly confirmed by the user.
    }

    func retry() async {
        if terminated || !hasPage { loadHome() } else { await checkConnection() }
    }

    func foreground() async {
        guard !busy, server != nil else { return }
        await notifications.refresh()
        // A terminated process cannot retain DOM drafts; require explicit recovery in that case.
        if terminated { return }
        if !hasPage, webView?.isLoading == false { loadHome() }
        else if hasPage { await checkConnection() }
    }

    private func checkConnection() async {
        guard !checking, let webView, hasPage, !terminated else { return }
        checking = true
        defer { checking = false }
        do {
            let value = try await webView.callAsyncJavaScript("""
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 10000);
                try {
                  const r = await fetch('/api/browser/config', {cache:'no-store', signal:controller.signal});
                  if (!r.ok) return {status:r.status, reload:false};
                  const config = await r.json();
                  const changed = typeof state !== 'undefined' && state.config && state.config.token !== config.token;
                  const closed = window.agentCoordNativeEvents?.readyState === 2;
                  return {status:r.status, reload:!!(changed || closed)};
                }
                finally { clearTimeout(timer); }
                """, arguments: [:], in: nil, contentWorld: .page)
            guard self.webView === webView, !terminated else { return }
            let result = value as? [String: Any]
            let status = result?["status"] as? Int ?? 0
            if status == 401 || status == 403 {
                message = "This device needs a fresh pairing link from your Mac. Your open page is kept while you reconnect."
                showingConnection = true
            } else if (200...299).contains(status) {
                if result?["reload"] as? Bool == true || needsReload { requireReload() }
                else { message = nil; await openPendingNotification() }
            } else {
                message = "Your Mac is unavailable. Check Tailscale and remote access, then try again."
            }
        } catch {
            guard self.webView === webView, !terminated else { return }
            message = "Connection interrupted. Your open page is kept. Check Tailscale and your Mac, then try again."
        }
    }

    private func openPendingNotification() async {
        guard let destination = notificationDestination, destination.server == server,
              hasPage, !terminated, !needsReload, let webView else { return }
        do {
            if webView.url?.path == "/" {
                _ = try await webView.callAsyncJavaScript("window.location.hash = fragment;", arguments: ["fragment": destination.fragment], in: nil, contentWorld: .page)
            } else {
                var address = URLComponents(url: destination.server.url, resolvingAgainstBaseURL: false)!
                address.fragment = destination.fragment.isEmpty ? nil : destination.fragment
                webView.load(URLRequest(url: address.url!))
            }
            notificationDestination = nil
            showingConnection = false
        } catch { message = "Reconnect to your Mac to open this notification." }
    }

    func receive(_ url: URL) {
        do {
            let invitation = try PairingInvitation.fromDeepLink(url)
            if let server, server != invitation.server { throw ConnectionError.differentServer }
            incomingInvitation = invitation
            showingConnection = true
            message = nil
        } catch {
            message = "This link cannot open here. Paste a fresh HTTPS pairing link to connect; Mac-only links stay on your Mac."
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let server, let url = action.request.url else { decisionHandler(.cancel); return }
        // The shared web UI reloads on 401; retain its draft until native re-pair/reload confirmation.
        if action.navigationType == .reload && hasPage {
            requireReload()
            decisionHandler(.cancel)
            Task { await checkConnection() }
            return
        }
        let decision = NavigationPolicy(server: server).decide(url,
            userActivated: action.navigationType == .linkActivated,
            mainFrame: action.targetFrame?.isMainFrame ?? true)
        switch decision {
        case .allow:
            if action.targetFrame == nil {
                decisionHandler(.cancel)
                webView.load(action.request)
            } else { decisionHandler(.allow) }
        case .external:
            externalURL = url
            decisionHandler(.cancel)
        case .block:
            if action.navigationType == .linkActivated { message = "This link cannot open inside Ribbon Field. Use your Mac for desktop links and files." }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let server, let url = response.response.url, server.contains(url), response.canShowMIMEType else {
            decisionHandler(.cancel); return
        }
        if response.isForMainFrame, let http = response.response as? HTTPURLResponse, http.statusCode >= 400 {
            message = "The Mac could not open this page. Check remote access and try again."
            if http.statusCode == 401 || http.statusCode == 403 { showingConnection = true }
            decisionHandler(.cancel)
        } else { decisionHandler(.allow) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard self.webView === webView else { return }
        hasPage = true
        Task {
            // The server returns its pairing HTML with 200 when a cookie expires at launch.
            let isPairPage = try? await webView.evaluateJavaScript("!!document.getElementById('pair-link-field')") as? Bool
            guard self.webView === webView, !terminated else { return }
            if isPairPage == true {
                hasPage = false
                showingConnection = true
                message = "Pair this device using a fresh link from your Mac."
            } else { await checkConnection() }
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
    private func failed(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        message = "Could not load your Mac. Check that Tailscale is connected and Ribbon Field is running, then try again."
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        terminated = true
        message = "iOS closed this page to free memory. Reopen it to continue. Unsaved text may need to be entered again."
    }
}
