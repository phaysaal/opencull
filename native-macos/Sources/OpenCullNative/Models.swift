import Foundation

struct DesktopState: Decodable {
    let queue: QueueState
    let providers: ProviderState
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

struct CullJob: Decodable, Identifiable {
    let id: String
    let photos: String
    let output: String
    let profile: String
    let status: String
    let message: String
    let providerProfileName: String?
    let providerPrivacy: String?
    let progress: CullProgress

    enum CodingKeys: String, CodingKey {
        case id, photos, output, profile, status, message, progress
        case providerProfileName = "provider_profile_name"
        case providerPrivacy = "provider_privacy"
    }

    var folderName: String {
        URL(fileURLWithPath: photos).lastPathComponent
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

    enum CodingKeys: String, CodingKey {
        case photos, recursive, profile
        case keepPerGroup = "keep_per_group"
        case providerProfileID = "provider_profile_id"
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
            "OpenCull native diagnostics",
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
