// SSO Login Webview — sandboxed browser for AWS SSO authentication.
//
// Opens an OAuth authorization URL in a dedicated WKWebView window with
// persistent cookie storage (Google/IdP credentials cached across launches).
// Detects the OAuth callback redirect and signals the parent process via
// stdout, then exits cleanly.
//
// Usage: SSOLogin.app/Contents/MacOS/sso-webview <authorize-url> <callback-host:port>
//
// Stdout signals (one per line, parent reads these):
//   SSO_CALLBACK_DETECTED  — OAuth callback received, login succeeded
//   SSO_WINDOW_CLOSED      — user closed the window before completing auth
//   SSO_TIMEOUT            — window timeout reached without callback
//   SSO_ERROR:<detail>     — startup or runtime error
//
// Exit codes:
//   0  callback detected (success)
//   1  error (bad args, missing content view, etc.)
//   2  user closed window
//   3  timeout

import Cocoa
import WebKit

// MARK: - Constants

private enum Layout {
    static let windowWidth: CGFloat = 820
    static let windowHeight: CGFloat = 720
    static let minWidth: CGFloat = 480
    static let minHeight: CGFloat = 400
    static let progressBarHeight: CGFloat = 3
}

private enum Timing {
    static let postCallbackDelay: TimeInterval = 1.5
    static let windowTimeout: TimeInterval = 300
}

private enum ExitCode: Int32 {
    case success = 0
    case error = 1
    case userClosed = 2
    case timeout = 3
}

// MARK: - Navigation Delegate

/// Monitors webview navigation to detect the OAuth callback redirect.
///
/// Detection strategies (belt and suspenders):
/// 1. Navigation to callback host:port (127.0.0.1:PORT from redirect_uri)
/// 2. Navigation to localhost:PORT (some AWS CLI versions use localhost)
/// 3. Landing on the SSO portal page (means auth completed, callback already fired)
/// 4. Failed navigation to callback (connection refused = aws already received it)
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
        // Prevent downloads (e.g. callback response with Content-Disposition)
        if !navigationResponse.canShowMIMEType {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation!
    ) {
        // Detect SSO portal page as success (auth completed, callback already handled)
        guard !callbackFired, let url = webView.url else { return }
        let urlStr = url.absoluteString
        if urlStr.contains("/start#/") || urlStr.contains("/start/#") ||
           urlStr.contains("awsapps.com/start") {
            callbackFired = true
            onCallbackDetected()
        }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        let nsError = error as NSError

        // Connection refused to callback host = aws CLI already got the callback
        // and shut down its local server. Treat as success.
        if !callbackFired && nsError.code == NSURLErrorCannotConnectToHost {
            if let url = navigationAction(from: error), matchesCallback(url) {
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

    /// Match callback by host:port (e.g. "127.0.0.1:60137").
    /// Also matches localhost variants.
    private func matchesCallback(_ url: URL) -> Bool {
        let host = url.host ?? ""
        let hostPort: String
        if let port = url.port, port != 80 {
            hostPort = "\(host):\(port)"
        } else {
            hostPort = host
        }
        if hostPort == callbackPattern { return true }
        // Also match localhost<->127.0.0.1 substitution
        let alt = callbackPattern.replacingOccurrences(of: "127.0.0.1", with: "localhost")
        if hostPort == alt { return true }
        let alt2 = callbackPattern.replacingOccurrences(of: "localhost", with: "127.0.0.1")
        return hostPort == alt2
    }

    /// Try to extract the failing URL from an error's userInfo.
    private func navigationAction(from error: Error) -> URL? {
        let nsError = error as NSError
        return nsError.userInfo[NSURLErrorFailingURLErrorKey] as? URL
    }

    private func escapeHTML(_ string: String) -> String {
        string
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }
}

// MARK: - App Delegate

final class SSOAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var progressBar: NSView?
    private var navigationDelegate: SSONavigationDelegate?
    private var timeoutTimer: Timer?
    private var progressObservation: NSKeyValueObservation?
    private var terminationReason: ExitCode = .userClosed

    private let authorizeURL: URL
    private let callbackHost: String

    init(authorizeURL: URL, callbackHost: String) {
        self.authorizeURL = authorizeURL
        self.callbackHost = callbackHost
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()
        setupWindow()
        setupWebView()
        setupTimeout()
        loadAuthorizeURL()
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
        }
    }

    // MARK: - NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        // terminationReason is already .userClosed by default;
        // if callback was detected, it was set to .success before we get here.
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

        // Edit menu — required for Cmd+C/V/X/A in webview text fields
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

    private func setupWindow() {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: Layout.windowWidth, height: Layout.windowHeight),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "AWS SSO Login"
        win.minSize = NSSize(width: Layout.minWidth, height: Layout.minHeight)
        win.center()
        win.delegate = self
        win.makeKeyAndOrderFront(nil)

        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
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

        // Progress bar — animates width from 0% to estimatedProgress
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

        // Modern KVO for progress tracking
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

    private func setupTimeout() {
        timeoutTimer = Timer.scheduledTimer(withTimeInterval: Timing.windowTimeout, repeats: false) { [weak self] _ in
            self?.terminationReason = .timeout
            NSApp.terminate(nil)
        }
    }

    private func loadAuthorizeURL() {
        webView?.load(URLRequest(url: authorizeURL))
    }

    // MARK: - Callback handling

    private func handleCallback() {
        timeoutTimer?.invalidate()
        timeoutTimer = nil
        terminationReason = .success

        // Brief delay so aws-cli's local HTTP server receives the redirect
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

// MARK: - Entry point

func parseArguments() -> (url: URL, callbackHost: String)? {
    let args = CommandLine.arguments
    guard args.count >= 2 else {
        fputs("Usage: sso-webview <authorize-url> [callback-host:port]\n", stderr)
        fputs("  authorize-url    AWS SSO OIDC authorization URL\n", stderr)
        fputs("  callback-host    OAuth callback host:port (default: 127.0.0.1)\n", stderr)
        return nil
    }

    guard let url = URL(string: args[1]) else {
        fputs("Error: invalid URL: \(args[1])\n", stderr)
        return nil
    }

    let callbackHost = args.count >= 3 ? args[2] : "127.0.0.1"
    return (url, callbackHost)
}

guard let config = parseArguments() else {
    exit(ExitCode.error.rawValue)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let delegate = SSOAppDelegate(authorizeURL: config.url, callbackHost: config.callbackHost)
app.delegate = delegate
app.run()
