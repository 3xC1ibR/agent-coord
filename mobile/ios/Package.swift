// swift-tools-version: 5.9
import PackageDescription
let package = Package(
    name: "RibbonFieldPolicy",
    platforms: [.macOS(.v13), .iOS("16.4")],
    products: [.library(name: "RibbonFieldPolicy", targets: ["RibbonFieldPolicy"])],
    targets: [
        .target(name: "RibbonFieldPolicy", path: "RibbonField", exclude: ["RibbonFieldApp.swift", "ConnectionModel.swift", "Info.plist", "Assets.xcassets", "PushNotifications.swift", "RibbonField.entitlements"], sources: ["ConnectionPolicy.swift", "PairingClient.swift", "NotificationPolicy.swift", "NativePushClient.swift"]),
        .testTarget(name: "RibbonFieldPolicyTests", dependencies: ["RibbonFieldPolicy"], path: "Tests")
    ]
)
