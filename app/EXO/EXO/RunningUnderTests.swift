import Foundation

/// True while this process is hosting a test bundle.
///
/// `EXOTests` is an app-hosted target, so `xcodebuild test` launches the app and
/// runs `EXOApp.init`. What that init starts for a real session has to stay out of
/// a test process: the network-setup step opens a modal alert nothing would
/// dismiss, `UNUserNotificationCenter` needs a bundle the system has registered,
/// and the node, the pollers and Sparkle would all outlive the tests.
///
/// XCTest sets these variables in the host process, and it hosts swift-testing
/// suites too. `ExoAPIKeyTests` asserts this is true, so a variable Apple renames
/// surfaces as a failing test instead of as a hung test run.
let isRunningTests: Bool = {
    let environment = ProcessInfo.processInfo.environment
    if environment["XCTestConfigurationFilePath"] != nil {
        return true
    }
    return environment["XCTestBundlePath"] != nil
}()
