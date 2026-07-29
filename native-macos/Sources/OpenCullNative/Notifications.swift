import Foundation
import UserNotifications

@MainActor
final class NativeNotifications {
    static let shared = NativeNotifications()
    private init() {}

    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]) { _, _ in }
    }

    func jobChanged(_ job: CullJob) {
        guard ["completed", "failed", "paused"].contains(job.status) else { return }
        let content = UNMutableNotificationContent()
        content.title = (
            job.status == "completed"
                ? "Culling ready to review"
                : job.status == "paused"
                    ? "Culling paused"
                    : "Culling needs attention")
        content.body = "\(job.folderName): \(job.message)"
        content.sound = job.status == "completed" ? .default : nil
        let request = UNNotificationRequest(
            identifier: "opencull-job-\(job.id)-\(job.status)",
            content: content,
            trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
