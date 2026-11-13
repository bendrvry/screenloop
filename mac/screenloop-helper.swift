// screenloop-helper: tells the Raspberry Pi whether this Mac is using the shared monitor.
//
// "Using the monitor" means an external display is connected and the screens are awake.
// macOS wakes in the background ("dark wake") dozens of times a day without ever turning
// the screens on, so those wake-ups don't interrupt the video.
//
// Reports go to POST <pi>/api/mac?name=<this Mac>&screen=on|off, at once on every change
// and every 10 s as a heartbeat. Build and install with install-mac.sh.

import AppKit
import CoreGraphics
import SystemConfiguration

setvbuf(stdout, nil, _IOLBF, 0)

let piURL = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "http://raspberrypi.local:8080"
let macName = (SCDynamicStoreCopyLocalHostName(nil) as String?) ?? ProcessInfo.processInfo.hostName
let heartbeatSeconds: TimeInterval = 10

let stamp: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func log(_ message: String) {
    print("\(stamp.string(from: Date())) \(message)")
}

func onlineDisplays() -> [CGDirectDisplayID] {
    var count: UInt32 = 0
    guard CGGetOnlineDisplayList(0, nil, &count) == .success, count > 0 else { return [] }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetOnlineDisplayList(count, &ids, &count) == .success else { return [] }
    return Array(ids.prefix(Int(count)))
}

final class Helper {
    let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    // Kept up to date by the screens-sleep/wake notifications. At launch, ask CoreGraphics.
    var screensAsleep: Bool = {
        let displays = onlineDisplays()
        return displays.isEmpty || displays.allSatisfy { CGDisplayIsAsleep($0) != 0 }
    }()
    var lastSent: Bool?
    var failing = false

    // Online includes sleeping displays, so this only changes when a cable is (un)plugged.
    var externalConnected: Bool { onlineDisplays().contains { CGDisplayIsBuiltin($0) == 0 } }
    var screenOn: Bool { !screensAsleep && externalConnected }

    func url(_ on: Bool) -> URL? {
        var parts = URLComponents(string: piURL + "/api/mac")
        parts?.queryItems = [URLQueryItem(name: "name", value: macName),
                             URLQueryItem(name: "screen", value: on ? "on" : "off")]
        return parts?.url
    }

    /// Report the current state. With `wait`, block until sent (used just before sleep).
    func report(_ why: String, wait: Bool = false, attempt: Int = 1) {
        let on = screenOn
        if on != lastSent {
            log("screen \(on ? "on" : "off") (\(why))")
        }
        lastSent = on
        guard let url = url(on) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        let done = DispatchSemaphore(value: 0)
        session.dataTask(with: request) { _, response, error in
            defer { done.signal() }
            let status = (response as? HTTPURLResponse)?.statusCode
            DispatchQueue.main.async {
                if error == nil && status == 200 {
                    if self.failing { log("reaching the Pi again") }
                    self.failing = false
                    return
                }
                if !self.failing {
                    log("can't reach the Pi at \(piURL): \(error?.localizedDescription ?? "HTTP \(status ?? 0)")")
                }
                self.failing = true
                // A change matters most, so retry it a few times; heartbeats cover the rest.
                if attempt < 5 && !wait && why != "heartbeat" && on == self.screenOn {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                        if on == self.screenOn { self.report(why, attempt: attempt + 1) }
                    }
                }
            }
        }.resume()
        if wait { _ = done.wait(timeout: .now() + 2) }
    }

    func start() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { _ in
            self.screensAsleep = false
            self.report("screens woke")
        }
        center.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { _ in
            self.screensAsleep = true
            self.report("screens slept")
        }
        center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { _ in
            self.screensAsleep = true
            self.report("going to sleep", wait: true)
        }
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { _ in
            if self.screenOn != self.lastSent { self.report("displays changed") }
        }
        Timer.scheduledTimer(withTimeInterval: heartbeatSeconds, repeats: true) { _ in
            self.report("heartbeat")
        }
        log("reporting to \(piURL) as \(macName)")
        report("started")
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)  // no Dock icon, no menu bar
let helper = Helper()
helper.start()
app.run()
