// SSO Login Webview — sandboxed browser for AWS SSO authentication.
//
// Two modes:
//   Direct:  sso-webview <authorize-url> <callback-host:port>
//            Opens the auth URL immediately in a WKWebView.
//
//   Notify:  sso-webview --notify <profile> <callback-host:port>
//            Shows a notification page (Refresh/Snooze/Don't Remind).
//            On Refresh: signals SSO_ACTION:refresh, shows spinner,
//            reads authorize URL from stdin, navigates to it.
//
// Persistent cookie storage (Google/IdP credentials cached across launches).
// Detects OAuth callback redirect and signals parent via stdout.
//
// Stdout signals (one per line, parent reads these):
//   SSO_CALLBACK_DETECTED  — OAuth callback received, login succeeded
//   SSO_WINDOW_CLOSED      — user closed the window before completing auth
//   SSO_TIMEOUT            — window timeout reached without callback
//   SSO_ERROR:<detail>     — startup or runtime error
//   SSO_ACTION:refresh     — user clicked Refresh (notify mode)
//   SSO_ACTION:snooze:<N>  — user chose snooze for N seconds
//   SSO_ACTION:suppress    — user chose Don't Remind
//   (dismiss is inferred from SSO_WINDOW_CLOSED or SSO_TIMEOUT, not a separate action)
//
// Exit codes:
//   0  callback detected (success)
//   1  error (bad args, missing content view, etc.)
//   2  user closed window
//   3  timeout
//   4  snooze
//   5  suppress

import Cocoa
import WebKit

// MARK: - Constants

private enum Layout {
    static let windowWidth: CGFloat = 820
    static let windowHeight: CGFloat = 720
    static let minWidth: CGFloat = 480
    static let minHeight: CGFloat = 400
    static let progressBarHeight: CGFloat = 3
    static let notifyWindowWidth: CGFloat = 480
    static let notifyWindowHeight: CGFloat = 340
}

private enum Timing {
    static let postCallbackDelay: TimeInterval = 1.5
    static let windowTimeout: TimeInterval = 300
    static let notifyTimeout: TimeInterval = 120
    static let stdinTimeout: TimeInterval = 45
}

private enum ExitCode: Int32 {
    case success = 0
    case error = 1
    case userClosed = 2
    case timeout = 3
    case snooze = 4
    case suppress = 5
}

private let snoozeOptions: [(label: String, seconds: Int)] = [
    ("15 min", 900),
    ("30 min", 1800),
    ("1 hour", 3600),
    ("4 hours", 14400),
]

// MARK: - Navigation Delegate

/// Monitors webview navigation to detect the OAuth callback redirect.
final class SSONavigationDelegate: NSObject, WKNavigationDelegate {
    private let callbackPattern: String
    private let onCallbackDetected: () -> Void
    private var callbackFired = false

    init(callbackPattern: String, onCallbackDetected: @escaping () -> Void) {
        self.callbackPattern = callbackPattern
        self.onCallbackDetected = onCallbackDetected
        super.init()
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }

        if !callbackFired && matchesCallback(url) {
            callbackFired = true
            decisionHandler(.allow)
            onCallbackDetected()
            return
        }

        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        if !navigationResponse.canShowMIMEType {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        let nsError = error as NSError

        if !callbackFired && nsError.code == NSURLErrorCannotConnectToHost {
            if let url = failingURL(from: error), matchesCallback(url) {
                callbackFired = true
                onCallbackDetected()
                return
            }
        }

        guard nsError.code != NSURLErrorCancelled else { return }

        let escapedMessage = escapeHTML(error.localizedDescription)
        let retryURL = escapeHTML(webView.url?.absoluteString ?? "")
        let html = """
        <html><body style="font-family:-apple-system,system-ui;padding:40px;text-align:center">
        <h2>Connection Failed</h2>
        <p style="color:#666">\(escapedMessage)</p>
        <p style="margin-top:20px"><a href="\(retryURL)">Retry</a></p>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func matchesCallback(_ url: URL) -> Bool {
        let host = url.host ?? ""
        let hostPort: String
        if let port = url.port, port != 80 {
            hostPort = "\(host):\(port)"
        } else {
            hostPort = host
        }
        if hostPort == callbackPattern { return true }
        let alt = callbackPattern.replacingOccurrences(of: "127.0.0.1", with: "localhost")
        if hostPort == alt { return true }
        let alt2 = callbackPattern.replacingOccurrences(of: "localhost", with: "127.0.0.1")
        return hostPort == alt2
    }

    private func failingURL(from error: Error) -> URL? {
        (error as NSError).userInfo[NSURLErrorFailingURLErrorKey] as? URL
    }

    private func escapeHTML(_ string: String) -> String {
        string
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }
}

// MARK: - Notification View (native macOS)

/// Native notification view shown in --notify mode before auth.
final class NotificationView: NSView {
    var onRefresh: (() -> Void)?
    var onSnooze: ((Int) -> Void)?
    var onSuppress: (() -> Void)?

    private var spinner: NSProgressIndicator?
    private var statusLabel: NSTextField?
    private var buttonStack: NSStackView?

    init(profile: String) {
        super.init(frame: .zero)
        setupUI(profile: profile)
    }

    required init?(coder: NSCoder) { fatalError() }

    private func setupUI(profile: String) {
        // Icon
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: "lock.trianglebadge.exclamationmark",
                             accessibilityDescription: "credentials expired")
            ?? NSImage(named: NSImage.cautionName)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 36, weight: .light)
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.setContentHuggingPriority(.required, for: .vertical)

        // Title
        let title = NSTextField(labelWithString: "AWS SSO Credentials Expired")
        title.font = .systemFont(ofSize: 17, weight: .semibold)
        title.alignment = .center
        title.translatesAutoresizingMaskIntoConstraints = false

        // Subtitle
        let subtitle = NSTextField(labelWithString: "Profile: \(profile)")
        subtitle.font = .systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        subtitle.alignment = .center
        subtitle.translatesAutoresizingMaskIntoConstraints = false

        // Buttons
        let refreshBtn = NSButton(title: "Refresh", target: self, action: #selector(refreshClicked))
        refreshBtn.bezelStyle = .rounded
        refreshBtn.keyEquivalent = "\r"
        refreshBtn.controlSize = .large

        let snoozeBtn = NSButton(title: "Snooze", target: self, action: #selector(snoozeClicked))
        snoozeBtn.bezelStyle = .rounded
        snoozeBtn.controlSize = .large

        let suppressBtn = NSButton(title: "Don't Remind", target: self, action: #selector(suppressClicked))
        suppressBtn.bezelStyle = .rounded
        suppressBtn.controlSize = .small
        suppressBtn.font = .systemFont(ofSize: 11)

        let mainButtons = NSStackView(views: [refreshBtn, snoozeBtn])
        mainButtons.spacing = 12
        mainButtons.translatesAutoresizingMaskIntoConstraints = false

        suppressBtn.translatesAutoresizingMaskIntoConstraints = false

        // Spinner (hidden initially)
        let spin = NSProgressIndicator()
        spin.style = .spinning
        spin.controlSize = .small
        spin.isHidden = true
        spin.translatesAutoresizingMaskIntoConstraints = false
        self.spinner = spin

        // Status label (hidden initially)
        let status = NSTextField(labelWithString: "")
        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.isHidden = true
        status.translatesAutoresizingMaskIntoConstraints = false
        self.statusLabel = status

        // Stack it all
        let stack = NSStackView(views: [icon, title, subtitle, mainButtons, suppressBtn, spin, status])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 12
        stack.setCustomSpacing(4, after: title)
        stack.setCustomSpacing(24, after: subtitle)
        stack.setCustomSpacing(16, after: mainButtons)
        stack.setCustomSpacing(8, after: spin)
        stack.translatesAutoresizingMaskIntoConstraints = false

        addSubview(stack)
        self.buttonStack = mainButtons

        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 30),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -30),
        ])
    }

    func showConnecting() {
        buttonStack?.isHidden = true
        // Hide suppress button (it's a sibling in the parent stack)
        if let stack = buttonStack?.superview as? NSStackView {
            for view in stack.arrangedSubviews {
                if let btn = view as? NSButton, btn.title == "Don't Remind" {
                    btn.isHidden = true
                }
            }
        }
        spinner?.isHidden = false
        spinner?.startAnimation(nil)
        statusLabel?.stringValue = "Connecting to SSO..."
        statusLabel?.isHidden = false
    }

    func showError(_ message: String) {
        spinner?.stopAnimation(nil)
        statusLabel?.stringValue = message
        statusLabel?.textColor = .systemRed
    }

    @objc private func refreshClicked() {
        onRefresh?()
    }

    @objc private func snoozeClicked() {
        let menu = NSMenu()
        for opt in snoozeOptions {
            let item = NSMenuItem(title: opt.label, action: #selector(snoozeSelected(_:)), keyEquivalent: "")
            item.target = self
            item.tag = opt.seconds
            menu.addItem(item)
        }
        if let event = NSApp.currentEvent {
            NSMenu.popUpContextMenu(menu, with: event, for: self)
        }
    }

    @objc private func snoozeSelected(_ sender: NSMenuItem) {
        onSnooze?(sender.tag)
    }

    @objc private func suppressClicked() {
        // Confirm
        let alert = NSAlert()
        alert.messageText = "Disable SSO Reminders?"
        alert.informativeText = "Reminders will be disabled until a new signal is received.\n\nTo refresh later:\n  mise run sso-login"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Disable Reminders")
        if alert.runModal() == .alertSecondButtonReturn {
            onSuppress?()
        }
    }
}

// MARK: - App Delegate

final class SSOAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var progressBar: NSView?
    private var notificationView: NotificationView?
    private var navigationDelegate: SSONavigationDelegate?
    private var timeoutTimer: Timer?
    private var progressObservation: NSKeyValueObservation?
    private var terminationReason: ExitCode = .userClosed

    private let launchConfig: LaunchConfig
    private var callbackHost: String

    init(config: LaunchConfig) {
        self.launchConfig = config
        self.callbackHost = config.callbackHost
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()

        switch launchConfig.mode {
        case .direct(let url):
            setupWindow(size: .login)
            setupWebView()
            setupTimeout(Timing.windowTimeout)
            navigateToAuth(url: url)
        case .notify(let profile):
            setupWindow(size: .notify)
            showNotification(profile: profile)
            setupTimeout(Timing.notifyTimeout)
            NSSound.beep()
            NSSound.beep()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        progressObservation?.invalidate()
        progressObservation = nil
        timeoutTimer?.invalidate()
        timeoutTimer = nil

        switch terminationReason {
        case .success:
            signal("SSO_CALLBACK_DETECTED")
        case .userClosed:
            signal("SSO_WINDOW_CLOSED")
        case .timeout:
            signal("SSO_TIMEOUT")
        case .error:
            signal("SSO_ERROR:unexpected termination")
        case .snooze, .suppress:
            break  // already signaled
        }
    }

    // MARK: - NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        // terminationReason is already set by the action that triggered close
    }

    // MARK: - Window sizes

    private enum WindowSize {
        case login
        case notify
    }

    // MARK: - Setup

    private func setupMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.close), keyEquivalent: "w")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        NSApp.mainMenu = mainMenu
    }

    private func setupWindow(size: WindowSize) {
        let (w, h): (CGFloat, CGFloat) = {
            switch size {
            case .login:  return (Layout.windowWidth, Layout.windowHeight)
            case .notify: return (Layout.notifyWindowWidth, Layout.notifyWindowHeight)
            }
        }()

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "AWS SSO Login"
        win.minSize = NSSize(width: Layout.minWidth, height: Layout.minHeight)
        win.center()
        win.delegate = self
        win.makeKeyAndOrderFront(nil)

        // Float above other windows so notification isn't lost behind
        // the user's current app (we're launched from a background agent)
        win.level = .floating

        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }

        // Re-activate after a brief delay — macOS sometimes steals focus
        // back from apps launched via `open -a` from background processes
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            win.makeKeyAndOrderFront(nil)
            if #available(macOS 14.0, *) {
                NSApp.activate()
            } else {
                NSApp.activate(ignoringOtherApps: true)
            }
        }

        self.window = win
    }

    private func setupWebView() {
        guard let contentView = window?.contentView else {
            terminationReason = .error
            signal("SSO_ERROR:no content view")
            NSApp.terminate(nil)
            return
        }

        let config = WKWebViewConfiguration()
        config.websiteDataStore = WKWebsiteDataStore.default()

        let wv = WKWebView(frame: contentView.bounds, configuration: config)
        wv.autoresizingMask = [.width, .height]

        let bar = NSView(frame: NSRect(
            x: 0,
            y: contentView.bounds.height - Layout.progressBarHeight,
            width: 0,
            height: Layout.progressBarHeight
        ))
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        bar.autoresizingMask = [.minYMargin]
        bar.isHidden = true

        navigationDelegate = SSONavigationDelegate(
            callbackPattern: callbackHost,
            onCallbackDetected: { [weak self] in self?.handleCallback() }
        )
        wv.navigationDelegate = navigationDelegate

        progressObservation = wv.observe(\.estimatedProgress, options: [.new]) { [weak bar, weak contentView] webView, _ in
            guard let bar = bar, let contentView = contentView else { return }
            DispatchQueue.main.async {
                let progress = webView.estimatedProgress
                let totalWidth = contentView.bounds.width
                bar.isHidden = progress >= 1.0
                NSAnimationContext.runAnimationGroup { ctx in
                    ctx.duration = 0.2
                    bar.animator().frame.size.width = totalWidth * CGFloat(progress)
                }
            }
        }

        contentView.addSubview(wv)
        contentView.addSubview(bar)

        self.webView = wv
        self.progressBar = bar
    }

    private func setupTimeout(_ interval: TimeInterval) {
        timeoutTimer?.invalidate()
        timeoutTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            self?.terminationReason = .timeout
            NSApp.terminate(nil)
        }
    }

    // MARK: - Notification mode

    private func showNotification(profile: String) {
        guard let contentView = window?.contentView else { return }

        let nv = NotificationView(profile: profile)
        nv.frame = contentView.bounds
        nv.autoresizingMask = [.width, .height]
        nv.translatesAutoresizingMaskIntoConstraints = false

        nv.onRefresh = { [weak self] in self?.handleRefresh() }
        nv.onSnooze = { [weak self] seconds in self?.handleSnooze(seconds) }
        nv.onSuppress = { [weak self] in self?.handleSuppress() }

        contentView.addSubview(nv)
        NSLayoutConstraint.activate([
            nv.topAnchor.constraint(equalTo: contentView.topAnchor),
            nv.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            nv.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            nv.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
        ])
        self.notificationView = nv
    }

    private func handleRefresh() {
        signal("SSO_ACTION:refresh")
        notificationView?.showConnecting()

        // Cancel the notification timeout, we'll set a new one for the auth
        timeoutTimer?.invalidate()
        timeoutTimer = nil

        // Read authorize URL from stdin on background thread
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let url = self.readURLFromStdin()
            DispatchQueue.main.async {
                if let url = url {
                    self.transitionToAuth(url: url)
                } else {
                    self.notificationView?.showError("Failed to connect to SSO. Close and retry.")
                    self.setupTimeout(30)
                }
            }
        }
    }

    private func readURLFromStdin() -> URL? {
        // Read one line from stdin with a timeout.
        // The watcher writes the authorize URL after starting aws sso login.
        let deadline = Date().addingTimeInterval(Timing.stdinTimeout)
        while Date() < deadline {
            if let line = readLine(strippingNewline: true), !line.isEmpty {
                return URL(string: line)
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return nil
    }

    private func transitionToAuth(url: URL) {
        // Drop from floating to normal — user committed to logging in,
        // no longer need to compete for attention
        if let win = window {
            win.level = .normal
            let newFrame = NSRect(
                x: win.frame.midX - Layout.windowWidth / 2,
                y: win.frame.midY - Layout.windowHeight / 2,
                width: Layout.windowWidth,
                height: Layout.windowHeight
            )
            win.setFrame(newFrame, display: true, animate: true)
        }

        // Remove notification view
        notificationView?.removeFromSuperview()
        notificationView = nil

        // Setup webview and load
        setupWebView()
        setupTimeout(Timing.windowTimeout)
        navigateToAuth(url: url)
    }

    private func handleSnooze(_ seconds: Int) {
        signal("SSO_ACTION:snooze:\(seconds)")
        terminationReason = .snooze
        NSApp.terminate(nil)
    }

    private func handleSuppress() {
        signal("SSO_ACTION:suppress")
        terminationReason = .suppress
        NSApp.terminate(nil)
    }

    // MARK: - Auth navigation

    private func navigateToAuth(url: URL) {
        webView?.load(URLRequest(url: url))
    }

    // MARK: - Callback handling

    private func handleCallback() {
        timeoutTimer?.invalidate()
        timeoutTimer = nil
        terminationReason = .success

        DispatchQueue.main.asyncAfter(deadline: .now() + Timing.postCallbackDelay) {
            NSApp.terminate(nil)
        }
    }

    // MARK: - Signaling

    private func signal(_ message: String) {
        print(message)
        fflush(stdout)
    }
}

// MARK: - Launch configuration

enum LaunchMode {
    case direct(URL)
    case notify(profile: String)
}

struct LaunchConfig {
    let mode: LaunchMode
    let callbackHost: String
}

func parseArguments() -> LaunchConfig? {
    let args = CommandLine.arguments

    // --notify <profile> <callback-host:port>
    if args.count >= 2 && args[1] == "--notify" {
        guard args.count >= 3 else {
            fputs("Usage: sso-webview --notify <profile> [callback-host:port]\n", stderr)
            return nil
        }
        let profile = args[2]
        let callback = args.count >= 4 ? args[3] : "127.0.0.1"
        return LaunchConfig(mode: .notify(profile: profile), callbackHost: callback)
    }

    // Direct: <authorize-url> [callback-host:port]
    guard args.count >= 2 else {
        fputs("Usage: sso-webview <authorize-url> [callback-host:port]\n", stderr)
        fputs("       sso-webview --notify <profile> [callback-host:port]\n", stderr)
        return nil
    }

    guard let url = URL(string: args[1]) else {
        fputs("Error: invalid URL: \(args[1])\n", stderr)
        return nil
    }

    let callbackHost = args.count >= 3 ? args[2] : "127.0.0.1"
    return LaunchConfig(mode: .direct(url), callbackHost: callbackHost)
}

// MARK: - Entry point

guard let config = parseArguments() else {
    exit(ExitCode.error.rawValue)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let delegate = SSOAppDelegate(config: config)
app.delegate = delegate
app.run()
