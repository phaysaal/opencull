import Foundation

struct RecentReview: Codable, Identifiable {
    let id: UUID
    let report: String
    let photos: String
    let title: String
    var lastOpened: Date
}

private struct AccessBookmark: Codable {
    let path: String
    let bookmark: Data
}

private struct LibraryDocument: Codable {
    var reviews: [RecentReview]
    var access: [AccessBookmark]
}

@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var reviews: [RecentReview] = []

    private var access: [AccessBookmark] = []
    private var activeURLs: [URL] = []
    private let documentURL: URL

    init() {
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Darkimiya", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: support, withIntermediateDirectories: true)
        documentURL = support.appendingPathComponent("NativeLibrary.json")
        let legacyURL = support.deletingLastPathComponent()
            .appendingPathComponent("OpenCull", isDirectory: true)
            .appendingPathComponent("NativeLibrary.json")
        if !FileManager.default.fileExists(atPath: documentURL.path),
           FileManager.default.fileExists(atPath: legacyURL.path) {
            try? FileManager.default.copyItem(at: legacyURL, to: documentURL)
        }
        load()
    }

    func rememberAccess(to url: URL) {
        let standardized = url.standardizedFileURL
        guard !access.contains(where: { $0.path == standardized.path }) else {
            activate(standardized)
            return
        }
        do {
            let data = try standardized.bookmarkData(
                options: .withSecurityScope,
                includingResourceValuesForKeys: nil,
                relativeTo: nil)
            access.append(AccessBookmark(path: standardized.path, bookmark: data))
            activate(standardized)
            save()
        } catch {
            // Unsandboxed development builds can still use the selected URL for
            // this session even if bookmark creation is unavailable.
            activate(standardized)
        }
    }

    func record(report: String, photos: String, title: String) {
        let normalizedReport = URL(fileURLWithPath: report).standardizedFileURL.path
        reviews.removeAll { $0.report == normalizedReport }
        reviews.insert(
            RecentReview(
                id: UUID(), report: normalizedReport, photos: photos,
                title: title, lastOpened: Date()),
            at: 0)
        reviews = Array(reviews.prefix(30))
        rememberAccess(to: URL(fileURLWithPath: report))
        rememberAccess(to: URL(fileURLWithPath: photos))
        save()
    }

    func forget(_ review: RecentReview) {
        reviews.removeAll { $0.id == review.id }
        save()
    }

    func available(_ review: RecentReview) -> Bool {
        FileManager.default.fileExists(atPath: review.report)
            && FileManager.default.fileExists(atPath: review.photos)
    }

    private func load() {
        guard
            let data = try? Data(contentsOf: documentURL),
            let document = try? JSONDecoder().decode(LibraryDocument.self, from: data)
        else { return }
        reviews = document.reviews.sorted { $0.lastOpened > $1.lastOpened }
        access = document.access
        restoreAccess()
    }

    private func restoreAccess() {
        for item in access {
            var stale = false
            guard let url = try? URL(
                resolvingBookmarkData: item.bookmark,
                options: .withSecurityScope,
                relativeTo: nil,
                bookmarkDataIsStale: &stale)
            else { continue }
            activate(url)
            if stale {
                // A replacement bookmark is captured the next time the user
                // explicitly chooses this item.
                continue
            }
        }
    }

    private func activate(_ url: URL) {
        guard !activeURLs.contains(url) else { return }
        if url.startAccessingSecurityScopedResource() {
            activeURLs.append(url)
        }
    }

    private func save() {
        let document = LibraryDocument(reviews: reviews, access: access)
        guard let data = try? JSONEncoder().encode(document) else { return }
        let temporary = documentURL.appendingPathExtension("tmp")
        do {
            try data.write(to: temporary, options: .atomic)
            _ = try FileManager.default.replaceItemAt(
                documentURL, withItemAt: temporary)
        } catch {
            try? data.write(to: documentURL, options: .atomic)
            try? FileManager.default.removeItem(at: temporary)
        }
    }
}
