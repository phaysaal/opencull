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

private struct EmbeddedReview: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.isElementFullscreenEnabled = true
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
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
    final class Coordinator: NSObject, WKNavigationDelegate, WKDownloadDelegate {
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
