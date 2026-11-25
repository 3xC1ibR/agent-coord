import AppKit
import WebKit

#if RIBBON_SMOKE_TESTS
extension AppDelegate {
    func configureSmokeLaunch(_ directory: inout URL) {
        guard let flag = CommandLine.arguments.firstIndex(of: "--smoke-test"),
              flag + 1 < CommandLine.arguments.count else { return }
        smokeReport = URL(fileURLWithPath: CommandLine.arguments[flag + 1])
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("agent-coord-smoke-" + UUID().uuidString)
        if let dirFlag = CommandLine.arguments.firstIndex(of: "--smoke-directory"),
           dirFlag + 1 < CommandLine.arguments.count {
            directory = URL(fileURLWithPath: CommandLine.arguments[dirFlag + 1])
        }
        smokeDirectory = directory
        if let prefFlag = CommandLine.arguments.firstIndex(of: "--smoke-preferences"),
           prefFlag + 1 < CommandLine.arguments.count,
           CommandLine.arguments[prefFlag + 1].hasPrefix("com.agentcoord.desktop.smoke.") {
            smokePreferences = CommandLine.arguments[prefFlag + 1]
        } else {
            smokePreferences = "com.agentcoord.desktop.smoke." + UUID().uuidString
        }
        preferences = UserDefaults(suiteName: smokePreferences!)!
        if !CommandLine.arguments.contains("--smoke-window-restoration") {
            application(NSApp, open: [URL(string: "agentcoord://overview")!])
        }
        smokeResults["cold_start_link_queued"] = pendingAppLinks.count == 1
        smokeResults["launch_working_directory_matches_environment"] =
            ProcessInfo.processInfo.environment["PWD"] == FileManager.default.currentDirectoryPath
    }

    func scheduleSmokeTimeoutIfNeeded() {
        guard smokeReport != nil else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 90) { [weak self] in
            guard let self = self, !self.quitting else { return }
            self.finishSmoke(error: "Native smoke test timed out")
        }
    }

    func cleanupSmokeResources() {
        if let name = smokePreferences, !CommandLine.arguments.contains("--smoke-preferences") {
            preferences.removePersistentDomain(forName: name)
        }
        smokePasteboard?.releaseGlobally()
        if let directory = smokeDirectory, !CommandLine.arguments.contains("--smoke-preserve-runtime") {
            try? FileManager.default.removeItem(at: directory)
        }
    }

    // Native smoke mode uses a disposable database and preference domain. It
    // exercises the actual WKWebView and window lifecycle without model turns.
    func checkSmoke(attempt: Int) {
        guard !quitting, smokeStage == 0, let first = windows.first, first.loaded else { return }
        smokeStage = 1
        Task { @MainActor in
            do { try await self.runSmoke(first) }
            catch { self.finishSmoke(error: String(describing: error)) }
        }
    }

    private func smokeCheck(_ key: String, _ passed: Bool) throws {
        smokeResults[key] = passed
        if !passed { throw NSError(domain: "AgentCoordSmoke", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Native behavior check failed: " + key]) }
    }

    @MainActor private func smokeJS(_ source: String, in view: WKWebView, arguments: [String: Any] = [:]) async throws -> Any {
        try await withCheckedThrowingContinuation { continuation in
            view.callAsyncJavaScript(source, arguments: arguments, in: nil, in: .page) { result in
                continuation.resume(with: result)
            }
        }
    }

    @MainActor private func smokeReady(_ item: DesktopWindow) async throws {
        for _ in 0..<120 {
            if item.loaded { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try smokeCheck("ui_loaded", item.loaded)
        let ready = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (document.querySelector('#desktop-new-window') &&
                  typeof window.agentCoordNavigate === 'function' &&
                  document.querySelector('#connection')?.dataset.state === 'connected') return true;
              await new Promise(resolve => setTimeout(resolve, 50));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("ui_loaded", ready as? Bool == true)
    }

    @MainActor private func smokeFocus(_ item: DesktopWindow) async throws {
        item.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        for _ in 0..<80 {
            if NSApp.isActive && NSApp.keyWindow === item.window { return }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["focus_debug"] = ["active": NSApp.isActive, "target": item.window.windowNumber,
            "key": NSApp.keyWindow?.windowNumber ?? -1, "visible": item.window.isVisible,
            "sheet": item.window.attachedSheet != nil,
            "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "none"]
        try smokeCheck("window_focus_ready", false)
    }

    private func smokeKey(_ key: String, flags: NSEvent.ModifierFlags, code: UInt16) -> Bool {
        let characters = flags.contains(.shift) ? key.uppercased() : key
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags,
            timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
            characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code) else { return false }
        NSApp.mainMenu?.update()
        let handled = NSApp.mainMenu?.performKeyEquivalent(with: event) == true
        if !handled {
            smokeResults["shortcut_debug"] = ["main_thread": Thread.isMainThread, "active": NSApp.isActive,
                "key": characters, "flags": flags.rawValue, "window_count": windows.count,
                "items": NSApp.mainMenu?.items.flatMap { $0.submenu?.items ?? [] }.filter { !$0.keyEquivalent.isEmpty }.map {
                    ["title": $0.title, "key": $0.keyEquivalent, "flags": $0.keyEquivalentModifierMask.rawValue,
                     "enabled": $0.isEnabled, "target": String(describing: $0.target)] as [String: Any]
                } ?? []] as [String: Any]
        }
        return handled
    }

    private func smokeWebKey(_ key: String, flags: NSEvent.ModifierFlags, code: UInt16,
                             throughApplication: Bool = false) -> Bool {
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: flags,
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
                characters: key, charactersIgnoringModifiers: key, isARepeat: false, keyCode: code) else { return false }
            if throughApplication { NSApp.postEvent(event, atStart: false) }
            else { window.sendEvent(event) }
        }
        return true;
    }

    @MainActor private func runSmoke(_ first: DesktopWindow) async throws {
        try await smokeReady(first)
        smokeResults["backend_pid"] = backend.process?.processIdentifier
        smokeResults["url"] = serverURL?.absoluteString
        if CommandLine.arguments.contains("--smoke-window-restoration") {
            try await smokeWindowRestoration()
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-origin-change-only") {
            try await smokeOriginChange(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-runtime-only") {
            smokeResults["runtime_attached"] = backend.process?.isRunning == true
            let image: NSImage = try await withCheckedThrowingContinuation { continuation in
                first.webView.takeSnapshot(with: nil) { image, error in
                    if let image = image { continuation.resume(returning: image) }
                    else { continuation.resume(throwing: error ?? Backend.failure("Screenshot unavailable.")) }
                }
            }
            if let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
               let png = bitmap.representation(using: .png, properties: [:]) {
                try png.write(to: URL(fileURLWithPath: smokeReport!.path + ".png"))
            }
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-attribution-only") {
            try await smokeAttribution()
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-sessions-only") {
            try await smokeFocus(first)
            try await smokeSessionCycling(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-titlebar-only") {
            try await smokeTitlebar(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-detach-only") {
            try await smokeDetach(first)
            finishSmoke(error: nil)
            return
        }
        let bridge = try await smokeJS("""
            localStorage.setItem('agent-coord.smoke', 'persisted');
            sessionStorage.setItem('desktop-smoke-window', 'first');
            document.querySelector('#message').value = 'First window draft';
            document.querySelector('#back-home').focus();
            return Notification.agentCoordNative === true;
            """, in: first.webView)
        try smokeCheck("notification_bridge", bridge as? Bool == true)
        try smokeCheck("new_window_shortcut", smokeKey("n", flags: [.command, .shift], code: 45))
        smokeResults["window_count_after_shortcut"] = windows.count
        guard let second = windows.last, second !== first else {
            try smokeCheck("multiple_windows", false); return
        }
        try await smokeReady(second)
        try smokeCheck("multiple_windows", windows.count == 2 && first.webView !== second.webView)
        try await smokeWorkingFolders(first, second)
        if CommandLine.arguments.contains("--smoke-notifications-only") {
            try await smokeNotificationRouting(first, second)
            try await smokeProjectLinkTabs(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-scratchpad-only") {
            _ = try await smokeJS("await switchProject(); return true;", in: second.webView)
            try await smokeFocus(second)
            try await smokeScratchpad(second, other: first)
            finishSmoke(error: nil)
            return
        }
        let independent = try await smokeJS("""
            const independent = sessionStorage.getItem('desktop-smoke-window') === null;
            sessionStorage.setItem('desktop-smoke-window', 'second');
            return independent && localStorage.getItem('agent-coord.smoke') === 'persisted';
            """, in: second.webView)
        try smokeCheck("window_storage_isolated", independent as? Bool == true)
        _ = try await smokeJS("localStorage.setItem('agent-coord.smoke-shared', 'live'); return true;", in: first.webView)
        let shared = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (localStorage.getItem('agent-coord.smoke-shared') === 'live') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("live_preferences_shared", shared as? Bool == true)
        try smokeCheck("find_thread_shortcut", smokeKey("f", flags: [.command], code: 3))
        let focused = try await smokeJS("""
            for (let i = 0; i < 40; i++) {
              if (document.activeElement?.id === 'search') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstUntouched = try await smokeJS("return document.activeElement?.id !== 'search';", in: first.webView)
        try smokeCheck("commands_target_key_window", focused as? Bool == true && firstUntouched as? Bool == true)

        // A private pasteboard exercises the same native resolver without
        // changing the user's clipboard or live drag pasteboard.
        let file = smokeDirectory!.appendingPathComponent("notes with spaces.md")
        let folder = smokeDirectory!.appendingPathComponent("Project folder", isDirectory: true)
        try Data("Native drop smoke".utf8).write(to: file)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        smokePasteboard = NSPasteboard(name: NSPasteboard.Name("com.agentcoord.smoke." + UUID().uuidString))
        smokePasteboard!.clearContents()
        smokePasteboard!.writeObjects([file as NSURL])
        let dropped = try await smokeJS("""
            history.replaceState(null, '', '#desktop-smoke');
            const message = document.querySelector('#message');
            document.querySelector('#conversation').hidden = false;
            document.querySelector('#composer').hidden = false;
            message.value = 'Second window draft'; message.setSelectionRange(message.value.length, message.value.length);
            window.smokeAttachments = new ChatImageAttachments({document, getThread: () => 'smoke', canAttach: () => true,
              onChange() {}, onError(error) { throw error; }});
            window.smokeAttachments.bind();
            const transfer = new DataTransfer();
            transfer.items.add(new File(['Native drop smoke'], 'notes with spaces.md', {type: 'text/markdown'}));
            transfer.items.add(new File([new Uint8Array([97])], 'preview.png', {type: 'image/png'}));
            message.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
            for (let i = 0; i < 80; i++) {
              if (message.value.includes('notes with spaces.md') && window.smokeAttachments.snapshot().length === 1)
                return message.value.startsWith('Second window draft\\n') && document.querySelectorAll('#sessions button[data-thread]').length === 0;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'Native file drop did not reach the draft');
            """, in: second.webView)
        try smokeCheck("mixed_file_drop", dropped as? Bool == true)
        let firstDraft = try await smokeJS("return document.querySelector('#message').value === 'First window draft' && location.hash === '';", in: first.webView)
        try smokeCheck("window_drafts_and_navigation_isolated", firstDraft as? Bool == true)

        smokePasteboard!.clearContents()
        smokePasteboard!.writeObjects([folder as NSURL])
        let workspaceDrop = try await smokeJS("""
            const message = document.querySelector('#message');
            // The image attachment callback re-renders the empty smoke state.
            // Restore the editable destination for this independent drop check.
            document.querySelector('#conversation').hidden = false;
            document.querySelector('#composer').hidden = false;
            message.value = '/cd '; message.setSelectionRange(4, 4);
            const transfer = new DataTransfer();
            transfer.items.add(new File([], 'Project folder'));
            message.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
            for (let i = 0; i < 80; i++) {
              if (message.value.startsWith('/cd "') && message.value.includes('/Project folder')) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'Folder path did not reach /cd: ' + message.value);
            """, in: second.webView)
        try smokeCheck("workspace_folder_drop", workspaceDrop as? Bool == true)
        try await smokeFiles(second, other: first)
        try await smokeSettings(first, second)
        if CommandLine.arguments.contains("--smoke-files-only") { finishSmoke(error: nil); return }

        _ = try await smokeJS("""
            const message = document.querySelector('#message');
            message.focus(); message.setSelectionRange(2, 6);
            window.smokePaletteDraft = message.value;
            return true;
            """, in: second.webView)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        let paletteOpen = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (document.querySelector('#desktop-command-dialog').open &&
                  document.activeElement?.id === 'desktop-command-input' &&
                  document.querySelector('#desktop-command-status').textContent !== 'Loading threads…')
                return document.querySelectorAll('#desktop-command-results [role=option]').length > 0;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstPaletteClosed = try await smokeJS("return !document.querySelector('#desktop-command-dialog').open;", in: first.webView)
        try smokeCheck("command_palette_active_window", paletteOpen as? Bool == true && firstPaletteClosed as? Bool == true)
        let paletteImage: NSImage = try await withCheckedThrowingContinuation { continuation in
            second.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = paletteImage.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("palette.png"))
        }
        let paletteAction = try await smokeJS("""
            const input = document.querySelector('#desktop-command-input');
            input.value = 'find thread'; input.dispatchEvent(new Event('input', {bubbles: true}));
            const options = Array.from(document.querySelectorAll('#desktop-command-results [role=option]'));
            const actions = options.filter(option => option.dataset.entry === 'findThread');
            if (actions.length !== 1) return false;
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
            for (let i = 0; i < options.length && input.getAttribute('aria-activedescendant') !== actions[0].id; i++)
              input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
            if (input.getAttribute('aria-activedescendant') !== actions[0].id) return false;
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
            for (let i = 0; i < 40; i++) {
              if (!document.querySelector('#desktop-command-dialog').open && document.activeElement?.id === 'search')
                return document.querySelector('#message').value === window.smokePaletteDraft;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("command_palette_action", paletteAction as? Bool == true)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        _ = try await smokeJS("""
            for (let i = 0; i < 40 && !document.querySelector('#desktop-command-dialog').open; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            return true;
            """, in: second.webView)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        let paletteToggle = try await smokeJS("""
            for (let i = 0; i < 40; i++) {
              if (!document.querySelector('#desktop-command-dialog').open) return document.activeElement?.id === 'search';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("command_palette_toggle", paletteToggle as? Bool == true)
        let paletteEscape = try await smokeJS("""
            const search = document.querySelector('#search');
            search.value = 'Saved search'; search.focus(); search.setSelectionRange(2, 6);
            if (!window.agentCoordDesktop.command('commandPalette')) return false;
            document.querySelector('#desktop-command-input').dispatchEvent(new KeyboardEvent('keydown',
              {key: 'Escape', bubbles: true, cancelable: true}));
            const restored = !document.querySelector('#desktop-command-dialog').open && document.activeElement === search &&
              search.value === 'Saved search' && search.selectionStart === 2 && search.selectionEnd === 6;
            search.value = '';
            document.querySelector('#settings-dialog').showModal();
            const blocked = window.agentCoordDesktop.command('commandPalette') === false;
            document.querySelector('#settings-dialog').close();
            return restored && blocked;
            """, in: second.webView)
        try smokeCheck("command_palette_focus_and_modal", paletteEscape as? Bool == true)
        _ = try await smokeJS("""
            const work = window.agentCoordSendWork;
            window.smokeWorkOriginal = {api: work.api, workspace: work.workspace, threads: work.threads};
            work.workspace = () => '/tmp/send-work-smoke';
            work.threads = () => [{thread_id: 'release-smoke', title: 'Release deployment', cwd: '/tmp/send-work-smoke',
              browser_session: {}, checkpoint: {summary: 'Build, verify, and deploy Ribbon Field'}}];
            work.api = async (path, body) => {
              if (path === 'send-work/suggest') return {candidates: []};
              window.smokeWorkSent = body;
              return {receipt: {id: 1, cwd: body.cwd, target_id: 'release-smoke', target_title: 'Release deployment', state: 'accepted'}};
            };
            const input = document.querySelector('#search'); input.focus(); input.value = 'Unrelated draft'; input.setSelectionRange(2, 6);
            window.smokeWorkHash = location.hash;
            return true;
            """, in: second.webView)
        try smokeCheck("send_work_shortcut", smokeKey("k", flags: [.command, .shift], code: 40))
        let sendWorkOpen = try await smokeJS("""
            for (let i = 0; i < 40 && !document.querySelector('#send-work-dialog').open; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const input = document.querySelector('#send-work-input');
            input.value = 'Redeploy'; input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true}));
            return document.querySelector('#send-work-dialog').open && document.activeElement === input &&
              document.querySelector('#send-work-target-name').textContent === 'Release deployment' &&
              document.querySelectorAll('#send-work-results [role=option]').length === 3;
            """, in: second.webView)
        try smokeCheck("send_work_selection", sendWorkOpen as? Bool == true)
        let sendWorkImage: NSImage = try await withCheckedThrowingContinuation { continuation in
            second.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = sendWorkImage.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("send-work.png"))
        }
        let sendWorkSent = try await smokeJS("""
            const work = window.agentCoordSendWork;
            await work.send();
            const input = document.querySelector('#search');
            const passed = !work.dialog.open && document.activeElement === input && input.value === 'Unrelated draft' &&
              input.selectionStart === 2 && input.selectionEnd === 6 && location.hash === window.smokeWorkHash &&
              window.smokeWorkSent.message === 'Redeploy' && window.smokeWorkSent.destination.id === 'release-smoke' &&
              Object.keys(window.smokeWorkSent).sort().join(',') === 'cwd,destination,message,requestId';
            Object.assign(work, window.smokeWorkOriginal); work.notice.hidden = true; input.value = '';
            return passed;
            """, in: second.webView)
        try smokeCheck("send_work_typed_only_and_focus", sendWorkSent as? Bool == true)
        try await smokeScratchpad(second, other: first)
        try smokeCheck("roll_up_shortcut", smokeKey("r", flags: [.command, .option], code: 15))
        let rollUpStarted = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (!document.querySelector('#roll-up-bar').hidden && !state.rollUp.busy)
                return state.rollUp.active || state.rollUp.done;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstRollUpQuiet = try await smokeJS("return !state.rollUp.active && !state.rollUp.done;", in: first.webView)
        try smokeCheck("roll_up_active_window", rollUpStarted as? Bool == true && firstRollUpQuiet as? Bool == true)
        _ = try await smokeJS("document.querySelector('#roll-up-exit').click(); return true;", in: second.webView)
        let rejected = try await smokeJS("""
            try { await webkit.messageHandlers.desktop.postMessage({action: 'newWindow', url: 'https://example.com/'}); }
            catch { return true; }
            return false;
            """, in: second.webView)
        try smokeCheck("external_window_rejected", rejected as? Bool == true)

        _ = try await smokeJS("""
            window.smokeNewSessionCount = 0;
            window.smokeSavedNewSession = newSession;
            newSession = async () => { window.smokeNewSessionCount++; };
            document.querySelector('#message').focus();
            return true;
            """, in: second.webView)
        try smokeCheck("new_session_shortcut", smokeKey("n", flags: [.command], code: 45))
        let sameNewSessionAction = try await smokeJS("""
            for (let i = 0; i < 40 && window.smokeNewSessionCount === 0; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const shortcutWorked = window.smokeNewSessionCount === 1;
            document.querySelector('#new-session').click();
            const sameAction = window.smokeNewSessionCount === 2;
            newSession = window.smokeSavedNewSession;
            return shortcutWorked && sameAction && !document.querySelector('dialog[open]');
            """, in: second.webView)
        try smokeCheck("new_session_button_and_shortcut_match", sameNewSessionAction as? Bool == true)
        try await smokeRollUp(first)
        try await smokeNavigation(first, second)
        try await smokeNotificationRouting(first, second)
        try await smokeProjectLinkTabs(first)

        second.window.close()
        try smokeCheck("secondary_window_closed", windows.count == 1 && second.window.contentView == nil)
        try smokeCheck("one_shared_backend", backend.process?.isRunning == true &&
            backend.process?.processIdentifier == smokeResults["backend_pid"] as? Int32)
        first.window.close()
        try smokeCheck("window_closed", !first.window.isVisible)
        try smokeCheck("backend_survived_close", backend.process?.isRunning == true)
        showWindow()
        try smokeCheck("window_reopened", first.window.isVisible)
        first.loaded = false
        first.webView.reload()
        try await smokeReady(first)
        let persisted = try await smokeJS("return localStorage.getItem('agent-coord.smoke') === 'persisted';", in: first.webView)
        try smokeCheck("preferences_persisted", persisted as? Bool == true &&
            preferences.dictionary(forKey: "webPreferences")?["agent-coord.smoke"] as? String == "persisted")
        try await smokeOriginChange(first)
        smokeStage = 2
        first.webView.takeSnapshot(with: nil) { image, error in
            if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
               let png = bitmap.representation(using: .png, properties: [:]) {
                try? png.write(to: self.smokeReport!.appendingPathExtension("png"))
            }
            self.finishSmoke(error: error?.localizedDescription)
        }
    }

    @MainActor private func smokeOriginChange(_ item: DesktopWindow) async throws {
        guard let direct = serverURL, direct.host == "127.0.0.1", let directory = smokeDirectory else {
            throw Backend.failure("Missing permanent loopback edge origin.")
        }
        let arguments = CommandLine.arguments
        let flag = arguments.firstIndex(of: "--smoke-observe-origins")
        let saved = flag.flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
        let firstSetup = try await smokeJS("""
            document.querySelector('[data-jev-skip]')?.click();
            const id = saved || (await api('sessions', {client:'claude', name:'Window continuity', cwd})).session.thread_id;
            await refreshList(); await select(id);
            const field = document.querySelector('#message');
            field.value = 'An unsent draft'; field.focus(); field.setSelectionRange(2, 8);
            field.dispatchEvent(new Event('input', {bubbles:true}));
            window.__smokeDocumentToken = crypto.randomUUID();
            return [id, window.__smokeDocumentToken];
            """, in: item.webView, arguments: ["cwd": directory.path, "saved": saved ?? ""]) as! [String]
        let thread = firstSetup[0], firstToken = firstSetup[1]
        let sibling = makeWindow()
        defer { sibling.window.close() }
        try await smokeReady(sibling)
        let secondToken = try await smokeJS("""
            await refreshList(); await select(id);
            const field = document.querySelector('#message');
            field.value = 'Second window draft'; field.setSelectionRange(2, 8);
            field.dispatchEvent(new Event('input', {bubbles:true}));
            window.__smokeDocumentToken = crypto.randomUUID();
            return window.__smokeDocumentToken;
            """, in: sibling.webView, arguments: ["id": thread]) as! String
        let expectedOrigin = String(direct.absoluteString.dropLast())
        if arguments.contains("--smoke-observe-origins") {
            try Data().write(to: directory.appendingPathComponent("windows-ready"))
            let committed = directory.appendingPathComponent("update-committed")
            for _ in 0..<400 {
                if FileManager.default.fileExists(atPath: committed.path) { break }
                try await Task.sleep(nanoseconds: 50_000_000)
            }
            try smokeCheck("observed_live_backend_replacement", FileManager.default.fileExists(atPath: committed.path))
        } else {
            backend.ready?(direct)
        }
        let firstIntact = try await smokeJS("""
            const field = document.querySelector('#message');
            await refreshList();
            return window.__smokeDocumentToken === token && location.origin === origin &&
              state.selected === id && field?.value === 'An unsent draft' &&
              field.selectionStart === 2 && field.selectionEnd === 8;
            """, in: item.webView, arguments: ["id": thread, "token": firstToken, "origin": expectedOrigin]) as? Bool
        let secondIntact = try await smokeJS("""
            const field = document.querySelector('#message');
            return window.__smokeDocumentToken === token && location.origin === origin &&
              state.selected === id && field?.value === 'Second window draft' &&
              field.selectionStart === 2 && field.selectionEnd === 8;
            """, in: sibling.webView, arguments: ["id": thread, "token": secondToken, "origin": expectedOrigin]) as? Bool
        try smokeCheck("permanent_origin_first_window", firstIntact == true)
        try smokeCheck("permanent_origin_second_window", secondIntact == true)
        if !arguments.contains("--smoke-observe-origins") {
            try await smokeWindowImage(item, suffix: "origin-change")
        }
    }

    @MainActor private func smokeScratchpad(_ item: DesktopWindow, other: DesktopWindow) async throws {
        _ = try await smokeJS("""
            const search = document.querySelector('#search');
            search.value = 'Resume here'; search.focus(); search.setSelectionRange(2, 6);
            return true;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let opened = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (document.querySelector('#project-scratchpad-dialog').open && document.activeElement === editor && !editor.disabled) {
                editor.value = 'Remember the launch feedback';
                editor.dispatchEvent(new Event('input', {bubbles: true}));
                return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        let quiet = try await smokeJS("return !document.querySelector('#project-scratchpad-dialog').open;", in: other.webView)
        try smokeCheck("project_scratchpad_active_window", opened as? Bool == true && quiet as? Bool == true)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let restored = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const search = document.querySelector('#search');
              if (!document.querySelector('#project-scratchpad-dialog').open) {
                const saved = await api('workspaces/scratchpad?path=' + encodeURIComponent(state.folders.selected.cwd));
                if (saved.text === 'Remember the launch feedback') {
                  const restored = document.activeElement === search && search.value === 'Resume here' &&
                    search.selectionStart === 2 && search.selectionEnd === 6;
                  search.value = '';
                  return restored;
                }
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_saved_and_focus_restored", restored as? Bool == true)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let escaped = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (document.activeElement === editor && !editor.disabled) {
                editor.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
                return !document.querySelector('#project-scratchpad-dialog').open && document.activeElement?.id === 'search';
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_escape", escaped as? Bool == true)
        let sharedTab = try await smokeJS("""
            if (document.querySelector('[data-thread-tab="project-scratchpad"]')) return false;
            document.querySelector('#open-project-scratchpad').click();
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (!document.querySelector('#project-scratchpad-page').hidden && !editor.disabled &&
                  editor.value === 'Remember the launch feedback') {
                const tab = document.querySelector('[data-thread-tab="project-scratchpad"]');
                tab.parentElement.querySelector('.conversation-tab-close').click();
                // Tab close awaits pane navigation before hiding the scratchpad.
                for (let j = 0; j < 80; j++) {
                  if (!document.querySelector('[data-thread-tab="project-scratchpad"]') &&
                      document.querySelector('#project-scratchpad-page').hidden) break;
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                if (document.querySelector('[data-thread-tab="project-scratchpad"]') ||
                    !document.querySelector('#project-scratchpad-page').hidden) return false;
                if (!window.agentCoordDesktop.command('openProjectScratchpad')) return false;
                for (let j = 0; j < 80; j++) {
                  if (!editor.disabled && editor.value === 'Remember the launch feedback') {
                    const reopened = Boolean(document.querySelector('[data-thread-tab="project-scratchpad"]')) &&
                      !document.querySelector('#project-scratchpad-page').hidden;
                    document.querySelector('[data-thread-tab=""]').click();
                    return reopened;
                  }
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                return false;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        let separate = try await smokeJS("""
            return (await api('workspaces/scratchpad?path=' + encodeURIComponent(state.folders.selected.cwd))).text === '';
            """, in: other.webView)
        try smokeCheck("project_scratchpad_tab_and_isolation", sharedTab as? Bool == true && separate as? Bool == true)
    }

    @MainActor private func smokeWorkingFolders(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let folder = smokeDirectory!.appendingPathComponent("Working folder", isDirectory: true).resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: [folder.path, smokeDirectory!.resolvingSymlinksInPath().path])
        let paths = String(data: data, encoding: .utf8)!
        let opened = try await smokeJS("""
            const paths = \(paths);
            const expected = (await api('workspaces/open', {path: paths[0]})).cwd;
            const original = state.folderPicker.desktop;
            state.folderPicker.desktop = {chooseFolder: async () => paths[0]};
            try {
              await state.folderPicker.choose();
              const selected = state.folders.selected.cwd;
              const displayed = document.querySelector('#working-folder').value;
              const welcomeHidden = document.querySelector('#folder-welcome').hidden;
              const groupLabel = document.querySelector('#project').options[0].text;
              return {selected, displayed, welcomeHidden, groupLabel, expected,
                ok: selected === expected && displayed === expected && welcomeHidden && groupLabel === 'All groups'};
            } finally { state.folderPicker.desktop = original; }
            """, in: first.webView)
        smokeResults["working_folder_debug"] = opened
        try smokeCheck("working_folder_open", (opened as? [String: Any])?["ok"] as? Bool == true)
        let independent = try await smokeJS("""
            const paths = \(paths);
            if (state.folders.selected) return false;
            const folder = await state.folders.open(paths[1]); renderFolders();
            return state.folders.selected.cwd === folder.cwd && folder.cwd.endsWith(paths[1].split('/').pop());
            """, in: second.webView)
        try smokeCheck("working_folder_window_isolation", independent as? Bool == true)
        let restored = try await smokeJS("""
            const paths = \(paths);
            const expected = (await api('workspaces/open', {path: paths[0]})).cwd;
            const restored = new WorkingFolders({storage: localStorage, preferences: localStorage,
              key: window.agentCoordDesktop.folderSlot, scope: state.config.workspaceRoot || '', api});
            await restored.restore();
            return restored.selected.cwd === expected && state.folders.selected.cwd === expected;
            """, in: first.webView)
        try smokeCheck("working_folder_restored", restored as? Bool == true)
    }

    @MainActor private func smokeNotificationRouting(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let root = smokeDirectory!.resolvingSymlinksInPath()
        let folder = root.appendingPathComponent("Working folder", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        _ = try smokePython("""
            for name, cwd in [('notify-owned', sys.argv[2]), ('notify-other', sys.argv[3]), ('notify-fresh', sys.argv[3])]:
                fixture_command('register', '--session-id', name, '--client', 'codex', '--cwd', cwd)
                fixture_command('thread', 'update', '--session-id', name, '--title', name)
            print('{}')
            """, arguments: [folder.path, root.path])
        try await smokeInventoryReady(second, present: ["notify-owned", "notify-other", "notify-fresh"])
        let data = try JSONSerialization.data(withJSONObject: [folder.path, root.path])
        let paths = String(data: data, encoding: .utf8)!
        // The second window owns the thread's tab but currently shows another project.
        _ = try await smokeJS("""
            const paths = \(paths);
            await refreshList(); await openProjectDirectory(paths[0]); await select('notify-owned');
            const field = document.querySelector('#message');
            field.value = 'Owned notification draft'; field.dispatchEvent(new Event('input', {bubbles: true}));
            await openProjectDirectory(paths[1]); await select('notify-other');
            return true;
            """, in: second.webView)
        _ = try await smokeJS("const paths = \(paths); await refreshList(); await openProjectDirectory(paths[0]); goHome(); return true;", in: first.webView)
        func activate(_ thread: String, poster: DesktopWindow) async -> DesktopWindow? {
            await withCheckedContinuation { continuation in
                activateNotification(UUID().uuidString, info: ["desktopWindow": poster.id, "thread": thread]) {
                    continuation.resume(returning: $0)
                }
            }
        }
        let shown = """
            for (let i = 0; i < 120; i++) {
              if (state.selected === thread && !navigation.applying) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return {selected: state.selected, project: state.projectCwd, draft: document.querySelector('#message').value,
              tabs: state.threadTabs.items.filter(item => item.thread_id === thread).length};
            """
        let absent = "return state.threadTabs.has(thread) === false && state.selected !== thread;"
        // The first window is focused and posted the alert: the old destination.
        try await smokeFocus(first)
        let owner = await activate("notify-owned", poster: first)
        let owned = try await smokeJS(shown, in: second.webView, arguments: ["thread": "notify-owned"]) as? [String: Any]
        let untouched = try await smokeJS(absent, in: first.webView, arguments: ["thread": "notify-owned"])
        smokeResults["notification_owner_debug"] = owned ?? [:]
        try smokeCheck("notification_reveals_existing_tab", owner === second && current === second &&
            owned?["selected"] as? String == "notify-owned" && owned?["tabs"] as? Int == 1 &&
            (owned?["project"] as? String)?.hasSuffix("/Working folder") == true && untouched as? Bool == true)
        try smokeCheck("notification_preserves_draft", owned?["draft"] as? String == "Owned notification draft")
        try await smokeFocus(first)
        second.window.miniaturize(nil)
        for _ in 0..<80 where !second.window.isMiniaturized { try await Task.sleep(nanoseconds: 25_000_000) }
        let restored = await activate("notify-owned", poster: first)
        for _ in 0..<80 where second.window.isMiniaturized || current !== second { try await Task.sleep(nanoseconds: 25_000_000) }
        try smokeCheck("notification_restores_minimized_owner", restored === second && !second.window.isMiniaturized && current === second)
        // Without an existing tab, the most recently used main window opens it.
        try await smokeFocus(first)
        let fallback = await activate("notify-fresh", poster: second)
        let fresh = try await smokeJS(shown, in: first.webView, arguments: ["thread": "notify-fresh"]) as? [String: Any]
        let elsewhere = try await smokeJS(absent, in: second.webView, arguments: ["thread": "notify-fresh"])
        smokeResults["notification_fallback_debug"] = fresh ?? [:]
        try smokeCheck("notification_recent_window_fallback", fallback === first && current === first &&
            fresh?["selected"] as? String == "notify-fresh" && fresh?["tabs"] as? Int == 1 && elsewhere as? Bool == true)
    }

    @MainActor private func smokeProjectLinkTabs(_ item: DesktopWindow) async throws {
        let root = smokeDirectory!.resolvingSymlinksInPath()
        let folder = root.appendingPathComponent("Working folder", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        // A closed conversation is absent from the open list, so its project is
        // unknown until its detail loads: the route that left misplaced tabs.
        _ = try smokePython("""
            fixture_command('register', '--session-id', 'link-elsewhere', '--client', 'codex', '--cwd', sys.argv[2])
            fixture_command('thread', 'update', '--session-id', 'link-elsewhere', '--title', 'link-elsewhere')
            print('{}')
            """, arguments: [root.path])
        try await smokeInventoryReady(item, present: ["link-elsewhere"])
        let data = try JSONSerialization.data(withJSONObject: [folder.path, root.path])
        let paths = String(data: data, encoding: .utf8)!
        // Use the backend's canonical directories; Foundation strips /private.
        let directories = "const cwd = await Promise.all(\(paths).map(async path => (await api('workspaces/open', {path})).cwd));"
        let placement = """
            {
            \(directories)
            const tabs = state.threadTabs;
            for (let i = 0; i < 120; i++) {
              if (state.selected === 'link-elsewhere' && state.projectCwd === cwd[1] && !navigation.applying) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            const saved = JSON.parse(localStorage.getItem(tabs.baseKey + ':' + cwd[0]) || '{}');
            const result = {selected: state.selected, project: state.projectCwd, expected: cwd[1],
              own: tabs.items.filter(item => item.thread_id === 'link-elsewhere').length,
              other: (tabs.projects.get(cwd[0]) || []).filter(item => item.thread_id === 'link-elsewhere').length,
              saved: (saved.items || []).filter(item => item.thread_id === 'link-elsewhere').length};
            result.ok = result.selected === 'link-elsewhere' && result.project === cwd[1] &&
              result.own === 1 && result.other === 0 && result.saved === 0;
            return result;
            }
            """
        let unknown = try await smokeJS("""
            \(directories)
            await api('threads/link-elsewhere', {attention: 'archived'});
            await refreshList(); await openProjectDirectory(cwd[1]); goHome();
            state.threadTabs.items.splice(0, state.threadTabs.items.length); state.threadTabs.save(true); state.threadTabs.render();
            await openProjectDirectory(cwd[0]); goHome();
            return !state.sessions.some(thread => thread.thread_id === 'link-elsewhere');
            """, in: item.webView)
        try await smokeFocus(item)
        application(NSApp, open: [URL(string: "agentcoord://thread/link-elsewhere")!])
        let linked = try await smokeJS(placement, in: item.webView) as? [String: Any]
        smokeResults["project_link_debug"] = linked ?? [:]
        try smokeCheck("project_link_tab_in_own_project", unknown as? Bool == true && linked?["ok"] as? Bool == true)
        // A tab saved in the wrong project by an earlier version moves when clicked.
        let misplaced = try await smokeJS("""
            {
            \(directories)
            const tabs = state.threadTabs;
            tabs.items.splice(0, tabs.items.length); tabs.save(true); tabs.render();
            await openProjectDirectory(cwd[0]); goHome();
            tabs.items.push({thread_id: 'link-elsewhere', title: 'link-elsewhere'}); tabs.save(); tabs.render();
            await tabs.activate('link-elsewhere', {force: true});
            }
            """ + placement, in: item.webView) as? [String: Any]
        smokeResults["project_link_misplaced_debug"] = misplaced ?? [:]
        try smokeCheck("project_link_misplaced_tab_removed", misplaced?["ok"] as? Bool == true)
    }

    @MainActor private func smokeSettings(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let snapshot = "return JSON.stringify([location.href, state.selected, document.querySelector('#message').value]);"
        let firstBefore = try await smokeJS(snapshot, in: first.webView) as? String
        let secondBefore = try await smokeJS(snapshot, in: second.webView) as? String
        _ = try await smokeJS("document.querySelector('#open-settings').click(); return true;", in: second.webView)
        for _ in 0..<80 {
            if windows.contains(where: { $0.isSettings && $0.loaded }) { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        guard let settings = windows.first(where: { $0.isSettings }) else {
            try smokeCheck("settings_opened", false); return
        }
        let ready = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (document.querySelector('#file-settings')?.hidden === false &&
                  document.querySelector('#file-editor-name')?.textContent) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: settings.webView)
        try smokeCheck("settings_opened", ready as? Bool == true && settings.filesItem == nil && windows.count == 3)
        try await smokeFocus(settings)
        let shortcut = smokeKey(",", flags: [.command], code: 43)
        _ = try await smokeJS("await window.agentCoordDesktop.openSettings(); return true;", in: first.webView)
        try smokeCheck("settings_single_window", shortcut && windows.count == 3 &&
            windows.first(where: { $0.isSettings }) === settings && NSApp.keyWindow === settings.window)
        _ = try await smokeJS("""
            const theme = document.querySelector('#settings-theme');
            theme.value = 'dark'; theme.dispatchEvent(new Event('change'));
            localStorage.setItem('agent-coord.smoke-settings', 'shared');
            return true;
            """, in: settings.webView)
        var shared = true
        for conversation in [first, second] {
            let updated = try await smokeJS("""
                for (let i = 0; i < 80; i++) {
                  if (document.documentElement.dataset.theme === 'dark' &&
                      localStorage.getItem('agent-coord.theme') === 'dark' &&
                      localStorage.getItem('agent-coord.smoke-settings') === 'shared') return true;
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                return false;
                """, in: conversation.webView)
            shared = shared && updated as? Bool == true
        }
        try smokeCheck("settings_preferences_shared", shared &&
            preferences.dictionary(forKey: "webPreferences")?["agent-coord.theme"] as? String == "dark")
        let editorBefore = preferences.string(forKey: FileBrowserController.editorKey)
        _ = try await smokeJS("document.querySelector('#choose-file-editor').click(); return true;", in: settings.webView)
        for _ in 0..<60 {
            if settings.window.attachedSheet is NSOpenPanel { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        let panel = settings.window.attachedSheet as? NSOpenPanel
        try smokeCheck("settings_editor_picker_opens", panel?.title == "Choose File Editor" && panel?.canChooseFiles == true)
        panel?.cancel(nil)
        for _ in 0..<60 {
            if settings.window.attachedSheet == nil { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("settings_editor_picker_cancels", settings.window.attachedSheet == nil &&
            preferences.string(forKey: FileBrowserController.editorKey) == editorBefore)
        _ = try await smokeJS("document.querySelector('#close-settings').click(); return true;", in: settings.webView)
        for _ in 0..<60 {
            if !windows.contains(where: { $0.isSettings }) { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("settings_closed", windows.count == 2 && settings.window.contentView == nil)
        let firstAfter = try await smokeJS(snapshot, in: first.webView) as? String
        let secondAfter = try await smokeJS(snapshot, in: second.webView) as? String
        if firstBefore != firstAfter || secondBefore != secondAfter {
            smokeResults["settings_state_debug"] = [firstBefore, firstAfter, secondBefore, secondAfter]
        }
        try smokeCheck("settings_conversations_preserved", firstBefore != nil && firstBefore == firstAfter &&
            secondBefore != nil && secondBefore == secondAfter && first.window.isVisible && second.window.isVisible &&
            backend.process?.isRunning == true)
        try await smokeFocus(second)
    }

    @MainActor private func smokeFiles(_ item: DesktopWindow, other: DesktopWindow) async throws {
        try smokeCheck("file_browser_default_collapsed",
            preferences.object(forKey: "fileBrowserVisible") == nil &&
            item.filesItem?.isCollapsed == true && other.filesItem?.isCollapsed == true)
        item.split.view.layoutSubtreeIfNeeded()
        try smokeCheck("file_browser_collapsed_layout",
            abs(item.webView.bounds.width - item.split.view.bounds.width) < 2)
        try smokeCheck("file_browser_open_shortcut", smokeKey("b", flags: [.command, .option], code: 11))
        try smokeCheck("file_browser_opened",
            item.filesItem?.isCollapsed == false && other.filesItem?.isCollapsed == true)
        let visibleWindow = makeWindow()
        try smokeCheck("file_browser_visible_preference_restored", visibleWindow.filesItem?.isCollapsed == false)
        visibleWindow.window.close()
        item.window.makeKeyAndOrderFront(nil)
        let base = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Browser fixture", isDirectory: true)
        try FileManager.default.createDirectory(at: base.appendingPathComponent("Sources"), withIntermediateDirectories: true)
        let file = base.appendingPathComponent("README.md")
        try "# Native files\nReady for local and remote workspaces.\n".write(to: file, atomically: true, encoding: .utf8)
        try "print(\"hello\")\n".write(to: base.appendingPathComponent("Sources/main.swift"), atomically: true, encoding: .utf8)
        _ = try smokePython("""
            print(json.dumps(fixture_command('register', '--session-id', 'files-smoke', '--client', 'codex', '--cwd', sys.argv[2])))
            """, arguments: [base.path])
        try await smokeInventoryReady(item, present: ["files-smoke"])
        _ = try await smokeJS("""
            // Keep the UI-only draft editable through metadata refreshes without
            // creating a provider session or starting a model turn.
            window.smokeFileRenderStatus = renderStatus;
            renderStatus = function () {
              if (state.selected === 'files-smoke' && state.detail?.work_thread)
                state.detail.work_thread.browser_session = true;
              return window.smokeFileRenderStatus();
            };
            await refreshList(); await select('files-smoke');
            // Make a draft destination without starting a model or importing a terminal.
            document.querySelector('#composer').hidden = false;
            document.querySelector('#message').value = 'Review this';
            document.querySelector('#message').setSelectionRange(11, 11);
            return true;
            """, in: item.webView)
        for _ in 0..<100 {
            if item.files.outline.numberOfRows == 2 { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["file_browser_debug"] = ["root": item.files.root, "expected": base.path,
            "rows": item.files.outline.numberOfRows]
        try smokeCheck("file_browser_follows_conversation",
            URL(fileURLWithPath: item.files.root).resolvingSymlinksInPath().path == base.resolvingSymlinksInPath().path && item.files.outline.numberOfRows == 2)
        try smokeCheck("file_browser_window_isolation", other.files.root != base.path)
        let tree = item.files.outline
        guard let folder = tree.item(atRow: 0) as? FileBrowserNode else { try smokeCheck("file_browser_folder", false); return }
        tree.expandItem(folder)
        for _ in 0..<100 {
            if tree.numberOfRows == 3 { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_lazy_expansion", tree.numberOfRows == 3 && folder.children.first?.file.path == "Sources/main.swift")
        tree.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
        for _ in 0..<100 {
            if item.files.previewText.string.hasPrefix("# Native files") { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_preview", item.files.previewText.string.hasPrefix("# Native files"))
        let windowCount = windows.count
        let linkHandled = try await smokeJS("""
            const message = document.createElement('div');
            message.className = 'markdown';
            message.innerHTML = messageMarkdown.render('[Missing file](./missing-link-test.txt:12)');
            document.querySelector('#timeline').append(message);
            document.querySelector('#error').hidden = true;
            message.querySelector('a').click();
            try {
              for (let i = 0; i < 60; i++) {
                if (!document.querySelector('#error').hidden)
                  return document.querySelector('#error span').textContent.includes('missing-link-test.txt');
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              return false;
            } finally { document.querySelector('#error').hidden = true; }
            """, in: item.webView)
        try smokeCheck("file_browser_chat_link_native_error", linkHandled as? Bool == true && windows.count == windowCount)
        item.files.insertSelected()
        let inserted = try await smokeJS("""
            for (let i = 0; i < 60; i++) {
              if (document.querySelector('#message').value.includes('README.md'))
                return document.querySelector('#message').value.startsWith('Review this') && state.detail.thread.turns.length === 0;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        if inserted as? Bool != true {
            smokeResults["file_browser_insert_debug"] = try await smokeJS("""
                return {value: document.querySelector('#message').value,
                        error: document.querySelector('#error span').textContent,
                        composerHidden: document.querySelector('#composer').hidden,
                        selected: state.selected};
                """, in: item.webView)
        }
        try smokeCheck("file_browser_inserts_without_sending", inserted as? Bool == true)
        try "Changed by an agent\n".write(to: file, atomically: true, encoding: .utf8)
        item.files.refreshFiles()
        for _ in 0..<100 {
            if item.files.previewText.string == "Changed by an agent\n" { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_refresh", item.files.previewText.string == "Changed by an agent\n")
        if let content = item.window.contentView, let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
            content.cacheDisplay(in: content.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                try data.write(to: smokeReport!.appendingPathExtension("files.png"))
            }
        }
        try smokeCheck("file_browser_shortcut", smokeKey("b", flags: [.command, .option], code: 11))
        try smokeCheck("file_browser_collapsed", item.filesItem?.isCollapsed == true && other.filesItem?.isCollapsed == true)
        let hiddenWindow = makeWindow()
        try smokeCheck("file_browser_hidden_preference_restored", hiddenWindow.filesItem?.isCollapsed == true)
        hiddenWindow.window.close()
        item.window.makeKeyAndOrderFront(nil)
        item.split.view.layoutSubtreeIfNeeded()
        if let content = item.window.contentView, let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
            content.cacheDisplay(in: content.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                try data.write(to: smokeReport!.appendingPathExtension("files-collapsed.png"))
            }
        }
        _ = try await smokeJS("renderStatus = window.smokeFileRenderStatus; delete window.smokeFileRenderStatus; return true;", in: item.webView)
    }

    @MainActor private func smokeRollUp(_ window: DesktopWindow) async throws {
        _ = try smokePython("""
            directory = str(Path(sys.argv[1]).parent)
            fixture_command('thread', 'update', '--session-id', 'files-smoke', '--attention', 'archived')
            for name in ['roll-old-session', 'roll-new-session', 'roll-later']:
                fixture_command('register', '--session-id', name, '--client', 'codex', '--cwd', directory)
            # Deliberately register and checkpoint in different orders to test oldest waiting.
            for name in ['roll-later', 'roll-new-session', 'roll-old-session']:
                fixture_command('checkpoint', '--session-id', name, '--json', json.dumps(
                    {'phase': 'finished', 'summary': 'A recommendation to review.', 'next_step': None}))
            fixture_command('thread', 'update', '--session-id', 'roll-later', '--attention', 'later')
            print(json.dumps({'ready': True}))
            """)
        try await smokeInventoryReady(window, present: ["roll-old-session", "roll-new-session", "roll-later"],
                                      absent: ["files-smoke"], attention: ["roll-old-session": "now", "roll-new-session": "now", "roll-later": "later"])
        let oldest = try await smokeJS("""
            await state.rollUp.start();
            return state.selected === 'roll-new-session' && state.rollUp.queue.length === 2 &&
                !document.querySelector('#roll-up-bar').hidden;
            """, in: window.webView)
        if oldest as? Bool != true {
            smokeResults["roll_up_debug"] = try await smokeJS("""
                return {selected: state.selected, project: state.projectCwd, active: state.rollUp.active,
                  done: state.rollUp.done, busy: state.rollUp.busy,
                  queue: state.rollUp.queue.map(thread => thread.thread_id),
                  barHidden: document.querySelector('#roll-up-bar').hidden,
                  threads: (await api('threads?archived=false')).data.map(thread => ({
                    id: thread.thread_id, cwd: thread.cwd, attention: thread.attention,
                    needsAttention: thread.needs_attention, since: thread.attention_since,
                    response: thread.response_state, checkpointStale: thread.checkpoint_stale}))};
                """, in: window.webView)
        }
        try smokeCheck("roll_up_oldest_waiting", oldest as? Bool == true)
        let picture: NSImage = try await withCheckedThrowingContinuation { continuation in
            window.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = picture.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("roll-up.png"))
        }
        let behavior = try await smokeJS("""
            document.querySelector('#roll-up-handle').click();
            for (let i = 0; i < 80 && (state.selected !== 'roll-old-session' || state.rollUp.busy); i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            if (state.selected !== 'roll-old-session') return false;
            const handled = await api('threads/roll-new-session');
            if (handled.needs_attention || handled.attention !== 'now') return false;
            document.querySelector('#roll-up-skip').click();
            for (let i = 0; i < 80 && state.rollUp.active; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const skipped = await api('threads/roll-old-session');
            const passed = state.rollUp.done && skipped.needs_attention &&
              document.querySelector('#roll-up-label').textContent === 'Pass complete · 1 skipped';
            document.querySelector('#roll-up-exit').click();
            return passed && document.querySelector('#roll-up-bar').hidden;
            """, in: window.webView)
        try smokeCheck("roll_up_queue_flow", behavior as? Bool == true)
        _ = try await smokeJS("""
            for (const id of ['roll-old-session', 'roll-new-session', 'roll-later'])
              await api('threads/' + id, {attention: 'archived'});
            goHome(); await refreshList(); return true;
            """, in: window.webView)
    }

    @MainActor private func smokeInventoryReady(_ item: DesktopWindow, present: [String],
                                                absent: [String] = [], attention: [String: String] = [:]) async throws {
        _ = try await smokeJS("""
            for (let i = 0; i < 120; i++) {
              const threads = (await api('threads?archived=false')).data;
              const visible = new Map(threads.map(thread => [thread.thread_id, thread]));
              if (present.every(id => visible.has(id)) && absent.every(id => !visible.has(id)) &&
                  Object.entries(attention).every(([id, value]) => visible.get(id)?.attention === value)) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error('Native fixture did not become visible through the thread inventory: ' + present.join(', '));
            """, in: item.webView, arguments: ["present": present, "absent": absent, "attention": attention])
    }

    private func smokePython(_ source: String, arguments: [String] = []) throws -> [String: Any] {
        let task = Process(), output = Pipe()
        task.executableURL = configuration.pythonURL(resources: resources)
        let commands = """
            import contextlib, io, json, sys
            from pathlib import Path
            from agent_coord.cli import main as coordinator_cli
            def fixture_command(*args):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    if coordinator_cli(['--db', sys.argv[1], *args]) != 0:
                        raise RuntimeError('Native smoke fixture command failed: ' + args[0])
                return json.loads(output.getvalue())
            """
        task.arguments = ["-c", commands + "\n" + source, smokeDirectory!.appendingPathComponent("state.sqlite3").path] + arguments
        task.environment = ["PATH": configuration.toolPath, "PYTHONPATH": resources.appendingPathComponent("backend").path,
                            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"]
        task.currentDirectoryURL = smokeDirectory
        task.standardOutput = output
        try task.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0,
              let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "AgentCoordSmoke", code: 3,
                userInfo: [NSLocalizedDescriptionKey: "Native smoke fixture command failed"])
        }
        return result
    }

    func prepareOfflineSmokeFixtures() throws {
        let mode = CommandLine.arguments.first { $0.hasPrefix("--smoke-") && ($0.hasSuffix("-only") || $0 == "--smoke-window-restoration") }
        guard mode == nil || mode == "--smoke-attribution-only" else { return }
        // Test bundles never start the telemetry sender. Initialize the disposable
        // table before ownership passes to the coordinator, then test native attribution normally.
        _ = try smokePython("""
            from agent_coord import launch_metrics
            from agent_coord.store import CoordinationStore
            launch_metrics.initialize(CoordinationStore(sys.argv[1]))
            print('{}')
            """)
        guard mode == nil, let window = windows.first else { return }
        // The actual CLI runs outside the checkout against the isolated database.
        // Capture its launch URL instead of sending it to the user's installed app.
        smokeNavigationFixture = try smokePython("""
            import contextlib, io, json, subprocess, sys
            from unittest.mock import patch
            from agent_coord.store import CoordinationStore
            from agent_coord.navigation import NavigationStore
            from agent_coord.cli import main
            from agent_coord.runtime_requests import owner_operation
            ownership = contextlib.ExitStack()
            if not ownership.enter_context(owner_operation(sys.argv[1])):
                raise RuntimeError("The private navigation fixture must be prepared before runtime startup")
            store = CoordinationStore(sys.argv[1])
            nav = NavigationStore(store)
            directory = str(store.database_path.parent.resolve())
            billing = store.threads.organization.create_project('Billing', cwd=directory)
            other = store.threads.organization.create_project('Other', cwd=directory)
            for name, project in [('navigation-manager', None), ('navigation-billing', billing['id']), ('navigation-other', other['id'])]:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name, project_id=project, attention='archived')
            view = nav.views.create({'name': 'Review', 'cwd': directory, 'filters': {'project': other['id'], 'search': 'keep'}})
            nav.bind('navigation-manager', sys.argv[2])
            output = io.StringIO()
            with patch('agent_coord.navigation.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as opener, contextlib.redirect_stdout(output):
                assert main(['--db', sys.argv[1], 'ui', 'open', '--project', 'Billing', '--from-session', 'navigation-manager', '--wait', '0']) == 0
            ownership.close()
            print(json.dumps({'url': opener.call_args.args[0][-1], 'result': json.loads(output.getvalue()),
                              'billing': billing['id'], 'other': other['id'], 'view': view['id'], 'directory': directory}))
            """, arguments: [window.id])
    }

    @MainActor private func smokeAttribution() async throws {
        let token = UUID().uuidString.lowercased()
        application(NSApp, open: [URL(string: "agentcoord://attribution?download_token=\(token)")!])
        var attributed = false
        for _ in 0..<20 {
            try await Task.sleep(nanoseconds: 100_000_000)
            let receipt = try smokePython("""
                import json, sqlite3, sys
                with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
                    token = db.execute('SELECT download_token FROM launch_install').fetchone()[0]
                print(json.dumps({'received': token == sys.argv[2]}))
                """, arguments: [token])
            if receipt["received"] as? Bool == true { attributed = true; break }
        }
        try smokeCheck("download_attribution_link", attributed)
    }

    @MainActor private func smokeNavigation(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        guard let fixture = smokeNavigationFixture else { throw Backend.failure("Missing private navigation fixture.") }
        _ = try smokePython("""
            for name in ('navigation-manager', 'navigation-billing', 'navigation-other'):
                fixture_command('thread', 'update', '--session-id', name, '--attention', 'now')
            print('{}')
            """)
        try await smokeInventoryReady(first, present: ["navigation-manager", "navigation-billing", "navigation-other"])
        try await smokeAttribution()
        let data = try JSONSerialization.data(withJSONObject: fixture)
        let json = String(data: data, encoding: .utf8)!
        _ = try await smokeJS("""
            window.navigationFixture = \(json);
            await refreshList(); await select('navigation-manager');
            document.querySelector('#message').value = 'Keep my navigation draft';
            return true;
            """, in: first.webView)
        // The fixture assigns the draft directly. Also interact with its page:
        // native Back can skip script-created history entries without a gesture.
        try await smokeFocus(first)
        _ = smokeWebKey("\t", flags: [], code: 48)
        _ = try await smokeJS("""
            window.navigationFixture = \(json);
            await openProjectDirectory(navigationFixture.directory);
            savedViews.sync((await api('views')).data);
            await savedViews.activate(window.navigationFixture.view);
            return true;
            """, in: second.webView)
        try await smokeFocus(second)
        application(NSApp, open: [URL(string: fixture["url"] as! String)!])
        let displayed = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.selected === null && document.querySelector('#project').value === navigationFixture.billing &&
                  !document.querySelector('#welcome').hidden && !navigation.applying) {
                const cards = [...document.querySelectorAll('#overview [data-thread]')].map(el => el.dataset.thread);
                return cards.includes('navigation-billing') && !cards.includes('navigation-other');
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'App link did not display Billing');
            """, in: first.webView)
        try smokeCheck("app_link_project_filter", displayed as? Bool == true)
        let otherUntouched = try await smokeJS("return savedViews.activeId === navigationFixture.view && document.querySelector('#search').value === 'keep';", in: second.webView)
        for _ in 0..<40 {
            if current === first { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["navigation_source_debug"] = ["key_window": NSApp.keyWindow?.title ?? "none",
            "current_is_first": current === first, "other_untouched": otherUntouched as? Bool ?? false]
        try smokeCheck("app_link_source_window", current === first && otherUntouched as? Bool == true)
        let acknowledgement = try smokePython("""
            import sqlite3
            with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT status,window_id FROM ui_navigation_requests WHERE id=?', (sys.argv[2],)).fetchone()
            print(json.dumps(dict(row)))
            """, arguments: [(fixture["result"] as! [String: Any])["request_id"] as! String])
        try smokeCheck("app_link_acknowledged", acknowledgement["status"] as? String == "displayed" && acknowledgement["window_id"] as? String == first.id)
        try smokeCheck("app_link_back_shortcut", smokeKey("[", flags: [.command], code: 33))
        // Let WebKit finish the history transition before starting an async
        // evaluation in the destination document.
        try await Task.sleep(nanoseconds: 200_000_000)
        let returned = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.selected === 'navigation-manager' && !navigation.applying &&
                  document.querySelector('#message').value === 'Keep my navigation draft') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        smokeResults["navigation_back_debug"] = try await smokeJS("""
            return {selected: state.selected, draft: document.querySelector('#message').value,
              stored: state.drafts.get('navigation-manager') || '', applying: navigation.applying,
              hash: location.hash, history: history.state, error: document.querySelector('#error span').textContent};
            """, in: first.webView)
        try smokeCheck("app_link_back_preserves_draft", returned as? Bool == true)
        let saved = try await smokeJS("""
            const anchor = document.createElement('a');
            anchor.href = 'agentcoord://view/' + navigationFixture.view;
            anchor.textContent = 'Review'; document.body.append(anchor); anchor.click(); anchor.remove();
            for (let i = 0; i < 100; i++) {
              if (savedViews.activeId === navigationFixture.view && !navigation.applying) {
                const view = (await api('views')).data.find(view => view.id === navigationFixture.view);
                return view.filters.project === navigationFixture.other && view.filters.search === 'keep' && view.version === 1;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        if saved as? Bool != true {
            smokeResults["navigation_saved_view_debug"] = try await smokeJS("""
                return {active: savedViews.activeId, expected: navigationFixture.view,
                  applying: navigation.applying, filters: savedViews.read(),
                  view: (await api('views')).data.find(view => view.id === navigationFixture.view),
                  error: document.querySelector('#error span').textContent};
                """, in: first.webView)
        }
        try smokeCheck("app_link_saved_view_unchanged", saved as? Bool == true)
        try await smokeFocus(second)
        application(NSApp, open: [URL(string: "agentcoord://overview?project=\(fixture["billing"] as! String)&window=\(UUID().uuidString)")!])
        let fallback = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (['all', 'group-' + navigationFixture.billing].includes(savedViews.activeId) && document.querySelector('#project').value === navigationFixture.billing && !navigation.applying) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("app_link_closed_window_fallback", fallback as? Bool == true && current === second)
        try await smokeFocus(first)
        _ = try await smokeJS("await agentCoordNavigate('agentcoord://overview'); return true;", in: first.webView)
        try await smokeFocus(first)
        try smokeCheck("tile_threads_shortcut", smokeKey("t", flags: [.command, .control, .option], code: 17))
        let paneReady = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              const shell = window.agentCoordPanes;
              if (shell.active && shell.records.get('navigation-manager')?.api) {
                await shell.openThread('navigation-manager');
                const doc = shell.focusedRecord.frame.contentDocument;
                doc.querySelector('#composer').hidden = false;
                const message = doc.querySelector('#message'); message.disabled = false;
                message.value = 'Keep my pane draft'; message.dispatchEvent(new Event('input', {bubbles: true})); message.focus();
                if (doc.activeElement === message) return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        if paneReady as? Bool != true {
            smokeResults["pane_native_debug"] = ["current_is_first": current === first,
                "key_window": NSApp.keyWindow?.windowNumber ?? -1,
                "first_window": first.window.windowNumber, "second_window": second.window.windowNumber]
            smokeResults["pane_composer_debug"] = try await smokeJS("""
                const shell = agentCoordPanes;
                return {active: shell.active, selected: shell.selected,
                  filters: savedViews.read(), busy: state.busy, dialog: document.querySelector('dialog[open]')?.id || '',
                  sessions: state.sessions.map(thread => ({id: thread.thread_id, attention: thread.attention})),
                  records: [...shell.records].map(([id, record]) => {
                    const doc = record.frame?.contentDocument, message = doc?.querySelector('#message');
                    return {id, ready: Boolean(record.api), hidden: record.frame?.hidden,
                      composerHidden: doc?.querySelector('#composer')?.hidden,
                      disabled: message?.disabled, activeElement: doc?.activeElement?.id || doc?.activeElement?.tagName,
                      error: doc?.querySelector('#error span')?.textContent || ''};
                  }), error: document.querySelector('#error span').textContent};
                """, in: first.webView)
            smokeResults["pane_second_window_debug"] = try await smokeJS("return {active: agentCoordPanes.active, selected: agentCoordPanes.selected};", in: second.webView)
        }
        try smokeCheck("pane_composer_focused", paneReady as? Bool == true)
        try smokeCheck("pane_maximize_shortcut", smokeWebKey("\r", flags: [.control, .option], code: 36))
        let maximized = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (agentCoordPanes.layout.maximized != null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_maximized_from_composer", maximized as? Bool == true)
        _ = smokeWebKey("\u{1b}", flags: [], code: 53)
        let restored = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (agentCoordPanes.layout.maximized == null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_escape_restores_layout", restored as? Bool == true)
        try await smokeFocus(first)
        _ = try await smokeJS("""
            // Changing an iframe's document creates a joint WebKit history entry.
            // Finish the tiled fixture's own initial navigations before opening the app link.
            const panes = agentCoordPanes;
            for (let i = 0; i < 100; i++) {
              const loaded = [...panes.records.values()].filter(record => record.frame);
              if (loaded.every(record => record.api && record.frame.contentDocument?.readyState === 'complete')) break;
              if (i === 99) throw new Error('Tiled conversation documents did not finish loading');
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            const result = await agentCoordNavigate('agentcoord://overview?project=' + navigationFixture.billing);
            if (result.status !== 'displayed') throw new Error('Pane navigation failed: ' + result.error);
            return true;
            """, in: first.webView)
        try await smokeFocus(first)
        try smokeCheck("app_link_pane_back_shortcut", smokeKey("[", flags: [.command], code: 33))
        try await Task.sleep(nanoseconds: 200_000_000)
        let paneBack = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!navigation.applying && agentCoordPanes.active && agentCoordPanes.selected === 'navigation-manager' &&
                  agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message')?.value === 'Keep my pane draft') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        smokeResults["navigation_pane_back_debug"] = try await smokeJS("""
            return {active: agentCoordPanes.active, selected: agentCoordPanes.selected,
              draft: agentCoordPanes.focusedRecord?.frame.contentDocument.querySelector('#message')?.value || '',
              stored: state.drafts.get('navigation-manager') || '', applying: navigation.applying,
              history: history.state, error: document.querySelector('#error span').textContent};
            """, in: first.webView)
        try smokeCheck("app_link_back_restores_pane_draft", paneBack as? Bool == true)
        _ = try await smokeJS("""
            // Begin the independent toggle test from a known overview. A restored
            // history entry can legitimately have a conversation as its return target.
            goHome();
            if (!await agentCoordPanes.toggleCurrent()) throw new Error('Could not open the saved pane layout from overview');
            window.smokePaneLayout = agentCoordPanes.layout;
            smokePaneLayout.widths = smokePaneLayout.columns.map((_, i) => 1 + i / 4);
            agentCoordPanes.render(); return true;
            """, in: first.webView)
        try await smokeFocus(first)
        guard smokeKey("t", flags: [.command, .control, .option], code: 17) else { throw Backend.failure("Tile Threads shortcut was unavailable") }
        let toggleOff = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!agentCoordPanes.active && state.selected === null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_returns_to_overview", toggleOff as? Bool == true)
        try await smokeFocus(first)
        guard smokeKey("t", flags: [.command, .control, .option], code: 17) else { throw Backend.failure("Tile Threads shortcut was unavailable") }
        let toggleOn = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.active)
                return agentCoordPanes.layout === smokePaneLayout && smokePaneLayout.widths[0] === 1 &&
                  agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message').value === 'Keep my pane draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_restores_layout_and_draft", toggleOn as? Bool == true)
        _ = try await smokeJS("""
            goHome(); await select('navigation-manager');
            if (state.selected !== 'navigation-manager' || agentCoordPanes.active)
              throw new Error('Single conversation did not open before toggling the pane layout');
            return true;
            """, in: first.webView)
        try await smokeFocus(first)
        guard smokeKey("t", flags: [.command, .control, .option], code: 17) else { throw Backend.failure("Tile Threads shortcut was unavailable") }
        let fromThread = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.active && agentCoordPanes.layout.returnTarget?.thread === 'navigation-manager') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        if fromThread as? Bool != true {
            smokeResults["pane_single_thread_debug"] = try await smokeJS("""
                return {active: agentCoordPanes.active, selected: state.selected, pane: agentCoordPanes.selected,
                  target: agentCoordPanes.layout?.returnTarget, history: history.state, busy: state.busy,
                  dialog: document.querySelector('dialog[open]')?.id, error: document.querySelector('#error span').textContent};
                """, in: first.webView)
        }
        try smokeCheck("pane_toggle_remembers_single_thread", fromThread as? Bool == true)
        try await smokeFocus(first)
        guard smokeKey("t", flags: [.command, .control, .option], code: 17) else { throw Backend.failure("Tile Threads shortcut was unavailable") }
        let toThread = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!agentCoordPanes.active && state.detail?.work_thread?.thread_id === 'navigation-manager')
                return document.querySelector('#message').value === 'Keep my pane draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_returns_to_single_thread", toThread as? Bool == true)
        try await smokeSessionCycling(first)
        _ = try await smokeJS("goHome(); return true;", in: first.webView)
    }

    @MainActor private func smokeDetach(_ first: DesktopWindow) async throws {
        _ = try smokePython("""
            for name in ['detach-one', 'detach-two']:
                fixture_command('register', '--session-id', name, '--client', 'claude', '--cwd', str(Path(sys.argv[1]).parent))
                fixture_command('thread', 'update', '--session-id', name, '--title', name)
            for index in range(40):
                fixture_command('send', '--from-session', 'detach-two', '--session', 'detach-one',
                    f'Saved message {index}\\n' + 'History preserved through detachment. ' * 25,
                    '--classification', 'informational', '--no-reply-required')
            print('{}')
            """)
        try await smokeInventoryReady(first, present: ["detach-one", "detach-two"])
        _ = try await smokeJS("""
            await refreshList(); await select('detach-one'); await select('detach-two');
            window.detachEvents = [];
            for (const kind of ['pointerdown', 'pointerup', 'pointermove', 'click'])
              document.addEventListener(kind, e => detachEvents.push({kind, x: e.clientX, y: e.clientY, target: e.target.outerHTML?.slice(0, 240)}), true);
            return true;
            """, in: first.webView)
        try await smokeFocus(first)
        func pointer(_ type: NSEvent.EventType, _ x: Double, _ y: Double, in item: DesktopWindow? = nil) {
            let item = item ?? first, view = item.webView
            let location = view.convert(NSPoint(x: x, y: view.isFlipped ? y : view.bounds.height - y), to: nil)
            if let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: item.window.windowNumber,
                context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseUp ? 0 : 1) {
                item.window.sendEvent(event)
            }
        }
        var tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-one\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseUp, tab["x"]!, tab["y"]!)
        for _ in 0..<100 {
            if (try await smokeJS("return state.selected === 'detach-one' && state.detail?.work_thread.thread_id === 'detach-one' && !state.detailRefreshing;", in: first.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        smokeResults["detach_click_debug"] = try await smokeJS("return {selected: state.selected, events: detachEvents, width: innerWidth, height: innerHeight};", in: first.webView)
        smokeResults["detach_click_target"] = tab
        try smokeCheck("detach_normal_tab_click", (try await smokeJS("return state.selected === 'detach-one';", in: first.webView)) as? Bool == true)
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, -250, 80)
        _ = try await smokeJS("return state.threadTabs.dragging;", in: first.webView)
        _ = smokeWebKey("\u{1b}", flags: [], code: 53)
        _ = try await smokeJS("return true;", in: first.webView)
        pointer(.leftMouseUp, -250, 80)
        try await Task.sleep(nanoseconds: 200_000_000)
        let cancelled = try await smokeJS("return !state.threadTabs.dragging && state.threadTabs.items.length === 2 && !document.querySelector('.pane-drop-surface');", in: first.webView)
        try smokeCheck("detach_escape_cancelled", cancelled as? Bool == true && windows.count == 1)
        let other = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-two\"]').getBoundingClientRect(); return {x: rect.right - 3, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, other["x"]!, other["y"]!)
        pointer(.leftMouseUp, other["x"]!, other["y"]!)
        try await Task.sleep(nanoseconds: 200_000_000)
        let reordered = try await smokeJS("return state.threadTabs.items.map(item => item.thread_id).join(',') === 'detach-two,detach-one';", in: first.webView)
        try smokeCheck("detach_reorder_preserved", reordered as? Bool == true && windows.count == 1)
        tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-one\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        _ = try await smokeJS("""
            const input = document.querySelector('#message');
            input.value = 'Keep detached draft'; input.setSelectionRange(2, 8);
            state.drafts.set('detach-one', input.value);
            state.attachments.drafts.set('detach-one', [{name: 'pixel.png', url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=='}]);
            const timeline = document.querySelector('#timeline');
            const firstMessage = timeline.querySelector('details[data-id]');
            firstMessage.open = true; rememberDisclosure(firstMessage, timelineDisclosures());
            await new Promise(resolve => requestAnimationFrame(resolve));
            state.timelineScroll.restore(260);
            return true;
            """, in: first.webView)
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, -250, 80)
        pointer(.leftMouseUp, -250, 80)
        for _ in 0..<150 {
            if windows.count == 2 && windows.last?.navigationReady == true && conversationTransfers.isEmpty,
               (try await smokeJS("return !state.threadTabs.items.some(item => item.thread_id === 'detach-one');", in: first.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard let second = windows.last, second !== first else { try smokeCheck("detach_window_created", false); return }
        try smokeCheck("detach_window_created", windows.count == 2)
        let restored = try await smokeJS("""
            const input = document.querySelector('#message');
            return state.selected === 'detach-one' && state.detail.work_thread.thread_id === 'detach-one' &&
              input.value === 'Keep detached draft' && input.selectionStart === 2 && input.selectionEnd === 8 &&
              state.attachments.items('detach-one')[0]?.name === 'pixel.png' &&
              state.threadTabs.items.length === 1 && state.threadTabs.items[0].thread_id === 'detach-one';
            """, in: second.webView)
        try smokeCheck("detach_state_restored", restored as? Bool == true)
        let reading = try await smokeJS("""
            return document.querySelectorAll('#timeline .agent-message').length === 40 &&
              document.querySelector('#timeline details[data-id]')?.open === true &&
              Math.abs(state.timelineScroll.position() - 260) < 3;
            """, in: second.webView)
        smokeResults["detach_reading_debug"] = try await smokeJS("return {scroll: state.timelineScroll.position(), messages: document.querySelectorAll('#timeline .agent-message').length, expanded: document.querySelector('#timeline details[data-id]')?.open};", in: second.webView)
        try smokeCheck("detach_history_and_reading", reading as? Bool == true)
        try smokeCheck("detach_window_focused", NSApp.keyWindow === second.window)
        let remaining = try await smokeJS("""
            return state.selected === 'detach-two' &&
              !state.threadTabs.items.some(item => item.thread_id === 'detach-one') &&
              state.threadTabs.items.some(item => item.thread_id === 'detach-two');
            """, in: first.webView)
        try smokeCheck("detach_remaining_tabs", remaining as? Bool == true)
        let inside = try await smokeJS("""
            return await webkit.messageHandlers.desktop.postMessage({action: 'detachConversation',
              payload: agentCoordConversationTransfer.capture('detach-one'), x: 100, y: 80}) === false;
            """, in: second.webView)
        try smokeCheck("detach_inside_window_ignored", inside as? Bool == true && windows.count == 2)
        let compact = try await smokeJS("""
            return agentCoordDesktop.conversationWindow && document.documentElement.classList.contains('conversation-window') &&
              getComputedStyle(document.querySelector('.sidebar')).display === 'none' &&
              getComputedStyle(document.querySelector('.workspace-bar')).display === 'none' &&
              getComputedStyle(document.querySelector('.projects-bar')).display === 'none' &&
              getComputedStyle(document.querySelector('#conversation-add-tab')).display !== 'none' &&
              document.querySelector('#conversation-tabs').getBoundingClientRect().top < 2;
            """, in: second.webView)
        try smokeCheck("detach_compact_host", compact as? Bool == true && second.filesItem == nil)
        func localPoint(_ screen: NSPoint, in item: DesktopWindow) -> NSPoint {
            let local = item.webView.convert(item.window.convertPoint(fromScreen: screen), from: nil)
            return NSPoint(x: local.x, y: item.webView.isFlipped ? local.y : item.webView.bounds.height - local.y)
        }
        func drag(_ id: String, from item: DesktopWindow, to point: NSPoint) async throws {
            try await smokeFocus(item)
            let tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"' + thread + '\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: item.webView, arguments: ["thread": id]) as! [String: Double]
            pointer(.leftMouseDown, tab["x"]!, tab["y"]!, in: item)
            pointer(.leftMouseDragged, point.x, point.y, in: item)
            pointer(.leftMouseUp, point.x, point.y, in: item)
        }
        let outside = NSPoint(x: (NSScreen.main?.frame.maxX ?? first.window.frame.maxX) + 400, y: first.window.frame.midY)
        let oldOrigin = second.window.frame.origin
        try await drag("detach-one", from: second, to: localPoint(outside, in: second))
        try await Task.sleep(nanoseconds: 300_000_000)
        let singleton = try await smokeJS("return state.selected === 'detach-one' && state.threadTabs.items.length === 1 && document.querySelector('#message').value === 'Keep detached draft';", in: second.webView)
        try smokeCheck("detach_singleton_moves_window", singleton as? Bool == true && windows.count == 2 && second.window.frame.origin != oldOrigin)
        // Keep a visible destination point outside the original source frame.
        second.window.setFrameOrigin(NSPoint(x: first.window.frame.maxX + 40, y: first.window.frame.minY))
        let targetPoint = NSPoint(x: second.window.frame.maxX - 10, y: second.window.frame.maxY - 80)
        _ = try await smokeJS("document.querySelector('#message').value = 'Second tab draft'; return true;", in: first.webView)
        try await drag("detach-two", from: first, to: localPoint(targetPoint, in: first))
        for _ in 0..<100 {
            if (try await smokeJS("return state.threadTabs.items.length === 2 && !state.transferLocks?.size;", in: second.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let added = try await smokeJS("return state.threadTabs.items.some(t => t.thread_id === 'detach-one') && state.threadTabs.items.some(t => t.thread_id === 'detach-two') && document.querySelector('#message').value === 'Second tab draft';", in: second.webView)
        try smokeCheck("detach_drop_into_existing", added as? Bool == true && windows.count == 2)
        let emptyMain = try await smokeJS("return state.selected === null && !state.threadTabs.items.length;", in: first.webView)
        try smokeCheck("detach_last_tab_overview", emptyMain as? Bool == true && first.window.isVisible)
        // An outward drag from a multi-tab host moves just that tab to a new host.
        let multiOutside = NSPoint(x: (windows.map { $0.window.frame.maxX }.max() ?? outside.x) + 400, y: outside.y)
        try await drag("detach-one", from: second, to: localPoint(multiOutside, in: second))
        for _ in 0..<100 {
            if windows.count == 3 && windows.last?.navigationReady == true && conversationTransfers.isEmpty { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard let third = windows.last, third !== second else { try smokeCheck("detach_repeat_moves_tab", false); return }
        let repeatSource = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-two';", in: second.webView)
        let repeatTarget = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-one' && document.querySelector('#message').value === 'Keep detached draft';", in: third.webView)
        try smokeCheck("detach_repeat_moves_tab", repeatSource as? Bool == true && repeatTarget as? Bool == true && windows.count == 3)
        // Dock the singleton back into the original window; its empty host closes.
        first.window.setFrameOrigin(NSPoint(x: second.window.frame.minX - first.window.frame.width - 40, y: second.window.frame.minY))
        let rootPoint = NSPoint(x: first.window.frame.minX + 100, y: first.window.frame.maxY - 80)
        try await drag("detach-one", from: third, to: localPoint(rootPoint, in: third))
        for _ in 0..<100 {
            if windows.count == 2 && conversationTransfers.isEmpty { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let docked = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-one' && document.querySelector('#message').value === 'Keep detached draft';", in: first.webView)
        try smokeCheck("detach_dock_back_closes_empty_host", docked as? Bool == true && windows.count == 2 && !windows.contains { $0 === third })
        // Native identity, rather than the navigation URL, survives Reload.
        second.loaded = false
        second.navigationReady = false
        second.webView.reload()
        try await smokeReady(second)
        for _ in 0..<100 {
            if second.navigationReady { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let reloaded = try await smokeJS("return agentCoordDesktop.conversationWindow && state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-two' && getComputedStyle(document.querySelector('.sidebar')).display === 'none';", in: second.webView)
        try smokeCheck("detach_host_survives_reload", reloaded as? Bool == true)
        try smokeCheck("detach_shared_backend", backend.process?.isRunning == true)
        second.window.close()
    }

    @MainActor private func smokeSessionCycling(_ item: DesktopWindow) async throws {
        let fixture = try smokePython("""
            directory = str(Path(sys.argv[1]).parent.resolve() / 'Cycling workspace')
            Path(directory).mkdir(exist_ok=True)
            group = fixture_command('project', 'create', '--name', 'Cycling scope', '--cwd', directory)['id']
            fixture_command('register', '--session-id', 'outside-cycle', '--client', 'codex', '--cwd', directory)
            for name in ['cycling-one', 'cycling-two', 'cycling-three']:
                fixture_command('register', '--session-id', name, '--client', 'codex', '--cwd', directory)
                fixture_command('thread', 'update', '--session-id', name, '--title', name, '--project', group)
            print(json.dumps({'cwd': directory, 'group': group}))
            """)
        try await smokeInventoryReady(item, present: ["cycling-one", "cycling-two", "cycling-three"])
        _ = try await smokeJS("""
            await openProjectDirectory(cyclingDirectory);
            goHome();
            // Search now opens saved conversation results. Use the group filter on the sidebar being cycled.
            await savedViews.overview({project: cyclingGroup});
            goHome();
            // A pane's read receipt can trigger a newer list request, superseding
            // the awaited refresh. Wait for the filtered cards to be displayed.
            for (let i = 0; i < 100; i++) {
              window.cycleIDs = Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => b.dataset.thread);
              if (cycleIDs.length === 3 && cycleIDs.every(id => id.startsWith('cycling-'))) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            if (cycleIDs.length !== 3 || cycleIDs.some(id => !id.startsWith('cycling-'))) throw new Error('Cycling filter did not settle');
            await select(cycleIDs[cycleIDs.length - 1]);
            document.querySelector('#message').value = 'Preserve cycling draft';
            document.querySelector('#composer').hidden = false;
            document.querySelector('#message').disabled = false;
            document.querySelector('#message').focus();
            return true;
            """, in: item.webView, arguments: ["cyclingDirectory": fixture["cwd"]!, "cyclingGroup": fixture["group"]!])
        try await smokeFocus(item)
        try smokeCheck("next_session_shortcut", smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true))
        let next = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.detail?.work_thread.thread_id === cycleIDs[0]) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        if next as? Bool != true {
            smokeResults["next_session_debug"] = try await smokeJS("""
                return {selected: state.selected, detail: state.detail?.work_thread?.thread_id, initial: cycleIDs,
                  current: Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => ({id: b.dataset.thread, current: b.getAttribute('aria-current')})),
                  panes: agentCoordPanes.active, dialog: document.querySelector('dialog[open]')?.id || '',
                  error: document.querySelector('#error span').textContent};
                """, in: item.webView)
        }
        try smokeCheck("next_session_wraps_in_filtered_view", next as? Bool == true)
        try smokeCheck("previous_session_shortcut", smokeWebKey("\u{19}", flags: [.control, .shift], code: 48))
        let previous = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.detail?.work_thread.thread_id === cycleIDs[cycleIDs.length - 1])
                return document.querySelector('#message').value === 'Preserve cycling draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        smokeResults["session_cycle_debug"] = try await smokeJS("""
            return {selected: state.selected, draft: document.querySelector('#message').value,
              stored: Array.from(state.drafts), initial: cycleIDs,
              current: Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => b.dataset.thread),
              error: document.querySelector('#error span').textContent};
            """, in: item.webView)
        try smokeCheck("previous_session_wraps_and_preserves_draft", previous as? Bool == true)
        _ = smokeWebKey("\t", flags: [], code: 48, throughApplication: true)
        let plainTab = try await smokeJS("""
            await new Promise(resolve => setTimeout(resolve, 100));
            return state.selected === cycleIDs[cycleIDs.length - 1];
            """, in: item.webView)
        try smokeCheck("next_session_plain_tab_keeps_selection", plainTab as? Bool == true)
        _ = try await smokeJS("""
            window.cycleDialog = document.createElement('dialog');
            cycleDialog.innerHTML = '<input autofocus><button>Close</button>';
            document.body.append(cycleDialog); cycleDialog.showModal();
            return true;
            """, in: item.webView)
        _ = smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true)
        let modal = try await smokeJS("""
            await new Promise(resolve => setTimeout(resolve, 100));
            const kept = cycleDialog.open && state.selected === cycleIDs[cycleIDs.length - 1];
            cycleDialog.close(); cycleDialog.remove(); return kept;
            """, in: item.webView)
        try smokeCheck("next_session_dialog_keeps_selection", modal as? Bool == true)
        _ = try await smokeJS("""
            const panes = agentCoordPanes;
            if (!panes.tileCurrent()) throw new Error('Could not tile the three cycling sessions');
            const slot = panes.layout.groups.findIndex(group => group.includes(cycleIDs[0]));
            const thread = state.sessions.find(thread => thread.thread_id === cycleIDs[1]);
            // Use the same move as a tab drop, keeping a second pane visible.
            if (!panes.moveThread(thread, slot, 'center', 1)) throw new Error('Could not stack the second cycling tab');
            panes.focus(cycleIDs[0]);
            for (let i = 0; i < 100; i++) {
              const record = agentCoordPanes.focusedRecord;
              if (record?.api) {
                const doc = record.frame.contentDocument;
                doc.querySelector('#composer').hidden = false;
                const field = doc.querySelector('#message');
                field.disabled = false; field.value = 'Keep tiled keyboard draft'; field.focus();
                if (panes.selected === cycleIDs[0] && document.activeElement === record.frame && doc.activeElement === field) return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error('Tiled composer did not become focused');
            """, in: item.webView)
        // Closing the modal publishes enabled commands asynchronously across the native bridge.
        for _ in 0..<100 where !item.commands.contains("nextSession") {
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("next_session_native_command_enabled", item.commands.contains("nextSession"))
        try smokeCheck("next_session_tiled_shortcut", smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true))
        let tiled = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              const slot = agentCoordPanes.layout.groups.findIndex(group => group.includes(cycleIDs[0]));
              if (agentCoordPanes.selected === cycleIDs[1] && agentCoordPanes.layout.tabs[slot] === cycleIDs[1]) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        if tiled as? Bool != true {
            smokeResults["tiled_cycle_debug"] = try await smokeJS("""
                return {selected: agentCoordPanes.selected, groups: agentCoordPanes.layout.groups, tabs: agentCoordPanes.layout.tabs,
                  active: agentCoordPanes.active, focus: document.activeElement?.tagName,
                  error: document.querySelector('#error span').textContent};
                """, in: item.webView)
        }
        try smokeCheck("next_session_selects_stacked_tab", tiled as? Bool == true)
        _ = smokeWebKey("\u{19}", flags: [.control, .shift], code: 48, throughApplication: true)
        let paneDraft = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.selected === cycleIDs[0])
                return agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message').value === 'Keep tiled keyboard draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("previous_session_preserves_tiled_draft", paneDraft as? Bool == true)
    }

    @MainActor private func smokeWindowRestoration() async throws {
        let flag = CommandLine.arguments.firstIndex(of: "--smoke-restore-stage")!
        let stage = CommandLine.arguments[flag + 1]
        smokeResults["restore_stage"] = stage
        if stage == "seed" {
            let firstProject = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Project A")
            let secondProject = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Project B")
            for project in [firstProject, secondProject] {
                try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
            }
            let first = windows[0]
            let ids = try await smokeJS("""
                const result = [];
                for (const [name, cwd] of [['Restore A1', firstProject], ['Restore A2', firstProject], ['Restore B', secondProject], ['Restore detached', firstProject]]) {
                  const created = await api('sessions', {client: 'claude', name, cwd});
                  result.push(created.session.thread_id);
                }
                await refreshList(); return result;
                """, in: first.webView, arguments: ["firstProject": firstProject.path, "secondProject": secondProject.path]) as! [String]
            _ = try smokePython("""
                for index in range(30):
                    fixture_command('send', '--from-session', sys.argv[2], '--session', sys.argv[3],
                        f'Restored history {index} ' + 'Keep the saved reading position. ' * 15,
                        '--classification', 'informational', '--no-reply-required')
                print('{}')
                """, arguments: [ids[0], ids[3]])
            _ = try await smokeJS("""
                await select(firstID); document.querySelector('#message').value = 'background draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                await select(secondID); document.querySelector('#message').value = 'alpha draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                return true;
                """, in: first.webView, arguments: ["firstID": ids[0], "secondID": ids[1]])
            var route = URLComponents(url: serverURL!, resolvingAgainstBaseURL: false)!
            route.queryItems = [URLQueryItem(name: "directory", value: secondProject.path)]
            let second = makeWindow(url: route.url!)
            try await smokeReady(second)
            _ = try await smokeJS("""
                await select(thread); document.querySelector('#message').value = 'beta conversation draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                await newSession(); document.querySelector('#message').value = 'beta new-session draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                return true;
                """, in: second.webView, arguments: ["thread": ids[2]])
            route.queryItems = [URLQueryItem(name: "directory", value: firstProject.path), URLQueryItem(name: "detached", value: "1")]
            let third = makeWindow(url: route.url!)
            try await smokeReady(third)
            _ = try await smokeJS("""
                await select(thread);
                document.querySelector('#message').value = 'gamma detached draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                document.querySelector('#message').setSelectionRange(2, 7);
                state.attachments.drafts.set(thread, [{name:'pixel.png', url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=='}]);
                state.timelineScroll.restore(240); return true;
                """, in: third.webView, arguments: ["thread": ids[3]])
            let visible = NSScreen.main!.visibleFrame
            for (index, item) in [first, second, third].enumerated() {
                let frame = CGRect(x: visible.minX + 20 + CGFloat(index) * 70, y: visible.minY + 20 + CGFloat(index) * 40,
                    width: item.isConversationWindow ? 880 : 1000, height: 720)
                item.window.setFrame(WindowRestorationStore.usableFrame(frame, screens: [visible], minimum: item.window.minSize), display: true)
            }
            makeWindow(url: serverURL!.appendingPathComponent("settings"))
            let monitor = makeWindow(url: serverURL!.appendingPathComponent("monitor"))
            monitor.window.miniaturize(nil)
            // Record the expected state only after the minimize animation has finished.
            for _ in 0..<60 where !monitor.window.isMiniaturized { try await Task.sleep(nanoseconds: 50_000_000) }
            try smokeCheck("restore_seed_minimized_monitor", monitor.window.isMiniaturized)
            third.window.makeKeyAndOrderFront(nil)
            let expected: [[String: Any]] = [
                ["id": first.id, "threads": [ids[0], ids[1]], "selected": ids[1], "draft": "alpha draft", "project": try await smokeJS("return state.projectCwd;", in: first.webView)],
                ["id": second.id, "threads": [ids[2], "new-session"], "selected": "new-session", "draft": "beta new-session draft", "project": try await smokeJS("return state.projectCwd;", in: second.webView)],
                ["id": third.id, "threads": [ids[3]], "selected": ids[3], "draft": "gamma detached draft", "project": try await smokeJS("return state.projectCwd;", in: third.webView)],
            ]
            preferences.set(expected, forKey: "restoreSmokeExpected")
            preferences.set(second.id, forKey: "restoreSmokeClosed")
            preferences.set(windows.map { ["id": $0.id, "frame": NSStringFromRect($0.window.frame), "slot": $0.folderSlot, "minimized": $0.window.isMiniaturized] }, forKey: "restoreSmokeFrames")
            try smokeCheck("restore_seed_five_windows", windows.count == 5)
            for (index, item) in [first, second, third].enumerated() { try await smokeWindowImage(item, suffix: "before-\(index)") }
        } else if stage == "empty" {
            // Closing the final window keeps it, so relaunch restores that window.
            try smokeCheck("restore_final_closed_window_reopens", windows.count == 1 &&
                           windows[0].id == preferences.string(forKey: "restoreSmokeFinal") &&
                           windows[0].folderSlot == preferences.integer(forKey: "restoreSmokeFinalSlot"))
            try await smokeReady(windows[0])
            let tabs = try await smokeJS("return state.threadTabs.items.length;", in: windows[0].webView)
            try smokeCheck("restore_final_window_keeps_tabs", (tabs as? Int) == preferences.integer(forKey: "restoreSmokeFinalTabs"))
        } else {
            for item in windows where !item.isSettings && item.launchRoute != "/monitor" {
                try await smokeReady(item)
                for _ in 0..<200 {
                    if !item.restoringWorkspace && item.restoredWorkspace == nil { break }
                    try await Task.sleep(nanoseconds: 50_000_000)
                }
                try smokeCheck("restore_workspace_finished", !item.restoringWorkspace && item.restoredWorkspace == nil)
            }
            let closed = preferences.string(forKey: "restoreSmokeClosed")!
            let expectedCount = ["closed", "again", "lastclose"].contains(stage) ? 4 : 5
            try smokeCheck("restore_window_count", windows.count == expectedCount)
            let expected = preferences.array(forKey: "restoreSmokeExpected") as! [[String: Any]]
            for (index, record) in expected.enumerated() {
                let id = record["id"] as! String
                if expectedCount == 4 && id == closed { continue }
                guard let item = windows.first(where: { $0.id == id }) else { try smokeCheck("restore_stable_ids", false); return }
                let matched = try await smokeJS("""
                    const threads = state.threadTabs.items.map(item => item.thread_id);
                    return JSON.stringify(threads) === JSON.stringify(expectedThreads) && state.selected === expectedSelection &&
                      document.querySelector('#message').value === expectedDraft && state.projectCwd === expectedProject;
                    """, in: item.webView, arguments: ["expectedThreads": record["threads"]!, "expectedSelection": record["selected"]!,
                        "expectedDraft": record["draft"]!, "expectedProject": record["project"]!])
                smokeResults["restore_window_\(index)_debug"] = try await smokeJS("return {tabs: state.threadTabs.items.map(item => item.thread_id), selected: state.selected, draft: document.querySelector('#message').value, project: state.projectCwd, newDraft: !!state.newSessionDraft};", in: item.webView)
                smokeResults["restore_window_\(index)_expected"] = record
                try smokeCheck("restore_window_\(index)_state", matched as? Bool == true)
                if index < 2 {
                    let background = try await smokeJS("return state.drafts.get(thread) === text;", in: item.webView,
                        arguments: ["thread": (record["threads"] as! [String])[0], "text": index == 0 ? "background draft" : "beta conversation draft"])
                    try smokeCheck("restore_background_draft_\(index)", background as? Bool == true)
                }
                if index == 2 {
                    let extra = try await smokeJS("return agentCoordDesktop.conversationWindow && document.querySelector('#message').selectionStart === 2 && document.querySelector('#message').selectionEnd === 7 && state.attachments.items()[0]?.name === 'pixel.png' && Math.abs(state.timelineScroll.position() - 240) < 3;", in: item.webView)
                    try smokeCheck("restore_auxiliary_draft_image_scroll", extra as? Bool == true)
                }
                try await smokeWindowImage(item, suffix: "after-\(index)")
            }
            let frames = preferences.array(forKey: "restoreSmokeFrames") as! [[String: Any]]
            for frame in frames {
                guard let item = windows.first(where: { $0.id == frame["id"] as? String }) else { continue }
                let saved = NSRectFromString(frame["frame"] as! String)
                smokeResults["restore_frame_\(item.folderSlot)_debug"] = ["saved": frame["frame"]!, "actual": NSStringFromRect(item.window.frame)]
                try smokeCheck("restore_geometry_\(item.folderSlot)", abs(item.window.frame.minX - saved.minX) < 2 && abs(item.window.frame.minY - saved.minY) < 2 &&
                    abs(item.window.frame.width - saved.width) < 2 && abs(item.window.frame.height - saved.height) < 2 && item.folderSlot == frame["slot"] as? Int)
                smokeResults["restore_minimized_\(item.folderSlot)_debug"] = ["saved": frame["minimized"] as? Bool ?? false, "actual": item.window.isMiniaturized]
                try smokeCheck("restore_minimized_\(item.folderSlot)", item.window.isMiniaturized == frame["minimized"] as? Bool)
            }
            try smokeCheck("restore_settings_and_monitor", windows.contains(where: \.isSettings) && windows.contains { $0.launchRoute == "/monitor" })
            // macOS cooperative activation keeps a probe launched in the background
            // inactive, with no key window to compare; record that instead of guessing.
            if NSApp.isActive {
                try smokeCheck("restore_focused_window", current?.id == expected[2]["id"] as? String)
            } else {
                smokeResults["restore_focus_unverified_inactive_app"] = expected[2]["id"] as? String ?? ""
            }
            if stage == "verify", let first = windows.first(where: { $0.id == expected[0]["id"] as? String }) {
                _ = try await smokeJS("document.querySelector('#message').value = 'alpha final keystroke'; return true;", in: first.webView)
                var changed = expected
                changed[0]["draft"] = "alpha final keystroke"
                preferences.set(changed, forKey: "restoreSmokeExpected")
            }
            if stage == "close" {
                windows.first { $0.id == closed }!.window.close()
                try smokeCheck("restore_explicit_close", !windows.contains { $0.id == closed })
            }
            if stage == "closed" {
                let fresh = makeWindow()
                try await smokeReady(fresh)
                let empty = try await smokeJS("return state.threadTabs.items.length === 0 && state.selected === null;", in: fresh.webView)
                try smokeCheck("restore_new_window_does_not_adopt_closed_tabs", empty as? Bool == true)
                fresh.window.close()
            }
            if stage == "lastclose" {
                let final = windows.last { !$0.isSettings && $0.launchRoute != "/monitor" }!
                let tabs = try await smokeJS("return state.threadTabs.items.length;", in: final.webView)
                for item in Array(windows) where item !== final { item.window.close() }
                final.window.close()
                try smokeCheck("restore_released_windows_stay_closed",
                               windows.count == 1 && windows[0] === final && final.restorable)
                preferences.set(final.id, forKey: "restoreSmokeFinal")
                preferences.set(final.folderSlot, forKey: "restoreSmokeFinalSlot")
                preferences.set(tabs as? Int ?? -1, forKey: "restoreSmokeFinalTabs")
            }
        }
        smokeResults["restored_ids"] = windows.map(\.id)
        smokeResults["runtime_survives_interface_quit"] = backend.process?.isRunning == true
        if let ready = CommandLine.arguments.firstIndex(of: "--smoke-restore-ready"),
           let proceed = CommandLine.arguments.firstIndex(of: "--smoke-restore-continue") {
            try Data("ready".utf8).write(to: URL(fileURLWithPath: CommandLine.arguments[ready + 1]))
            let deadline = Date().addingTimeInterval(20)
            while !FileManager.default.fileExists(atPath: CommandLine.arguments[proceed + 1]) && Date() < deadline {
                try await Task.sleep(nanoseconds: 50_000_000)
            }
            try smokeCheck("restore_duplicate_launch_keeps_window_set", windows.count == (stage == "verify" ? 5 : 4))
        }
    }

    /// Delivers a native press to the view that wins the window's hit test, as a
    /// pointer click would arrive, without depending on the app becoming active.
    /// Returns whether the press reached the web view rather than the titlebar.
    @discardableResult private func smokeClick(_ item: DesktopWindow, page point: NSPoint) -> Bool {
        let location = item.webView.convert(point, to: nil)
        guard let frame = item.window.contentView?.superview, let target = frame.hitTest(location) else { return false }
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            guard let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [],
                    timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: item.window.windowNumber,
                    context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0) else { continue }
            if type == .leftMouseDown { target.mouseDown(with: event) } else { target.mouseUp(with: event) }
        }
        return target.isDescendant(of: item.webView)
    }

    @MainActor private func smokeTitlebarLayout(_ item: DesktopWindow) async throws -> [String: Any] {
        try await smokeJS("""
            const root = document.documentElement, header = document.querySelector('main > .workspace-bar');
            const rect = header.getBoundingClientRect(), toggle = document.querySelector('#menu-toggle').getBoundingClientRect();
            const tab = document.querySelector('.header-tabs .project-tab')?.getBoundingClientRect();
            const empty = document.querySelector('.header-tabs .projects-bar')?.getBoundingClientRect();
            return {native: root.classList.contains('native-titlebar'), controls: root.classList.contains('native-titlebar-controls'),
                leading: parseFloat(root.style.getPropertyValue('--native-titlebar-leading')),
                top: rect.top, height: rect.height, left: rect.left, paddingLeft: parseFloat(getComputedStyle(header).paddingLeft),
                toggleX: toggle.left + toggle.width / 2, toggleY: toggle.top + toggle.height / 2, toggleLeft: toggle.left,
                tabCenter: tab ? tab.top + tab.height / 2 : -1, emptyX: empty ? empty.right - 8 : rect.right - 60,
                sidebarTop: parseFloat(getComputedStyle(document.querySelector('.sidebar')).paddingTop),
                collapsed: document.body.classList.contains('sidebar-collapsed')};
            """, in: item.webView) as? [String: Any] ?? [:]
    }

    @MainActor private func smokeTitlebar(_ item: DesktopWindow) async throws {
        let band = IntegratedTitlebar.height, center = IntegratedTitlebar.controlCenter
        let aligned = { (window: NSWindow) in
            IntegratedTitlebar.controlCenters(window).count == 3
                && IntegratedTitlebar.controlCenters(window).allSatisfy { abs($0 - center) <= 1 }
        }
        // Open two projects so the band shows its tabs.
        let projects = ["Ribbon project", "Second project"].map {
            smokeDirectory!.appendingPathComponent($0, isDirectory: true).resolvingSymlinksInPath().path
        }
        for path in projects { try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true) }
        let paths = String(data: try JSONSerialization.data(withJSONObject: projects), encoding: .utf8)!
        _ = try await smokeJS("""
            const original = state.folderPicker.desktop;
            try {
              for (const path of \(paths)) {
                state.folderPicker.desktop = {chooseFolder: async () => path};
                await state.folderPicker.choose({navigate: false});
              }
            } finally { state.folderPicker.desktop = original; }
            return true;
            """, in: item.webView)
        item.window.setFrame(NSRect(x: 80, y: 80, width: 1240, height: 820), display: true)
        try await Task.sleep(nanoseconds: 300_000_000)
        smokeResults["titlebar_control_centers"] = IntegratedTitlebar.controlCenters(item.window).map(Double.init)
        try smokeCheck("titlebar_controls_centered", aligned(item.window))
        var layout = try await smokeTitlebarLayout(item)
        if layout["collapsed"] as? Bool == true {
            _ = try await smokeJS("document.querySelector('#menu-toggle').click(); return true;", in: item.webView)
            layout = try await smokeTitlebarLayout(item)
        }
        smokeResults["titlebar_layout_expanded"] = layout
        let leading = Double(item.titlebarLeading ?? -1)
        try smokeCheck("titlebar_page_state", layout["native"] as? Bool == true && layout["controls"] as? Bool == true
            && layout["leading"] as? Double == leading)
        try smokeCheck("titlebar_header_in_band", layout["top"] as? Double == 0
            && abs((layout["height"] as? Double ?? 0) - Double(band)) <= 1)
        try smokeCheck("titlebar_sidebar_clears_controls", layout["sidebarTop"] as? Double == Double(band)
            && (layout["left"] as? Double ?? 0) >= leading)
        if let tab = layout["tabCenter"] as? Double, tab >= 0 {
            try smokeCheck("titlebar_tabs_match_controls", abs(tab - Double(center)) <= 1.5)
        }
        // Native presses in the band must reach web controls; empty band space requests a window drag.
        item.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        try await Task.sleep(nanoseconds: 300_000_000)
        smokeResults["titlebar_window_key"] = item.window.isKeyWindow
        smokeResults["titlebar_app_active"] = NSApp.isActive
        let toggle = NSPoint(x: layout["toggleX"] as? Double ?? 0, y: layout["toggleY"] as? Double ?? 0)
        try smokeCheck("titlebar_band_hits_web_view", smokeClick(item, page: toggle))
        try await Task.sleep(nanoseconds: 300_000_000)
        let collapsed = try await smokeTitlebarLayout(item)
        smokeResults["titlebar_layout_collapsed"] = collapsed
        try smokeCheck("titlebar_band_click_reaches_page", collapsed["collapsed"] as? Bool == true)
        try smokeCheck("titlebar_collapsed_header_clears_controls", collapsed["paddingLeft"] as? Double == leading
            && (collapsed["toggleLeft"] as? Double ?? 0) >= leading)
        try await smokeWindowImage(item, suffix: "titlebar-collapsed")
        smokeClick(item, page: NSPoint(x: collapsed["toggleX"] as? Double ?? 0, y: collapsed["toggleY"] as? Double ?? 0))
        try await Task.sleep(nanoseconds: 300_000_000)
        try await smokeWindowImage(item, suffix: "titlebar")
        let before = smokeResults["titlebar_drag_requests"] as? Int ?? 0
        smokeClick(item, page: NSPoint(x: layout["emptyX"] as? Double ?? 0, y: 12))
        try await Task.sleep(nanoseconds: 300_000_000)
        try smokeCheck("titlebar_empty_band_requests_drag", smokeResults["titlebar_drag_requests"] as? Int == before + 1)
        smokeClick(item, page: NSPoint(x: layout["emptyX"] as? Double ?? 0, y: Double(band) + 40))
        try await Task.sleep(nanoseconds: 300_000_000)
        try smokeCheck("titlebar_content_press_ignored", smokeResults["titlebar_drag_requests"] as? Int == before + 1)
        item.window.setFrame(NSRect(x: 120, y: 60, width: 980, height: 700), display: true)
        try await Task.sleep(nanoseconds: 300_000_000)
        try smokeCheck("titlebar_controls_after_resize", aligned(item.window))
        // The native file pane leaves the band to the window and starts its content below it.
        item.filesItem?.isCollapsed = false
        item.split.view.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 300_000_000)
        let pane = item.files.view
        let drag = pane.subviews.first { $0 is TitlebarDragView }
        let content = pane.subviews.first { $0 is NSStackView }
        let top = { (view: NSView) in item.window.frame.height - view.convert(view.bounds, to: nil).maxY }
        let filePaneMetrics: [String: Double] = [
            "drag_top": drag.map(top).map(Double.init) ?? -1,
            "drag_height": Double(drag?.frame.height ?? -1),
            "content_top": content.map(top).map(Double.init) ?? -1,
        ]
        smokeResults["titlebar_file_pane"] = filePaneMetrics
        try smokeCheck("titlebar_file_pane_band", drag.map(top) == 0 && drag?.frame.height == band
            && (content.map(top) ?? 0) >= band)
        // The narrower conversation area uses the compact header, which must also clear the controls.
        let compact = try await smokeTitlebarLayout(item)
        smokeResults["titlebar_layout_compact"] = compact
        try smokeCheck("titlebar_compact_header_clears_controls", (compact["toggleLeft"] as? Double ?? 0) >= leading)
        if let view = item.window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: smokeReport!.appendingPathExtension("titlebar-files.png"))
        }
        item.filesItem?.isCollapsed = true
    }

    @MainActor private func smokeWindowImage(_ item: DesktopWindow, suffix: String) async throws {
        let image: NSImage? = try await withCheckedThrowingContinuation { continuation in
            item.webView.takeSnapshot(with: nil) { image, error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: image) }
            }
        }
        if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: URL(fileURLWithPath: smokeReport!.path + "." + suffix + ".png"))
        }
    }

    func finishSmoke(error: String?) {
        guard let report = smokeReport, !quitting else { return }
        smokeResults["error"] = error ?? NSNull()
        if let data = try? JSONSerialization.data(withJSONObject: smokeResults, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: report)
        }
        // Terminate outside the Swift task/GCD callback. AppKit's deferred
        // termination loop must remain able to drain main-queue cleanup work.
        RunLoop.main.perform { NSApp.terminate(nil) }
    }
}
#endif
