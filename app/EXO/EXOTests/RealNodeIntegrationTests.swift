import Foundation
import Testing

@testable import EXO

/// What the unit tests cannot reach: the app talking to a node that demands the key.
///
/// `ExoAPIKeyTests` injects every source, so it proves the precedence and nothing
/// about the file the node actually writes. The Python suite proves the middleware
/// from the other side. Neither meets the other, and the gap between them is where a
/// mismatch would live: the header name, the trailing newline the node writes, the
/// path the key is read from, or the payload the app decodes.
///
/// Skipped unless a node is already listening, so running `EXOTests` alone stays
/// green with nothing else set up. The `macos-app` workflow starts one and fails its
/// own step when the node does not answer, so a skip cannot hide a broken node there.
@Suite(.enabled(if: RealNode.isListening))
struct RealNodeIntegrationTests {
    private let session = URLSession(configuration: .ephemeral)

    @Test func theKeyTheNodeWroteResolves() throws {
        let key = try #require(ExoAPIKey.current, "the live sources resolved no key")
        #expect(key.count >= 32)
        #expect(key == key.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    @Test func aRequestTheAppBuildsIsAccepted() async throws {
        let status = try await statusCode(for: URLRequest.exoNode(url: RealNode.state))
        #expect(status == 200)
    }

    /// Proves the acceptance above came from the key rather than from an open node.
    @Test func aRequestWithoutTheKeyIsRefused() async throws {
        let status = try await statusCode(
            for: URLRequest.exoNode(url: RealNode.state, key: nil)
        )
        #expect(status == 401)
    }

    /// Proves the node compares the key rather than checking that a header is present.
    @Test func aRequestWithTheWrongKeyIsRefused() async throws {
        let status = try await statusCode(
            for: URLRequest.exoNode(url: RealNode.state, key: String(repeating: "w", count: 43))
        )
        #expect(status == 401)
    }

    /// `/node_id` carries the network profile a node reads from a peer it has no key for.
    @Test func theNodeIdRouteStaysPublic() async throws {
        let status = try await statusCode(
            for: URLRequest.exoNode(url: RealNode.nodeId, key: nil)
        )
        #expect(status == 200)
    }

    /// Decodes what the node really serves, so a field the node renames fails here.
    @Test func theRealStatePayloadDecodesIntoTheAppsModel() async throws {
        let (data, _) = try await session.data(for: URLRequest.exoNode(url: RealNode.state))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        _ = try decoder.decode(ClusterState.self, from: data)
    }

    /// The whole path the menu bar depends on, through the service the app itself uses.
    ///
    /// `startPolling` fetches once in a `Task` before it installs its timer, and
    /// `stopPolling` only invalidates that timer, so this takes the one fetch without
    /// depending on a run loop to drive the rest.
    @MainActor
    @Test func theStateServiceFillsFromARealNode() async throws {
        let service = ClusterStateService()
        service.startPolling()
        service.stopPolling()

        var remaining = 40
        while service.latestSnapshot == nil && remaining > 0 {
            try await Task.sleep(nanoseconds: 500_000_000)
            remaining -= 1
        }

        #expect(service.latestSnapshot != nil, "lastError: \(service.lastError ?? "none")")
        #expect(service.localNodeId != nil)
    }

    private func statusCode(for request: URLRequest) async throws -> Int {
        let (_, response) = try await session.data(for: request)
        return try #require(response as? HTTPURLResponse).statusCode
    }
}

/// Where a node listens, and whether one is listening now.
enum RealNode {
    static let baseURL = URL(string: "http://127.0.0.1:52415")!
    static let state = baseURL.appendingPathComponent("state")
    static let nodeId = baseURL.appendingPathComponent("node_id")

    /// True when something answers the one route that needs no key.
    ///
    /// Loaded synchronously because a suite condition cannot await.
    static var isListening: Bool {
        (try? Data(contentsOf: nodeId)) != nil
    }
}
