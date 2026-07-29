import AppKit
import SwiftUI

@main
struct OpenCullNativeApp: App {
    @StateObject private var backend = Backend()
    @StateObject private var library = LibraryStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(backend)
                .environmentObject(library)
                .task { await backend.start() }
                .onReceive(
                    NotificationCenter.default.publisher(
                        for: NSApplication.willTerminateNotification)
                ) { _ in backend.stop() }
                .frame(minWidth: 1040, minHeight: 680)
        }
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Culling Job") {
                    NotificationCenter.default.post(name: .newCullJob, object: nil)
                }
                .keyboardShortcut("n")
                Button("Open Existing Result…") {
                    select(.results)
                    DispatchQueue.main.async {
                    NotificationCenter.default.post(
                        name: .openExistingResult, object: nil)
                    }
                }
                .keyboardShortcut("o")
            }
            CommandMenu("Workspace") {
                Button("Overview") { select(.overview) }.keyboardShortcut("1")
                Button("Culling Queue") { select(.queue) }.keyboardShortcut("2")
                Button("Results") { select(.results) }.keyboardShortcut("3")
                Button("Providers & Privacy") { select(.providers) }.keyboardShortcut("4")
                Button("Recovery & Diagnostics") { select(.recovery) }.keyboardShortcut("5")
            }
        }
        WindowGroup(for: ReviewTarget.self) { $target in
            if let target {
                ReviewWindow(target: target)
            } else {
                EmptyState(
                    title: "Review unavailable",
                    symbol: "exclamationmark.triangle",
                    detail: "Choose a completed result from the Results workspace.")
            }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1380, height: 900)
    }

    private func select(_ workspace: Workspace) {
        NotificationCenter.default.post(
            name: .selectWorkspace, object: workspace.rawValue)
    }
}

extension Notification.Name {
    static let newCullJob = Notification.Name("OpenCullNewJob")
    static let openExistingResult = Notification.Name("OpenCullOpenExistingResult")
    static let selectWorkspace = Notification.Name("OpenCullSelectWorkspace")
}
