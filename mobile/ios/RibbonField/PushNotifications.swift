import SwiftUI
import WebKit
import UserNotifications

@MainActor
final class PushNotifications: ObservableObject {
    static let shared = PushNotifications()
    @Published private(set) var enabled = false
    @Published private(set) var busy = false
    @Published private(set) var status = "Enable notifications for finished turns and requests for approval."
    @Published private(set) var permissionDenied = false
    var onOpen: ((NotificationDestination) -> Void)?
    private var pendingDestination: NotificationDestination?
    private var server: ServerAddress?
    private var store: WKWebsiteDataStore?
    private var token: String?
    private var registrationTask: Task<Void, Never>?
    private let preferenceKey = "agentcoord.notificationsOrigin"
    private let pendingRemovalKey = "agentcoord.notificationsPendingRemoval"
    var requested: Bool { wanted }
    private var wanted: Bool { server?.origin == UserDefaults.standard.string(forKey: preferenceKey) && server != nil }

    func connect(server: ServerAddress, store: WKWebsiteDataStore) async {
        self.server = server
        self.store = store
        await refresh()
        if let destination = pendingDestination, destination.server == server {
            pendingDestination = nil
            onOpen?(destination)
        }
    }

    func refresh() async {
        guard server != nil, !busy else { return }
        if UserDefaults.standard.string(forKey: pendingRemovalKey) == server?.origin {
            await disable()
            return
        }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        permissionDenied = settings.authorizationStatus == .denied
        if wanted, permissionDenied { await disable(); status = "Notifications are off in iOS Settings."; return }
        if wanted, settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional {
            UIApplication.shared.registerForRemoteNotifications()
            if let token { receivedToken(token) }
        }
    }

    func enable() async {
        guard !busy, server != nil else { return }
        busy = true
        defer { busy = false }
        do {
            if UserDefaults.standard.string(forKey: pendingRemovalKey) == server?.origin {
                _ = try await api("unsubscribe", body: [:])
                UserDefaults.standard.removeObject(forKey: pendingRemovalKey)
            }
            let data = try await api("status")
            let result = try JSONDecoder().decode(NativePushStatus.self, from: data)
            guard result.available else { throw NativePushError.unavailable }
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            guard granted else { permissionDenied = true; status = "Allow notifications in iOS Settings to receive alerts."; return }
            UserDefaults.standard.set(server?.origin, forKey: preferenceKey)
            status = "Registering this device with Apple…"
            UIApplication.shared.registerForRemoteNotifications()
            // Registration callback uploads the current token; it is never persisted locally.
        } catch { status = (error as? NativePushError)?.localizedDescription ?? "Could not enable notifications. Try again." }
    }

    func restorePreference() async {
        guard let server else { return }
        UserDefaults.standard.set(server.origin, forKey: preferenceKey)
        await refresh()
    }

    func receivedToken(_ value: String) {
        token = value
        guard wanted, let server, registrationTask == nil else { return }
        let profile = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision").flatMap { try? Data(contentsOf: $0) }
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        guard let environment = APNsEnvironment.resolve(profile: profile, simulator: simulator) else {
            status = "This app needs a provisioning profile with push notifications enabled."
            return
        }
        registrationTask = Task {
            defer {
                registrationTask = nil
                if wanted, let latest = token, latest != value { receivedToken(latest) }
            }
            do {
                let data = try await api("subscribe", body: ["registration": ["token": value, "environment": environment]])
                guard wanted, self.server == server else { return }
                let result = try JSONDecoder().decode(NativePushStatus.self, from: data)
                enabled = result.enabled
                status = result.error.isEmpty ? "Notifications are on for this Mac." : result.error
            } catch {
                if wanted, self.server == server { status = (error as? NativePushError)?.localizedDescription ?? "Could not register notifications." }
            }
        }
    }

    func registrationFailed() {
        guard wanted else { return }
        status = "Apple could not register this device. Check your connection and the app’s push signing, then try again."
    }

    /// Stops OS delivery immediately and attempts to remove the server enrollment before cookies are cleared.
    @discardableResult
    func disable() async -> Bool {
        let removeRegistration = wanted || enabled || (server != nil && UserDefaults.standard.string(forKey: pendingRemovalKey) == server?.origin)
        busy = true
        defer { busy = false }
        if removeRegistration { UserDefaults.standard.set(server?.origin, forKey: pendingRemovalKey) }
        UserDefaults.standard.removeObject(forKey: preferenceKey)
        UIApplication.shared.unregisterForRemoteNotifications()
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        UNUserNotificationCenter.current().removeAllPendingNotificationRequests()
        pendingDestination = nil
        enabled = false
        await registrationTask?.value
        do {
            if removeRegistration, server != nil { _ = try await api("unsubscribe", body: [:]) }
            UserDefaults.standard.removeObject(forKey: pendingRemovalKey)
            status = "Notifications are off."
            return true
        } catch {
            status = "Notifications are off on this iPhone. Reconnect to remove the Mac’s registration, or revoke this device on your Mac."
            return false
        }
    }

    func disconnect() async -> Bool {
        let removed = await disable()
        // After cookie deletion, only Mac-side revocation can remove an unreachable old grant.
        UserDefaults.standard.removeObject(forKey: pendingRemovalKey)
        server = nil
        store = nil
        token = nil
        return removed
    }

    func sendTest() async {
        guard enabled, !busy else { return }
        busy = true
        defer { busy = false }
        do { _ = try await api("test", body: [:]); status = "Test notification queued. It should arrive shortly." }
        catch { status = (error as? NativePushError)?.localizedDescription ?? "Could not send a test notification." }
    }

    func accepts(_ payload: [AnyHashable: Any]) -> Bool {
        guard let destination = NotificationDestination(payload: payload),
              let saved = UserDefaults.standard.string(forKey: "agentcoord.serverOrigin"),
              let address = try? ServerAddress(saved), destination.server == address,
              UserDefaults.standard.string(forKey: preferenceKey) == address.origin else { return false }
        return true
    }

    func open(_ payload: [AnyHashable: Any]) {
        guard accepts(payload), let destination = NotificationDestination(payload: payload) else { return }
        if server == destination.server { onOpen?(destination) }
        else { pendingDestination = destination }
    }

    private func api(_ action: String, body: [String: Any]? = nil) async throws -> Data {
        guard let server, let store else { throw NativePushError.authorization }
        return try await NativePushClient().call(server: server, cookies: store.httpCookieStore.allCookies(), action: action, body: body)
    }
}

final class NotificationAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in PushNotifications.shared.receivedToken(token) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushNotifications.shared.registrationFailed() }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in completionHandler(PushNotifications.shared.accepts(notification.request.content.userInfo) ? [.banner, .sound] : []) }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        Task { @MainActor in
            PushNotifications.shared.open(response.notification.request.content.userInfo)
            completionHandler()
        }
    }
}
