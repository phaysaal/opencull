import AppKit
import SwiftUI
import WebKit

struct ReviewWindow: View {
    let target: ReviewTarget

    var body: some View {
        if let url = URL(string: target.url) {
            EmbeddedReview(url: url)
                .frame(minWidth: 960, minHeight: 640)
                .navigationTitle(target.title)
        } else {
            EmptyState(
                title: "Review address is invalid",
                symbol: "exclamationmark.triangle",
                detail: "Return to Results and open the review again.")
        }
    }
}

struct ProjectWorkspaceView: View {
    let target: ReviewTarget

    var body: some View {
        ReviewWindow(target: target)
        .frame(minWidth: 1040, minHeight: 680)
        .navigationTitle(target.title)
    }
}

struct EmbeddedReview: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.isElementFullscreenEnabled = true
        configuration.userContentController.add(
            context.coordinator, name: "openCullNative")
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsMagnification = true
        webView.underPageBackgroundColor = .windowBackgroundColor
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        guard webView.url == nil else { return }
        webView.load(URLRequest(url: url))
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKDownloadDelegate,
        WKUIDelegate, WKScriptMessageHandler {
        private var contentProcessRecoveryAttempts = 0

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == "openCullNative",
                  let value = message.body as? [String: Any],
                  value["action"] as? String == "chooseRawFolder",
                  let webView = message.webView
            else { return }
            let panel = NSOpenPanel()
            panel.title = "Choose the folder containing RAW originals"
            panel.prompt = "Link RAW Folder"
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.allowsMultipleSelection = false
            let path = panel.runModal() == .OK ? panel.url?.path : nil
            let encoded: String
            if let path,
               let data = try? JSONSerialization.data(
                   withJSONObject: path, options: [.fragmentsAllowed]),
               let json = String(data: data, encoding: .utf8)
            {
                encoded = json
            } else {
                encoded = "null"
            }
            webView.evaluateJavaScript(
                "window.openCullRawFolderSelected(\(encoded))")
        }
        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor @Sendable () -> Void
        ) {
            let alert = NSAlert()
            alert.messageText = "Darkimiya"
            alert.informativeText = message
            alert.alertStyle = .informational
            alert.addButton(withTitle: "OK")
            alert.runModal()
            completionHandler()
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor @Sendable (Bool) -> Void
        ) {
            let alert = NSAlert()
            alert.messageText = "Confirm Darkimiya action"
            alert.informativeText = message
            alert.alertStyle = .warning
            alert.addButton(withTitle: "Continue")
            alert.addButton(withTitle: "Cancel")
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptTextInputPanelWithPrompt prompt: String,
            defaultText: String?,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor @Sendable (String?) -> Void
        ) {
            let alert = NSAlert()
            alert.messageText = "Darkimiya confirmation"
            alert.informativeText = prompt
            alert.alertStyle = .warning
            alert.addButton(withTitle: "Continue")
            alert.addButton(withTitle: "Cancel")
            let field = NSTextField(
                frame: NSRect(x: 0, y: 0, width: 420, height: 24))
            field.stringValue = defaultText ?? ""
            field.placeholderString = "Enter the requested confirmation"
            alert.accessoryView = field
            let response = alert.runModal()
            completionHandler(
                response == .alertFirstButtonReturn ? field.stringValue : nil)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction
        ) async -> WKNavigationActionPolicy {
            guard let url = navigationAction.request.url else {
                return .cancel
            }
            if isTrustedLocalURL(url) {
                return .allow
            }
            if navigationAction.navigationType == .linkActivated {
                NSWorkspace.shared.open(url)
            }
            return .cancel
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationResponse: WKNavigationResponse
        ) async -> WKNavigationResponsePolicy {
            let disposition = (
                navigationResponse.response as? HTTPURLResponse
            )?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
            return disposition.lowercased().contains("attachment")
                ? .download : .allow
        }

        func webView(
            _ webView: WKWebView,
            navigationResponse: WKNavigationResponse,
            didBecome download: WKDownload
        ) {
            download.delegate = self
        }

        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation!,
            withError error: Error
        ) {
            presentFailure(error, in: webView)
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            presentFailure(error, in: webView)
        }

        func webView(
            _ webView: WKWebView,
            didFinish navigation: WKNavigation!
        ) {
            contentProcessRecoveryAttempts = 0
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            guard contentProcessRecoveryAttempts < 2 else {
                presentFailure(
                    NSError(
                        domain: "org.darkimiya.review",
                        code: 1,
                        userInfo: [NSLocalizedDescriptionKey:
                            "The macOS web rendering process stopped repeatedly."]),
                    in: webView)
                return
            }
            contentProcessRecoveryAttempts += 1
            // Review decisions are committed by the local server, so reloading
            // restores the workspace without risking source photographs.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                webView.reload()
            }
        }

        private func isTrustedLocalURL(_ url: URL) -> Bool {
            if url.scheme == "about" { return true }
            return url.scheme == "http"
                && ["127.0.0.1", "localhost"].contains(url.host ?? "")
        }

        private func presentFailure(_ error: Error, in webView: WKWebView) {
            let message = error.localizedDescription
                .replacingOccurrences(of: "&", with: "&amp;")
                .replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
            webView.loadHTMLString(
                """
                <style>
                  body { font: -apple-system-body; color: #263449;
                         display: grid; place-items: center; height: 90vh; }
                  main { max-width: 34rem; text-align: center; }
                </style>
                <main>
                  <h2>Review could not be loaded</h2>
                  <p>\(message)</p>
                  <p>Your photographs and review decisions were not changed.</p>
                </main>
                """,
                baseURL: nil)
        }

        func download(
            _ download: WKDownload,
            decideDestinationUsing response: URLResponse,
            suggestedFilename: String
        ) async -> URL? {
            let panel = NSSavePanel()
            panel.nameFieldStringValue = suggestedFilename
            panel.canCreateDirectories = true
            return panel.runModal() == .OK ? panel.url : nil
        }
    }
}
