import XCTest
#if SWIFT_PACKAGE
@testable import RibbonFieldPolicy
#else
@testable import RibbonField
#endif

final class PairingProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, [String: String]))?
    static var responseBody = Data("{}".utf8)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, headers) = try Self.handler!(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Self.responseBody)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

final class PairingClientTests: XCTestCase {
    private let cookie = "__Host-agent-coord-device=credential; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600"
    override func tearDown() { PairingProtocol.handler = nil; PairingProtocol.responseBody = Data("{}".utf8); super.tearDown() }

    func testNativeRedemptionUsesOriginAndBodyNeverSecretURL() async throws {
        PairingProtocol.handler = { request in
            XCTAssertEqual(request.url?.absoluteString, "https://mac.tail.ts.net/api/remote/pair")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "https://mac.tail.ts.net")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            var data = request.httpBody ?? Data()
            if let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 1024)
                while stream.hasBytesAvailable {
                    let count = stream.read(&bytes, maxLength: bytes.count)
                    if count <= 0 { break }
                    data.append(contentsOf: bytes.prefix(count))
                }
            }
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: String])
            XCTAssertEqual(body, ["token":"invitation", "name":"Test iPhone"])
            return (200, ["Set-Cookie": self.cookie])
        }
        let result = try await PairingClient(protocolClasses: [PairingProtocol.self]).redeem(
            PairingInvitation("https://mac.tail.ts.net/pair#pair=invitation"), name: "Test iPhone")
        XCTAssertEqual(result.name, "__Host-agent-coord-device")
        XCTAssertEqual(result.domain, "mac.tail.ts.net")
        XCTAssertTrue(result.isHTTPOnly)
        XCTAssertTrue(result.isSecure)
    }

    func testInvalidCredentialsAreRejected() async throws {
        for header in ["", "other=value; Secure; HttpOnly; Path=/; Max-Age=3600",
                       "__Host-agent-coord-device=x; Path=/; HttpOnly; Max-Age=3600",
                       "__Host-agent-coord-device=x; Path=/; Secure; Max-Age=3600",
                       "__Host-agent-coord-device=x; Path=/other; Secure; HttpOnly; Max-Age=3600",
                       "__Host-agent-coord-device=x; Domain=other.test; Path=/; Secure; HttpOnly; Max-Age=3600"] {
            PairingProtocol.handler = { _ in (200, ["Set-Cookie":header]) }
            do {
                _ = try await PairingClient(protocolClasses: [PairingProtocol.self]).redeem(
                    PairingInvitation("https://mac.tail.ts.net/pair#pair=invitation"), name: "Test")
                XCTFail("Accepted an invalid credential")
            } catch { XCTAssertTrue(error is ConnectionError) }
        }
    }

    func testUnsuccessfulResponseIsNotPaired() async throws {
        for status in [302, 400, 401, 403, 500] {
            PairingProtocol.handler = { _ in (status, ["Set-Cookie":self.cookie]) }
            do {
                _ = try await PairingClient(protocolClasses: [PairingProtocol.self]).redeem(
                    PairingInvitation("https://mac.tail.ts.net/pair#pair=invitation"), name: "Test")
                XCTFail("Accepted unsuccessful status \(status)")
            } catch { XCTAssertTrue(error is ConnectionError) }
        }
    }

    func testRedirectDelegateNeverForwardsInvitation() {
        let client = PairingClient()
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let source = URL(string: "https://mac.tail.ts.net/api/remote/pair")!
        for destination in [source, URL(string:"https://evil.test/steal")!] {
            let response = HTTPURLResponse(url: source, statusCode: 307, httpVersion: nil, headerFields: ["Location":destination.absoluteString])!
            let task = session.dataTask(with: source)
            var called = false
            client.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: URLRequest(url: destination)) { result in
                called = true
                XCTAssertNil(result)
            }
            XCTAssertTrue(called)
            task.cancel()
        }
    }
}
