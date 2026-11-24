import Network
import SwiftUI
import WebKit

@MainActor
final class ConnectionModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    @Published private(set) var server: ServerAddress?
    @Published private(set) var webView: WKWebView?
    @Published var showingConnection = false
    @Published var message: String?
    @Published var busy = false
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
    private var queuedCheck: Bool?
    private var started = false
    private var active = false
    private let network = NWPathMonitor()

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
        network.pathUpdateHandler = { [weak self] path in
            guard path.status == .satisfied else { return }
            Task { @MainActor [weak self] in
                guard let self, self.active else { return }
                await self.retryConnection(quietly: true)
            }
        }
        network.start(queue: DispatchQueue(label: "com.agentcoord.ios.connection"))
        busy = true
        let clearing = defaults.bool(forKey: clearingKey)
        if clearing {
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
            if !clearing { await clearWebsiteData() }
            showingConnection = true
        }
        busy = false
    }

    deinit { network.cancel() }

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
            if !hasPage || terminated { loadSavedPage() } else { await checkConnection() }
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
        view.allowsBackForwardNavigationGestures = true
        view.isOpaque = false
        view.backgroundColor = .systemBackground
        webView = view
    }

    private func loadHome() { loadSavedPage(preserveLocation: false) }

    private func loadSavedPage(preserveLocation: Bool = true) {
        guard let server else { return }
        let location = webView?.url.flatMap { server.contains($0) && $0.path != "/pair" ? $0 : nil }
        message = nil
        terminated = false
        hasPage = false
        webView?.load(URLRequest(url: (preserveLocation ? location : nil) ?? server.url))
    }

    func retry() async {
        if terminated { loadSavedPage() }
        else { await retryConnection(quietly: false) }
    }

    private func retryConnection(quietly: Bool) async {
        guard !busy, server != nil, !terminated else { return }
        if hasPage { await checkConnection(quietly: quietly) }
        else if webView?.isLoading == false { loadSavedPage() }
    }

    func background() { active = false }

    func foreground() async {
        active = true
        guard !busy, server != nil else { return }
        await retryConnection(quietly: true)
        await notifications.refresh()
    }

    private func checkConnection(quietly: Bool = false) async {
        guard let webView, hasPage, !terminated else { return }
        if checking { queuedCheck = (queuedCheck ?? true) && quietly; return }
        checking = true
        defer {
            checking = false
            if let queuedCheck {
                self.queuedCheck = nil
                Task { await checkConnection(quietly: queuedCheck) }
            }
        }
        do {
            let value = try await webView.callAsyncJavaScript("""
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 10000);
                try {
                  const r = await fetch('/api/browser/config', {cache:'no-store', signal:controller.signal});
                  if (!r.ok) return r.status;
                  const config = await r.json();
                  if (typeof window.agentCoordReconnect === 'function') await window.agentCoordReconnect(config);
                  else if (typeof state !== 'undefined' && state.config && typeof config.token === 'string') {
                    // A companion update can be installed before its Mac web assets.
                    state.config.token = config.token;
                    if (window.agentCoordNativeEvents?.readyState === EventSource.CLOSED && typeof connect === 'function') connect();
                  }
                  return r.status;
                }
                finally { clearTimeout(timer); }
                """, arguments: [:], in: nil, contentWorld: .page)
            guard self.webView === webView, !terminated else { return }
            let status = value as? Int ?? 0
            if status == 401 || status == 403 {
                message = "This device needs a fresh pairing link from your Mac. Your open page is kept while you reconnect."
                showingConnection = true
            } else if (200...299).contains(status) {
                message = nil
                await openPendingNotification()
            } else if !quietly {
                message = "Your Mac is unavailable. Check Tailscale and remote access, then try again."
            }
        } catch {
            guard self.webView === webView, !terminated, !quietly else { return }
            message = "Connection interrupted. Your open page is kept. Check Tailscale and your Mac, then try again."
        }
    }

    private func openPendingNotification() async {
        guard let destination = notificationDestination, destination.server == server,
              hasPage, !terminated, let webView else { return }
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
        // Older Mac web assets reload on 401. Check the paired grant in place.
        if action.navigationType == .reload && hasPage {
            decisionHandler(.cancel)
            Task { await checkConnection(quietly: true) }
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
        // WebKit can finish its initial empty document while pairing is still
        // installing the cookie. Only a page from the selected Mac is reusable.
        guard self.webView === webView, let server, let url = webView.url, server.contains(url) else { return }
        hasPage = true
        Task {
            // The server returns its pairing HTML with 200 when a cookie expires at launch.
            let isPairPage = try? await webView.evaluateJavaScript("!!document.getElementById('pair-link-field')") as? Bool
            guard self.webView === webView, !terminated else { return }
            if isPairPage == true {
                hasPage = false
                showingConnection = true
                message = "Pair this device using a fresh link from your Mac."
            } else { await checkConnection(quietly: true) }
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
    private func failed(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        message = "Could not load your Mac. Check that Tailscale is connected and Ribbon Field is running, then try again."
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard self.webView === webView else { return }
        terminated = true
        message = "iOS closed this page to free memory. Reopen it to continue."
    }
}
