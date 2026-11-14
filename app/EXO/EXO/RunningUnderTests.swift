import Foundation

/// The signals that this process is hosting a test bundle, read once at first use.
///
/// `EXOTests` is an app-hosted target, so `xcodebuild test` launches the app. What a
/// real session starts has to stay out of a test process: the welcome window, the login
/// item, `UNUserNotificationCenter`, which needs a bundle the system has registered, and
/// the node, the pollers and Sparkle, which would all outlive the tests. `main.swift`
/// reads these signals to host the tests without entering `EXOApp.main()` at all, and
/// `EXOApp.init` reads them again should it be reached some other way.
///
/// Missing every signal does not fail a test, it starts a real session beside the
/// tests: the app has launched before XCTest executes anything. Three signals are read
/// for that reason, one of them independent of any name Apple can change, and
/// `ExoAPIKeyTests` asserts the independent one still holds.
struct TestHostSignals {
    /// True when the XCTest framework is loaded in this process.
    ///
    /// The test runner inserts `libXCTestBundleInject.dylib`, which links XCTest, so dyld
    /// has registered its classes before `main`. No variable name is involved.
    let xctestFrameworkLoaded: Bool

    /// True when `XCTestConfigurationFilePath` is set, as the runner sets it for the host.
    let configurationFilePath: Bool

    /// True when `XCTestBundlePath` is set, as the runner sets it for the host.
    let bundlePath: Bool

    /// True when any one of the three signals held.
    var isRunningTests: Bool {
        xctestFrameworkLoaded || configurationFilePath || bundlePath
    }

    /// The signals as they stood when something first read them, which is `main.swift`.
    static let atLaunch = TestHostSignals()

    private init() {
        xctestFrameworkLoaded = NSClassFromString("XCTestCase") != nil
        let environment = ProcessInfo.processInfo.environment
        configurationFilePath = environment["XCTestConfigurationFilePath"] != nil
        bundlePath = environment["XCTestBundlePath"] != nil
    }
}

/// True while this process is hosting a test bundle: see `TestHostSignals`.
let isRunningTests: Bool = TestHostSignals.atLaunch.isRunningTests
