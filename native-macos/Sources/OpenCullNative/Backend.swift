import AppKit
import Foundation

enum BackendError: LocalizedError {
    case launch(String)
    case invalidBootstrap
    case service(String)

    var errorDescription: String? {
        switch self {
        case .launch(let detail): return "Could not start Darkimiya: \(detail)"
        case .invalidBootstrap: return "The Darkimiya backend returned an invalid startup response."
        case .service(let detail): return detail
        }
    }
}

private struct Bootstrap: Decodable {
    let format: String
    let url: String?
    let token: String?
    let error: String?
}

private struct ErrorEnvelope: Decodable {
    let error: String
}

@MainActor
final class Backend: ObservableObject {
    @Published private(set) var state = DesktopState(
        queue: QueueState(revision: 0, activeJobID: nil, jobs: []),
        providers: ProviderState(revision: 0, profiles: []),
        projects: ProjectCatalogState(revision: 0, projects: [])
    )
    @Published private(set) var connected = false
    @Published private(set) var connectionDetail: String?
    @Published private(set) var busy = false
    @Published var errorMessage: String?

    private var process: Process?
    private var baseURL: URL?
    private var token = ""
    private var pollTask: Task<Void, Never>?
    private var hasLoadedState = false
    private var consecutivePollFailures = 0
    private var reportedStoppedEngine = false

    func start() async {
        guard process == nil else { return }
        do {
            let process = Process()
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.standardError

            if let explicit = ProcessInfo.processInfo.environment["OPENCULL_BACKEND"] {
                process.executableURL = URL(fileURLWithPath: explicit)
                process.arguments = ["--native-server"]
            } else if let bundled = Bundle.main.url(
                forAuxiliaryExecutable: "DarkimiyaBackend")
            {
                process.executableURL = bundled
                process.arguments = ["--native-server"]
            } else {
                let root = sourceRoot()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                process.arguments = [
                    "python3", root.appendingPathComponent("opencull_desktop.py").path,
                    "--native-server",
                ]
            }
            try process.run()
            self.process = process
            let line = try await readBootstrap(from: pipe.fileHandleForReading)
            guard let bootstrap = try? JSONDecoder().decode(
                Bootstrap.self, from: line),
                bootstrap.format == "opencull-native-bootstrap-v1"
            else {
                process.terminate()
                throw BackendError.invalidBootstrap
            }
            if let error = bootstrap.error {
                throw BackendError.service(error)
            }
            guard
                let address = bootstrap.url,
                let sessionToken = bootstrap.token,
                let url = URL(string: address)
            else {
                throw BackendError.invalidBootstrap
            }
            baseURL = url
            token = sessionToken
            connected = true
            connectionDetail = nil
            consecutivePollFailures = 0
            reportedStoppedEngine = false
            NativeNotifications.shared.requestAuthorization()
            try await refresh()
            beginPolling()
        } catch {
            if process?.isRunning == true { process?.terminate() }
            process = nil
            connected = false
            errorMessage = error.localizedDescription
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
        if process?.isRunning == true { process?.terminate() }
        process = nil
        connected = false
        connectionDetail = nil
    }

    func refresh() async throws {
        let previous = Dictionary(
            uniqueKeysWithValues: state.queue.jobs.map { ($0.id, $0.status) })
        let updated = try await request("/state", as: DesktopState.self)
        if hasLoadedState {
            for job in updated.queue.jobs where previous[job.id] != job.status {
                NativeNotifications.shared.jobChanged(job)
            }
        }
        state = updated
        hasLoadedState = true
    }

    func diagnostics() async -> DiagnosticSnapshot? {
        do {
            return try await request("/diagnostics", as: DiagnosticSnapshot.self)
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    func add(_ requestBody: NewJobRequest) async {
        await mutate("/jobs", requestBody)
    }

    func addProject(_ photos: String) async -> Bool {
        await mutate("/projects", AddProjectRequest(photos: photos))
    }

    func importLegacyReview(report: String, photos: String) async -> Bool {
        await mutate(
            "/projects/import-report",
            ImportProjectReportRequest(photos: photos, report: report))
    }

    func startCulling(_ requestBody: CullProjectRequest) async -> Bool {
        await mutate("/projects/cull", requestBody)
    }

    func openProject(_ id: String) async -> ReviewTarget? {
        await launchReview(
            "/projects/open", body: ProjectIDRequest(projectID: id))
    }

    func openManualSelection(_ id: String) async -> ReviewTarget? {
        await launchReview(
            "/projects/manual", body: ProjectIDRequest(projectID: id))
    }

    func act(jobID: String, action: String) async {
        await mutate("/jobs/action", JobActionRequest(jobID: jobID, action: action))
    }

    func openReview(jobID: String) async -> ReviewTarget? {
        busy = true
        defer { busy = false }
        do {
            let launch: ReviewLaunchResponse = try await request(
                "/jobs/review", method: "POST",
                body: JobActionRequest(jobID: jobID, action: "review"))
            return ReviewTarget(url: launch.url, title: launch.title)
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    func openReview(report: String, photos: String) async -> ReviewTarget? {
        await launchReview(
            "/reviews/open",
            body: OpenReviewRequest(report: report, photos: photos))
    }

    func relink(jobID: String, photos: String) async -> Bool {
        await mutate(
            "/jobs/relink",
            RelinkJobRequest(jobID: jobID, photos: photos))
    }

    func removeJob(_ id: String, removeArtifacts: Bool) async -> Bool {
        await mutate(
            "/jobs/remove",
            RemoveJobRequest(jobID: id, removeArtifacts: removeArtifacts))
    }

    private func launchReview<B: Encodable>(
        _ path: String, body: B
    ) async -> ReviewTarget? {
        busy = true
        defer { busy = false }
        do {
            let launch: ReviewLaunchResponse = try await request(
                path, method: "POST", body: body)
            return ReviewTarget(url: launch.url, title: launch.title)
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    func reveal(_ path: String) async {
        await mutate("/reveal", ["path": path])
    }

    func saveProvider(_ draft: ProviderDraft, secret: String) async -> Bool {
        await mutate(
            "/providers/save",
            SaveProviderRequest(
                revision: state.providers.revision,
                profile: draft,
                secret: secret
            )
        )
    }

    func deleteProvider(_ id: String, removeCredential: Bool) async -> Bool {
        await mutate(
            "/providers/delete",
            DeleteProviderRequest(
                profileID: id,
                revision: state.providers.revision,
                removeCredential: removeCredential
            )
        )
    }

    func testProvider(_ id: String) async -> ProviderTestResult? {
        busy = true
        defer { busy = false }
        do {
            return try await request(
                "/providers/test", method: "POST",
                body: ProviderIDRequest(profileID: id))
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    @discardableResult
    private func mutate<T: Encodable>(_ path: String, _ body: T) async -> Bool {
        busy = true
        defer { busy = false }
        do {
            let _: EmptyResponse = try await request(path, method: "POST", body: body)
            try await refresh()
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    private func request<T: Decodable>(
        _ path: String, as type: T.Type = T.self
    ) async throws -> T {
        try await request(path, method: "GET", body: Optional<String>.none)
    }

    private func request<T: Decodable, B: Encodable>(
        _ path: String, method: String, body: B?
    ) async throws -> T {
        let component = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = baseURL?.appendingPathComponent(component) else {
            throw BackendError.launch("engine is not connected")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw BackendError.service("Darkimiya returned no response.")
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONDecoder().decode(ErrorEnvelope.self, from: data).error)
            throw BackendError.service(detail ?? "Darkimiya request failed.")
        }
        if T.self == EmptyResponse.self {
            return EmptyResponse() as! T
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch let error as DecodingError {
            throw BackendError.service(
                "Darkimiya returned incomplete data: \(decodingSummary(error))")
        }
    }

    private func decodingSummary(_ error: DecodingError) -> String {
        switch error {
        case .keyNotFound(let key, _):
            return "missing field ‘\(key.stringValue)’"
        case .typeMismatch(_, let context):
            return "invalid field ‘\(context.codingPath.last?.stringValue ?? "unknown")’"
        case .valueNotFound(_, let context):
            return "empty field ‘\(context.codingPath.last?.stringValue ?? "unknown")’"
        case .dataCorrupted(let context):
            return "invalid value at ‘\(context.codingPath.last?.stringValue ?? "unknown")’"
        @unknown default:
            return "response schema mismatch"
        }
    }

    private func beginPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                do {
                    try await self.refresh()
                    self.consecutivePollFailures = 0
                    self.reportedStoppedEngine = false
                    self.connected = true
                    self.connectionDetail = nil
                } catch is CancellationError {
                    return
                } catch {
                    self.consecutivePollFailures += 1
                    guard self.consecutivePollFailures >= 2 else { continue }

                    self.connected = false
                    let engineStopped = self.process?.isRunning != true
                    self.connectionDetail = engineStopped
                        ? "Local engine stopped"
                        : "Engine connection interrupted — retrying…"

                    // A polling failure is background health information, not
                    // a new user action failure.  Report an exited backend once
                    // and never recreate the alert after it is dismissed.
                    if engineStopped && !self.reportedStoppedEngine {
                        self.reportedStoppedEngine = true
                        self.errorMessage = "The local Darkimiya backend stopped. Your saved work is unchanged. Quit and reopen Darkimiya to reconnect."
                        return
                    }
                }
            }
        }
    }

    private func readBootstrap(from handle: FileHandle) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                var data = Data()
                while data.count < 16_384 {
                    let byte = handle.readData(ofLength: 1)
                    if byte.isEmpty { break }
                    if byte == Data([0x0A]) { break }
                    data.append(byte)
                }
                data.isEmpty
                    ? continuation.resume(throwing: BackendError.invalidBootstrap)
                    : continuation.resume(returning: data)
            }
        }
    }

    private func sourceRoot() -> URL {
        if let explicit = ProcessInfo.processInfo.environment["OPENCULL_SOURCE_ROOT"] {
            return URL(fileURLWithPath: explicit)
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .deletingLastPathComponent()
    }
}

private struct EmptyResponse: Decodable {
    init() {}
}
