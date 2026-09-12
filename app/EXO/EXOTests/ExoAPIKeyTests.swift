import Foundation
import Testing

@testable import EXO

/// The app has to read the same key sources the node reads, in the same order.
///
/// Reading fewer of them leaves the app holding no key while the node has one,
/// which is a 401 on every request the app makes and an empty menu bar with no
/// indication why.
struct ExoAPIKeyTests {
    private let settingsRowKey = "settings-row-0000000000000000000000"
    private let environmentKey = "environment-000000000000000000000000"
    private let fileKey = "file-00000000000000000000000000000000"

    private static let nodeURL = URL(string: "http://127.0.0.1:52415/state")!

    private func sources(
        settings: String? = nil,
        environment: String? = nil,
        file: String? = nil
    ) -> ExoAPIKey.Sources {
        ExoAPIKey.Sources(
            settings: { settings },
            environment: { environment },
            file: { file }
        )
    }

    @Test func aSettingsRowOutranksTheEnvironmentAndTheFile() {
        let candidates = sources(
            settings: settingsRowKey,
            environment: environmentKey,
            file: fileKey
        )
        #expect(ExoAPIKey.resolve(from: candidates) == settingsRowKey)
    }

    @Test func theInheritedEnvironmentOutranksTheFile() {
        let candidates = sources(environment: environmentKey, file: fileKey)
        #expect(ExoAPIKey.resolve(from: candidates) == environmentKey)
    }

    @Test func theFileIsReadWhenNothingElseIsSet() {
        #expect(ExoAPIKey.resolve(from: sources(file: fileKey)) == fileKey)
    }

    @Test func noSourceYieldsNoKey() {
        #expect(ExoAPIKey.resolve(from: sources()) == nil)
    }

    @Test func aBlankSourceFallsThroughToTheNextOne() {
        let candidates = sources(settings: "   ", environment: environmentKey)
        #expect(ExoAPIKey.resolve(from: candidates) == environmentKey)
    }

    @Test func theTrailingNewlineTheNodeWritesIsTrimmed() {
        let candidates = sources(file: "\(fileKey)\n")
        #expect(ExoAPIKey.resolve(from: candidates) == fileKey)
    }

    @Test func theRowIsFoundInWhatSettingsPersists() throws {
        let rows = [
            CustomEnvironmentVariable(key: "HF_TOKEN", value: "unrelated"),
            CustomEnvironmentVariable(key: " EXO_API_KEY ", value: settingsRowKey),
        ]
        let data = try JSONEncoder().encode(rows)
        #expect(ExoAPIKey.settingsKey(from: data) == settingsRowKey)
    }

    @Test func anotherRowIsNotMistakenForTheKey() throws {
        let rows = [CustomEnvironmentVariable(key: "HF_TOKEN", value: "unrelated")]
        let data = try JSONEncoder().encode(rows)
        #expect(ExoAPIKey.settingsKey(from: data) == nil)
    }

    @Test func rowsThatDoNotDecodeYieldNoKey() {
        #expect(ExoAPIKey.settingsKey(from: Data("not json".utf8)) == nil)
    }

    @Test func theStorageKeyIsTheOneSettingsWritesTo() {
        #expect(CustomEnvironmentVariable.storageKey == "EXOCustomEnvironmentVariables")
    }

    @Test func theFileSitsWhereTheNodeWritesIt() {
        #expect(ExoAPIKey.fileURL.path.hasSuffix("/.exo/api_key"))
    }

    @Test func aRequestCarriesTheKeyAsABearerToken() {
        let request = URLRequest.exoNode(url: Self.nodeURL, key: fileKey)
        let header = request.value(forHTTPHeaderField: "Authorization")
        #expect(header == "Bearer \(fileKey)")
    }

    @Test func aRequestWithoutAKeyCarriesNoAuthorization() {
        let request = URLRequest.exoNode(url: Self.nodeURL, key: nil)
        #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test func theKeyIsReadOnceItHasBeenFound() {
        let loader = CountingLoader(value: fileKey)
        let cache = ExoAPIKey.Cache(load: loader.load)

        #expect(cache.value == fileKey)
        #expect(cache.value == fileKey)
        #expect(loader.calls == 1)
    }

    @Test func aMissingKeyIsLookedForAgain() {
        let loader = CountingLoader(value: nil)
        let cache = ExoAPIKey.Cache(load: loader.load)

        #expect(cache.value == nil)
        #expect(cache.value == nil)
        #expect(loader.calls == 2)
    }

    @Test func aKeyWrittenAfterTheFirstReadIsPickedUp() {
        let loader = CountingLoader(value: nil)
        let cache = ExoAPIKey.Cache(load: loader.load)

        #expect(cache.value == nil)
        loader.value = fileKey
        #expect(cache.value == fileKey)
    }

    @Test func invalidatingForcesAReRead() {
        let loader = CountingLoader(value: fileKey)
        let cache = ExoAPIKey.Cache(load: loader.load)

        #expect(cache.value == fileKey)
        cache.invalidate()
        #expect(cache.value == fileKey)
        #expect(loader.calls == 2)
    }

    /// Proves the guard that keeps app launch out of this process is still live.
    ///
    /// This test runs in exactly the environment `isRunningTests` describes, so the
    /// signals asserted here are the ones `EXOApp.init` read moments earlier.
    @Test func theHostAppKnowsItIsRunningTests() {
        #expect(isRunningTests)
    }

    /// Losing every signal hangs the run at the network-setup alert instead of failing a
    /// test, so the signal that depends on no variable name is asserted on its own.
    @Test func theHostAppSeesTheTestFrameworkWithoutReadingTheEnvironment() {
        let signals = TestHostSignals.atLaunch
        let environmentSignals = """
            XCTestConfigurationFilePath=\(signals.configurationFilePath), \
            XCTestBundlePath=\(signals.bundlePath)
            """
        #expect(
            signals.xctestFrameworkLoaded,
            "XCTest was not loaded when EXOApp.init read the signals. \(environmentSignals)"
        )
    }
}

/// A key source that records how often it was read.
private final class CountingLoader {
    var value: String?
    private(set) var calls = 0

    init(value: String?) {
        self.value = value
    }

    func load() -> String? {
        calls += 1
        return value
    }
}
