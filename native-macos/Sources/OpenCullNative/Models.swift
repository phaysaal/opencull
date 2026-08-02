import Foundation

@MainActor
final class ProjectSession: ObservableObject {
    @Published private(set) var activeReview: ReviewTarget?

    var isOpen: Bool { activeReview != nil }

    func open(_ target: ReviewTarget) {
        activeReview = target
    }

    func close() {
        activeReview = nil
    }
}

struct DesktopState: Decodable {
    let queue: QueueState
    let providers: ProviderState
    let projects: ProjectCatalogState
}

struct ProjectCatalogState: Decodable {
    let revision: Int
    let projects: [DarkimiyaProject]
}

struct DarkimiyaProject: Decodable, Identifiable {
    let id: String
    let name: String
    let photos: String
    let project: String
    let stage: String
    let available: Bool
    let manifestAvailable: Bool
    let report: String
    let reportAvailable: Bool
    let activityCount: Int
    let addedAt: String?
    let importWarning: String?
    let culling: CullJob?

    enum CodingKeys: String, CodingKey {
        case id, name, photos, project, stage, available, report, culling
        case manifestAvailable = "manifest_available"
        case reportAvailable = "report_available"
        case activityCount = "activity_count"
        case addedAt = "added_at"
        case importWarning = "import_warning"
    }

    var cullingBlocksWorkflow: Bool {
        guard let culling else { return false }
        return ["queued", "running", "stopping", "detached"].contains(culling.status)
    }
}

struct QueueState: Decodable {
    let revision: Int
    let activeJobID: String?
    let jobs: [CullJob]

    enum CodingKeys: String, CodingKey {
        case revision, jobs
        case activeJobID = "active_job_id"
    }
}

struct ProviderState: Decodable {
    let revision: Int
    let profiles: [ProviderProfile]
}

struct ProviderProfile: Decodable, Identifiable {
    let id: String
    let name: String
    let kind: String
    let endpoint: String
    let models: [String: String]
    let privacy: String?
    let credentialStatus: String?
    let credentialRequired: Bool
    let zdr: Bool
    let costNote: String

    enum CodingKeys: String, CodingKey {
        case id, name, kind, endpoint, models, privacy, zdr
        case credentialStatus = "credential"
        case credentialRequired = "credential_required"
        case costNote = "cost_note"
    }

    var label: String { "\(name) · \(kind.capitalized)" }
}

struct ProviderDraft: Encodable {
    let id: String?
    let name: String
    let kind: String
    let endpoint: String
    let models: [String: String]
    let credentialRequired: Bool
    let zdr: Bool
    let costNote: String

    enum CodingKeys: String, CodingKey {
        case id, name, kind, endpoint, models, zdr
        case credentialRequired = "credential_required"
        case costNote = "cost_note"
    }
}

struct SaveProviderRequest: Encodable {
    let revision: Int
    let profile: ProviderDraft
    let secret: String
}

struct DeleteProviderRequest: Encodable {
    let profileID: String
    let revision: Int
    let removeCredential: Bool

    enum CodingKeys: String, CodingKey {
        case revision
        case profileID = "profile_id"
        case removeCredential = "remove_credential"
    }
}

struct ProviderIDRequest: Encodable {
    let profileID: String

    enum CodingKeys: String, CodingKey {
        case profileID = "profile_id"
    }
}

struct ProviderTestResult: Decodable {
    let profileID: String
    let reachable: Bool
    let availableModelCount: Int
    let configuredModelsPresent: Bool
    let missingModels: [String]
    let visionCapability: [String: String]
    let notice: String

    enum CodingKeys: String, CodingKey {
        case reachable, notice
        case profileID = "profile_id"
        case availableModelCount = "available_model_count"
        case configuredModelsPresent = "configured_models_present"
        case missingModels = "missing_models"
        case visionCapability = "vision_capability"
    }
}

struct CullProgress: Decodable {
    let completedClusters: Int
    let totalClusters: Int
    let fraction: Double

    enum CodingKeys: String, CodingKey {
        case fraction
        case completedClusters = "completed_clusters"
        case totalClusters = "total_clusters"
    }
}

struct JobJudgmentPolicy: Codable {
    let panel: [String]
    let votes: Int
    let required: Int

    var summary: String {
        "\(panel.joined(separator: " + ")) · \(required) of \(votes) approvals"
    }
}

struct CullJob: Decodable, Identifiable {
    let id: String
    let kind: String?
    let photos: String
    let output: String
    // Not every queue item is a culling run (style-profile and verification
    // jobs do not carry a culling intent). Keep the native shell tolerant of
    // those valid job records.
    let profile: String?
    let status: String
    let message: String
    let providerProfileName: String?
    let providerPrivacy: String?
    let createdAt: String?
    let photoExamples: [String]?
    let judgmentPolicy: JobJudgmentPolicy?
    let progress: CullProgress

    enum CodingKeys: String, CodingKey {
        case id, kind, photos, output, profile, status, message, progress
        case providerProfileName = "provider_profile_name"
        case providerPrivacy = "provider_privacy"
        case createdAt = "created_at"
        case photoExamples = "photo_examples"
        case judgmentPolicy = "judgment_policy"
    }

    var folderName: String {
        let source: String
        if kind == "style_profile", let first = photoExamples?.first {
            source = URL(fileURLWithPath: first).deletingLastPathComponent().path
        } else {
            source = photos
        }
        return URL(fileURLWithPath: source).lastPathComponent
    }

    var kindLabel: String {
        switch kind ?? "culling" {
        case "culling": "Culling"
        case "professional_shortlist": "Professional Shortlist"
        case "edit_suggestions": "Edit Directions"
        case "semantic_verification": "Semantic Verification"
        case "style_profile": "Personal Style"
        case "development_render": "Photo Development"
        case "development_pipeline": "Guided Development"
        case "delivery_export": "Image Export"
        default: "Darkimiya Job"
        }
    }

    var displayName: String {
        "\(kindLabel) — \(folderName.isEmpty ? "Unknown folder" : folderName)"
    }

    var addedLabel: String {
        guard let createdAt else { return "Added time unavailable" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = parser.date(from: createdAt) ?? ISO8601DateFormatter().date(from: createdAt)
        guard let date else { return "Added \(createdAt)" }
        return "Added \(date.formatted(date: .abbreviated, time: .shortened))"
    }

    var isActive: Bool {
        ["running", "stopping", "detached"].contains(status)
    }

    var needsAttention: Bool {
        ["failed", "paused", "detached"].contains(status)
    }
}

struct NewJobRequest: Encodable {
    let photos: String
    let keepPerGroup: Int
    let recursive: Bool
    let profile: String
    let providerProfileID: String
    let judgePanel: [String]
    let judgeVotes: Int
    let judgeRequired: Int

    enum CodingKeys: String, CodingKey {
        case photos, recursive, profile
        case keepPerGroup = "keep_per_group"
        case providerProfileID = "provider_profile_id"
        case judgePanel = "judge_panel"
        case judgeVotes = "judge_votes"
        case judgeRequired = "judge_required"
    }
}

struct AddProjectRequest: Encodable {
    let photos: String
}

struct ImportProjectReportRequest: Encodable {
    let photos: String
    let report: String
}

struct ProjectIDRequest: Encodable {
    let projectID: String

    enum CodingKeys: String, CodingKey {
        case projectID = "project_id"
    }
}

struct CullProjectRequest: Encodable {
    let projectID: String
    let keepPerGroup: Int
    let recursive: Bool
    let profile: String
    let providerProfileID: String
    let judgePanel: [String]
    let judgeVotes: Int
    let judgeRequired: Int

    enum CodingKeys: String, CodingKey {
        case recursive, profile
        case projectID = "project_id"
        case keepPerGroup = "keep_per_group"
        case providerProfileID = "provider_profile_id"
        case judgePanel = "judge_panel"
        case judgeVotes = "judge_votes"
        case judgeRequired = "judge_required"
    }
}

struct JobActionRequest: Encodable {
    let jobID: String
    let action: String

    enum CodingKeys: String, CodingKey {
        case action
        case jobID = "job_id"
    }
}

struct RelinkJobRequest: Encodable {
    let jobID: String
    let photos: String

    enum CodingKeys: String, CodingKey {
        case photos
        case jobID = "job_id"
    }
}

struct RemoveJobRequest: Encodable {
    let jobID: String
    let removeArtifacts: Bool

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case removeArtifacts = "remove_artifacts"
    }
}

struct OpenReviewRequest: Encodable {
    let report: String
    let photos: String
}

struct ReviewLaunchResponse: Decodable {
    let url: String
    let title: String
}

struct ReviewTarget: Codable, Hashable {
    let url: String
    let title: String
}

struct DiagnosticSnapshot: Decodable {
    let format: String
    let python: String
    let architecture: String
    let queueRevision: Int
    let providerRevision: Int
    let resourceRoot: String?
    let queuePath: String?
    let providerPath: String?
    let logPath: String?
    let resultsPath: String?
    let frozen: Bool?

    enum CodingKeys: String, CodingKey {
        case format, python, architecture, frozen
        case queueRevision = "queue_revision"
        case providerRevision = "provider_revision"
        case resourceRoot = "resource_root"
        case queuePath = "queue_path"
        case providerPath = "provider_path"
        case logPath = "log_path"
        case resultsPath = "results_path"
    }

    var text: String {
        [
            "Darkimiya diagnostics",
            "Format: \(format)",
            "Architecture: \(architecture)",
            "Python: \(python)",
            "Frozen backend: \(frozen == true ? "yes" : "no")",
            "Queue revision: \(queueRevision)",
            "Provider revision: \(providerRevision)",
            "Resource root: \(resourceRoot ?? "unknown")",
            "Queue: \(queuePath ?? "unknown")",
            "Providers: \(providerPath ?? "unknown")",
            "Logs: \(logPath ?? "unknown")",
            "Results: \(resultsPath ?? "unknown")",
        ].joined(separator: "\n")
    }
}
