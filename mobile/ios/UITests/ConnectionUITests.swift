import XCTest

final class ConnectionUITests: XCTestCase {
    @MainActor
    func testPairRelaunchDraftAndDisconnect() throws {
        continueAfterFailure = false
        guard let fixture = ProcessInfo.processInfo.environment["RIBBON_SMOKE_URL"],
              fixture.hasPrefix("https://"), let origin = URL(string: fixture) else {
            throw XCTSkip("Set RIBBON_SMOKE_URL to an isolated trusted HTTPS backend fixture.")
        }
        let fetched = expectation(description: "Fetch isolated fixture invitation")
        var result: Result<Data, Error>?
        URLSession.shared.dataTask(with: origin.appendingPathComponent("__smoke__/invitation")) { data, response, error in
            if let error { result = .failure(error) }
            else if let data, (response as? HTTPURLResponse)?.statusCode == 200 { result = .success(data) }
            fetched.fulfill()
        }.resume()
        wait(for: [fetched], timeout: 20)
        let data = try XCTUnwrap(result).get()
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let invitation = try XCTUnwrap(object["url"] as? String)
        let app = XCUIApplication()
        app.launch()
        addUIInterruptionMonitor(withDescription: "Local network permission") { alert in
            if alert.buttons["Allow"].exists { alert.buttons["Allow"].tap(); return true }
            if alert.buttons["OK"].exists { alert.buttons["OK"].tap(); return true }
            return false
        }
        // A dedicated test simulator may retain a prior smoke connection.
        if !app.secureTextFields["connection.invitation"].waitForExistence(timeout: 3) {
            app.buttons["connection.settings"].firstMatch.tap()
        }
        if app.buttons["connection.disconnect"].exists {
            app.buttons["connection.disconnect"].firstMatch.tap()
            let confirm = app.buttons["Disconnect and clear data"].firstMatch
            guard confirm.waitForExistence(timeout: 5) else { XCTFail("Disconnect confirmation missing"); return }
            confirm.tap()
        }
        let field = app.secureTextFields["connection.invitation"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText(invitation)
        app.buttons["connection.pair"].firstMatch.tap()
        let newSession = app.webViews.buttons["New session"].firstMatch
        guard newSession.waitForExistence(timeout: 30) else { XCTFail("Pairing must load the actual mobile UI"); return }
        let paired = XCTAttachment(screenshot: app.screenshot()); paired.name = "Paired mobile UI"; paired.lifetime = .keepAlways; add(paired)
        app.terminate()
        app.launch()
        guard newSession.waitForExistence(timeout: 30) else { XCTFail("Device cookie and origin must survive relaunch"); return }
        newSession.tap()
        // ARIA role=combobox makes this textarea a TextField in WebKit accessibility.
        let composer = app.webViews.textFields["Message Codex"].firstMatch
        guard composer.waitForExistence(timeout: 10) else {
            XCTFail("Composer not found: \(app.webViews.debugDescription)"); return
        }
        composer.tap()
        let draft = "Unsent iOS lifecycle check"
        composer.typeText(draft)
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        XCTAssertEqual(composer.value as? String, draft, "Normal foreground recovery must preserve the draft")
        app.buttons["connection.settings"].firstMatch.tap()
        let enable = app.buttons["notifications.enable"].firstMatch
        XCTAssertTrue(enable.waitForExistence(timeout: 5))
        enable.tap()
        XCTAssertTrue(app.staticTexts["Set up Apple push notifications on your Mac first."].waitForExistence(timeout: 10),
                      "An unconfigured Mac must explain setup before asking for iOS permission")
        let notifications = XCTAttachment(screenshot: app.screenshot())
        notifications.name = "Native notification setup"; notifications.lifetime = .keepAlways; add(notifications)
        app.buttons["connection.disconnect"].firstMatch.tap()
        let confirm = app.buttons["Disconnect and clear data"].firstMatch
            guard confirm.waitForExistence(timeout: 5) else { XCTFail("Disconnect confirmation missing"); return }
            confirm.tap()
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        app.terminate()
        app.launch()
        XCTAssertTrue(field.waitForExistence(timeout: 10), "Disconnected grant must not return on relaunch")
    }
}
