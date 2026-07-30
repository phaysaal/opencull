import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum Workspace: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case queue = "Culling Queue"
    case results = "Results"
    case providers = "Providers & Privacy"
    case recovery = "Recovery & Diagnostics"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .overview: "rectangle.grid.2x2"
        case .queue: "list.bullet.rectangle"
        case .results: "photo.stack"
        case .providers: "lock.shield"
        case .recovery: "cross.case"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var backend: Backend
    @State private var selection: Workspace? = .overview
    @State private var showingNewJob = false

    var body: some View {
        NavigationSplitView {
            List(Workspace.allCases, selection: $selection) { item in
                Label(item.rawValue, systemImage: item.symbol)
                    .tag(item)
            }
            .navigationSplitViewColumnWidth(min: 210, ideal: 238)
            .safeAreaInset(edge: .bottom) {
                ConnectionBadge(connected: backend.connected)
                    .padding(12)
            }
        } detail: {
            Group {
                switch selection ?? .overview {
                case .overview: OverviewView(showingNewJob: $showingNewJob)
                case .queue: QueueView()
                case .results: ResultsView()
                case .providers: ProvidersView()
                case .recovery: RecoveryView()
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showingNewJob = true
                } label: {
                    Label("New Culling Job", systemImage: "plus")
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .sheet(isPresented: $showingNewJob) { NewJobView() }
        .alert(
            "OpenCull needs attention",
            isPresented: Binding(
                get: { backend.errorMessage != nil },
                set: { if !$0 { backend.errorMessage = nil } }
            ),
            actions: { Button("OK") { backend.errorMessage = nil } },
            message: { Text(backend.errorMessage ?? "") }
        )
        .onReceive(NotificationCenter.default.publisher(for: .newCullJob)) { _ in
            showingNewJob = true
        }
        .onReceive(NotificationCenter.default.publisher(for: .selectWorkspace)) {
            if
                let raw = $0.object as? String,
                let workspace = Workspace(rawValue: raw)
            {
                selection = workspace
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .openExistingResult)) { _ in
            selection = .results
        }
    }
}

private struct ConnectionBadge: View {
    let connected: Bool
    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(connected ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
            Text(connected ? "Culling engine ready" : "Starting engine…")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }
}

struct EmptyState: View {
    let title: String
    let symbol: String
    let detail: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 38))
                .foregroundStyle(.secondary)
            Text(title).font(.title3.weight(.semibold))
            Text(detail)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct OverviewView: View {
    @EnvironmentObject private var backend: Backend
    @Binding var showingNewJob: Bool

    private var running: Int {
        backend.state.queue.jobs.filter(\.isActive).count
    }
    private var waiting: Int {
        backend.state.queue.jobs.filter { $0.status == "queued" }.count
    }
    private var completed: Int {
        backend.state.queue.jobs.filter { $0.status == "completed" }.count
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Good \(dayPart)")
                        .font(.system(size: 32, weight: .semibold, design: .rounded))
                    Text("Cull deliberately. Originals remain untouched.")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 14) {
                    MetricCard(title: "Active", value: running, symbol: "waveform.path.ecg")
                    MetricCard(title: "Waiting", value: waiting, symbol: "clock")
                    MetricCard(title: "Ready to review", value: completed, symbol: "checkmark.circle")
                }
                GroupBox {
                    HStack(spacing: 18) {
                        Image(systemName: "photo.on.rectangle.angled")
                            .font(.system(size: 38))
                            .foregroundStyle(.tint)
                        VStack(alignment: .leading, spacing: 5) {
                            Text("Start with a folder of photographs")
                                .font(.headline)
                            Text("OpenCull groups related frames, evaluates photographic intent, and proposes at most the number of keepers you choose.")
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Choose Folder…") { showingNewJob = true }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.large)
                    }
                    .padding(10)
                }
                if let active = backend.state.queue.jobs.first(where: \.isActive) {
                    ActiveJobCard(job: active)
                } else {
                    RecentJobs(jobs: Array(backend.state.queue.jobs.suffix(4).reversed()))
                }
            }
            .padding(32)
            .frame(maxWidth: 1100, alignment: .leading)
        }
        .navigationTitle("OpenCull")
    }

    private var dayPart: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: "morning"
        case 12..<18: "afternoon"
        default: "evening"
        }
    }
}

private struct MetricCard: View {
    let title: String
    let value: Int
    let symbol: String
    var body: some View {
        GroupBox {
            HStack {
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(value)").font(.system(size: 30, weight: .semibold))
                    Text(title).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: symbol)
                    .font(.title2)
                    .foregroundStyle(.tint)
            }
            .padding(8)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct ActiveJobCard: View {
    let job: CullJob
    var body: some View {
        GroupBox("Culling now") {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(job.folderName).font(.title3.weight(.semibold))
                    Spacer()
                    Text(job.status.capitalized).foregroundStyle(.secondary)
                }
                ProgressView(value: job.progress.fraction)
                Text(progressText).font(.caption).foregroundStyle(.secondary)
            }
            .padding(8)
        }
    }

    private var progressText: String {
        guard job.progress.totalClusters > 0 else { return job.message }
        return "\(job.progress.completedClusters) of \(job.progress.totalClusters) clusters assessed"
    }
}

private struct RecentJobs: View {
    let jobs: [CullJob]
    var body: some View {
        GroupBox("Recent work") {
            if jobs.isEmpty {
                EmptyState(
                    title: "No culling jobs yet",
                    symbol: "photo.stack",
                    detail: "Choose a folder to create your first trustworthy result.")
                .frame(maxWidth: .infinity, minHeight: 170)
            } else {
                VStack(spacing: 0) {
                    ForEach(jobs) { JobRow(job: $0) }
                }
            }
        }
    }
}

private struct QueueView: View {
    @EnvironmentObject private var backend: Backend
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Culling Queue").font(.largeTitle.weight(.semibold))
                    Text("Jobs run sequentially so model use and checkpoints stay predictable.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if backend.busy { ProgressView().controlSize(.small) }
            }
            if backend.state.queue.jobs.isEmpty {
                EmptyState(
                    title: "The queue is empty",
                    symbol: "list.bullet.rectangle",
                    detail: "Add a photo folder to begin.")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(backend.state.queue.jobs) { JobRow(job: $0) }
                    .listStyle(.inset)
            }
        }
        .padding(28)
    }
}

private struct JobRow: View {
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var library: LibraryStore
    @Environment(\.openWindow) private var openWindow
    let job: CullJob
    @State private var showingRemoval = false

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: statusSymbol)
                .font(.title3)
                .foregroundStyle(statusColor)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(job.folderName).font(.headline)
                    Text(job.status.capitalized)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(statusColor.opacity(0.12), in: Capsule())
                    if !sourceAvailable {
                        Label("Drive unavailable", systemImage: "externaldrive.badge.exclamationmark")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.orange)
                    }
                }
                Text(job.message).font(.subheadline).foregroundStyle(.secondary)
                if job.progress.totalClusters > 0 {
                    ProgressView(value: job.progress.fraction)
                        .frame(maxWidth: 360)
                }
            }
            Spacer()
            Menu {
                if job.status == "running" {
                    Button("Pause") { Task { await backend.act(jobID: job.id, action: "pause") } }
                }
                if ["paused", "failed", "cancelled"].contains(job.status) {
                    Button("Resume") { Task { await backend.act(jobID: job.id, action: "resume") } }
                }
                if job.status == "completed" {
                    Button("Open Review") {
                        Task {
                            if let target = await backend.openReview(jobID: job.id) {
                                library.record(
                                    report: job.output, photos: job.photos,
                                    title: target.title)
                                openWindow(value: target)
                            }
                        }
                    }
                }
                if !sourceAvailable {
                    Button("Locate Source Folder…") { locateSource() }
                }
                Button("Reveal in Finder") {
                    Task { await backend.reveal(job.status == "completed" ? job.output : job.photos) }
                }
                if removable {
                    Divider()
                    Button("Remove from Queue…", role: .destructive) {
                        showingRemoval = true
                    }
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 28)
        }
        .padding(.vertical, 9)
        .confirmationDialog(
            "Remove \(job.folderName) from the queue?",
            isPresented: $showingRemoval
        ) {
            Button("Remove Queue Entry", role: .destructive) {
                remove(cleanup: false)
            }
            Button("Remove Entry, Checkpoint, and Log", role: .destructive) {
                remove(cleanup: true)
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "Source photographs and any completed result JSON remain "
                + "untouched. Deleting the checkpoint prevents this job from "
                + "being resumed.")
        }
    }

    private var statusSymbol: String {
        switch job.status {
        case "running": "sparkles"
        case "completed": "checkmark.circle.fill"
        case "failed": "exclamationmark.triangle.fill"
        case "paused": "pause.circle.fill"
        default: "clock.fill"
        }
    }
    private var sourceAvailable: Bool {
        FileManager.default.fileExists(atPath: job.photos)
    }
    private var removable: Bool {
        ["failed", "cancelled", "paused", "completed"].contains(job.status)
    }
    private var statusColor: Color {
        switch job.status {
        case "completed": .green
        case "failed": .red
        case "paused", "detached": .orange
        case "running": .accentColor
        default: .secondary
        }
    }

    private func locateSource() {
        let panel = NSOpenPanel()
        panel.title = "Locate the source folder for \(job.folderName)"
        panel.prompt = "Relink Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        library.rememberAccess(to: url)
        Task { _ = await backend.relink(jobID: job.id, photos: url.path) }
    }

    private func remove(cleanup: Bool) {
        Task {
            _ = await backend.removeJob(
                job.id, removeArtifacts: cleanup)
        }
    }
}

private struct ResultsView: View {
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var library: LibraryStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Results").font(.largeTitle.weight(.semibold))
                    Text("Completed culls are ready for human review. AI decisions never overwrite your photographs.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    openExisting()
                } label: {
                    Label("Open Existing Result", systemImage: "doc.badge.plus")
                }
            }
            let results = backend.state.queue.jobs.filter { $0.status == "completed" }
            if results.isEmpty && library.reviews.isEmpty {
                EmptyState(
                    title: "No finished results",
                    symbol: "photo.stack",
                    detail: "Completed queue items and previously opened results will appear here.")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    if !results.isEmpty {
                        Section("Completed queue") {
                            ForEach(results) { JobRow(job: $0) }
                        }
                    }
                    if !library.reviews.isEmpty {
                        Section("Recent reviews") {
                            ForEach(library.reviews) { review in
                                RecentReviewRow(
                                    review: review,
                                    available: library.available(review),
                                    open: { openRecent(review) },
                                    forget: { library.forget(review) })
                            }
                        }
                    }
                }
                .listStyle(.inset)
            }
        }
        .padding(28)
        .onReceive(
            NotificationCenter.default.publisher(for: .openExistingResult)
        ) { _ in
            openExisting()
        }
    }

    private func openExisting() {
        let reportPanel = NSOpenPanel()
        reportPanel.title = "Choose an OpenCull result"
        reportPanel.prompt = "Choose Result"
        reportPanel.allowedContentTypes = [.json]
        reportPanel.canChooseDirectories = false
        guard reportPanel.runModal() == .OK, let report = reportPanel.url else {
            return
        }
        let photosPanel = NSOpenPanel()
        photosPanel.title = "Choose the corresponding photo folder"
        photosPanel.prompt = "Choose Photo Folder"
        photosPanel.canChooseFiles = false
        photosPanel.canChooseDirectories = true
        guard photosPanel.runModal() == .OK, let photos = photosPanel.url else {
            return
        }
        library.rememberAccess(to: report)
        library.rememberAccess(to: photos)
        Task {
            if let target = await backend.openReview(
                report: report.path, photos: photos.path)
            {
                library.record(
                    report: report.path, photos: photos.path,
                    title: target.title)
                openWindow(value: target)
            }
        }
    }

    private func openRecent(_ review: RecentReview) {
        guard library.available(review) else {
            backend.errorMessage = (
                "The result or photo folder is unavailable. Reconnect the "
                + "drive or use Open Existing Result to locate it again.")
            return
        }
        Task {
            if let target = await backend.openReview(
                report: review.report, photos: review.photos)
            {
                library.record(
                    report: review.report, photos: review.photos,
                    title: target.title)
                openWindow(value: target)
            }
        }
    }
}

private struct RecoveryView: View {
    @EnvironmentObject private var backend: Backend
    @State private var diagnostics: DiagnosticSnapshot?

    private var attention: [CullJob] {
        backend.state.queue.jobs.filter {
            $0.needsAttention || !FileManager.default.fileExists(atPath: $0.photos)
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Recovery & Diagnostics")
                        .font(.largeTitle.weight(.semibold))
                    Text("Resume safely from checkpoints and inspect the local engine without exposing credentials.")
                        .foregroundStyle(.secondary)
                }
                GroupBox("Engine health") {
                    HStack(spacing: 12) {
                        Circle()
                            .fill(backend.connected ? Color.green : Color.orange)
                            .frame(width: 10, height: 10)
                        VStack(alignment: .leading) {
                            Text(backend.connected ? "Culling engine connected" : "Culling engine unavailable")
                                .font(.headline)
                            Text("One supervised Kimiya process · persistent checkpoints · originals read-only")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Refresh Diagnostics") { loadDiagnostics() }
                    }
                    .padding(8)
                }
                GroupBox("Jobs needing attention") {
                    if attention.isEmpty {
                        Label("No interrupted jobs or missing source drives", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                            .frame(maxWidth: .infinity, minHeight: 80)
                    } else {
                        VStack(spacing: 0) {
                            ForEach(attention) { JobRow(job: $0) }
                        }
                    }
                }
                GroupBox("Diagnostic receipt") {
                    if let diagnostics {
                        VStack(alignment: .leading, spacing: 12) {
                            Text(diagnostics.text)
                                .font(.caption.monospaced())
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            HStack {
                                Button("Copy Receipt") {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(
                                        diagnostics.text, forType: .string)
                                }
                                if let log = diagnostics.logPath {
                                    Button("Reveal Log") {
                                        Task { await backend.reveal(log) }
                                    }
                                }
                                if let results = diagnostics.resultsPath {
                                    Button("Reveal Results") {
                                        Task { await backend.reveal(results) }
                                    }
                                }
                            }
                        }
                        .padding(8)
                    } else {
                        HStack {
                            Text("Load a credential-free environment receipt for support or auditing.")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Load Receipt") { loadDiagnostics() }
                        }
                        .padding(8)
                    }
                }
            }
            .padding(28)
            .frame(maxWidth: 1100, alignment: .leading)
        }
    }

    private func loadDiagnostics() {
        Task { diagnostics = await backend.diagnostics() }
    }
}

private struct RecentReviewRow: View {
    let review: RecentReview
    let available: Bool
    let open: () -> Void
    let forget: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: available ? "photo.stack.fill" : "externaldrive.badge.exclamationmark")
                .font(.title3)
                .foregroundStyle(available ? Color.accentColor : Color.orange)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(review.title).font(.headline)
                Text(URL(fileURLWithPath: review.photos).lastPathComponent)
                    .foregroundStyle(.secondary)
                Text(review.lastOpened, style: .relative)
                    .font(.caption).foregroundStyle(.tertiary)
            }
            Spacer()
            if available {
                Button("Open", action: open)
            } else {
                Text("Drive unavailable")
                    .font(.caption).foregroundStyle(.orange)
            }
            Menu {
                Button("Open", action: open).disabled(!available)
                Button("Forget from Recents", role: .destructive, action: forget)
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 28)
        }
        .padding(.vertical, 7)
    }
}

private struct ProvidersView: View {
    @EnvironmentObject private var backend: Backend
    @State private var showingNewProvider = false
    @State private var editingProvider: ProviderProfile?
    @State private var testingID: String?
    @State private var testResult: ProviderTestResult?
    @State private var deletingProvider: ProviderProfile?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Providers & Privacy").font(.largeTitle.weight(.semibold))
                    Text("Choose where image observations are processed and verify every route before culling.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    showingNewProvider = true
                } label: {
                    Label("Add Provider", systemImage: "plus")
                }
                .buttonStyle(.borderedProminent)
            }

            PrivacySummary(profiles: backend.state.providers.profiles)

            if backend.state.providers.profiles.isEmpty {
                EmptyState(
                    title: "No provider configured",
                    symbol: "lock.shield",
                    detail: "Add OpenRouter, a local Ollama service, or an OpenAI-compatible endpoint.")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(backend.state.providers.profiles) { provider in
                    ProviderRow(
                        provider: provider,
                        isTesting: testingID == provider.id,
                        edit: { editingProvider = provider },
                        test: { test(provider) },
                        delete: { deletingProvider = provider }
                    )
                }
                .listStyle(.inset)
            }
        }
        .padding(28)
        .sheet(isPresented: $showingNewProvider) {
            ProviderEditor(provider: nil)
        }
        .sheet(item: $editingProvider) { provider in
            ProviderEditor(provider: provider)
        }
        .sheet(item: $testResult) { result in
            ProviderTestView(result: result)
        }
        .confirmationDialog(
            "Delete \(deletingProvider?.name ?? "provider")?",
            isPresented: Binding(
                get: { deletingProvider != nil },
                set: { if !$0 { deletingProvider = nil } }
            )
        ) {
            Button("Delete Profile", role: .destructive) {
                deleteProvider(removeCredential: false)
            }
            if deletingProvider?.credentialStatus == "stored" {
                Button("Delete Profile and Keychain Credential", role: .destructive) {
                    deleteProvider(removeCredential: true)
                }
            }
            Button("Cancel", role: .cancel) { deletingProvider = nil }
        } message: {
            Text("Queued jobs keep their immutable generated configuration. This only removes the reusable profile.")
        }
    }

    private func test(_ provider: ProviderProfile) {
        testingID = provider.id
        Task {
            testResult = await backend.testProvider(provider.id)
            testingID = nil
        }
    }

    private func deleteProvider(removeCredential: Bool) {
        guard let provider = deletingProvider else { return }
        deletingProvider = nil
        Task {
            _ = await backend.deleteProvider(
                provider.id, removeCredential: removeCredential)
        }
    }
}

private struct PrivacySummary: View {
    let profiles: [ProviderProfile]

    var body: some View {
        HStack(spacing: 12) {
            SummaryPill(
                value: profiles.filter { $0.privacy == "local" }.count,
                label: "Local routes", symbol: "desktopcomputer")
            SummaryPill(
                value: profiles.filter { $0.privacy == "remote-zdr" }.count,
                label: "ZDR routes", symbol: "shield.checkered")
            SummaryPill(
                value: profiles.filter {
                    $0.credentialRequired && $0.credentialStatus != "stored"
                }.count,
                label: "Need credentials", symbol: "key")
            Spacer()
        }
    }
}

private struct SummaryPill: View {
    let value: Int
    let label: String
    let symbol: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundStyle(.tint)
            Text("\(value)").fontWeight(.semibold)
            Text(label).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ProviderRow: View {
    let provider: ProviderProfile
    let isTesting: Bool
    let edit: () -> Void
    let test: () -> Void
    let delete: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: provider.privacy == "local" ? "desktopcomputer" : "network")
                .font(.title2)
                .foregroundStyle(provider.privacy == "local" ? Color.green : Color.accentColor)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(provider.name).font(.headline)
                    Text(privacyLabel)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(.quaternary, in: Capsule())
                }
                Text(provider.endpoint)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(routeExplanation)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Label(
                provider.credentialStatus == "stored" ? "Credential stored" : "Credential missing",
                systemImage: provider.credentialStatus == "stored" ? "key.fill" : "key.slash"
            )
            .font(.caption)
            .foregroundStyle(
                provider.credentialRequired && provider.credentialStatus != "stored"
                    ? Color.orange : Color.secondary)
            if isTesting {
                ProgressView().controlSize(.small).frame(width: 28)
            }
            Menu {
                Button("Test Connection", action: test)
                Button("Edit", action: edit)
                Divider()
                Button("Delete…", role: .destructive, action: delete)
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 28)
        }
        .padding(.vertical, 10)
    }

    private var privacyLabel: String {
        switch provider.privacy {
        case "local": "Local"
        case "remote-zdr": "Remote · ZDR"
        default: "Remote policy"
        }
    }

    private var routeExplanation: String {
        provider.privacy == "local"
            ? "Photograph pixels stay on this Mac."
            : "Agents A, B, and D may receive observed image pixels."
    }
}

private struct ProviderEditor: View {
    @EnvironmentObject private var backend: Backend
    @Environment(\.dismiss) private var dismiss
    let provider: ProviderProfile?

    @State private var name: String
    @State private var kind: String
    @State private var endpoint: String
    @State private var agentA: String
    @State private var agentB: String
    @State private var agentC: String
    @State private var agentD: String
    @State private var secret = ""
    @State private var credentialRequired: Bool
    @State private var zdr: Bool
    @State private var costNote: String

    init(provider: ProviderProfile?) {
        self.provider = provider
        _name = State(initialValue: provider?.name ?? "OpenRouter")
        _kind = State(initialValue: provider?.kind ?? "openrouter")
        _endpoint = State(initialValue: provider?.endpoint ?? "")
        _agentA = State(initialValue: provider?.models["A"] ?? "google/gemini-2.5-flash")
        _agentB = State(initialValue: provider?.models["B"] ?? "openai/gpt-4.1-mini")
        _agentC = State(initialValue: provider?.models["C"] ?? "mistralai/mistral-small-3.2-24b-instruct")
        _agentD = State(initialValue: provider?.models["D"] ?? "qwen/qwen3-vl-30b-a3b-instruct")
        _credentialRequired = State(initialValue: provider?.credentialRequired ?? true)
        _zdr = State(initialValue: provider?.zdr ?? true)
        _costNote = State(initialValue: provider?.costNote ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text(provider == nil ? "Add Model Provider" : "Edit Model Provider")
                    .font(.title.weight(.semibold))
                Text("Credentials are write-only and stored in macOS Keychain.")
                    .foregroundStyle(.secondary)
            }
            Form {
                Section("Identity and route") {
                    TextField("Profile name", text: $name)
                    Picker("Provider type", selection: $kind) {
                        Text("OpenRouter").tag("openrouter")
                        Text("Local Ollama").tag("ollama")
                        Text("OpenAI-compatible").tag("openai")
                    }
                    TextField("Endpoint", text: $endpoint)
                        .disabled(kind == "openrouter")
                    if kind == "openrouter" {
                        Toggle("Request zero-data-retention routing", isOn: $zdr)
                    }
                }
                Section("Agent assignments") {
                    TextField("Agent A · perception", text: $agentA)
                    TextField("Agent B · ranking", text: $agentB)
                    TextField("Agent C · textual panel", text: $agentC)
                    TextField("Agent D · visual arbitration", text: $agentD)
                }
                Section("Credential and policy") {
                    if kind != "openrouter" {
                        Toggle("This endpoint requires a credential", isOn: $credentialRequired)
                    }
                    if requiresCredential {
                        SecureField(
                            provider?.credentialStatus == "stored"
                                ? "Leave blank to keep stored credential"
                                : "API credential",
                            text: $secret)
                    }
                    TextField("Optional cost or retention note", text: $costNote)
                }
            }
            .formStyle(.grouped)

            HStack {
                Label(privacyMessage, systemImage: privacySymbol)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save Provider") { save() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!isValid || backend.busy)
            }
        }
        .padding(26)
        .frame(width: 650, height: 670)
        .onChange(of: kind) { newKind in
            if newKind == "openrouter" {
                endpoint = ""
                credentialRequired = true
            } else if newKind == "ollama" && endpoint.isEmpty {
                endpoint = "http://127.0.0.1:11434"
                credentialRequired = false
            }
        }
    }

    private var requiresCredential: Bool {
        kind == "openrouter" || credentialRequired
    }

    private var isValid: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && [agentA, agentB, agentC, agentD].allSatisfy {
                !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
            && (kind == "openrouter" || !endpoint.isEmpty)
            && (!requiresCredential
                || provider?.credentialStatus == "stored"
                || !secret.isEmpty)
    }

    private var privacyMessage: String {
        kind == "ollama" && (
            endpoint.contains("127.0.0.1") || endpoint.contains("localhost"))
            ? "Local route: photograph pixels stay on this Mac."
            : "Remote route: observed image pixels may leave this Mac."
    }

    private var privacySymbol: String {
        privacyMessage.hasPrefix("Local") ? "lock.shield.fill" : "network"
    }

    private func save() {
        let draft = ProviderDraft(
            id: provider?.id,
            name: name.trimmingCharacters(in: .whitespacesAndNewlines),
            kind: kind,
            endpoint: endpoint.trimmingCharacters(in: .whitespacesAndNewlines),
            models: ["A": agentA, "B": agentB, "C": agentC, "D": agentD],
            credentialRequired: requiresCredential,
            zdr: kind == "openrouter" && zdr,
            costNote: costNote
        )
        Task {
            if await backend.saveProvider(draft, secret: secret) {
                secret = ""
                dismiss()
            }
        }
    }
}

extension ProviderTestResult: Identifiable {
    var id: String { profileID }
}

private struct ProviderTestView: View {
    @Environment(\.dismiss) private var dismiss
    let result: ProviderTestResult

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: result.configuredModelsPresent
                  ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.system(size: 42))
                .foregroundStyle(result.configuredModelsPresent ? Color.green : Color.orange)
            Text(result.configuredModelsPresent
                 ? "Provider is ready" : "Provider is reachable")
                .font(.title.weight(.semibold))
            Text("\(result.availableModelCount) models were reported by the endpoint.")
            if !result.missingModels.isEmpty {
                GroupBox("Configured models not found") {
                    VStack(alignment: .leading) {
                        ForEach(result.missingModels, id: \.self) { Text($0) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Text(result.notice)
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 500)
    }
}

private struct NewJobView: View {
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var library: LibraryStore
    @Environment(\.dismiss) private var dismiss
    @State private var folder = ""
    @State private var maximumKeepers = 2
    @State private var profile = "family"
    @State private var recursive = false
    @State private var providerID = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 5) {
                Text("New Culling Job").font(.title.weight(.semibold))
                Text("Original photographs remain read-only throughout culling.")
                    .foregroundStyle(.secondary)
            }
            GroupBox("Source") {
                HStack {
                    Image(systemName: "folder").foregroundStyle(.tint)
                    Text(folder.isEmpty ? "No photo folder selected" : folder)
                        .lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose…", action: chooseFolder)
                }
                .padding(8)
            }
            Form {
                Picker("Culling intent", selection: $profile) {
                    Text("Family").tag("family")
                    Text("Professional").tag("professional")
                    Text("Balanced").tag("balanced")
                }
                Stepper("At most \(maximumKeepers) keeper\(maximumKeepers == 1 ? "" : "s") per cluster",
                        value: $maximumKeepers, in: 1...20)
                Toggle("Include nested folders", isOn: $recursive)
                Picker("Model provider", selection: $providerID) {
                    Text("Choose a provider").tag("")
                    ForEach(backend.state.providers.profiles) { provider in
                        Text(provider.label).tag(provider.id)
                    }
                }
            }
            HStack {
                Label("Jobs run one at a time", systemImage: "checkmark.shield")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add to Queue") {
                    Task {
                        await backend.add(NewJobRequest(
                            photos: folder,
                            keepPerGroup: maximumKeepers,
                            recursive: recursive,
                            profile: profile,
                            providerProfileID: providerID
                        ))
                        if backend.errorMessage == nil { dismiss() }
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(folder.isEmpty || providerID.isEmpty || backend.busy)
            }
        }
        .padding(28)
        .frame(width: 620)
        .onAppear {
            if providerID.isEmpty, backend.state.providers.profiles.count == 1 {
                providerID = backend.state.providers.profiles[0].id
            }
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose photographs to cull"
        panel.prompt = "Choose Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            library.rememberAccess(to: url)
            folder = url.path
        }
    }
}
