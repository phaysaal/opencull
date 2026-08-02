import AppKit
import SwiftUI

@main
struct DarkimiyaApp: App {
    @StateObject private var backend = Backend()
    @StateObject private var library = LibraryStore()
    @StateObject private var projectSession = ProjectSession()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(backend)
                .environmentObject(library)
                .environmentObject(projectSession)
                .task {
                    await backend.start()
                    for review in library.reviews {
                        _ = await backend.importLegacyReview(
                            report: review.report, photos: review.photos)
                    }
                }
                .onReceive(
                    NotificationCenter.default.publisher(
                        for: NSApplication.willTerminateNotification)
                ) { _ in backend.stop() }
                .frame(minWidth: 1040, minHeight: 680)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1100, height: 760)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Project…") {
                    NotificationCenter.default.post(name: .newProject, object: nil)
                }
                .keyboardShortcut("n")
                Button("Show Projects") {
                    select(.projects)
                    DispatchQueue.main.async {
                    NotificationCenter.default.post(
                        name: .showProjects, object: nil)
                    }
                }
                .keyboardShortcut("o")
            }
            CommandMenu("Workspace") {
                Button("Projects") { select(.projects) }.keyboardShortcut("1")
                Button("Activity") { select(.activity) }.keyboardShortcut("2")
                Button("Providers & Privacy") { select(.providers) }.keyboardShortcut("3")
                Button("Recovery & Diagnostics") { select(.recovery) }.keyboardShortcut("4")
            }
        }
    }

    private func select(_ workspace: Workspace) {
        NotificationCenter.default.post(
            name: .selectWorkspace, object: workspace.rawValue)
    }
}

extension Notification.Name {
    static let newProject = Notification.Name("DarkimiyaNewProject")
    static let showProjects = Notification.Name("DarkimiyaShowProjects")
    static let selectWorkspace = Notification.Name("DarkimiyaSelectWorkspace")
}
