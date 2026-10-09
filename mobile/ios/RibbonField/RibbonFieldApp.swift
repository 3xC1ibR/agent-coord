import SwiftUI
import WebKit

@main
struct RibbonFieldApp: App {
    @UIApplicationDelegateAdaptor(NotificationAppDelegate.self) private var appDelegate
    @StateObject private var model = ConnectionModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .task { await model.start() }
                .onOpenURL { model.receive($0) }
                .onChange(of: scenePhase) { phase in
                    if phase == .active { Task { await model.foreground() } }
                }
        }
    }
}

private struct WebContent: UIViewRepresentable {
    let webView: WKWebView
    func makeUIView(context: Context) -> WKWebView { webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

struct ContentView: View {
    @ObservedObject var model: ConnectionModel
    @Environment(\.openURL) private var openURL
    @State private var confirmReload = false
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Ribbon Field").font(.headline)
                Spacer()
                Button { model.showingConnection = true } label: {
                    Image(systemName: "network").padding(8)
                }
                .accessibilityLabel("Connection settings")
                .accessibilityIdentifier("connection.settings")
            }.padding(.horizontal)
            if let message = model.message, !model.showingConnection {
                VStack(alignment: .leading, spacing: 8) {
                    Text(message).font(.callout).accessibilityIdentifier("connection.error")
                    if model.needsReload {
                        Button("Reload page") { confirmReload = true }
                            .accessibilityIdentifier("connection.reload")
                    } else {
                        Button("Try again") { Task { await model.retry() } }
                            .accessibilityIdentifier("connection.retry")
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding().background(.thinMaterial)
            }
            if let webView = model.webView {
                WebContent(webView: webView)
            } else {
                VStack(spacing: 20) {
                    Image(systemName: "laptopcomputer.and.iphone").font(.system(size: 48)).accessibilityHidden(true)
                    Text("Your Mac, within reach").font(.title2)
                    Text("Connect to Ribbon Field on your Mac to continue your conversations here.")
                        .multilineTextAlignment(.center)
                    Button("Connect to your Mac") { model.showingConnection = true }
                        .buttonStyle(.borderedProminent)
                }.padding(30).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .tint(Color(red: 0.13, green: 0.35, blue: 0.27))
        .sheet(isPresented: $model.showingConnection, onDismiss: { model.incomingInvitation = nil }) {
            ConnectionSheet(model: model)
        }
        .alert("Reload this page?", isPresented: $confirmReload) {
            Button("Reload", role: .destructive) { model.reloadPage() }
            Button("Keep page", role: .cancel) {}
        } message: { Text("Copy any unsent text first. Reloading keeps your current thread, but clears unsent text and attachments.") }
        .confirmationDialog("Open this link in your browser?", isPresented: Binding(
            get: { model.externalURL != nil }, set: { if !$0 { model.externalURL = nil } }
        ), titleVisibility: .visible) {
            if let url = model.externalURL {
                Button("Open \(url.host ?? "website")") { openURL(url); model.externalURL = nil }
            }
            Button("Cancel", role: .cancel) { model.externalURL = nil }
        } message: { Text("You are leaving your Mac’s Ribbon Field connection.") }
    }
}

private struct ConnectionSheet: View {
    @ObservedObject var model: ConnectionModel
    @State private var invitation = ""
    @State private var deviceName = "My iPhone"
    @State private var confirmDisconnect = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                if let server = model.server {
                    Section("Connected Mac") {
                        Text(server.origin).textSelection(.enabled)
                        Button("Check connection") { dismiss(); Task { await model.retry() } }
                            .accessibilityIdentifier("connection.check")
                        Button("Disconnect", role: .destructive) { confirmDisconnect = true }
                            .accessibilityIdentifier("connection.disconnect")
                    }
                }
                Section(model.server == nil ? "Connect to your Mac" : "Pair again") {
                    Text("On your Mac, open Ribbon Field → Remote access and create a pairing link. Connect both devices to the same Tailscale network.")
                    if let pending = model.incomingInvitation {
                        Text("Pair with \(pending.server.origin)")
                    } else {
                        SecureField("HTTPS pairing link", text: $invitation)
                            .textContentType(.none).keyboardType(.URL)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                            .accessibilityIdentifier("connection.invitation")
                    }
                    TextField("Device name", text: $deviceName)
                        .accessibilityIdentifier("connection.name")
                    Button {
                        let text = invitation
                        invitation = ""
                        Task { await model.pair(text: text, name: deviceName) }
                    } label: {
                        if model.busy { ProgressView() } else { Text("Pair this device") }
                    }
                    .disabled(model.busy || (invitation.isEmpty && model.incomingInvitation == nil))
                    .accessibilityIdentifier("connection.pair")
                }
                if let message = model.message {
                    Section { Text(message).foregroundStyle(.secondary).accessibilityIdentifier("connection.error") }
                }
                if model.server != nil { NotificationControls(notifications: model.notifications) }
                Section {
                    Text("Disconnect removes this app’s saved connection and website data. You can also revoke this device in Remote access on your Mac.")
                }
            }
            .navigationTitle("Connection")
            .toolbar { ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }.disabled(model.busy)
            } }
            .disabled(model.busy)
            .interactiveDismissDisabled(model.busy)
            .confirmationDialog("Disconnect from your Mac?", isPresented: $confirmDisconnect, titleVisibility: .visible) {
                Button("Disconnect and clear data", role: .destructive) {
                    invitation = ""
                    Task { await model.disconnect() }
                }.accessibilityIdentifier("connection.confirmDisconnect")
            } message: { Text("Cookies, cached pages and locally saved drafts will be removed from this app.") }
            .onDisappear { invitation = "" }
        }
    }
}

private struct NotificationControls: View {
    @ObservedObject var notifications: PushNotifications
    @Environment(\.openURL) private var openURL
    var body: some View {
        Section("Notifications") {
            Text(notifications.status).accessibilityIdentifier("notifications.status")
            if notifications.requested || notifications.enabled {
                Button("Turn off notifications") { Task { await notifications.disable() } }
                    .accessibilityIdentifier("notifications.disable")
                if notifications.enabled {
                    Button("Send test notification") { Task { await notifications.sendTest() } }
                        .accessibilityIdentifier("notifications.test")
                } else {
                    Button("Retry setup") { Task { await notifications.refresh() } }
                }
            } else {
                Button("Enable notifications") { Task { await notifications.enable() } }
                    .accessibilityIdentifier("notifications.enable")
            }
            if notifications.permissionDenied {
                Button("Open iOS Settings") { openURL(URL(string: UIApplication.openSettingsURLString)!) }
            }
        }.disabled(notifications.busy)
    }
}
