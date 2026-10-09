import XCTest
#if SWIFT_PACKAGE
@testable import RibbonFieldPolicy
#else
@testable import RibbonField
#endif

final class NotificationPolicyTests: XCTestCase {
    func testNotificationRoutesAreBoundToSafeOriginsAndThreadFragments() throws {
        let payload: [AnyHashable: Any] = ["agentcoord": ["origin":"https://mac.tail.ts.net", "url":"/#thread-1.a_~"]]
        let destination = try XCTUnwrap(NotificationDestination(payload: payload))
        XCTAssertEqual(destination.server, try ServerAddress("https://mac.tail.ts.net/"))
        XCTAssertEqual(destination.fragment, "thread-1.a_~")
        XCTAssertNotEqual(destination.server, try ServerAddress("https://other.tail.ts.net/"))
        XCTAssertEqual(NotificationDestination(payload: ["agentcoord": ["origin":"https://mac.tail.ts.net", "url":"/"]])?.fragment, "")
        for route in ["https://evil.test/", "//evil.test", "/pair#pair=secret", "/#a/b", "/#%22", "/?token=x", "/#", "/#" + String(repeating:"x",count:201)] {
            XCTAssertNil(NotificationDestination(payload: ["agentcoord": ["origin":"https://mac.tail.ts.net", "url":route]]), route)
        }
        for origin in ["http://mac.tail.ts.net", "https://u:p@mac.tail.ts.net", "https://mac.tail.ts.net/path", "https://mac.tail.ts.net/#pair=secret"] {
            XCTAssertNil(NotificationDestination(payload: ["agentcoord": ["origin":origin, "url":"/#thread"]]))
        }
    }

    func testEnvironmentUsesProvisioningEntitlementAndStoreFallback() throws {
        for environment in ["development", "production"] {
            let plist = try PropertyListSerialization.data(fromPropertyList: ["Entitlements":["aps-environment":environment]], format:.xml, options:0)
            var profile = Data([0x30,0x82,0x01,0x00]); profile.append(plist); profile.append(Data([0,1,2]))
            XCTAssertEqual(APNsEnvironment.resolve(profile: profile, simulator:false), environment)
        }
        XCTAssertEqual(APNsEnvironment.resolve(profile:nil, simulator:false), "production")
        XCTAssertEqual(APNsEnvironment.resolve(profile:nil, simulator:true), "development")
        XCTAssertNil(APNsEnvironment.resolve(profile:Data("invalid".utf8), simulator:false))
        let missing = try PropertyListSerialization.data(fromPropertyList: ["Entitlements":[:]], format:.xml, options:0)
        XCTAssertNil(APNsEnvironment.resolve(profile:missing, simulator:false))
    }
}

final class NativePushClientTests: XCTestCase {
    override func tearDown() { PairingProtocol.handler = nil; PairingProtocol.responseBody = Data("{}".utf8); super.tearDown() }
    private func cookie() -> HTTPCookie {
        HTTPCookie.cookies(withResponseHeaderFields:["Set-Cookie":"__Host-agent-coord-device=credential; Path=/; Secure; HttpOnly; Max-Age=3600"], for:URL(string:"https://mac.tail.ts.net")!)[0]
    }
    func testRegistrationObtainsFreshCSRFAndUsesOnlyPairedCookie() async throws {
        var paths: [String] = []
        PairingProtocol.handler = { request in
            paths.append(request.url!.path)
            XCTAssertEqual(request.value(forHTTPHeaderField:"Origin"), "https://mac.tail.ts.net")
            XCTAssertEqual(request.value(forHTTPHeaderField:"Cookie"), "__Host-agent-coord-device=credential")
            if request.url!.path == "/api/browser/config" {
                // PairingProtocol supplies a static body; exercise auth failure separately below.
                return (401, [:])
            }
            XCTFail("A failed config fetch must prevent registration")
            return (200, [:])
        }
        do {
            _ = try await NativePushClient(protocolClasses:[PairingProtocol.self]).call(
                server:ServerAddress("https://mac.tail.ts.net"), cookies:[cookie()], action:"subscribe",
                body:["registration":["token":String(repeating:"ab",count:32), "environment":"development"]])
            XCTFail("Should require current CSRF")
        } catch { XCTAssertTrue(error is NativePushError) }
        XCTAssertEqual(paths, ["/api/browser/config"])
    }
    func testRegistrationPostsFreshCSRFWithCurrentDeviceToken() async throws {
        var paths: [String] = []
        PairingProtocol.handler = { request in
            paths.append(request.url!.path)
            if request.url!.path == "/api/browser/config" {
                PairingProtocol.responseBody = Data("{\"token\":\"fresh-csrf\"}".utf8)
            } else {
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(request.value(forHTTPHeaderField: "X-Agent-Coord-Token"), "fresh-csrf")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "https://mac.tail.ts.net")
                PairingProtocol.responseBody = Data("{\"available\":true,\"enabled\":true,\"error\":\"\"}".utf8)
            }
            return (200, [:])
        }
        let data = try await NativePushClient(protocolClasses: [PairingProtocol.self]).call(
            server: ServerAddress("https://mac.tail.ts.net"), cookies: [cookie()], action: "subscribe",
            body: ["registration": ["token": String(repeating: "ab", count: 32), "environment": "development"]])
        XCTAssertTrue(try JSONDecoder().decode(NativePushStatus.self, from: data).enabled)
        XCTAssertEqual(paths, ["/api/browser/config", "/api/browser/native-push/subscribe"])
    }

    func testStatusUsesBoundOriginAndCannotRedirect() async throws {
        PairingProtocol.handler = { request in
            XCTAssertEqual(request.url?.absoluteString, "https://mac.tail.ts.net/api/browser/native-push/status")
            XCTAssertEqual(request.httpMethod, "GET")
            return (302, ["Location":"https://other.tail.ts.net/"])
        }
        do {
            _ = try await NativePushClient(protocolClasses:[PairingProtocol.self]).call(
                server:ServerAddress("https://mac.tail.ts.net"), cookies:[cookie()], action:"status")
            XCTFail("Redirect must not be followed")
        } catch { XCTAssertTrue(error is NativePushError) }
        let client = NativePushClient()
        let session = URLSession(configuration:.ephemeral)
        defer { session.invalidateAndCancel() }
        let url = URL(string:"https://mac.tail.ts.net")!
        let task = session.dataTask(with:url)
        client.urlSession(session, task:task, willPerformHTTPRedirection:HTTPURLResponse(url:url,statusCode:307,httpVersion:nil,headerFields:nil)!, newRequest:URLRequest(url:URL(string:"https://evil.test")!)) {
            XCTAssertNil($0)
        }
        task.cancel()
    }
    func testUnpairedRequestsNeverLeaveDevice() async throws {
        PairingProtocol.handler = { _ in XCTFail("Unpaired request sent"); return (200,[:]) }
        do {
            _ = try await NativePushClient(protocolClasses:[PairingProtocol.self]).call(server:ServerAddress("https://other.tail.ts.net"), cookies:[cookie()], action:"status")
            XCTFail("Foreign cookies cannot authorize requests")
        } catch { XCTAssertTrue(error is NativePushError) }
    }
}
