import Foundation

/// The key the local node requires on every API route except `/node_id`.
///
/// The node generates one on first run and writes it to `~/.exo/api_key`, which
/// this app reads. That file does not exist until the node has started, and this
/// app is what starts it, so the value is looked up lazily and re-read whenever
/// it is still missing rather than captured once at launch.
///
/// An operator who sets `EXO_API_KEY` in Settings chooses the node's key instead,
/// and the node then writes no file, so that value is read from the same place
/// `ExoProcessController` reads it when building the child process environment.
enum ExoAPIKey {
    private static let fileURL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".exo")
        .appendingPathComponent("api_key")

    private static let lock = NSLock()
    private static var cached: String?

    /// The node's key, or nil when it has not written one and none is configured.
    static var current: String? {
        lock.lock()
        defer { lock.unlock() }
        if let cached {
            return cached
        }
        guard let key = configuredKey() ?? storedKey() else {
            return nil
        }
        cached = key
        return key
    }

    /// Forget the cached key so the next request re-reads it.
    ///
    /// Called when the node refuses a request, which is what a node restarted
    /// under a different key looks like from here.
    static func invalidate() {
        lock.lock()
        cached = nil
        lock.unlock()
    }

    /// The key from the user-defined environment variables, if one is set there.
    private static func configuredKey() -> String? {
        guard
            let data = UserDefaults.standard.data(forKey: "EXOCustomEnvironmentVariables"),
            let variables = try? JSONDecoder().decode([CustomEnvironmentVariable].self, from: data)
        else {
            return nil
        }
        let match = variables.first {
            $0.key.trimmingCharacters(in: .whitespaces) == "EXO_API_KEY"
        }
        return nonEmpty(match?.value)
    }

    /// The key from the file the node writes on first run.
    private static func storedKey() -> String? {
        nonEmpty(try? String(contentsOf: fileURL, encoding: .utf8))
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

extension URLRequest {
    /// Build a request to the local node carrying its API key.
    ///
    /// Used for every call to the node so that adding one cannot forget the key.
    /// Requests to anything else — the bug-report endpoint, a presigned upload —
    /// keep using `URLRequest(url:)`, since the key belongs only to this node.
    static func exoNode(url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        if let key = ExoAPIKey.current {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        return request
    }
}
