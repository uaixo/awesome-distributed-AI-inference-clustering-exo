import Foundation

/// The key the local node requires on every API route except `/node_id`.
///
/// The node generates one on first run and writes it to `~/.exo/api_key`, which
/// this app reads. That file does not exist until the node has started, and this
/// app is what starts it, so the value is looked up lazily and re-read whenever
/// it is still missing rather than captured once at launch.
///
/// An operator who sets `EXO_API_KEY` chooses the node's key instead, and the node
/// then writes no file. The sources below are the three `ExoProcessController`
/// composes into the child environment, in the precedence that method gives them:
/// a Settings row wins, since those are applied last as overrides; otherwise the
/// environment this app itself inherited, which seeds the child's; otherwise the
/// file. Reading fewer of them than the node does would leave the app holding no
/// key while the node has one.
enum ExoAPIKey {
    /// Where the node writes the key it generates on first run.
    static let fileURL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".exo")
        .appendingPathComponent("api_key")

    /// The node's key, or nil when it has not written one and none is configured.
    static var current: String? {
        cache.value
    }

    private static let cache = Cache { resolve(from: .live) }

    /// Forget the cached key so the next request re-reads it.
    ///
    /// Called when the node refuses a request, which is what a node restarted
    /// under a different key looks like from here.
    static func invalidate() {
        cache.invalidate()
    }

    /// Take the key from the first source holding one, trimmed of whitespace.
    ///
    /// - Parameter sources: where to look, in precedence order.
    /// - Returns: the key, or nil when every source is absent or blank.
    static func resolve(from sources: Sources) -> String? {
        if let settings = nonEmpty(sources.settings()) {
            return settings
        }
        if let environment = nonEmpty(sources.environment()) {
            return environment
        }
        return nonEmpty(sources.file())
    }

    /// The `EXO_API_KEY` value among the Settings rows encoded in `data`.
    ///
    /// Decodes what `ExoProcessController` persists, so the two stay tied to the
    /// `CustomEnvironmentVariable` encoding and to its `storageKey`.
    ///
    /// - Parameter data: the rows as stored in UserDefaults.
    /// - Returns: the row's value, or nil when there is no such row.
    static func settingsKey(from data: Data) -> String? {
        let decoder = JSONDecoder()
        guard let rows = try? decoder.decode([CustomEnvironmentVariable].self, from: data) else {
            return nil
        }
        let match = rows.first {
            $0.key.trimmingCharacters(in: .whitespaces) == "EXO_API_KEY"
        }
        return match?.value
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
            !trimmed.isEmpty
        else {
            return nil
        }
        return trimmed
    }
}

extension ExoAPIKey {
    /// The three places the node's key can come from.
    ///
    /// Each is a closure so that resolution reads only as far as it has to: with a
    /// Settings row set, the file is never touched.
    struct Sources {
        /// The `EXO_API_KEY` row the user added in Settings, if there is one.
        let settings: () -> String?
        /// `EXO_API_KEY` in this app's own environment, which seeds the node's.
        let environment: () -> String?
        /// The contents of the file the node writes on first run.
        let file: () -> String?

        /// The sources as they exist on this machine.
        static var live: Sources {
            Sources(
                settings: {
                    let key = CustomEnvironmentVariable.storageKey
                    let stored = UserDefaults.standard.data(forKey: key)
                    return stored.flatMap(ExoAPIKey.settingsKey(from:))
                },
                environment: { ProcessInfo.processInfo.environment["EXO_API_KEY"] },
                file: { try? String(contentsOf: ExoAPIKey.fileURL, encoding: .utf8) }
            )
        }
    }

    /// A key read on demand and kept once found.
    ///
    /// Finding nothing is not recorded: the node writes its file only after it
    /// starts, so a caller that comes up empty has to be able to find the key on a
    /// later read.
    final class Cache: @unchecked Sendable {
        private let load: () -> String?
        private let lock = NSLock()
        /// Guarded by `lock`, which is what makes the shared instance safe.
        private var cached: String?

        /// - Parameter load: reads the key, called again until it returns one.
        init(load: @escaping () -> String?) {
            self.load = load
        }

        /// The key, loading it unless an earlier read already found one.
        var value: String? {
            lock.lock()
            defer { lock.unlock() }
            if let cached {
                return cached
            }
            cached = load()
            return cached
        }

        /// Forget the key so the next read loads it again.
        func invalidate() {
            lock.lock()
            cached = nil
            lock.unlock()
        }
    }
}

extension URLRequest {
    /// Build a request to the local node carrying its API key.
    ///
    /// Used for every call to the node so that adding one cannot forget the key.
    /// Requests to anything else — the bug-report endpoint, a presigned upload —
    /// keep using `URLRequest(url:)`, since the key belongs only to this node.
    static func exoNode(url: URL) -> URLRequest {
        exoNode(url: url, key: ExoAPIKey.current)
    }

    /// Build a request carrying `key` as a bearer token, or none when it is nil.
    static func exoNode(url: URL, key: String?) -> URLRequest {
        var request = URLRequest(url: url)
        if let key {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        return request
    }
}
