import XCTest
#if SWIFT_PACKAGE
@testable import RibbonFieldPolicy
#else
@testable import RibbonField
#endif

final class ConnectionPolicyTests: XCTestCase {
    func testSafeAddressCanonicalization() throws {
        let server = try ServerAddress("https://MAC.tail123.ts.net:443")
        XCTAssertEqual(server.url.absoluteString, "https://mac.tail123.ts.net/")
        XCTAssertEqual(server.origin, "https://mac.tail123.ts.net")
        XCTAssertEqual(try ServerAddress("https://mac.tail123.ts.net:8443/").origin, "https://mac.tail123.ts.net:8443")
    }
    func testOtherHTTPSOriginsAreAllowedWithoutTLSDowngrade() throws {
        for text in ["https://localhost:19443/", "https://192.168.1.10/", "https://[::1]:19443/", "https://example.com/"] {
            XCTAssertEqual(try ServerAddress(text).url.absoluteString, text)
        }
    }
    func testRejectUnsafeRememberedAddresses() {
        for text in ["http://mac.tail.ts.net", "https://user@mac.tail.ts.net", "https://mac.tail.ts.net/path", "https://mac.tail.ts.net?pair=secret", "https://mac.tail.ts.net/#pair=secret", "https://mac.tail.ts.net.", "https://mac..ts.net", "https://mac.tail.ts.net:0", "https://mac.tail.ts.net:65536", "https://%6dac.tail.ts.net", "https://mac.tail.ts.net\\@evil.test"] {
            XCTAssertThrowsError(try ServerAddress(text), text)
        }
    }
    func testInvitationSeparatesSecretFromPersistableOrigin() throws {
        let invitation = try PairingInvitation(" https://mac.tail.ts.net:8443/pair#pair=Abc_123-xyz \n")
        XCTAssertEqual(invitation.token, "Abc_123-xyz")
        XCTAssertEqual(invitation.server.url.absoluteString, "https://mac.tail.ts.net:8443/")
    }
    func testMalformedInvitations() {
        for text in ["https://mac.tail.ts.net/#pair=abc", "https://mac.tail.ts.net/pair?pair=abc", "https://mac.tail.ts.net/pair#pair=", "https://mac.tail.ts.net/pair#pair=a&pair=b", "https://mac.tail.ts.net/pair#pair=%61", "https://mac.tail.ts.net/%70air#pair=abc", "http://mac.tail.ts.net/pair#pair=abc", "https://u:p@mac.tail.ts.net/pair#pair=abc", "https://mac.tail.ts.net/pair#pair=" + String(repeating: "a", count: 101)] {
            XCTAssertThrowsError(try PairingInvitation(text), text)
        }
    }
    func testNavigationStaysOnExactOrigin() throws {
        let policy = NavigationPolicy(server: try ServerAddress("https://mac.tail.ts.net"))
        for text in ["https://mac.tail.ts.net/", "https://mac.tail.ts.net:443/#thread-1", "https://mac.tail.ts.net/monitor"] {
            XCTAssertEqual(policy.decide(URL(string: text)!, userActivated: false, mainFrame: true), .allow)
        }
        for text in ["https://mac.tail.ts.net:8443/", "https://other.tail.ts.net/", "https://mac.tail.ts.net.evil.test/"] {
            XCTAssertEqual(policy.decide(URL(string: text)!, userActivated: false, mainFrame: true), .block)
            XCTAssertEqual(policy.decide(URL(string: text)!, userActivated: true, mainFrame: false), .block)
            XCTAssertEqual(policy.decide(URL(string: text)!, userActivated: true, mainFrame: true), .external)
        }
    }
    func testDangerousNavigationNeverOpens() throws {
        let policy = NavigationPolicy(server: try ServerAddress("https://mac.tail.ts.net"))
        for text in ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,hi", "http://example.com", "agentcoord://thread/1", "https://u:p@example.com/", "https://mac.tail.ts.net/pair#pair=secret", "https://example.com/pair#pair=secret", "https://mac.tail.ts.net/#pair=secret"] {
            XCTAssertEqual(policy.decide(URL(string: text)!, userActivated: true, mainFrame: true), .block, text)
        }
    }
    func testDeepLinkOnlyAcceptsExplicitPairEnvelope() throws {
        var c = URLComponents(string: "agentcoord://pair")!
        c.queryItems = [URLQueryItem(name: "url", value: "https://mac.tail.ts.net/pair#pair=abc")]
        XCTAssertEqual(try PairingInvitation.fromDeepLink(c.url!).token, "abc")
        for text in ["agentcoord://thread/123", "agentcoord://pair?url=http://evil.test", "agentcoord://pair?url=a&url=b", "other://pair?url=a"] {
            XCTAssertThrowsError(try PairingInvitation.fromDeepLink(URL(string: text)!))
        }
    }
}
