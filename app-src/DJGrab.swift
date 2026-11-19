// DJ Grab: drop a link, get a tagged AIFF ready for Serato / Pulse DJ.
// Built by install.sh with: swiftc -parse-as-library DJGrab.swift

import SwiftUI
import AppKit
import UniformTypeIdentifiers
import AVFoundation
import CoreAudio
import Network

// MARK: - Paths & settings

let supportDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/DJGrab")
let pythonPath = supportDir.appendingPathComponent("venv/bin/python3").path
let sharedEngine = supportDir.appendingPathComponent("engine")
let enginePath = ownerCopy ? supportDir.appendingPathComponent("djgrab.py").path
                         : sharedEngine.appendingPathComponent("djgrab.py").path
/// Where the engine finds ffmpeg / deno: the shared app's own copies first, then Homebrew.
let toolPATH = supportDir.appendingPathComponent("bin").path + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
/// The shared app's update feed (a small JSON file on GitHub). Filled in once Vinny's GitHub is set up.
let updateFeed = "https://raw.githubusercontent.com/vinnyricott/djgrab/main/latest.json"
// Symlink to ~/Documents/DJ Grab (DJ Grab's code folder), where updates are dropped in. Set up by build_app.sh.
let updatesDir = supportDir.appendingPathComponent("updates")
let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
/// How a version number is shown: up to 99 as is ("v99"); from 100 on as 1.0.0 style, where each small
/// update adds 1 to the last number (1.0.1) and a big one moves to the next 10 (1.1.0)
func shownVersion(_ v: Int) -> String { v < 100 ? "\(v)" : "\(v / 100).\(v / 10 % 10).\(v % 10)" }
func shownVersion(_ s: String) -> String { shownVersion(Int(s) ?? 0) }

/// Free space left on the USB drive, e.g. "48.2 GB free".
func usbFree(_ name: String?) -> String {
    guard let name = name else { return "" }
    let url = URL(fileURLWithPath: "/Volumes/\(name)")
    let vals = try? url.resourceValues(forKeys: [.volumeAvailableCapacityKey])
    guard let free = vals?.volumeAvailableCapacity else { return "" }
    return ByteCountFormatter.string(fromByteCount: Int64(free), countStyle: .file) + " free"
}
/// The owner's copy (Vinny's): genre sorting, Serato crates and cues, name cleanup, the Intros & Cues tab.
/// Every other copy is the shared app: rips into one folder, intro versions optional, nothing tied to any DJ
/// software. The owner's Mac has a file named ADMIN in DJ Grab's code folder.
/// This Mac is the owner's (Vinny's) copy: it has a file named ADMIN in DJ Grab's code folder.
let ownerCopy = ["Documents/DJ Grab/ADMIN", "DJ music/.djgrab/ADMIN"].contains {
    FileManager.default.fileExists(atPath: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent($0).path) }
/// The full owner features (genre sorting, Serato crates, name cleanup, publishing). On the owner's Mac,
/// Settings ▸ "View as everyone else" turns this off to see the app the way other people get it.
let isAdmin = ownerCopy && !UserDefaults.standard.bool(forKey: "viewAsShared")
let defaultOutput = isAdmin ? "/Volumes/Vinny's USB/Vinny's USB/00 DJ GRAB NEW"
    : FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Music/DJ Grab").path

enum Keys {
    static let output = "outputFolder"
    static let cookies = "cookiesBrowser"
    static let loudness = "loudnessCheck"
    static let lastUpdate = "lastYtdlpUpdate"
    static let parallel = "parallelDownloads"
}

// MARK: - Model

enum RowState { case waiting, running, done, skipped, failed, checked, lowQuality }

struct Row: Identifiable {
    let id = UUID()
    var link: String
    var name: String
    var stage: String = "Waiting"
    var pct: Double = 0
    var state: RowState = .waiting
    var path: String? = nil
    var detail: String = ""
    var warnings: [String] = []
    var note: String = ""
    var noteIsNew = false
    var key: String = ""
    var lowLink: String? = nil      // poor-quality upload waiting for your OK
}

// Same song = same key, however the link or name was written
func queueKey(_ link: String) -> String {
    if link.hasPrefix("search:") {
        return "s:" + String(link.dropFirst(7)).lowercased().filter { $0.isLetter || $0.isNumber }
    }
    guard let c = URLComponents(string: link) else { return link.lowercased() }
    var host = (c.host ?? "").lowercased()
    for pre in ["www.", "m.", "music."] where host.hasPrefix(pre) { host = String(host.dropFirst(pre.count)) }
    if host == "youtu.be" { return "yt:" + String(c.path.dropFirst()) }
    if host.contains("youtube.com"), let v = c.queryItems?.first(where: { $0.name == "v" })?.value { return "yt:" + v }
    if let l = c.queryItems?.first(where: { $0.name == "list" })?.value { return "list:" + l }
    return host + c.path.lowercased()
}

final class Store: ObservableObject {
    static let shared = Store()

    @Published var rows: [Row] = []
    @Published var busy = false
    @Published var celebrate = false          // the "all done" animation after a batch
    @Published var banner: String? = nil
    @Published var updating = false
    @Published var cratesWaiting = 0
    @Published var driveConnected = true
    @Published var etaText = ""
    @Published var updateAvailable = false
    @Published var availableVersion = 0
    @Published var updatePreparing = false    // Claude is still adding files for this update: wait
    private var durations: [TimeInterval] = []
    private var crateTimer: Timer?
    var tick = 0
    private var flushing = false

    private var queue: [(link: String, rowID: UUID, checkOnly: Bool)] = []
    private var currentRow: UUID?
    private var placeholderFree = false

    var outputFolder: String {
        UserDefaults.standard.string(forKey: Keys.output) ?? defaultOutput
    }

    // Accepts anything: a single link, several links, or text with links inside.
    // The drive that holds the save folder (nil when saving to the Mac itself)
    var driveName: String? {
        let parts = outputFolder.split(separator: "/")
        guard outputFolder.hasPrefix("/Volumes/"), parts.count >= 2 else { return nil }
        return String(parts[1])
    }

    @discardableResult
    func refreshDrive() -> Bool {
        if let d = driveName {
            driveConnected = FileManager.default.fileExists(atPath: "/Volumes/" + d)
        } else {
            driveConnected = true
        }
        return driveConnected
    }

    @Published var ejecting = false

    /// Eject the USB: stop what DJ Grab is doing on it, eject like Finder does, and if something else still
    /// has it open, say which app.
    func eject() {
        guard let name = driveName, refreshDrive(), !ejecting else { return }
        if busy {
            banner = "Finish downloads first"
            return
        }
        if ToolsStore.shared.running { ToolsStore.shared.pause() }
        ejecting = true
        let vol = "/Volumes/" + name
        DispatchQueue.global(qos: .userInitiated).async {
            // Spotlight indexing the USB is the most common thing that blocks ejecting
            FileManager.default.createFile(atPath: vol + "/.metadata_never_index", contents: nil)
            func run(_ tool: String, _ args: [String]) -> (Int32, String) {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: tool)
                p.arguments = args
                let out = Pipe()
                p.standardOutput = out
                p.standardError = out
                guard (try? p.run()) != nil else { return (-1, "") }
                let d = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                return (p.terminationStatus, String(data: d, encoding: .utf8) ?? "")
            }
            var ok = false
            for attempt in 0..<3 {
                if attempt > 0 { Thread.sleep(forTimeInterval: 1.5) }
                if run("/usr/sbin/diskutil", ["eject", vol]).0 == 0 { ok = true; break }
            }
            var blockers: [String] = []
            if !ok {
                let (_, lsof) = run("/usr/sbin/lsof", ["-F", "c", vol])
                var seen = Set<String>()
                for line in lsof.split(separator: "\n") where line.hasPrefix("c") {
                    var app = String(line.dropFirst())
                    if app == "mds" || app.hasPrefix("mds_") || app == "mdworker" || app.hasPrefix("mdworker") { app = "Spotlight" }
                    if app.hasPrefix("Serato") { app = "Serato DJ Pro" }
                    if seen.insert(app).inserted { blockers.append(app) }
                }
            }
            DispatchQueue.main.async {
                self.ejecting = false
                _ = self.refreshDrive()
                if ok {
                    self.banner = "\(name) ejected"
                } else if !blockers.isEmpty {
                    self.banner = "Can't eject: in use by \(blockers.joined(separator: ", "))"
                } else {
                    self.banner = "Couldn't eject \(name). Try again in a few seconds."
                }
            }
        }
    }

    func resumeAfterOffline() {
        for i in rows.indices where rows[i].state == .waiting { rows[i].stage = "Waiting" }
        startNext()
    }

    func add(text: String, checkOnly: Bool = false) {
        if !refreshDrive() {
            let a = NSAlert()
            a.alertStyle = .warning
            a.messageText = "\(driveName ?? "Your USB") isn't connected"
            a.informativeText = "Plug in your USB and try again. Nothing was downloaded."
            a.addButton(withTitle: "OK")
            a.runModal()
            return
        }
        var links: [String] = []
        if let det = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) {
            let ns = text as NSString
            for m in det.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
                if let u = m.url, u.scheme == "http" || u.scheme == "https" {
                    links.append(u.absoluteString)
                }
            }
        }
        if links.isEmpty {
            // No links: treat each line as a song name (Title - Artist, Song by Artist, ...) and search for it
            var names: [String] = []
            for raw in text.components(separatedBy: .newlines) {
                var l = raw.replacingOccurrences(of: "**", with: "")
                if let r = l.range(of: "*(") { l = String(l[..<r.lowerBound]) }
                l = l.replacingOccurrences(of: "*", with: "")
                l = l.trimmingCharacters(in: CharacterSet(charactersIn: " \t-•"))
                if let r = l.range(of: #"^\d+[.)]\s*"#, options: .regularExpression) { l.removeSubrange(r) }
                if l.count < 3 || l.hasSuffix(":") || l.hasPrefix("#") { continue }
                names.append(l)
            }
            if names.isEmpty {
                banner = "Nothing to download in that"
                return
            }
            var added = 0
            for n in names where enqueue("search:" + n, checkOnly: checkOnly) { added += 1 }
            let dupes = names.count - added
            banner = "Searching for \(added) song\(added == 1 ? "" : "s")" + (dupes > 0 ? " · skipped \(dupes) already queued" : "")
            return
        }
        var added = 0
        for l in links where enqueue(l, checkOnly: checkOnly) { added += 1 }
        if links.count > added {
            let dupes = links.count - added
            banner = "Skipped \(dupes) link\(dupes == 1 ? "" : "s") already queued"
        }
    }

    var allowLow = Set<String>()          // poor-quality uploads you said yes to

    /// "Download anyway" on a poor-quality song: grab that same upload, below the minimum
    func downloadAnyway(_ id: UUID) {
        guard let i = index(id), let l = rows[i].lowLink else { return }
        allowLow.insert(l)
        rows.remove(at: i)
        _ = enqueue(l)
    }
    /// Undo on a finished download: that exact file (and its intro version, if one was made) goes to
    /// _To Delete ▸ Undone downloads. Moved, never deleted, so it can be brought back.
    func undo(_ id: UUID) {
        guard let i = index(id), let p = rows[i].path else { return }
        let parts = p.split(separator: "/").map(String.init)
        let root = p.hasPrefix("/Volumes/") && parts.count > 2 ? "/Volumes/" + parts[1] : (p as NSString).deletingLastPathComponent
        let dest = root + "/_To Delete/Undone downloads"
        try? FileManager.default.createDirectory(atPath: dest, withIntermediateDirectories: true)
        let intro = p.lowercased().hasSuffix(".mp3") ? String(p.dropLast(4)) + " (Intro).mp3" : ""
        for f in [p, intro] where !f.isEmpty && FileManager.default.fileExists(atPath: f) {
            if Preview.shared.playing == f { Preview.shared.stop() }
            try? FileManager.default.moveItem(atPath: f, toPath: dest + "/" + (f as NSString).lastPathComponent)
        }
        let tools = ToolsStore.shared
        if let w = tools.waiting.firstIndex(of: p) { tools.waiting.remove(at: w); UserDefaults.standard.set(tools.waiting, forKey: "introsWaiting") }
        rows[i].state = .skipped
        rows[i].stage = "Removed"
        rows[i].detail = "Moved to _To Delete ▸ Undone downloads"
        rows[i].path = nil
        banner = "Removed \(rows[i].name)"
    }

    /// ✕ on a song that's waiting or downloading: it stops, nothing half-done is saved
    func cancel(_ id: UUID) {
        guard let i = index(id) else { return }
        rows[i].state = .skipped
        rows[i].stage = "Cancelled"
        rows[i].detail = ""
        if let q = queue.firstIndex(where: { $0.rowID == id }) { queue.remove(at: q); updateETA(); return }
        if let job = running.first(where: { $0.rowID == id || $0.currentRow == id }) {
            job.proc?.terminate()                     // the engine's unfinished file is never moved into place
        }
    }

    func skipLow(_ id: UUID) {
        guard let i = index(id) else { return }
        rows[i].state = .skipped; rows[i].stage = "Skipped (poor quality)"; rows[i].lowLink = nil
    }

    /// After a song or a whole batch: one popup for every poor-quality song in it
    func askAboutLowQuality() {
        let low = rows.filter { $0.state == .lowQuality }
        guard !low.isEmpty else { return }
        let a = NSAlert()
        a.alertStyle = .warning
        if low.count == 1 {
            a.messageText = "Poor quality: \(low[0].name)"
            a.informativeText = low[0].detail + "\n\nDo you still want to download it?"
            a.addButton(withTitle: "Download Anyway")
            a.addButton(withTitle: "Skip")
        } else {
            a.messageText = "\(low.count) songs are poor quality"
            a.informativeText = low.prefix(8).map { "• " + $0.name }.joined(separator: "\n")
                + (low.count > 8 ? "\n…and \(low.count - 8) more" : "")
                + "\n\nDo you still want to download them? You can also choose one by one in the list."
            a.addButton(withTitle: "Download All Anyway")
            a.addButton(withTitle: "Choose in List")
            a.addButton(withTitle: "Skip All")
        }
        let r = a.runModal()
        if r == .alertFirstButtonReturn { low.forEach { downloadAnyway($0.id) } }
        else if (low.count == 1 && r == .alertSecondButtonReturn) || r == .alertThirdButtonReturn { low.forEach { skipLow($0.id) } }
    }

    @discardableResult
    func enqueue(_ link: String, checkOnly: Bool = false) -> Bool {
        let key = queueKey(link)
        // already waiting or downloading: skip it. Finished songs go through the library check instead,
        // which skips anything already in your folders (and lets a redo through once you've removed the file).
        if rows.contains(where: { $0.key == key && ($0.state == .waiting || $0.state == .running) }) { return false }
        var row = Row(link: link, name: link.hasPrefix("search:") ? String(link.dropFirst(7)) : link)
        row.key = key
        if checkOnly { row.stage = "Waiting to check" }
        rows.insert(row, at: 0)
        queue.append((link: link, rowID: row.id, checkOnly: checkOnly))
        startNext()
        return true
    }

    private func index(_ id: UUID?) -> Int? {
        guard let id = id else { return nil }
        return rows.firstIndex { $0.id == id }
    }

    // One running engine process (one link)
    final class Job {
        let rowID: UUID
        var currentRow: UUID?
        var placeholderFree = true
        var buffer = Data()
        let started = Date()
        var proc: Process?
        init(rowID: UUID) { self.rowID = rowID; self.currentRow = rowID }
    }
    private var running: [Job] = []

    var maxParallel: Int {
        let v = UserDefaults.standard.integer(forKey: Keys.parallel)
        return v == 0 ? 4 : min(max(v, 1), 6)
    }

    private func startNext() {
        if !Connection.shared.online {             // hold the queue until the internet is back
            for q in queue { if let i = index(q.rowID) { rows[i].stage = "Waiting for internet" } }
            busy = !running.isEmpty
            return
        }
        while running.count < maxParallel && !queue.isEmpty {
            launch(queue.removeFirst())
        }
        let wasBusy = busy
        busy = !running.isEmpty
        if wasBusy && !busy && rows.contains(where: { $0.state == .lowQuality }) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.askAboutLowQuality() }
        }
        if wasBusy && !busy && rows.contains(where: { $0.state == .done }) {
            withAnimation(.spring(response: 0.45, dampingFraction: 0.6)) { celebrate = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) {
                withAnimation(.easeOut(duration: 0.4)) { self.celebrate = false }
            }
        }
        updateETA()
        DockProgress.update()
    }

    // A newer version of the app has been dropped into the updates folder
    @Published var feedZip: String? = nil
    func checkUpdateAvailable() {
        if !ownerCopy { checkFeed(); return }
        let verURL = updatesDir.appendingPathComponent("app-src/VERSION")
        let script = updatesDir.appendingPathComponent("build_app.sh").path
        guard let raw = try? String(contentsOf: verURL, encoding: .utf8),
              let available = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              FileManager.default.fileExists(atPath: script) else { updateAvailable = false; return }
        availableVersion = available
        updateAvailable = available > (Int(appVersion) ?? 0)
        // a file named PREPARING is written while an update is still being added, and says "ready" when done
        let prep = (try? String(contentsOf: updatesDir.appendingPathComponent("app-src/PREPARING"), encoding: .utf8)) ?? "ready"
        let parts = prep.split(separator: " ")
        updatePreparing = parts.first == "busy" && (Double(parts.last ?? "") ?? 0) > Date().timeIntervalSince1970 - 600
    }

    /// Shared app: look for a newer version online (at most every 6 hours).
    private var lastFeedCheck = Date.distantPast
    func checkFeed() {
        guard !updateFeed.contains("GITHUB_USER"), Date().timeIntervalSince(lastFeedCheck) > 6 * 3600,
              let url = URL(string: updateFeed) else { return }
        lastFeedCheck = Date()
        URLSession.shared.dataTask(with: url) { d, _, _ in
            guard let d = d, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let v = o["version"] as? Int, let z = o["url"] as? String else { return }
            DispatchQueue.main.async {
                self.feedZip = z
                self.updateAvailable = v > (Int(appVersion) ?? 0)
            }
        }.resume()
    }

    /// Shared app: download the new app, swap it in place of this one, reopen.
    func installSharedUpdate() {
        guard let z = feedZip, let url = URL(string: z) else { return }
        updating = true
        URLSession.shared.downloadTask(with: url) { tmp, _, _ in
            guard let tmp = tmp else { DispatchQueue.main.async { self.updating = false }; return }
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("djgrab-update")
            try? FileManager.default.removeItem(at: dir)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let zip = dir.appendingPathComponent("DJ Grab.zip")
            try? FileManager.default.moveItem(at: tmp, to: zip)
            let app = Bundle.main.bundlePath, pid = ProcessInfo.processInfo.processIdentifier
            let sh = """
            cd "\(dir.path)" && /usr/bin/ditto -x -k "DJ Grab.zip" . || exit 1
            [ -d "DJ Grab.app" ] || exit 1
            while kill -0 \(pid) 2>/dev/null; do sleep 0.5; done
            rm -rf "\(app)" && mv "DJ Grab.app" "\(app)" && xattr -dr com.apple.quarantine "\(app)" 2>/dev/null
            open "\(app)"
            """
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = ["-c", "nohup /bin/bash -c '\(sh.replacingOccurrences(of: "'", with: "'\\''"))' >/dev/null 2>&1 &"]
            try? p.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { NSApp.terminate(nil) }
        }.resume()
    }

    /// Zip this app and open the share menu (AirDrop, Messages, Mail…). The person who gets it
    /// gets the plain version: it only becomes your full version on a Mac with the ADMIN file.
    func shareApp() {
        let out = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Desktop/DJ Grab.zip")
        try? FileManager.default.removeItem(at: out)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        p.arguments = ["-c", "-k", "--keepParent", Bundle.main.bundlePath, out.path]
        p.terminationHandler = { _ in
            DispatchQueue.main.async {
                guard let w = NSApp.keyWindow, let v = w.contentView else { NSWorkspace.shared.activateFileViewerSelecting([out]); return }
                let picker = NSSharingServicePicker(items: [out])
                picker.show(relativeTo: NSRect(x: v.bounds.maxX - 40, y: v.bounds.maxY - 30, width: 1, height: 1), of: v, preferredEdge: .minY)
                self.banner = "Saved DJ Grab.zip to your Desktop. First open: right-click the app, choose Open."
            }
        }
        try? p.run()
    }

    // Rebuild in the background; build_app.sh quits this app and reopens the new one.
    func installUpdate() {
        if !ownerCopy { installSharedUpdate(); return }
        guard !busy && !ToolsStore.shared.running && !updatePreparing else { return }
        let script = updatesDir.appendingPathComponent("build_app.sh").path
        updating = true
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/DJGrab-update.log").path
        try? FileManager.default.removeItem(atPath: log)       // so an old failure isn't read as this one
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", "nohup /bin/bash \"\(script)\" > \"\(log)\" 2>&1 &"]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        p.environment = env
        try? p.run()
        // Watch the update's log: a failed build stops the "Installing update…" notice right away
        let started = Date()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { t in
            let txt = (try? String(contentsOfFile: log, encoding: .utf8)) ?? ""
            if txt.contains("ERROR") || Date().timeIntervalSince(started) > 300 {
                t.invalidate()
                Store.shared.updating = false
                Store.shared.banner = "The update didn't install. Try Settings ▸ Install update again."
            } else if txt.contains("Installed DJ Grab") {
                t.invalidate()                            // the new version is opening
            }
        }
    }

    /// Settings ▸ Restart & Update: refresh the YouTube downloader, then install the app update if there is
    /// one (the update reopens the app by itself), otherwise just quit and reopen.
    @Published var restarting = false
    func restartAndUpdate() {
        guard !restarting else { return }          // one press is enough
        restarting = true
        updateEngineIfNeeded(force: true)
        checkUpdateAvailable()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {     // give the downloader update a moment
            if self.updateAvailable && !self.updatePreparing {
                self.updating = false
                self.installUpdate()
            } else {
                self.restartApp()
            }
        }
        banner = "Updating and restarting…"
    }

    /// Quit and reopen DJ Grab (clears anything stuck)
    func restartApp() {
        let app = Bundle.main.bundlePath
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", "nohup /bin/bash -c 'sleep 1.5; open \"\(app)\"' >/dev/null 2>&1 &"]
        try? p.run()
        NSApp.terminate(nil)
    }

    // "12 left · about 4 min", learned from how long your songs actually take
    func updateETA() {
        let left = queue.count + running.count
        guard left > 0 else { etaText = ""; return }
        let recent = durations.suffix(12)
        let avg = recent.isEmpty ? 45.0 : recent.reduce(0, +) / Double(recent.count)
        let slots = Double(maxParallel)
        let runningLeft = running.map { max(5.0, avg - Date().timeIntervalSince($0.started)) }.reduce(0, +)
        let seconds = (Double(queue.count) * avg + runningLeft) / slots
        let when: String
        if seconds < 60 { when = "under a minute" }
        else { when = "about \(Int((seconds / 60).rounded(.up))) min" }
        etaText = "\(left) left · \(when)"
    }

    private func launch(_ q: (link: String, rowID: UUID, checkOnly: Bool)) {
        let job = Job(rowID: q.rowID)
        if let i = index(q.rowID) {
            rows[i].state = .running
            rows[i].stage = "Starting"
        }
        guard FileManager.default.fileExists(atPath: pythonPath) else {
            fail("DJ Grab isn't fully installed. Run install.sh again.", job: job, wasRunning: false)
            return
        }
        var args = [enginePath, q.link, "--out", outputFolder, "--json"]
        if let c = UserDefaults.standard.string(forKey: Keys.cookies), !c.isEmpty, c != "none" {
            args += ["--cookies-browser", c]
        }
        if UserDefaults.standard.object(forKey: Keys.loudness) as? Bool == false {
            args.append("--no-loudness")
        }
        if q.checkOnly { args.append("--check") }
        let target = UserDefaults.standard.object(forKey: "normalizeLUFS") as? Double ?? -10
        if target < 0 { args += ["--normalize", String(target)] }
        let minK = UserDefaults.standard.object(forKey: "minQuality") as? Int ?? 128
        if minK > 0 { args += ["--min-kbps", String(minK)] }
        if allowLow.contains(q.link) { args.append("--allow-low") }
        if !isAdmin {
            args.append("--plain")                       // shared app: their own folder, no Serato
            let mode = UserDefaults.standard.string(forKey: "sortMode") ?? "off"
            if mode != "off" { args += ["--sort", mode] }
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = toolPATH + ":" + (env["PATH"] ?? "")
        env["PYTHONUNBUFFERED"] = "1"
        env["DJGRAB_DJAPP"] = UserDefaults.standard.string(forKey: "djApp") ?? "serato"
        env["DJGRAB_HOOK"] = UserDefaults.standard.string(forKey: "hookTease") == "off" ? "off" : "fits"   // Auto or Off
        env["DJGRAB_OUTRO"] = "0"            // outro versions are off
        env["DJGRAB_SPEED"] = UserDefaults.standard.string(forKey: "workSpeed") ?? "balanced"
        env["DJGRAB_OUTPUT"] = UserDefaults.standard.string(forKey: Keys.output) ?? defaultOutput
        p.environment = env

        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            DispatchQueue.main.async { self?.consume(d, job: job) }
        }
        p.terminationHandler = { [weak self] proc in
            out.fileHandleForReading.readabilityHandler = nil
            let rest = out.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self = self else { return }
                if !rest.isEmpty { self.consume(rest, job: job) }
                if proc.terminationStatus != 0, let i = self.index(job.currentRow),
                   self.rows[i].state == .running {
                    let last = errText.split(separator: "\n").last.map(String.init) ?? "Unknown error"
                    self.rows[i].state = .failed
                    self.rows[i].stage = "Failed"
                    self.rows[i].detail = last
                }
                // a search row that never got its own track row: mark it finished
                if let i = self.index(job.rowID), self.rows[i].state == .running {
                    self.rows[i].state = .skipped
                    self.rows[i].stage = "Done"
                }
                let took = Date().timeIntervalSince(job.started)
                if took > 8 { self.durations.append(took) }     // ignore instant skips
                self.running.removeAll { $0 === job }
                self.startNext()
                if !self.busy { self.flushCrates() }
            }
        }
        job.proc = p
        running.append(job)
        do { try p.run() } catch {
            fail("Couldn't start the engine: \(error.localizedDescription)", job: job, wasRunning: true)
        }
    }

    private func fail(_ msg: String, job: Job, wasRunning: Bool) {
        if let i = index(job.currentRow) {
            rows[i].state = .failed
            rows[i].stage = "Failed"
            rows[i].detail = msg
        }
        if wasRunning { running.removeAll { $0 === job } }
        startNext()
    }

    private func consume(_ d: Data, job: Job) {
        job.buffer.append(d)
        while let nl = job.buffer.firstIndex(of: 0x0A) {
            let line = job.buffer.subdata(in: job.buffer.startIndex..<nl)
            job.buffer.removeSubrange(job.buffer.startIndex...nl)
            if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                // handle() works on the "current" row; point it at this job's row
                currentRow = job.currentRow
                placeholderFree = job.placeholderFree
                handle(obj)
                job.currentRow = currentRow
                job.placeholderFree = placeholderFree
            }
        }
    }

    private func handle(_ e: [String: Any]) {
        let event = e["event"] as? String ?? ""
        switch event {
        case "item_start":
            let name = e["name"] as? String ?? "Track"
            if placeholderFree, let i = index(currentRow) {
                rows[i].name = name
                placeholderFree = false
            } else {
                let parent = index(currentRow).map { rows[$0].link } ?? ""
                var r = Row(link: parent, name: name)
                r.state = .running
                let at = index(currentRow) ?? 0
                rows.insert(r, at: at)
                currentRow = r.id
            }
            if let i = index(currentRow) {
                rows[i].state = .running
                rows[i].stage = "Starting"
                rows[i].pct = 0
                if let n = e["index"] as? Int, let t = e["total"] as? Int, t > 1 {
                    rows[i].detail = "\(n) of \(t)"
                }
            }
        case "match":
            if let i = index(currentRow) {
                let m = e["matches"] as? [String] ?? []
                rows[i].noteIsNew = m.isEmpty
                rows[i].note = m.isEmpty ? "New, not in your folders" : "Grabbing this version too. Other versions you have: " + m.map { ($0 as NSString).lastPathComponent }.joined(separator: ", ")
            }
        case "lowquality":
            if let i = index(currentRow) {
                rows[i].state = .lowQuality
                rows[i].stage = "Poor quality"
                rows[i].detail = e["message"] as? String ?? "Poor quality"
                rows[i].lowLink = e["link"] as? String
                if let n = e["name"] as? String, !n.isEmpty { rows[i].name = n }
            }
        case "checked":
            if let i = index(currentRow) {
                let m = e["matches"] as? [String] ?? []
                rows[i].name = e["name"] as? String ?? rows[i].name
                rows[i].state = .checked
                rows[i].stage = m.isEmpty ? "New" : "Already have it"
                rows[i].noteIsNew = m.isEmpty
                rows[i].note = m.isEmpty ? "New, not in your folders" : m.joined(separator: "\n")
            }
        case "progress":
            if let i = index(currentRow) {
                rows[i].stage = e["stage"] as? String ?? rows[i].stage
                rows[i].pct = (e["pct"] as? Double) ?? Double(e["pct"] as? Int ?? 0)
                if let n = e["name"] as? String, !n.isEmpty, rows[i].state == .running { rows[i].name = n }  // typo fixed
            }
        case "done":
            if let i = index(currentRow) {
                rows[i].state = .done
                rows[i].pct = 100
                rows[i].stage = "Ready"
                rows[i].path = e["path"] as? String
                let artist = e["artist"] as? String ?? ""
                let title = e["title"] as? String ?? ""
                rows[i].name = "\(artist) - \(title)"
                var bits: [String] = []
                if let s = e["source"] as? String, !s.isEmpty { bits.append(s) }   // "High quality · from <channel> on YouTube"
                if let crate = e["crate"] as? String {
                    let status = e["crate_status"] as? String ?? ""
                    bits.append(status == "added" ? "in crate \(crate)" : "goes into crate \(crate) when Serato is closed")
                    if status == "waiting" { cratesWaiting = max(cratesWaiting, 1) }
                }
                rows[i].detail = bits.joined(separator: " · ")
                rows[i].warnings = e["warnings"] as? [String] ?? []
                if let p = rows[i].path {
                    ToolsStore.shared.addNewDownload(p)
                }
            }
        case "skipped":
            if let i = index(currentRow) {
                rows[i].state = .skipped
                rows[i].stage = "Already have it"
                rows[i].path = e["path"] as? String
                rows[i].detail = ((e["path"] as? String) as NSString?)?.lastPathComponent ?? ""
                rows[i].note = ""
                // already in your library: still goes through Intros & Cues (makes its intro if it's missing)
                if let p = rows[i].path {
                    ToolsStore.shared.addNewDownload(p)
                }
            }
        case "error":
            if let i = index(currentRow) {
                rows[i].state = .failed
                rows[i].stage = "Failed"
                rows[i].detail = e["message"] as? String ?? "Error"
            }
        case "info":
            if let m = e["message"] as? String, let i = index(currentRow), placeholderFree {
                rows[i].detail = m
            }
        default:
            break
        }
    }

    // Tracks are only added to Serato crates while Serato is closed. Check every 20 s.
    func startCrateWatcher() {
        crateTimer?.invalidate()
        crateTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            Store.shared.refreshDrive()
            Store.shared.updateETA()
            Store.shared.tick += 1
            if Store.shared.tick % 6 == 0 {
                let st = Store.shared
                st.checkUpdateAvailable()
                // updates install on their own as soon as nothing is running (no trip to Settings)
                if UserDefaults.standard.object(forKey: "autoUpdate") as? Bool ?? !ownerCopy, st.updateAvailable, !st.updatePreparing, !st.updating, !st.busy,
                   !ToolsStore.shared.running, Preview.shared.playing == nil {
                    st.banner = "Installing update v\(shownVersion(st.availableVersion))… DJ Grab will reopen by itself"
                    st.installUpdate()
                }
            }
            if Store.shared.tick % 60 == 5 { ShareData.flush() }          // every 5 min: send ratings that waited
            if Store.shared.tick == 3 { RatingStore.shared.exportText() }    // once on opening (each rating also writes it)
            if Store.shared.tick == 2 || Store.shared.tick % 24 == 0 { ToolsStore.shared.tryWaiting() }   // 10 s after opening, then every 2 min
            if Store.shared.tick % 4 == 0 { Store.shared.flushCrates() }
            KeepAwake.update(busy: ToolsStore.shared.running || Store.shared.busy)
            if Store.shared.tick % 24 == 12 { Store.shared.fixNames() }
        }
        refreshDrive()
        flushCrates()
    }

    private var fixingNames = false

    /// Serato shows "Artist - Title" as the Name of tracks it picked up from a crate file until
    /// their Name/Artist are filled in. This fills them from the song tags while Serato is closed.
    func fixNames() {
        guard isAdmin, !busy, !flushing, !fixingNames, !ejecting, !ToolsStore.shared.running,
              FileManager.default.fileExists(atPath: pythonPath) else { return }
        fixingNames = true
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [enginePath, "--fix-names", "--json"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in
            let d = out.fileHandleForReading.readDataToEndOfFile()
            DispatchQueue.main.async {
                Store.shared.fixingNames = false
                for line in d.split(separator: 0x0A) {
                    if let obj = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                       obj["event"] as? String == "renamed", let n = obj["count"] as? Int, n > 0 {
                        Store.shared.banner = "Cleaned up \(n) song names"
                    }
                    if let obj = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                       obj["event"] as? String == "names", let n = obj["fixed"] as? Int, n > 0 {
                        Store.shared.banner = "Fixed \(n) track name\(n == 1 ? "" : "s") in Serato"
                    }
                }
            }
        }
        if (try? p.run()) == nil { fixingNames = false }
    }

    func flushCrates() {
        if ejecting || !isAdmin { return }
        let pending = supportDir.appendingPathComponent("pending_crates.json").path
        guard FileManager.default.fileExists(atPath: pending) else {
            cratesWaiting = 0
            return
        }
        guard !busy, !flushing, FileManager.default.fileExists(atPath: pythonPath) else { return }
        flushing = true
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [enginePath, "--flush-crates", "--json"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        p.terminationHandler = { _ in
            let d = out.fileHandleForReading.readDataToEndOfFile()
            DispatchQueue.main.async {
                let s = Store.shared
                s.flushing = false
                for line in d.split(separator: 0x0A) {
                    if let obj = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                       obj["event"] as? String == "crates" {
                        let added = obj["added"] as? Int ?? 0
                        s.cratesWaiting = obj["waiting"] as? Int ?? 0
                        if added > 0 {
                            s.banner = "Added \(added) track\(added == 1 ? "" : "s") to Serato crates"
                            for i in s.rows.indices where s.rows[i].detail.contains("when Serato is closed") {
                                s.rows[i].detail = s.rows[i].detail
                                    .replacingOccurrences(of: "goes into crate", with: "in crate")
                                    .replacingOccurrences(of: " when Serato is closed", with: "")
                            }
                        }
                    }
                }
            }
        }
        do { try p.run() } catch { flushing = false }
    }

    func clearFinished() {
        rows.removeAll { $0.state == .done || $0.state == .skipped || $0.state == .failed || $0.state == .checked }
    }

    // Keep yt-dlp current: YouTube and SoundCloud change often.
    func updateEngineIfNeeded(force: Bool = false) {
        let last = UserDefaults.standard.double(forKey: Keys.lastUpdate)
        guard force || Date().timeIntervalSince1970 - last > 3 * 86400 else { return }
        guard FileManager.default.fileExists(atPath: pythonPath) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [enginePath, "--update", "--json"]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        p.terminationHandler = { proc in
            if proc.terminationStatus == 0 {
                UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Keys.lastUpdate)
                if force { DispatchQueue.main.async { Store.shared.banner = "Downloader updated" } }
            }
        }
        try? p.run()
    }
}

// MARK: - Opening links from outside (dock icon, .webloc files, djgrab:// links)

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // just updated? say so, so you know the new version is the one running
        let last = UserDefaults.standard.integer(forKey: "lastRunVersion")
        if last != 0 && last < (Int(appVersion) ?? 0) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { Store.shared.banner = "✓ Updated to v\(shownVersion(appVersion))" }
        }
        UserDefaults.standard.set(Int(appVersion) ?? 0, forKey: "lastRunVersion")
        Store.shared.checkUpdateAvailable()
        Store.shared.updateEngineIfNeeded()
        Store.shared.startCrateWatcher()
    }

    // If a newer version of the app's source has been dropped into the updates folder,
    // rebuild in the background. build_app.sh quits this app and reopens the new one.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func application(_ application: NSApplication, open urls: [URL]) {
        for u in urls { route(u) }
    }

    func route(_ u: URL) {
        if u.scheme == "djgrab" {
            // djgrab://grab?url=<encoded link>  (used by the browser bookmark button)
            let comps = URLComponents(url: u, resolvingAgainstBaseURL: false)
            if let link = comps?.queryItems?.first(where: { $0.name == "url" })?.value {
                Store.shared.add(text: link)
            }
        } else if u.isFileURL {
            if let link = readWebloc(u) { Store.shared.add(text: link) }
        } else {
            Store.shared.add(text: u.absoluteString)
        }
        NSApp.activate(ignoringOtherApps: true)
    }
}

func readWebloc(_ u: URL) -> String? {
    guard let d = try? Data(contentsOf: u) else { return nil }
    if let plist = try? PropertyListSerialization.propertyList(from: d, format: nil) as? [String: Any],
       let s = plist["URL"] as? String {
        return s
    }
    return String(data: d, encoding: .utf8) // .url / .txt files
}

// MARK: - Views


// MARK: - Looks (Settings > Look)

enum Look: String, CaseIterable, Identifiable {
    case clean, dark, light, neon          // "clean" = System (the stored name from before)
    var id: String { rawValue }
    var name: String { ["clean": "System", "dark": "Dark", "light": "Light", "neon": "Neon"][rawValue]! }
    var scheme: ColorScheme? { self == .dark || self == .neon ? .dark : self == .light ? .light : nil }
    var blurb: String {
        switch self {
        case .clean: return "Follows your Mac: light or dark, whichever it's set to."
        case .dark: return "Always dark."
        case .light: return "Always light."
        case .neon: return "Dark with glowing pink and cyan, like club lights."
        }
    }
}

struct Theme {
    let look: Look
    static var current: Theme { Theme(look: Look(rawValue: UserDefaults.standard.string(forKey: "uiLook") ?? "neon") ?? .clean) }
    var pixel: Bool { false }
    var bg: Color {
        switch look {
        case .clean, .dark, .light: return Color(nsColor: .windowBackgroundColor)
        case .neon: return Color(red: 0.05, green: 0.04, blue: 0.10)
        }
    }
    var panel: Color {
        switch look {
        case .clean, .dark, .light: return Color.primary.opacity(0.045)
        case .neon: return Color.white.opacity(0.05)
        }
    }
    var accent: Color {
        switch look {
        case .clean, .dark, .light: return Color.accentColor
        case .neon: return Color(red: 1.0, green: 0.22, blue: 0.62)
        }
    }
    var accent2: Color {
        switch look {
        case .clean, .dark, .light: return Color.secondary
        case .neon: return Color(red: 0.20, green: 0.90, blue: 1.0)
        }
    }
    var radius: CGFloat { pixel ? 0 : look == .neon ? 12 : 10 }
    func font(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        switch look {
        case .neon: return .system(size: size, weight: weight, design: .rounded)
        case .clean, .dark, .light: return .system(size: size, weight: weight)
        }
    }
}

/// Applies the chosen Look to a whole screen: background, accent colour, rounded type and button style.
struct Looked: ViewModifier {
    @AppStorage("uiLook") private var look = "neon"
    func body(content: Content) -> some View {
        let t = Theme.current
        content
            .background(t.bg)
            .tint(t.accent)
            .fontDesign(t.look == .neon ? .rounded : .default)
            .buttonStyle(LookButton())
            .toggleStyle(.switch)
            .preferredColorScheme(t.look.scheme)
    }
}
extension View { func looked() -> some View { modifier(Looked()) } }

/// Buttons that follow the Look: soft pills (Clean), glowing outlines (Neon), raised 8-bit blocks (Pixel).
struct LookButton: ButtonStyle {
    var prominent = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration c: Configuration) -> some View {
        let t = Theme.current
        let fill: Color = prominent ? t.accent : t.panel
        let fg: Color = prominent ? (t.pixel ? .black : .white) : .primary
        return c.label
            .font(t.font(13, prominent ? .semibold : .medium))
            .padding(.horizontal, 14).padding(.vertical, 7)
            .foregroundStyle(fg)
            .background(
                ZStack {
                    if t.pixel {
                        Rectangle().fill(Color.black).offset(x: c.isPressed ? 0 : 3, y: c.isPressed ? 0 : 3)
                        Rectangle().fill(fill)
                        Rectangle().strokeBorder(Color.black, lineWidth: 2)
                    } else {
                        RoundedRectangle(cornerRadius: t.radius).fill(fill)
                        if t.look == .neon {
                            RoundedRectangle(cornerRadius: t.radius)
                                .strokeBorder(prominent ? t.accent : t.accent2.opacity(0.5), lineWidth: 1)
                        }
                    }
                }
            )
            .shadow(color: t.look == .neon && prominent ? t.accent.opacity(0.7) : .clear, radius: 8)
            .offset(x: t.pixel && c.isPressed ? 3 : 0, y: t.pixel && c.isPressed ? 3 : 0)
            .opacity(enabled ? (c.isPressed && !t.pixel ? 0.75 : 1) : 0.4)
    }
}

/// An 8-bit "download" arrow drawn from square pixels.
struct PixelArrow: View {
    var color: Color
    private let rows = ["...##...", "...##...", "...##...", "...##...", "#..##..#", ".#.##.#.", "..####..", "...##...", "........", "########"]
    var body: some View {
        Canvas { ctx, size in
            let px = size.width / 8
            for (y, row) in rows.enumerated() {
                for (x, ch) in row.enumerated() where ch == "#" {
                    ctx.fill(Path(CGRect(x: CGFloat(x) * px, y: CGFloat(y) * px, width: px, height: px)), with: .color(color))
                }
            }
        }
        .frame(width: 40, height: 50)
    }
}

/// Progress as a row of blocks (Pixel Art), a glowing bar (Neon), or the normal bar (Clean).
struct LookProgress: View {
    var pct: Double?          // nil = still waiting
    @State private var phase = 0
    var body: some View {
        let t = Theme.current
        if t.pixel {
            GeometryReader { g in
                let n = max(8, Int(g.size.width / 10))
                let on = pct.map { Int(Double(n) * min($0, 100) / 100) } ?? -1
                HStack(spacing: 2) {
                    ForEach(0..<n, id: \.self) { i in
                        Rectangle()
                            .fill(on >= 0 ? (i < on ? t.accent : t.panel)
                                          : ((i + phase) % 6 < 2 ? t.accent2 : t.panel))
                    }
                }
            }
            .frame(height: 8)
            .onReceive(Timer.publish(every: 0.12, on: .main, in: .common).autoconnect()) { _ in
                if pct == nil { phase = (phase + 1) % 6 }
            }
        } else if let p = pct {
            ProgressView(value: min(p, 100), total: 100).tint(t.accent)
                .shadow(color: t.look == .neon ? t.accent.opacity(0.8) : .clear, radius: 4)
        } else {
            ProgressView().progressViewStyle(.linear).tint(t.accent)
        }
    }
}


// MARK: - Sounds, Dock progress, preview player

/// "All done": a check that pops in with a burst of sparks when a batch finishes.
struct DoneBurst: View {
    @State private var go = false
    var body: some View {
        let t = Theme.current
        ZStack {
            ForEach(0..<14, id: \.self) { i in
                let a = Double(i) / 14 * 2 * .pi
                Circle()
                    .fill(i % 2 == 0 ? t.accent : t.accent2)
                    .frame(width: 7, height: 7)
                    .offset(x: go ? cos(a) * 70 : 0, y: go ? sin(a) * 70 : 0)
                    .opacity(go ? 0 : 1)
            }
            VStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 54, weight: .bold))
                    .foregroundStyle(t.accent)
                    .shadow(color: t.look == .neon ? t.accent.opacity(0.9) : .clear, radius: 14)
                    .scaleEffect(go ? 1 : 0.3)
                Text("All done").font(t.font(15, .semibold))
            }
            .padding(26)
            .background(RoundedRectangle(cornerRadius: 18).fill(.ultraThinMaterial))
        }
        .onAppear { withAnimation(.spring(response: 0.5, dampingFraction: 0.55)) { go = true } }
        .allowsHitTesting(false)
    }
}

extension ToolbarContent {
    /// macOS 26 wraps toolbar buttons in a glass pill; this turns that off so the gear's own circle shows
    @ToolbarContentBuilder func hiddenGlass() -> some ToolbarContent {
        if #available(macOS 26.0, *) { self.sharedBackgroundVisibility(.hidden) } else { self }
    }
}

/// Watches the Mac's internet connection. Downloads wait while it's off and pick up when it's back.
final class Connection: ObservableObject {
    static let shared = Connection()
    @Published var online = true
    private let monitor = NWPathMonitor()
    init() {
        monitor.pathUpdateHandler = { path in
            DispatchQueue.main.async {
                let was = self.online
                self.online = path.status == .satisfied
                if !was && self.online { Store.shared.resumeAfterOffline() }
            }
        }
        monitor.start(queue: DispatchQueue(label: "djgrab.net"))
    }
}

/// Orange bar when the drive songs save to has under 5% space left (checked every 30 seconds).
struct StorageBar: View {
    @EnvironmentObject var store: Store
    @State private var info: (free: Int64, total: Int64)? = nil
    func check() {
        var url = URL(fileURLWithPath: store.outputFolder)
        while !FileManager.default.fileExists(atPath: url.path) && url.pathComponents.count > 1 {
            url.deleteLastPathComponent()
        }
        guard let v = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey,
                                                        .volumeAvailableCapacityKey, .volumeTotalCapacityKey]),
              let total = v.volumeTotalCapacity, total > 0 else { info = nil; return }
        // USB drives report 0 for "important usage" space, so take whichever number is real
        let free = max(v.volumeAvailableCapacityForImportantUsage ?? 0, Int64(v.volumeAvailableCapacity ?? 0))
        info = free > 0 ? (free, Int64(total)) : nil
    }
    var body: some View {
        Group {
            if let i = info, Double(i.free) / Double(i.total) < 0.05 {
                HStack(spacing: 8) {
                    Image(systemName: "externaldrive.badge.exclamationmark")
                    Text("Your drive is almost full (\(ByteCountFormatter.string(fromByteCount: i.free, countStyle: .file)) left). Free up some space so downloads don't fail.")
                        .font(.callout)
                    Spacer()
                }
                .foregroundStyle(.white)
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.85)))
            }
        }
        .onAppear(perform: check)
        .onReceive(Timer.publish(every: 30, on: .main, in: .common).autoconnect()) { _ in check() }
        .onChange(of: store.busy) { _ in check() }
    }
}

/// Red bar at the top when there's no internet.
struct OfflineBar: View {
    @ObservedObject private var net = Connection.shared
    var body: some View {
        if !net.online {
            HStack(spacing: 8) {
                Image(systemName: "wifi.slash")
                Text("No internet connection. Connect to Wi-Fi; downloads continue once you're back online.")
                    .font(.callout)
                Spacer()
            }
            .foregroundStyle(.white)
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.red.opacity(0.8)))
            .transition(.move(edge: .top).combined(with: .opacity))
        }
    }
}

/// A ring on the Dock icon while songs are downloading: how much of this batch is done.
enum DockProgress {
    private static let view = DockRing()
    static func update() {
        let rows = Store.shared.rows
        let active = rows.filter { [.waiting, .running].contains($0.state) }.count
        let tile = NSApp.dockTile
        if active == 0 {
            tile.contentView = nil
            tile.badgeLabel = nil
        } else {
            let total = max(1, rows.count)
            view.fraction = Double(total - active) / Double(total)
            tile.contentView = view
            tile.badgeLabel = "\(active)"
        }
        tile.display()
    }
}

final class DockRing: NSView {
    var fraction = 0.0
    override func draw(_ dirty: NSRect) {
        NSApp.applicationIconImage?.draw(in: bounds)
        let r = bounds.insetBy(dx: bounds.width * 0.1, dy: bounds.height * 0.1)
        let c = NSPoint(x: r.midX, y: r.midY), rad = r.width / 2
        let track = NSBezierPath()
        track.appendArc(withCenter: c, radius: rad, startAngle: 0, endAngle: 360)
        track.lineWidth = bounds.width * 0.07
        NSColor.black.withAlphaComponent(0.35).setStroke(); track.stroke()
        let arc = NSBezierPath()
        arc.appendArc(withCenter: c, radius: rad, startAngle: 90, endAngle: 90 - 360 * fraction, clockwise: true)
        arc.lineWidth = bounds.width * 0.07
        arc.lineCapStyle = .round
        let t = Theme.current
        (t.look == .neon ? NSColor(red: 1.0, green: 0.22, blue: 0.62, alpha: 1) : NSColor.controlAccentColor).setStroke()
        arc.stroke()
    }
}

/// Plays a song right in DJ Grab.
final class Preview: ObservableObject {
    static let shared = Preview()
    @Published var playing: String? = nil
    private var player: AVAudioPlayer?
    func toggle(_ path: String, startAt: Double = 0) {
        if playing == path { stop(); return }
        stop()
        guard let p = try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: path)) else { return }
        if let dev = UserDefaults.standard.string(forKey: "previewDevice"), !dev.isEmpty,
           AudioOutputs.list().contains(where: { $0.uid == dev }) {
            p.currentDevice = dev                     // the speaker / headphones picked in Settings
        }
        p.volume = Float(UserDefaults.standard.object(forKey: "previewVolume") as? Double ?? 0.8)
        p.currentTime = max(0, min(startAt, p.duration - 1))
        p.play()
        player = p
        playing = path
        let mine = path
        position = p.currentTime
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { tm in
            if self.playing == mine { self.position = self.player?.currentTime ?? 0 }
            if self.playing != mine || !(self.player?.isPlaying ?? false) {
                tm.invalidate()
                if self.playing == mine { self.playing = nil }
            }
        }
    }
    func stop() { player?.stop(); player = nil; playing = nil }
    func setVolume(_ v: Double) { player?.volume = Float(v) }
    @Published var position = 0.0
    var duration: Double { player?.duration ?? 0 }
    func seek(_ t: Double) { player?.currentTime = max(0, min(t, duration - 0.2)); position = t }
}

/// The Mac's sound outputs (speakers, headphones, a DJ controller, AirPlay...), for Settings.
enum AudioOutputs {
    struct Device: Identifiable, Hashable { let uid: String; let name: String; var id: String { uid } }
    static func list() -> [Device] {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
        var out: [Device] = []
        for id in ids {
            // only devices that can play sound
            var sAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                   mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
            var sSize: UInt32 = 0
            AudioObjectGetPropertyDataSize(id, &sAddr, 0, nil, &sSize)
            if sSize == 0 { continue }
            func str(_ sel: AudioObjectPropertySelector) -> String? {
                var a = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                                   mElement: kAudioObjectPropertyElementMain)
                var v: Unmanaged<CFString>?
                var sz = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
                guard AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &v) == noErr, let r = v else { return nil }
                return r.takeRetainedValue() as String
            }
            if let uid = str(kAudioDevicePropertyDeviceUID), let name = str(kAudioObjectPropertyName) {
                out.append(Device(uid: uid, name: name))
            }
        }
        return out
    }
}

/// A row of bars that bounce while a song downloads.
struct LiveBars: View {
    var color: Color
    @State private var t = 0.0
    var body: some View {
        HStack(alignment: .center, spacing: 3) {
            ForEach(0..<18, id: \.self) { i in
                Capsule().fill(color.opacity(0.85))
                    .frame(width: 3, height: 6 + 18 * abs(sin(t * 2.3 + Double(i) * 0.55) * cos(t * 0.9 + Double(i) * 0.31)))
            }
        }
        .frame(height: 26)
        .onReceive(Timer.publish(every: 0.06, on: .main, in: .common).autoconnect()) { _ in t += 0.06 }
    }
}

/// "Now Grabbing": the song in progress, with moving bars, above the list.
struct NowGrabbing: View {
    @EnvironmentObject var store: Store
    @AppStorage("uiLook") private var look = "neon"
    var body: some View {
        let t = Theme.current
        let running = store.rows.filter { $0.state == .running }
        if let r = running.first {
            HStack(spacing: 14) {
                LiveBars(color: t.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("NOW GRABBING" + (running.count > 1 ? "  +\(running.count - 1) more" : ""))
                        .font(t.font(10, .bold)).foregroundStyle(t.accent2).tracking(1.2)
                    Text(r.name).font(t.font(15, .semibold)).lineLimit(1).truncationMode(.middle)
                    Text(r.stage).font(t.font(11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if !store.etaText.isEmpty {
                    Text(store.etaText).font(t.font(12, .medium)).foregroundStyle(.secondary)
                }
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: t.radius + 2).fill(t.panel)
                    .overlay(RoundedRectangle(cornerRadius: t.radius + 2)
                        .strokeBorder(t.look == .neon ? t.accent.opacity(0.5) : .clear, lineWidth: 1))
                    .shadow(color: t.look == .neon ? t.accent.opacity(0.35) : .clear, radius: 10)
            )
            .transition(.move(edge: .top).combined(with: .opacity))
        }
    }
}

/// Menu bar mini window: paste links or song names without opening the full app.
struct MiniGrab: View {
    @EnvironmentObject var store: Store
    @State private var typed = ""
    var body: some View {
        let t = Theme.current
        VStack(alignment: .leading, spacing: 10) {
            Text("DJ Grab").font(t.font(14, .bold))
            TextField("Link, or Title - Artist", text: $typed, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...6)
                .onSubmit(grab)
            HStack {
                Button("Grab", action: grab).buttonStyle(LookButton(prominent: true))
                Button("Paste & Grab") {
                    if let s = NSPasteboard.general.string(forType: .string) { store.add(text: s) }
                }
                .buttonStyle(LookButton())
                Spacer()
            }
            let active = store.rows.filter { [.waiting, .running].contains($0.state) }.count
            if active > 0 {
                HStack {
                    LookProgress(pct: nil)
                    Text("\(active) left").font(t.font(11)).foregroundStyle(.secondary)
                }
            } else if let last = store.rows.last(where: { $0.state == .done }) {
                Text("Last: \(last.name)").font(t.font(11)).foregroundStyle(.secondary).lineLimit(1)
            }
            Divider()
            Button("Open DJ Grab") {
                NSApp.activate(ignoringOtherApps: true)
                NSApp.windows.first { $0.title == "DJ Grab" }?.makeKeyAndOrderFront(nil)
            }
            .buttonStyle(.borderless)
        }
        .padding(14)
        .frame(width: 300)
        .tint(t.accent)
    }
    func grab() {
        let s = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return }
        store.add(text: s); typed = ""
    }
}

// MARK: - Download view

struct DropZone: View {
    @EnvironmentObject var store: Store
    @State private var targeted = false
    @State private var typed = ""

    @AppStorage("uiLook") private var look = "neon"
    var body: some View {
        let t = Theme.current
        VStack(spacing: 12) {
            if t.pixel {
                PixelArrow(color: targeted ? t.accent : t.accent2)
            } else {
                // the app's logo (the claw grabbing a record); bounces a little when something is dragged over
                Image(nsImage: NSApp.applicationIconImage)
                    .resizable()
                    .frame(width: 64, height: 64)
                    .scaleEffect(targeted ? 1.12 : 1)
                    .animation(.spring(response: 0.3, dampingFraction: 0.6), value: targeted)
                    .shadow(color: t.look == .neon ? t.accent.opacity(0.55) : .clear, radius: 12)
            }
            Text(t.pixel ? "DROP A LINK OR SONG NAMES" : "Drop a link or paste song names")
                .font(t.font(16, .semibold))
            HStack(spacing: 8) {
                TextField("Link, or Title - Artist", text: $typed)
                    .textFieldStyle(.plain)
                    .font(t.font(14))
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(
                        ZStack {
                            if t.pixel { Rectangle().fill(Color.black.opacity(0.5)); Rectangle().strokeBorder(t.accent2, lineWidth: 2) }
                            else { RoundedRectangle(cornerRadius: t.radius).fill(Color.black.opacity(t.look == .neon ? 0.4 : 0.15)) }
                        }
                    )
                    .onSubmit(grabTyped)
                Button("Grab", action: grabTyped)
                    .buttonStyle(LookButton(prominent: true))
                    .keyboardShortcut(.defaultAction)
                Button("Check") {
                    let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !t.isEmpty {
                        store.add(text: t, checkOnly: true); typed = ""
                    } else if let s = NSPasteboard.general.string(forType: .string) {
                        store.add(text: s, checkOnly: true)
                    }
                }
                .buttonStyle(LookButton())
                .help("Check if it's already in your library")
                Button("Paste & Grab") {
                    if let s = NSPasteboard.general.string(forType: .string) { store.add(text: s) }
                }
                .buttonStyle(LookButton())
                .keyboardShortcut("v", modifiers: [.command, .shift])
                .help("Paste & Grab (⇧⌘V)")
            }
            .frame(maxWidth: 600)
        }
        .padding(.vertical, 22).padding(.horizontal, 18)
        .frame(maxWidth: .infinity)
        .background(
            ZStack {
                if t.pixel {
                    Rectangle().fill(t.panel)
                    Rectangle().strokeBorder(style: StrokeStyle(lineWidth: 3, dash: [6, 6]))
                        .foregroundStyle(targeted ? t.accent : t.accent2)
                } else {
                    RoundedRectangle(cornerRadius: 16).fill(t.panel)
                    RoundedRectangle(cornerRadius: 16)
                        .strokeBorder(style: StrokeStyle(lineWidth: targeted ? 2 : 1, dash: targeted ? [] : [6]))
                        .foregroundStyle(targeted ? t.accent : (t.look == .neon ? t.accent2.opacity(0.5) : Color.secondary.opacity(0.3)))
                }
            }
            .shadow(color: t.look == .neon && targeted ? t.accent.opacity(0.6) : .clear, radius: 14)
        )
        .onDrop(of: [UTType.url, UTType.fileURL, UTType.plainText], isTargeted: $targeted) { providers in
            for p in providers { load(p) }
            return true
        }
    }

    func grabTyped() {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        store.add(text: t)
        typed = ""
    }

    func load(_ p: NSItemProvider) {
        if p.canLoadObject(ofClass: URL.self) {
            _ = p.loadObject(ofClass: URL.self) { url, _ in
                guard let url = url else { return }
                DispatchQueue.main.async {
                    if url.isFileURL {
                        if let s = readWebloc(url) { store.add(text: s) }
                    } else {
                        store.add(text: url.absoluteString)
                    }
                }
            }
        } else if p.canLoadObject(ofClass: String.self) {
            _ = p.loadObject(ofClass: String.self) { s, _ in
                guard let s = s else { return }
                DispatchQueue.main.async { store.add(text: s) }
            }
        }
    }
}

/// Drag to jump around the song that's playing. Only shown while that row is playing.
struct SeekBar: View {
    let path: String
    @ObservedObject private var preview = Preview.shared
    @State private var dragging: Double? = nil
    func clock(_ t: Double) -> String { String(format: "%d:%02d", Int(t) / 60, Int(t) % 60) }
    var body: some View {
        if preview.playing == path, preview.duration > 0 {
            HStack(spacing: 8) {
                Text(clock(dragging ?? preview.position)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                Slider(value: Binding(get: { dragging ?? preview.position }, set: { dragging = $0 }),
                       in: 0...preview.duration) { editing in
                    if !editing, let d = dragging { preview.seek(d); dragging = nil }
                }
                .controlSize(.small)
                Text(clock(preview.duration)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            .transition(.opacity)
        }
    }
}

struct PlayButton: View {
    let path: String
    var startAt: Double = 0
    @ObservedObject private var preview = Preview.shared
    var body: some View {
        Button { preview.toggle(path, startAt: startAt) } label: {
            Image(systemName: preview.playing == path ? "stop.circle.fill" : "play.circle.fill")
                .font(.system(size: 20))
                .foregroundStyle(Theme.current.accent)
        }
        .buttonStyle(.borderless)
        .help(preview.playing == path ? "Stop" : "Play")
    }
}

struct RowView: View {
    let row: Row
    @AppStorage("uiLook") private var look = "neon"

    var icon: (String, Color) {
        switch row.state {
        case .waiting: return ("clock", Color.secondary)
        case .running: return ("arrow.down.circle", Color.accentColor)
        case .done: return (row.warnings.isEmpty ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                            row.warnings.isEmpty ? Color.green : Color.orange)
        case .skipped: return ("checkmark.circle", Color.secondary)
        case .failed: return ("xmark.octagon.fill", Color.red)
        case .checked: return (row.noteIsNew ? "sparkles" : "checkmark.seal.fill", row.noteIsNew ? Color.blue : Color.secondary)
        case .lowQuality: return ("exclamationmark.triangle.fill", Color.orange)
        }
    }

    var body: some View {
        let t = Theme.current
        HStack(alignment: .top, spacing: 10) {
            Group {
                if t.pixel {
                    Rectangle().fill(icon.1 == Color.secondary ? Color.gray : icon.1)
                        .frame(width: 12, height: 12)
                        .overlay(Rectangle().strokeBorder(Color.black, lineWidth: 2))
                        .padding(.top, 3)
                } else {
                    Image(systemName: icon.0)
                        .foregroundStyle(icon.1 == Color.accentColor ? t.accent : icon.1)
                        .font(.title3)
                }
            }
            .frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text(row.name).font(t.font(13, .medium)).lineLimit(1).truncationMode(.middle)
                if row.state == .running {
                    if row.pct < 6 || row.stage.hasPrefix("Searching") || row.stage.hasPrefix("Getting track") {
                        LookProgress(pct: nil)      // animated while waiting on YouTube
                    } else {
                        LookProgress(pct: row.pct)
                    }
                    Text(row.stage + (row.detail.isEmpty ? "" : "  ·  \(row.detail)"))
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    let text = row.state == .done ? row.detail : "\(row.stage)\(row.detail.isEmpty ? "" : ": \(row.detail)")"
                    Text(text)
                        .font(.caption)
                        .foregroundStyle(row.state == .failed ? Color.red : Color.secondary)
                        .lineLimit(3)
                }
                if !row.note.isEmpty {
                    Text(row.note)
                        .font(.caption)
                        .foregroundStyle(row.noteIsNew ? Color.blue : Color.secondary)
                        .lineLimit(6)
                }
                ForEach(row.warnings, id: \.self) { w in
                    Text("⚠︎ " + w).font(.caption).foregroundStyle(.orange)
                }
                if let p = row.path { SeekBar(path: p) }
                if row.state == .done, row.path != nil {        // bottom left of the song, out of the way
                    Button { Store.shared.undo(row.id) } label: {
                        Image(systemName: "trash").font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.borderless)
                    .help("Remove this download (moved to _To Delete, not deleted)")
                    .padding(.top, 2)
                }
            }
            Spacer()
            if row.state == .waiting || row.state == .running {
                Button { Store.shared.cancel(row.id) } label: {
                    Image(systemName: "xmark.circle.fill").font(.system(size: 18)).foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
                .help("Cancel this song")
            }
            if row.state == .lowQuality {
                Button("Download Anyway") { Store.shared.downloadAnyway(row.id) }
                    .buttonStyle(LookButton(prominent: true))
                Button("Skip") { Store.shared.skipLow(row.id) }
                    .buttonStyle(LookButton())
            }
            if let p = row.path, FileManager.default.fileExists(atPath: p) {
                PlayButton(path: p)
            }
            if row.path != nil {
                Image(systemName: "line.3.horizontal")
                    .foregroundStyle(.tertiary)
                    .help("Drag into Serato")
            }
        }
        .padding(.vertical, 8).padding(.horizontal, 10)
        .background(
            Group {
                if t.pixel { Rectangle().fill(t.panel).overlay(Rectangle().strokeBorder(Color.black.opacity(0.6), lineWidth: 2)) }
                else { RoundedRectangle(cornerRadius: t.radius).fill(t.panel) }
            }
        )
        .contentShape(Rectangle())
    }
}

struct ContentView: View {
    @EnvironmentObject var store: Store
    @ObservedObject private var jokes = EmptyJoke.shared
    @AppStorage("uiLook") private var look = "neon"

    var body: some View {
        let t = Theme.current
        VStack(spacing: 12) {
            OfflineBar()
            StorageBar()
            DropZone()
            NowGrabbing().animation(.easeInOut(duration: 0.25), value: store.busy)
            if let b = store.banner {
                Text(b).font(.caption).foregroundStyle(.secondary)
                    .onAppear {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { store.banner = nil }
                    }
            }
            List {
                ForEach(store.rows) { row in
                    RowView(row: row)
                        .onDrag {
                            if let p = row.path {
                                return NSItemProvider(object: URL(fileURLWithPath: p) as NSURL)
                            }
                            return NSItemProvider()
                        }
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                        .listRowInsets(EdgeInsets(top: 3, leading: 0, bottom: 3, trailing: 0))
                        .onTapGesture(count: 2) { reveal(row.path) }
                        .contextMenu {
                            if let p = row.path {
                                Button("Show in Finder") { reveal(p) }
                                Button("Copy Path") {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(p, forType: .string)
                                }
                            }
                            Button("Open Source Link") {
                                if let u = URL(string: row.link) { NSWorkspace.shared.open(u) }
                            }
                            if row.state == .failed {
                                Button("Retry") { store.enqueue(row.link) }
                            }
                        }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .overlay {
                if store.rows.isEmpty {
                    VStack(spacing: 8) {
                        if t.pixel {
                            Text("PRESS GRAB TO START").font(t.font(15)).foregroundStyle(t.accent2)
                        } else {
                            Image(systemName: "music.note.list").font(.system(size: 28)).foregroundStyle(.tertiary)
                            Text(jokes.grab).font(t.font(14, .medium)).foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                        Text("Songs you grab show up here.").font(t.font(12)).foregroundStyle(.tertiary)
                    }
                }
            }
            HStack(spacing: 10) {
                Text("v\(shownVersion(appVersion))").foregroundStyle(.tertiary).font(t.font(11))
                Image(systemName: "folder")
                Text((store.outputFolder as NSString).lastPathComponent)
                    .lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(.secondary)
                Button("Open") {
                    if FileManager.default.fileExists(atPath: store.outputFolder) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: store.outputFolder))
                    } else {
                        store.banner = "USB not connected"
                    }
                }
                .buttonStyle(LookButton())
                Spacer()
                if store.driveName != nil {
                    if store.driveConnected && !usbFree(store.driveName).isEmpty {
                        Text(usbFree(store.driveName)).foregroundStyle(Color.blue).lineLimit(1)
                    }
                    Label(store.driveConnected ? (store.driveName ?? "USB") : "\(store.driveName ?? "USB") not connected",
                          systemImage: store.driveConnected ? "externaldrive.fill.badge.checkmark" : "externaldrive.badge.xmark")
                        .foregroundStyle(store.driveConnected ? Color.green : Color.red)
                        .lineLimit(1)
                    if store.driveConnected {
                        Button { store.eject() } label: {
                            if store.ejecting { ProgressView().controlSize(.small) } else { Image(systemName: "eject.fill") }
                        }
                        .buttonStyle(.borderless)
                        .help("Eject \(store.driveName ?? "USB")")
                        .disabled(store.ejecting)
                    }
                }
                if store.cratesWaiting > 0 {
                    Label("Crates pending", systemImage: "clock")
                        .help("New tracks are added to Serato crates once Serato is closed")
                        .foregroundStyle(.orange)
                        .lineLimit(1)
                }
                IntroActivity()
                Button("Clear") { store.clearFinished(); EmptyJoke.shared.shuffle() }
                    .buttonStyle(LookButton())
                    .disabled(store.rows.isEmpty)
            }
            .font(t.font(13))
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(
                Group {
                    if t.pixel { Rectangle().fill(t.panel) }
                    else { RoundedRectangle(cornerRadius: t.radius + 2).fill(t.panel) }
                }
            )
        }
        .padding(16)
        .overlay { if store.celebrate { DoneBurst().transition(.scale.combined(with: .opacity)) } }
        .background(t.bg)
        .tint(t.accent)
        .frame(minWidth: 620, minHeight: 480)
    }

    func reveal(_ path: String?) {
        guard let p = path else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)])
    }
}

// MARK: - Settings

struct SettingsView: View {
    @AppStorage(Keys.output) private var output = defaultOutput
    @AppStorage(Keys.cookies) private var cookies = "none"
    @AppStorage(Keys.loudness) private var loudness = true
    @AppStorage(Keys.parallel) private var parallel = 4
    @AppStorage("minQuality") private var minQuality = 128
    @AppStorage("sortMode") private var sortMode = "off"
    @AppStorage("normalizeLUFS") private var normalize = -10.0
    @AppStorage("autoUpdate") private var autoUpdate = !ownerCopy     // on for everyone, off on Vinny's Mac
    @AppStorage("hookTease") private var hookTease = "fits"
    @ObservedObject private var storeObs = Store.shared
    @AppStorage("makeIntros") private var sharedIntros = false
    @AppStorage("uiLook") private var look = "neon"
    @AppStorage("menuBarIcon") private var menuBar = true
    @AppStorage("djApp") private var djApp = "serato"
    @AppStorage("preventSleep") private var preventSleep = true
    @AppStorage("workSpeed") private var workSpeed = "balanced"
    @AppStorage("shareData") private var shareData = true
    @AppStorage("previewVolume") private var previewVolume = 0.8
    @AppStorage("previewDevice") private var previewDevice = ""
    @State private var outputs: [AudioOutputs.Device] = []

    var body: some View {
        TabView {
            Form {
            Section {
                LabeledContent("Save to") {
                    HStack {
                        Text((output as NSString).abbreviatingWithTildeInPath)
                            .lineLimit(1).truncationMode(.middle)
                            .foregroundStyle(.secondary)
                        Button("Choose…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true
                            panel.canChooseFiles = false
                            panel.canCreateDirectories = true
                            panel.directoryURL = URL(fileURLWithPath: output)
                            if panel.runModal() == .OK, let u = panel.url { output = u.path }
                        }
                    }
                }
            } footer: {
                Text(isAdmin ? "Tracks are added to Serato crates while Serato is closed."
                             : "Everything DJ Grab makes is saved here. Your other music is never moved or changed.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !isAdmin {
                Section("Sorting") {
                    VStack(alignment: .leading, spacing: 2) {
                        Picker("Sort into genre folders", selection: $sortMode) {
                            Text("Off").tag("off")
                            Text("Simple").tag("simple")
                            Text("Detailed").tag("detailed")
                        }
                        Text("Off: every song in your folder. Simple: one folder per genre (Hip Hop, R&B, Pop, Latin, Dance & House, Afro & Caribbean, Country, Rock). Detailed: also sub-genre folders (Hip Hop ▸ Drill), made only once a sub-genre has 3 songs. Only new downloads are sorted; your other music is never moved.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section("While working") {
                VStack(alignment: .leading, spacing: 2) {
                    Toggle("Keep the computer awake while working", isOn: $preventSleep)
                    Text("While songs download or intros are being made, the Mac won't go to sleep on its own, so long runs (like overnight) finish. The screen can still turn off. It goes back to normal as soon as DJ Grab is done.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Picker("Speed", selection: $workSpeed) {
                        Text("Balanced").tag("balanced")
                        Text("Background").tag("background")
                        Text("Full speed").tag("full")
                    }
                    Text("Balanced: DJ Grab uses about half the processor and gives way to other apps, so you can keep working while it runs. Background: barely noticeable, but songs take longer. Full speed: fastest, but other apps may lag. Takes effect from the next song.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("DJ software") {
                VStack(alignment: .leading, spacing: 2) {
                    Picker("I use", selection: $djApp) { DJAppPicker.options }
                    Text(DJAppPicker.note(djApp)).font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Downloads") {
                VStack(alignment: .leading, spacing: 2) {
                    Picker("Normalize volume", selection: $normalize) {
                        Text("Off").tag(0.0)
                        Text("Club (-8 LUFS)").tag(-8.0)
                        Text("Standard (-10 LUFS)").tag(-10.0)
                        Text("Quiet (-12 LUFS)").tag(-12.0)
                    }
                    Text("Suggested: Standard. Every new download comes out at the same loudness, so you don't have to fix volume between songs. Songs are only turned up or down, never squashed, and never pushed into clipping.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Picker("Minimum quality", selection: $minQuality) {
                        Text("Off").tag(0)
                        Text("Basic (96 kbps)").tag(96)
                        Text("Good (128 kbps)").tag(128)
                        Text("High (160 kbps)").tag(160)
                    }
                    Text("Suggested: Good. A song below this (or one that sounds muffled) isn't saved until you say so: DJ Grab tries other uploads first, then asks \"Poor quality, do you still want to download it?\"")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Stepper("Simultaneous downloads: \(parallel)", value: $parallel, in: 1...6)
                    Text("Suggested: 4. More at once finishes a big batch faster, but past about 4 YouTube can take it for a bot and block downloads for a while.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Toggle("Loudness warnings", isOn: $loudness)
                    Text("Flags a song that comes out unusually quiet, which usually means a bad rip or a live recording. It never changes the file.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Picker("Browser sign-in", selection: $cookies) {
                    Text("Off").tag("none")
                    Text("Chrome").tag("chrome")
                    Text("Safari").tag("safari")
                    Text("Firefox").tag("firefox")
                    Text("Brave").tag("brave")
                }
                .help("Only needed for age-restricted videos")
                Text("Only for age-restricted videos: uses that browser's YouTube sign-in. Leave it off otherwise.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            }
        .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .frame(width: 500, height: 460)
            .tabItem { Label("General", systemImage: "gearshape") }
            Form {
            if !isAdmin {
                Section("Intro versions") {
                    VStack(alignment: .leading, spacing: 2) {
                        Toggle("Make intro versions", isOn: $sharedIntros)
                        Text("For songs that start with vocals, also save a copy with a DJ-style beat intro (\"Song (Intro).mp3\") so you can mix into it. Made in the background after each download; the first time it downloads about 1 GB of extra tools.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section("Rate intros") {
                VStack(alignment: .leading, spacing: 2) {
                    Button("Rate intros…") { RateIntrosWindow.open() }
                    Text("A list of every song with an intro version: play the original or the intro, then 👍 or 👎, tap what was wrong and add a comment in your own words. Your ratings teach DJ Grab which intros you like.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Extras") {
                VStack(alignment: .leading, spacing: 2) {
                    Picker("Hook tease", selection: $hookTease) {
                        Text("Auto").tag("fits"); Text("Off").tag("off")
                    }
                    Text("Plays 2 bars of the chorus vocal softly during the intro, then 4 bars of beat alone before the song, so the crowd hears what's coming. Auto: only for hip hop, R&B, pop, latin, afro and caribbean songs with a short, clean, repeating hook that matches the intro's key, never when the song opens on its chorus, and it stops for a genre if you keep giving those intros 👎.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            }
        .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .frame(width: 500, height: 460)
            .tabItem { Label("Intros", systemImage: "waveform") }
            Form {
            Section("Theme") {
                VStack(alignment: .leading, spacing: 4) {
                    Picker("Theme", selection: $look) {
                        ForEach(Look.allCases) { Text($0.name).tag($0.rawValue) }
                    }
                    .pickerStyle(.segmented)
                    Text((Look(rawValue: look) ?? .clean).blurb)
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Playback") {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text("Volume")
                        Image(systemName: "speaker.fill").foregroundStyle(.secondary)
                        Slider(value: $previewVolume, in: 0...1)
                            .onChange(of: previewVolume) { Preview.shared.setVolume($0) }
                        Image(systemName: "speaker.wave.3.fill").foregroundStyle(.secondary)
                    }
                    Text("How loud songs play when you press ▶ in DJ Grab. Doesn't change your files or your Mac's volume.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Picker("Play through", selection: $previewDevice) {
                        Text("Mac's current output").tag("")
                        ForEach(outputs) { Text($0.name).tag($0.uid) }
                    }
                    .onAppear { outputs = AudioOutputs.list() }
                    Text("Where ▶ plays: speakers, headphones, or a DJ controller's headphone out to preview before you mix.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Menu bar") {
                VStack(alignment: .leading, spacing: 2) {
                    Toggle("Menu bar icon", isOn: $menuBar)
                    Text("A DJ Grab icon at the top of your screen: paste links or song names without opening the window.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            }
        .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .frame(width: 500, height: 460)
            .tabItem { Label("Look", systemImage: "paintbrush") }
            Form {
            if isAdmin {
                Section("Share") {
                    VStack(alignment: .leading, spacing: 2) {
                        Button("Share DJ Grab…") { Store.shared.shareApp() }
                        Text("Makes DJ Grab.zip on your Desktop and opens the share menu. Friends get the simple version: it just rips songs (intro versions optional), with nothing tied to Serato. Their first open: right-click the app, choose Open.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section("Privacy") {
                VStack(alignment: .leading, spacing: 2) {
                    Text("When you rate an intro, the rating (song, genre, what was wrong, your comment) and how DJ Grab made that intro are sent to DJ Grab's developer, so intros keep getting better.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Updates") {
                VStack(alignment: .leading, spacing: 2) {
                    Toggle("Install updates automatically", isOn: $autoUpdate)
                    Text("When an update is ready, DJ Grab installs it on its own as soon as nothing is downloading, processing or playing, then reopens. Off: you'll see an Install button instead.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Fix things") {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 10) {
                        Button { storeObs.restartAndUpdate() } label: {
                            Label("Restart & Update", systemImage: "arrow.clockwise")
                        }
                        .disabled(storeObs.busy || ToolsStore.shared.running || storeObs.restarting || storeObs.updating)
                        if storeObs.restarting || storeObs.updating {
                            ProgressView().controlSize(.small)
                            Text(storeObs.updating ? "Installing the update… DJ Grab will close and reopen by itself"
                                                   : "Working… DJ Grab will close and reopen by itself")
                                .font(.caption).foregroundStyle(Theme.current.accent2)
                        }
                    }
                    Text("Gets the newest YouTube downloader, installs any app update that's ready, and restarts DJ Grab. Also fixes anything that looks stuck. Finish downloads and intros first.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if ownerCopy {
                Section("Admin") {
                    VStack(alignment: .leading, spacing: 2) {
                        Toggle("View as everyone else", isOn: Binding(
                            get: { UserDefaults.standard.bool(forKey: "viewAsShared") },
                            set: { UserDefaults.standard.set($0, forKey: "viewAsShared"); Store.shared.restartApp() }))
                        Text("Reopens DJ Grab as the version other people get: no genre sorting (songs stay in one folder), no Serato crates or name cleanup, intro versions off until turned on. Turn off to get your full version back.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section {
                LabeledContent("Version", value: shownVersion(appVersion))
            }
            }
        .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .frame(width: 500, height: 460)
            .tabItem { Label("More", systemImage: "ellipsis.circle") }
        }
    }
}

// MARK: - Intros & Cues tab

let toolsPath = ownerCopy ? supportDir.appendingPathComponent("updates/djtools.py").path
                       : sharedEngine.appendingPathComponent("djtools.py").path

struct ToolResult: Identifiable {
    let id = UUID()
    var name: String
    var text: String
    var ok: Bool
    var path: String?          // the intro version (when one was made)
    var madeIntro = false
    var song: String? = nil    // the original song

    /// Intro version made: blue seal. Cues only (song already had a clean intro): green check.
    var icon: (String, Color, String) {
        if !ok { return ("minus.circle", Color.secondary, "Skipped") }
        return madeIntro ? ("checkmark.seal.fill", Color.blue, "Cues set · intro version made")
                         : ("checkmark.circle.fill", Color.green, "Cues set · no intro version needed")
    }
}

/// Shared app: when "Make intro versions" is on, each downloaded song gets its intro version made in the
/// background, one at a time. The song itself is never changed; the intro is saved next to it.
final class PlainIntros: ObservableObject {
    static let shared = PlainIntros()
    @Published var queue: [String] = []
    @Published var current: String? = nil
    func add(_ path: String) {
        guard UserDefaults.standard.bool(forKey: "sharedIntros") else { return }    // off unless turned on
        let n = (path as NSString).lastPathComponent.lowercased()
        guard n.hasSuffix(".mp3"), !n.contains("intro"), !queue.contains(path) else { return }
        queue.append(path)
        next()
    }
    private func next() {
        guard current == nil, !queue.isEmpty else { return }
        let path = queue.removeFirst()
        current = (path as NSString).lastPathComponent
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [toolsPath, "intro", path, "--bars", "8", "--json"]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = toolPATH + ":" + (env["PATH"] ?? "")
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in DispatchQueue.main.async { self.current = nil; self.next() } }
        if (try? p.run()) == nil { current = nil }
    }
}

/// Shared app: installs Python, ffmpeg and the download tools on first open (and refreshes the
/// engine after an update), by running setup.sh from inside the app.
final class SetupStore: ObservableObject {
    static let shared = SetupStore()
    @Published var needed = false
    @Published var pct = 0.0
    @Published var stage = "Starting"
    @Published var failed: String? = nil
    private var bundledVersion: String {
        (try? String(contentsOfFile: Bundle.main.resourcePath! + "/engine/ENGINE_VERSION", encoding: .utf8)) ?? ""
    }
    init() {
        guard !ownerCopy, Bundle.main.path(forResource: "setup", ofType: "sh") != nil else { return }
        let have = (try? String(contentsOf: sharedEngine.appendingPathComponent("ENGINE_VERSION"), encoding: .utf8)) ?? "none"
        needed = !FileManager.default.fileExists(atPath: pythonPath) || have != bundledVersion
    }
    func run() {
        guard let script = Bundle.main.path(forResource: "setup", ofType: "sh") else { return }
        failed = nil
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out; p.standardError = err
        out.fileHandleForReading.readabilityHandler = { h in
            guard let t = String(data: h.availableData, encoding: .utf8) else { return }
            for line in t.split(separator: "\n") where line.hasPrefix("STEP ") {
                let parts = line.split(separator: " ", maxSplits: 2)
                DispatchQueue.main.async {
                    self.pct = Double(parts[1]) ?? self.pct
                    if parts.count > 2 { self.stage = String(parts[2]) }
                }
            }
        }
        p.terminationHandler = { pr in
            out.fileHandleForReading.readabilityHandler = nil
            let e = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                if pr.terminationStatus == 0 { self.needed = false }
                else { self.failed = "Setup didn't finish. Check your internet connection and try again.\n" + String(e.suffix(200)) }
            }
        }
        do { try p.run() } catch { failed = "Setup couldn't start." }
    }
}

struct SetupView: View {
    @ObservedObject var setup = SetupStore.shared
    var body: some View {
        VStack(spacing: 14) {
            Text("Setting up DJ Grab").font(.title3.weight(.semibold))
            Text("One time only: downloading the tools DJ Grab uses (about 150 MB). Takes a few minutes.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
            ProgressView(value: setup.pct, total: 100)
            Text(setup.stage).font(.caption).foregroundStyle(.secondary)
            if let f = setup.failed {
                Text(f).font(.caption).foregroundStyle(.red).multilineTextAlignment(.center)
                Button("Try Again") { setup.run() }.buttonStyle(.borderedProminent)
            }
        }
        .padding(28)
        .frame(width: 420)
        .onAppear { setup.run() }
        .interactiveDismissDisabled()
    }
}

/// First launch of the shared app: pick where songs go, and hear about intro versions (off to start).
struct WelcomeView: View {
    @AppStorage(Keys.output) private var output = defaultOutput
    @AppStorage("makeIntros") private var intros = false
    @AppStorage("shareData") private var share = true
    @AppStorage("djApp") private var djApp = "serato"
    @AppStorage("sortMode") private var sortMode = "off"
    @AppStorage("minQuality") private var minQuality = 128
    @AppStorage("autoUpdate") private var autoUpdate = !ownerCopy
    var done: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Welcome to DJ Grab").font(.title2.weight(.semibold))
            Text("Set up DJ Grab for this computer. You can change any of this later in Settings.")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 6) {
                Picker("Which DJ software do you use?", selection: $djApp) { DJAppPicker.options }
                    .font(.headline)
                Text(DJAppPicker.note(djApp)).font(.caption).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Where should your songs go?").font(.headline)
                HStack {
                    Text((output as NSString).abbreviatingWithTildeInPath).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose…") {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = true
                        panel.canChooseFiles = false
                        panel.canCreateDirectories = true
                        if panel.runModal() == .OK, let u = panel.url { output = u.path }
                    }
                }
                Text("Everything DJ Grab makes is saved here. Your other music is never moved or changed.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Picker("Sort into genre folders", selection: $sortMode) {
                Text("Off").tag("off"); Text("Simple").tag("simple"); Text("Detailed").tag("detailed")
            }
            Picker("Minimum quality", selection: $minQuality) {
                Text("Off").tag(0); Text("Basic (96 kbps)").tag(96); Text("Good (128 kbps)").tag(128); Text("High (160 kbps)").tag(160)
            }
            Toggle("Install updates automatically", isOn: $autoUpdate)
            VStack(alignment: .leading, spacing: 6) {
                Toggle("Make intro versions", isOn: $intros)
                Text("Optional. For songs that start with vocals, DJ Grab also saves a copy with a DJ-style beat intro (\"Song (Intro).mp3\") so you can mix into it. The first time, it downloads about 1 GB of extra tools. You can turn this on or off later in Settings.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text("When you rate an intro, the rating and how DJ Grab made that intro are sent to DJ Grab's developer to make intros better.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Get Started") {
                    try? FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
                    UserDefaults.standard.set(true, forKey: "welcomed")
                    done()
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(24)
        .frame(width: 500)
    }
}

/// The DJ software choice: decides where cue points go.
enum DJAppPicker {
    @ViewBuilder static var options: some View {
        Text("Serato DJ").tag("serato")
        Text("Rekordbox").tag("rekordbox")
        Text("Traktor").tag("traktor")
        Text("VirtualDJ").tag("virtualdj")
        Text("Other / none").tag("other")
    }
    static func note(_ app: String) -> String {
        switch app {
        case "rekordbox": return "Cue points go into \"DJ Grab cues/DJ Grab (rekordbox).xml\" in your music folder. In Rekordbox, choose that file under Preferences ▸ Advanced ▸ Database ▸ rekordbox xml, then open the DJ Grab playlist."
        case "traktor": return "Cue points go into \"DJ Grab cues/DJ Grab (Traktor).nml\" in your music folder. In Traktor, use File ▸ Import Collection on that file."
        case "virtualdj": return "Cue points are added to VirtualDJ's library automatically whenever VirtualDJ is closed."
        case "other": return "Songs and intro versions work in any software. Cue points are skipped."
        default: return "Cue points and beat grids are saved inside each song file and show up in Serato right away."
        }
    }
}

/// Your 👍 / 👎 on intro versions, kept in intro_ratings.json next to DJ Grab's other settings, so the intro
/// rules can be tuned to what you like.
/// "Share song data for improvements" (Settings, on by default, and said on the welcome screen): each
/// 👍/👎 is sent to Vinny's Google Sheet through a Google Form, so the intro rules can be improved for
/// everyone. Sent: song, artist, genre folder, the kind of intro, the rating, the app version, and a comment if you write one. Nothing else.
/// Until the form is set up (formURL still has FORM_ID), ratings wait in a file and are sent later.
enum ShareData {
    static let formURL = "https://docs.google.com/forms/d/e/1FAIpQLSeHPgyVeKGnFWfrhUwK1qXXzJXett_X9zqoGctw_s_7LPxC4g/formResponse"
    /// A random id made once per computer, so ratings from the same person can be told apart (no name).
    static var installID: String {
        if let v = UserDefaults.standard.string(forKey: "installID") { return v }
        let v = String(UUID().uuidString.prefix(8)); UserDefaults.standard.set(v, forKey: "installID"); return v
    }
    static let dataField = "entry.823349679"      // the form's one "Data" box: each rating as JSON (one row in the sheet)
    static let fields = ["song": "entry.SONG", "genre": "entry.GENRE", "intro": "entry.INTRO",
                         "rating": "entry.RATING", "version": "entry.VERSION", "details": "entry.DETAILS",
                         "reasons": "entry.REASONS", "comment": "entry.COMMENT", "log": "entry.LOG"]
    /// That song's lines from this computer's intro log (how its intro was built), newest last.
    static func logLines(for path: String) -> String {
        let song = ((path as NSString).lastPathComponent as NSString).deletingPathExtension.replacingOccurrences(of: " (Intro)", with: "")
        let cands = [supportDir.appendingPathComponent("engine/logs/intros.log"), supportDir.appendingPathComponent("logs/intros.log"),
                     supportDir.appendingPathComponent("updates/logs/intros.log"),
                     FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/DJ Grab/logs/intros.log")]
        guard let f = cands.first(where: { FileManager.default.fileExists(atPath: $0.path) }),
              let h = try? FileHandle(forReadingFrom: f) else { return "" }
        defer { try? h.close() }
        let size = (try? h.seekToEnd()) ?? 0
        try? h.seek(toOffset: size > 3_000_000 ? size - 3_000_000 : 0)        // the last ~3 MB is plenty
        let text = String(decoding: h.readDataToEndOfFile(), as: UTF8.self)
        let lines = text.split(separator: "\n").filter { $0.contains(song) }
        return lines.suffix(30).joined(separator: "\n")
    }
    static let pending = supportDir.appendingPathComponent("share_pending.json")
    static var on: Bool { true }       // always on (shared with a few friends): sent in the background

    static func rating(song: String, path: String, details: String, value: Int) {
        guard on else { return }
        let parts = path.split(separator: "/").map(String.init)
        let genre = parts.count >= 3 ? parts[parts.count - 3] + "/" + parts[parts.count - 2] : ""
        let kind = ["loop A", "loop B", "straight", "original instrumental", "drums into the soft opening"]
            .first { details.contains($0) } ?? ""
        let e = RatingStore.shared.entries.first { ($0["path"] as? String) == path || ($0["path"] as? String)?.hasPrefix(String(path.dropLast(4))) == true }
        queue(["song": song, "genre": genre, "intro": kind,
               "rating": value > 0 ? "up" : value < 0 ? "down" : "cleared", "version": appVersion,
               "details": String(details.prefix(900)),         // what the intro did: cues, how it was built
               "reasons": ((e?["reasons"] as? [String]) ?? []).joined(separator: ", "),
               "comment": (e?["comment"] as? String) ?? "",
               "log": String(logLines(for: path).suffix(20000)),
               "when": ISO8601DateFormatter().string(from: Date()), "user": installID])
        flush()
    }
    private static func load() -> [[String: String]] {
        (try? JSONSerialization.jsonObject(with: Data(contentsOf: pending)) as? [[String: String]]) ?? []
    }
    private static func save(_ a: [[String: String]]) {
        if let d = try? JSONSerialization.data(withJSONObject: a) { try? d.write(to: pending) }
    }
    static func queue(_ row: [String: String]) { var a = load(); a.append(row); save(Array(a.suffix(2000))) }
    /// Sends what's waiting (when online, sharing on, and the form is set up). Failed sends stay for next time.
    static func flush() {
        guard on, !formURL.contains("FORM_ID"), Connection.shared.online, let url = URL(string: formURL) else { return }
        let rows = load()
        guard !rows.isEmpty else { return }
        save([])
        for row in rows {
            var c = URLComponents()
            let json = (try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) } ?? ""
            c.queryItems = [URLQueryItem(name: dataField, value: json)]
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            req.httpBody = c.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B").data(using: .utf8)
            URLSession.shared.dataTask(with: req) { _, resp, err in
                if err != nil || ((resp as? HTTPURLResponse)?.statusCode ?? 500) >= 400 { queue(row) }
            }.resume()
        }
    }
}

final class RatingStore: ObservableObject {
    static let shared = RatingStore()
    @Published var ratings: [String: Int] = [:]
    @Published var entries: [[String: Any]] = []        // every rating: song, 👍/👎, when, what the intro did
    private let file = supportDir.appendingPathComponent("intro_ratings.json")
    init() { reload() }
    func reload() {
        if let d = try? Data(contentsOf: file),
           let a = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] {
            entries = a
            for r in a { if let p = r["path"] as? String, let v = r["rating"] as? Int { ratings[p] = v } }
        }
    }
    func rate(_ r: ToolResult, _ v: Int) {
        guard let p = r.path else { return }
        let nv = ratings[p] == v ? 0 : v                    // tap the same thumb again to take it back
        ratings[p] = nv
        var a = ((try? JSONSerialization.jsonObject(with: (try? Data(contentsOf: file)) ?? Data("[]".utf8))) as? [[String: Any]]) ?? []
        let old = a.first { ($0["path"] as? String) == p }
        a.removeAll { ($0["path"] as? String) == p }
        if nv != 0 {
            var e: [String: Any] = ["path": p, "name": r.name, "rating": nv, "details": r.text,
                                    "when": ISO8601DateFormatter().string(from: Date())]
            if let c = old?["comment"] as? String, !c.isEmpty { e["comment"] = c }
            if nv == -1, let rs = old?["reasons"] { e["reasons"] = rs }
            a.append(e)
        }
        if let d = try? JSONSerialization.data(withJSONObject: a, options: [.prettyPrinted]) { try? d.write(to: file) }
        entries = a
        if nv != -1 { reasons[p] = nil }
        exportText()
        share(p, name: r.name, song: r.song ?? p, details: r.text)
    }
    @Published var reasons: [String: Set<String>] = [:]
    /// What was wrong with an intro rated 👎 (saved with the rating as "reasons").
    func toggleReason(_ p: String, _ why: String) {
        var r = reasons[p] ?? Set((entries.first { ($0["path"] as? String) == p }?["reasons"] as? [String]) ?? [])
        if r.contains(why) { r.remove(why) } else { r.insert(why) }
        reasons[p] = r
        var a = ((try? JSONSerialization.jsonObject(with: (try? Data(contentsOf: file)) ?? Data("[]".utf8))) as? [[String: Any]]) ?? []
        if let i = a.firstIndex(where: { ($0["path"] as? String) == p }) { a[i]["reasons"] = Array(r).sorted() }
        if let d = try? JSONSerialization.data(withJSONObject: a, options: [.prettyPrinted]) { try? d.write(to: file) }
        entries = a
        exportText()
        share(p)
    }
    /// Every rating (song, 👍/👎, what was wrong, your comment, how the intro was made) written as a plain
    /// text file next to DJ Grab's logs, rewritten the moment anything is rated, so they can be
    /// looked at together with the intro log.
    func exportText() {
        let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/DJ Grab/logs")
        guard FileManager.default.fileExists(atPath: logs.path) else { return }
        let rows = ((try? JSONSerialization.jsonObject(with: (try? Data(contentsOf: file)) ?? Data("[]".utf8))) as? [[String: Any]]) ?? []
        var out = "DJ Grab intro ratings, \(rows.count) songs, written \(ISO8601DateFormatter().string(from: Date()))\n\n"
        for r in rows.sorted(by: { ($0["when"] as? String ?? "") < ($1["when"] as? String ?? "") }) {
            let v = r["rating"] as? Int ?? 0
            out += (v > 0 ? "UP   " : "DOWN ") + (r["when"] as? String ?? "") + "  " + (r["name"] as? String ?? "") + "\n"
            out += "     file: " + (r["path"] as? String ?? "") + "\n"
            if let rs = r["reasons"] as? [String], !rs.isEmpty { out += "     wrong: " + rs.joined(separator: ", ") + "\n" }
            if let c = r["comment"] as? String, !c.isEmpty { out += "     comment: " + c + "\n" }
            if let d = r["details"] as? String, !d.isEmpty { out += "     intro: " + d.replacingOccurrences(of: "\n", with: " ") + "\n" }
            out += "\n"
        }
        try? out.write(to: logs.appendingPathComponent("intro_ratings.txt"), atomically: true, encoding: .utf8)
        if let d = try? Data(contentsOf: file) { try? d.write(to: logs.appendingPathComponent("intro_ratings.json")) }
    }
    private var sendTimers: [String: DispatchWorkItem] = [:]
    /// Sent in the background 15 s after the last change to a rating (the thumb, what was wrong, the comment),
    /// so one rating goes out once, complete.
    func share(_ p: String, name: String? = nil, song: String? = nil, details: String? = nil) {
        sendTimers[p]?.cancel()
        let e = entries.first { ($0["path"] as? String) == p }
        let nm = name ?? (e?["name"] as? String) ?? ((p as NSString).lastPathComponent as NSString).deletingPathExtension
        let det = details ?? (e?["details"] as? String) ?? ""
        let sg = song ?? p.replacingOccurrences(of: " (Intro).mp3", with: ".mp3")
        let w = DispatchWorkItem { [weak self] in
            ShareData.rating(song: nm, path: sg, details: det, value: self?.ratings[p] ?? 0)
        }
        sendTimers[p] = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: w)
    }
    /// Your own words on what's wrong (or right) with an intro, saved with its rating.
    func setComment(_ p: String, _ text: String) {
        var a = ((try? JSONSerialization.jsonObject(with: (try? Data(contentsOf: file)) ?? Data("[]".utf8))) as? [[String: Any]]) ?? []
        guard let i = a.firstIndex(where: { ($0["path"] as? String) == p }) else { return }
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { a[i].removeValue(forKey: "comment") } else { a[i]["comment"] = t }
        if let d = try? JSONSerialization.data(withJSONObject: a, options: [.prettyPrinted]) { try? d.write(to: file) }
        entries = a
        exportText()
        share(p)
    }
    func commentFor(_ p: String) -> String {
        (entries.first { ($0["path"] as? String) == p }?["comment"] as? String) ?? ""
    }
    func reasonsFor(_ p: String) -> Set<String> {
        reasons[p] ?? Set((entries.first { ($0["path"] as? String) == p }?["reasons"] as? [String]) ?? [])
    }
}

/// A line to say in your own words what's wrong with a rated intro (saved as you type, sent with the rating).
struct RatingComment: View {
    let path: String
    var name: String = ""
    @ObservedObject private var ratings = RatingStore.shared
    @State private var text = ""
    var body: some View {
        TextField("What's wrong with it? (optional)", text: $text)
            .textFieldStyle(.roundedBorder)
            .font(.caption)
            .onAppear { text = ratings.commentFor(path) }
            .onChange(of: text) { t in ratings.setComment(path, t) }
    }
}

/// Pops up after a 👎: what was wrong (tap any), your own words, or "Doesn't need an intro".
struct RateNote: View {
    let path: String           // the intro file
    let name: String
    let song: String           // the original
    @ObservedObject private var ratings = RatingStore.shared
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What's wrong with this intro?").font(.headline)
            Text(name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            let chosen = ratings.reasonsFor(path)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 170), spacing: 6)], alignment: .leading, spacing: 6) {
                ForEach(RateIntros.whys, id: \.self) { w in
                    Button(w) { ratings.toggleReason(path, w) }
                        .buttonStyle(.bordered)
                        .tint(chosen.contains(w) ? .pink : .gray)
                }
            }
            RatingComment(path: path, name: name)
            HStack {
                Button("Doesn't need an intro") {
                    if !ratings.reasonsFor(path).contains("Not needed") { ratings.toggleReason(path, "Not needed") }
                    ToolsStore.shared.noIntro(song: song, intro: path)
                    dismiss()
                }
                .help("The song already works without one: its intro version moves to _To Delete and it won't get one again")
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(18)
        .frame(width: 460)
    }
}

/// A window listing every song that has an intro version, to listen and rate them one after another.
enum RateIntrosWindow {
    static var window: NSWindow?
    static func open() {
        if let w = window { w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Rate intros"
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: RateIntros())
        w.center()
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window = w
    }
}

struct IntroItem: Hashable, Identifiable { let song: String; let intro: String; var id: String { intro } }

struct RateIntros: View {
    @ObservedObject private var tools = ToolsStore.shared
    @ObservedObject private var ratings = RatingStore.shared
    @State private var items: [IntroItem] = []
    @State private var show = 0          // 0 not rated yet, 1 all, 2 👎, 3 👍
    @State private var loading = true
    @State private var note: IntroItem? = nil
    static let whys = ["Beat jumps / off time", "Cut off at the start", "Doubled or clashing hits", "Bad build or effect",
                       "Wrong part of the song", "Sounds muddy / artifacts", "Handover into the song", "Not needed"]
    var shown: [IntroItem] {
        items.filter { it in
            let r = ratings.ratings[it.intro] ?? 0
            return show == 1 || (show == 0 && r == 0) || (show == 2 && r == -1) || (show == 3 && r == 1)
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("", selection: $show) {
                    Text("Not rated").tag(0); Text("All").tag(1); Text("👎").tag(2); Text("👍").tag(3)
                }
                .pickerStyle(.segmented).frame(maxWidth: 320)
                Spacer()
                Text(loading ? "Finding intros…" : "\(shown.count) of \(items.count) intros").foregroundStyle(.secondary).monospacedDigit()
                StopSoundButton()
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(shown, id: \.intro) { it in row(it) }
                }
            }
        }
        .padding(12)
        .frame(minWidth: 560, minHeight: 420)
        .onAppear(perform: load)
        .sheet(item: $note) { it in
            RateNote(path: it.intro, name: ((it.song as NSString).lastPathComponent as NSString).deletingPathExtension, song: it.song)
        }
    }
    @ViewBuilder func row(_ it: IntroItem) -> some View {
        let r = ratings.ratings[it.intro] ?? 0
        let name = ((it.song as NSString).lastPathComponent as NSString).deletingPathExtension
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 0) {
                    Text(name).lineLimit(1)
                    Text(((it.song as NSString).deletingLastPathComponent as NSString).lastPathComponent)
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                VStack(spacing: 1) { PlayButton(path: it.song); Text("Original").font(.system(size: 9)).foregroundStyle(.secondary) }
                VStack(spacing: 1) { PlayButton(path: it.intro); Text("Intro").font(.system(size: 9)).foregroundStyle(.secondary) }
                Button { ratings.rate(result(it, name), 1) } label: { Text("👍").font(.system(size: 20)).opacity(r == 1 ? 1 : 0.35) }
                    .buttonStyle(.plain).help("Good intro")
                Button { ratings.rate(result(it, name), -1); if ratings.ratings[it.intro] == -1 { note = it } } label: { Text("👎").font(.system(size: 20)).opacity(r == -1 ? 1 : 0.35) }
                    .buttonStyle(.plain).help("Something's wrong with this intro")
                Button { ToolsStore.shared.noIntro(song: it.song, intro: it.intro); items.removeAll { $0 == it } } label: {
                    Image(systemName: "nosign").font(.system(size: 15)).foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Doesn't need an intro: the intro version moves to _To Delete and this song won't get one again")
            }
            SeekBar(path: it.intro)
            if r == -1 {
                let chosen = ratings.reasonsFor(it.intro)
                HStack(spacing: 4) {
                    Text("What's wrong:").font(.caption).foregroundStyle(.secondary)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 4) {
                            ForEach(Self.whys, id: \.self) { w in
                                Button(w) { ratings.toggleReason(it.intro, w) }
                                    .font(.caption)
                                    .buttonStyle(.bordered)
                                    .tint(chosen.contains(w) ? .pink : .gray)
                            }
                        }
                    }
                }
            }
            if r != 0 { RatingComment(path: it.intro, name: name) }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.current.panel))
    }
    private func result(_ it: IntroItem, _ name: String) -> ToolResult {
        ToolResult(name: name, text: "", ok: true, path: it.intro, madeIntro: true, song: it.song)
    }
    private func load() {
        let base = isAdmin ? tools.libraryRoot : Store.shared.outputFolder
        loading = true
        DispatchQueue.global().async {
            var out: [IntroItem] = []
            if let en = FileManager.default.enumerator(atPath: base) {
                for case let f as String in en {
                    let name = (f as NSString).lastPathComponent
                    if f.hasPrefix("_") || f.contains("/_") || name.hasPrefix(".") { continue }
                    guard name.hasSuffix(" (Intro).mp3") else { continue }
                    let song = base + "/" + String(f.dropLast(" (Intro).mp3".count)) + ".mp3"
                    if FileManager.default.fileExists(atPath: song) { out.append(IntroItem(song: song, intro: base + "/" + f)) }
                }
            }
            out.sort { ($0.song as NSString).lastPathComponent.localizedCaseInsensitiveCompare(($1.song as NSString).lastPathComponent) == .orderedAscending }
            DispatchQueue.main.async { items = out; loading = false }
        }
    }
}

final class ToolsStore: ObservableObject {
    static let shared = ToolsStore()
    @Published var running = false
    @Published var total = 0
    @Published var done = 0
    @Published var current = ""
    @Published var stage = ""
    @Published var pct: Double = 0
    @Published var status = ""
    @Published var lastRun: String = UserDefaults.standard.string(forKey: "introsLastRun") ?? ""
    @Published var runLabel = ""
    @Published var results: [ToolResult] = []
    @Published var waiting: [String] = UserDefaults.standard.stringArray(forKey: "introsWaiting") ?? []
    private var proc: Process?
    private var buffer = Data()
    private var secs: [Double] = []            // how long each song in THIS batch took (wall clock)
    private var songStart: Date?
    private var pauseStart: Date?
    private var etaTimer: Timer?
    @Published var clock = 0                     // ticks every few seconds so the estimate keeps counting down

    private func saveWaiting() { UserDefaults.standard.set(waiting, forKey: "introsWaiting") }

    // New downloads: intro + cues start straight away (the engine makes its own beatgrid)
    func addNewDownload(_ path: String) {
        guard UserDefaults.standard.object(forKey: "autoIntros") as? Bool != false else { return }
        let name = (path as NSString).lastPathComponent.lowercased()
        guard name.hasSuffix(".mp3"), !name.contains("intro"), !name.contains("acapella"),
              !name.contains("instrumental"), !waiting.contains(path) else { return }
        waiting.append(path); saveWaiting()
        tryWaiting()
    }

    @Published var redoing: String? = nil
    @Published var currentPath: String? = nil

    /// "No intro for this song": remembered (by artist + title) so no run ever makes one again. If it's the
    /// song being worked on now, the run restarts and does only its cues. An intro already made is moved to
    /// _To Delete (never deleted).
    func noIntro(song: String, intro: String? = nil) {
        let file = supportDir.appendingPathComponent("no_intro.json")
        var keys = (try? JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String]) ?? []
        var k = ((song as NSString).lastPathComponent as NSString).deletingPathExtension.lowercased()
        k = k.replacingOccurrences(of: "\\s*[\\(\\[].*?[\\)\\]]", with: "", options: .regularExpression)
        k = k.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        if !keys.contains(k) { keys.append(k) }
        if let d = try? JSONSerialization.data(withJSONObject: keys) { try? d.write(to: file) }
        let ip = intro ?? (song.lowercased().hasSuffix(".mp3") ? String(song.dropLast(4)) + " (Intro).mp3" : "")
        if FileManager.default.fileExists(atPath: ip) {
            if Preview.shared.playing == ip { Preview.shared.stop() }
            let parts = ip.split(separator: "/").map(String.init)
            let root = ip.hasPrefix("/Volumes/") && parts.count > 2 ? "/Volumes/" + parts[1] : (ip as NSString).deletingLastPathComponent
            let dest = root + "/_To Delete/DJ Grab intros not needed"
            try? FileManager.default.createDirectory(atPath: dest, withIntermediateDirectories: true)
            try? FileManager.default.moveItem(atPath: ip, toPath: dest + "/" + (ip as NSString).lastPathComponent)
            results.removeAll { $0.path == ip }
        }
        if running && currentPath == song { restart() }
    }
    /// "Make intro" on a song DJ Grab decided didn't need one: remembered, then the song runs again
    func makeIntro(song: String) {
        let file = supportDir.appendingPathComponent("force_intro.json")
        var keys = (try? JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String]) ?? []
        var k = ((song as NSString).lastPathComponent as NSString).deletingPathExtension.lowercased()
        k = k.replacingOccurrences(of: "\\s*[\\(\\[].*?[\\)\\]]", with: "", options: .regularExpression)
        k = k.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        if !keys.contains(k) { keys.append(k) }
        if let d = try? JSONSerialization.data(withJSONObject: keys) { try? d.write(to: file) }
        // it may be on the "no intro" list from earlier: taken off it
        let nf = supportDir.appendingPathComponent("no_intro.json")
        if var no = (try? JSONSerialization.jsonObject(with: Data(contentsOf: nf)) as? [String]) {
            no.removeAll { $0 == k }
            if let d = try? JSONSerialization.data(withJSONObject: no) { try? d.write(to: nf) }
        }
        results.removeAll { $0.song == song && !$0.madeIntro }
        start(file: song)
    }
    /// songs waiting that aren't part of the run going on now
    var notStarted: Int { running ? waiting.filter { !(lastFiles ?? []).contains($0) }.count : waiting.count }
    /// 👎 → "Try another": a new intro for this one song, from a version you haven't heard yet
    func another(_ r: ToolResult) {
        guard !running, let p = r.song ?? r.path?.replacingOccurrences(of: " (Intro).mp3", with: ".mp3") else { return }
        redoing = r.path
        // the intro being replaced (or its song) stops playing right away
        if let now = Preview.shared.playing, now == r.path || now == p { Preview.shared.stop() }
        if RatingStore.shared.ratings[r.path ?? ""] == -1 { RatingStore.shared.rate(r, -1) }   // the new intro starts unrated
        if let i = results.firstIndex(where: { $0.id == r.id }) { results.remove(at: i) }
        start(files: [p], quiet: false, mode: "another")
    }

    func tryWaiting() {
        guard !running, !userPaused, !waiting.isEmpty, Store.shared.driveConnected else { return }
        start(files: waiting, quiet: true)
    }

    var libraryRoot: String { (Store.shared.outputFolder as NSString).deletingLastPathComponent }

    /// Time left on the song being worked on, in plain words, to the nearest 10 seconds.
    /// Learned from your last songs: for each one, how long it still took from the step this song is at
    /// now to the end. The middle of those (so one odd song doesn't throw it off) is the estimate.
    private var marks: [(Double, Double)] = []          // (percent reached, seconds since the song started)
    private var etaDoneAt: Date? = nil                   // when the engine expects this song to finish
    private func profiles() -> [[[Double]]] {
        (UserDefaults.standard.array(forKey: "introProfiles") as? [[[Double]]]) ?? []
    }
    private func saveProfile(marks: [(Double, Double)], total: Double) {
        var all = profiles()
        all.append(marks.map { [$0.0, $0.1] } + [[100, total]])
        UserDefaults.standard.set(Array(all.suffix(25)), forKey: "introProfiles")
    }
    var songEta: String {
        _ = clock
        guard running, pauseStart == nil, let st = songStart else { return "" }
        if let d = etaDoneAt {
            let left = d.timeIntervalSinceNow
            if left < 10 { return left <= 0 ? "Almost done" : "Less than 10 seconds left" }
            return spokenTime(left) + " left on this song"
        }
        let elapsed = Date().timeIntervalSince(st)
        let past = profiles()
        var left: Double
        if past.count >= 2 {
            // for each past song: its total minus when it first got to this song's current step
            let rem = past.compactMap { prof -> Double? in
                guard let total = prof.last?[1], let at = prof.first(where: { $0[0] >= pct })?[1] else { return nil }
                return total - at
            }.sorted()
            guard !rem.isEmpty else { return "" }
            let mid = rem[rem.count / 2]
            // time already spent on this step counts against what's left
            let stepStart = marks.last?.1 ?? elapsed
            left = mid - (elapsed - stepStart)
        } else {
            // the first couple of songs: your usual time per song, spread over the steps it has left
            let saved = UserDefaults.standard.double(forKey: "introsAvgSecs")
            let avg = saved > 0 ? saved : 60
            left = max(avg - elapsed, avg * (100 - pct) / 100 * 0.8)
        }
        if left < 10 { return left <= 0 ? "Almost done" : "Less than 10 seconds left" }
        return spokenTime(left) + " left on this song"
    }

    var eta: String {
        let left = max(0, total - done)
        guard running, left > 0 else { return "" }
        if pauseStart != nil { return "" }
        // this batch's own pace; before the first song finishes, the pace of your last runs
        let saved = UserDefaults.standard.double(forKey: "introsAvgSecs")
        let recent = secs.suffix(30)
        var avg = recent.isEmpty ? (saved > 0 ? saved : 35.0) : recent.reduce(0, +) / Double(recent.count)
        let totals = profiles().compactMap { $0.last?[1] }.sorted()
        if totals.count >= 3 && recent.count < 3 { avg = totals[totals.count / 2] }   // until this run has its own pace
        let into = songStart.map { min(Date().timeIntervalSince($0), avg * 0.9) } ?? 0
        let s = max(5, avg * Double(left) - into)
        return spokenTime(s) + " left in total"
    }

    func start(file: String? = nil) {
        start(files: file.map { [$0] }, quiet: false)
    }

    // Folders you can run one at a time: "Hip Hop", "Hip Hop/Rap", ...
    func folderChoices() -> [String] {
        let fm = FileManager.default
        var out: [String] = []
        let tops = ((try? fm.contentsOfDirectory(atPath: libraryRoot)) ?? []).sorted()
        for t in tops where !t.hasPrefix(".") && !t.hasPrefix("_") && !t.hasPrefix("00") && t != "Tools" && t != "Other" && t != "Mixes" {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: libraryRoot + "/" + t, isDirectory: &isDir), isDir.boolValue else { continue }
            out.append(t)
            let subs = ((try? fm.contentsOfDirectory(atPath: libraryRoot + "/" + t)) ?? []).sorted()
            for sname in subs where !sname.hasPrefix(".") && !sname.hasPrefix("_") {
                var d: ObjCBool = false
                if fm.fileExists(atPath: libraryRoot + "/" + t + "/" + sname, isDirectory: &d), d.boolValue { out.append(t + "/" + sname) }
            }
        }
        return out
    }

    var folder: String = ""      // "" = whole library

    func isComplete(_ f: String) -> Bool {
        if f.hasPrefix("/") {
            if let c = chosenStatus[f], c.count == 2, c[1] > 0 { return c[0] >= c[1] }
            return false
        }
        if f.isEmpty {
            let tops = folderStatus.filter { !$0.key.contains("/") }
            return !tops.isEmpty && tops.values.allSatisfy { $0.count == 2 && $0[1] > 0 && $0[0] >= $0[1] }
        }
        if let c = folderStatus[f.precomposedStringWithCanonicalMapping], c.count == 2, c[1] > 0 { return c[0] >= c[1] }
        return false
    }

    var progress: Double {
        guard total > 0 else { return 0 }
        return min(1, (Double(done) + (running ? min(pct, 100) / 100 : 0)) / Double(total))
    }

    private var targetName: String {
        if folder.hasPrefix("/") { return (folder as NSString).lastPathComponent }
        return folder.isEmpty ? "Library" : folder.replacingOccurrences(of: "/", with: " ▸ ")
    }

    func start(files: [String]?, quiet: Bool, mode: String = "files", forceRedo: Bool = false) {
        if running {
            // busy with another song: line these up next instead of ignoring the click
            if let f = files, !quiet {
                for p in f where !waiting.contains(p) { waiting.append(p) }
                saveWaiting()
                let name = f.count == 1 ? ((f[0] as NSString).lastPathComponent as NSString).deletingPathExtension : "\(f.count) songs"
                status = "Queued: \(name) (\(waiting.count) waiting, starts when this one finishes)"
            }
            return
        }
        let chosen = folder.hasPrefix("/")          // a folder picked anywhere on the computer
        if chosen && files == nil {
            guard FileManager.default.fileExists(atPath: folder) else {
                if !quiet { status = "Folder not found (drive unplugged?)" }
                return
            }
        } else {
            guard Store.shared.refreshDrive() else {
                if !quiet { status = "USB not connected" }
                return
            }
        }
        let bars = 8                 // every intro is 8 bars (32 beats): loop it in your DJ software for longer
        var args = [toolsPath, files == nil ? "batch" : mode]
        if let f = files { args += f }
        // Start on a folder that's already finished = do it again, overwriting the old cues and intros
        let redo = files == nil && (forceRedo || isComplete(folder))
        if redo { args.append("--redo") }
        if files == nil && !quiet { results.removeAll() }      // a new folder run starts with a fresh list
        lastFiles = files
        lastMode = mode
        userPaused = false
        runLabel = files == nil ? targetName : (files!.count == 1 ? ((files![0] as NSString).lastPathComponent as NSString).deletingPathExtension : "\(files!.count) songs")
        // others: "whole library" means their DJ Grab folder, never anything outside it
        let base = isAdmin ? libraryRoot : Store.shared.outputFolder
        let root = chosen ? folder : (folder.isEmpty ? base : base + "/" + folder)
        args += ["--root", root, "--bars", String(bars), "--json"]
        if (UserDefaults.standard.object(forKey: "makeIntros") as? Bool ?? isAdmin) == false { args.append("--no-intro") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = toolPATH + ":" + (env["PATH"] ?? "")
        env["PYTHONUNBUFFERED"] = "1"
        env["DJGRAB_DJAPP"] = UserDefaults.standard.string(forKey: "djApp") ?? "serato"
        env["DJGRAB_HOOK"] = UserDefaults.standard.string(forKey: "hookTease") == "off" ? "off" : "fits"   // Auto or Off
        env["DJGRAB_OUTRO"] = "0"            // outro versions are off
        env["DJGRAB_SPEED"] = UserDefaults.standard.string(forKey: "workSpeed") ?? "balanced"
        env["DJGRAB_OUTPUT"] = UserDefaults.standard.string(forKey: Keys.output) ?? defaultOutput
        if !isAdmin { env["DJGRAB_PLAIN"] = "1" }     // no renaming, no crates: only cues + intro versions
        p.environment = env
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        buffer = Data()
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            DispatchQueue.main.async { self?.consume(d) }
        }
        p.terminationHandler = { [weak self] proc in
            out.fileHandleForReading.readabilityHandler = nil
            let errText = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.running = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.tryWaiting() }   // queued songs go next
                self.refreshStatus()                     // the folder menu's "x of y done" counts
                self.redoing = nil
                self.etaTimer?.invalidate(); self.etaTimer = nil
                self.proc = nil
                if self.restartPending {
                    self.restartPending = false
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self.start(files: self.lastFiles, quiet: false) }
                    return
                }
                self.stage = ""
                self.refreshStatus()
                // songs that came in while this run was going: start them right after (not when stopped by you)
                if proc.terminationReason != .uncaughtSignal {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.tryWaiting() }
                }
                if proc.terminationStatus != 0 && proc.terminationReason != .uncaughtSignal && !errText.isEmpty {
                    self.status = "Stopped: " + (errText.split(separator: "\n").last.map(String.init) ?? "error")
                } else if self.status.isEmpty || self.status.hasPrefix("Working") || self.status.hasPrefix("Paused") {
                    self.status = proc.terminationReason == .uncaughtSignal ? "Paused" : "Done"
                }
            }
        }
        do {
            try p.run(); proc = p; running = true
            secs = []; songStart = nil; pauseStart = nil       // each batch is timed on its own
            etaTimer?.invalidate()
            etaTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.clock += 1 }
            if files == nil { status = redo ? "Redoing \(targetName)" : "Processing \(targetName)" }
            else if !quiet { status = "Processing \(files!.count) song\(files!.count == 1 ? "" : "s")" }
        } catch { status = "Couldn't start: \(error.localizedDescription)" }
    }

    @Published var userPaused = false          // you pressed Pause: nothing starts on its own until Resume
    private var lastMode = "files"
    func pause() { userPaused = true; proc?.terminate() }
    func resume() {
        userPaused = false
        if let f = lastFiles {
            // the songs still waiting (queue) or the ones from the paused run
            let left = f.filter { FileManager.default.fileExists(atPath: $0) }
            start(files: waiting.isEmpty ? left : Array(Set(waiting + left)), quiet: false, mode: lastMode)
        } else {
            start()
        }
    }

    // Stop and start the same run again right away (picks up engine updates; a redo carries on as a redo)
    private var restartPending = false
    private var lastFiles: [String]? = nil
    var lastFilesPending: Bool { lastFiles != nil }      // the paused run was single songs, not a folder
    func restart() {
        guard running else { return }
        restartPending = true
        status = "Restarting…"
        proc?.terminate()
    }

    @Published var folderStatus: [String: [Int]] = [:]
    @Published var chosenStatus: [String: [Int]] = [:]    // folders picked anywhere on the computer

    /// Counts songs done per folder from the saved progress, so finished folders get a tick.
    func refreshStatus() {
        let py = pythonPath, tool = toolsPath
        for f in recentFolders where FileManager.default.fileExists(atPath: f) {
            DispatchQueue.global(qos: .utility).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: py)
                p.arguments = [tool, "status", "--root", f, "--json"]
                let out = Pipe()
                p.standardOutput = out
                p.standardError = FileHandle.nullDevice
                guard (try? p.run()) != nil else { return }
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                for line in data.split(separator: 0x0A) {
                    guard let e = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                          e["event"] as? String == "status",
                          let d = e["folders"] as? [String: [Int]], let c = d["."] else { continue }
                    DispatchQueue.main.async { self.chosenStatus[f] = c }
                }
            }
        }
        guard FileManager.default.fileExists(atPath: libraryRoot) else { return }
        let root = libraryRoot
        DispatchQueue.global(qos: .utility).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [tool, "status", "--root", root, "--json"]
            let out = Pipe()
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            guard (try? p.run()) != nil else { return }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            for line in data.split(separator: 0x0A) {
                guard let e = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                      e["event"] as? String == "status",
                      let f = e["folders"] as? [String: [Int]] else { continue }
                DispatchQueue.main.async { self.folderStatus = f }
            }
        }
    }

    /// Folders picked with "Choose Folder…", newest first (kept between launches).
    var recentFolders: [String] {
        get { (UserDefaults.standard.string(forKey: "introRecentFolders") ?? "").split(separator: "\n").map(String.init) }
        set { UserDefaults.standard.set(Array(newValue.prefix(8)).joined(separator: "\n"), forKey: "introRecentFolders"); objectWillChange.send() }
    }
    func remember(_ f: String) { recentFolders = [f] + recentFolders.filter { $0 != f } }

    func label(for folder: String) -> String {
        if folder.hasPrefix("/") {
            let u = URL(fileURLWithPath: folder)
            let name = u.lastPathComponent + "  —  " + (u.deletingLastPathComponent().path as NSString).abbreviatingWithTildeInPath
            guard let c = chosenStatus[folder], c.count == 2, c[1] > 0 else { return name }
            if c[0] >= c[1] { return "✓ " + name }
            return name + "  (\(c[0]) of \(c[1]) done)"
        }
        let name = folder.contains("/") ? "      " + folder.replacingOccurrences(of: "/", with: " ▸ ") : folder
        if running && folder == self.folder && lastFiles == nil && total > 0 {      // the folder being run: live count
            return name + "  (\(done) of \(total) done)"
        }
        guard let c = folderStatus[folder.precomposedStringWithCanonicalMapping], c.count == 2, c[1] > 0 else { return name }
        if c[0] >= c[1] { return "✓ " + name }
        return name + "  (\(c[0]) of \(c[1]) done)"          // every folder, even ones not started (0 of n)
    }

    private func consume(_ d: Data) {
        buffer.append(d)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
            if let e = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { handle(e) }
        }
    }

    private func handle(_ e: [String: Any]) {
        let ev = e["event"] as? String ?? ""
        let name = e["name"] as? String ?? ""
        switch ev {
        case "batch":
            total = e["total"] as? Int ?? 0
            done = e["done"] as? Int ?? 0
        case "item_start":
            songStart = Date(); pauseStart = nil
            marks = [(0, 0)]
            etaDoneAt = nil
            current = name; stage = "Starting"; pct = 0           // the song shows as soon as it starts
            currentPath = e["path"] as? String
        case "eta":
            // the engine knows which steps this song still needs and how long each takes on this Mac
            if let d = e["done_at"] as? Double { etaDoneAt = Date(timeIntervalSince1970: d) }
            // (the time estimate comes several times per song: it must not reset the song or its progress)
        case "progress":
            stage = e["stage"] as? String ?? stage
            pct = (e["pct"] as? Double) ?? Double(e["pct"] as? Int ?? 0)
            if let st = songStart, pct > (marks.last?.0 ?? -1) { marks.append((pct, Date().timeIntervalSince(st))) }
        case "paused":
            if pauseStart == nil { pauseStart = Date() }
            let m = (e["message"] as? String ?? "").lowercased()
            status = m.contains("usb") ? "Paused · USB disconnected" : m.contains("serato") ? "Paused · close Serato to continue" : "Paused"
        case "resumed":
            if let ps = pauseStart, let ss = songStart { songStart = ss.addingTimeInterval(Date().timeIntervalSince(ps)) }
            if let ps = pauseStart, let d = etaDoneAt { etaDoneAt = d.addingTimeInterval(Date().timeIntervalSince(ps)) }
            pauseStart = nil
            status = "Processing \(targetName)"
        case "later":
            break                                    // stays on the waiting list until Serato analyses it
        case "done", "skipped", "error":
            // remember how this song's time was spread over its steps (only full intro builds), so the next
            // estimate knows e.g. that "Building the intro" at 80% still has most of the time to go
            if ev == "done", let st = songStart, marks.count >= 3 {
                saveProfile(marks: marks, total: Date().timeIntervalSince(st))
            }
            if let p = e["path"] as? String, let i = waiting.firstIndex(of: p) { waiting.remove(at: i); saveWaiting() }
            if let ss = songStart {                  // every song counts, quick skips included
                secs.append(Date().timeIntervalSince(ss)); songStart = nil
            } else if ev != "done" { secs.append(0.2) }
            if secs.count >= 3 {
                let r = secs.suffix(30)
                UserDefaults.standard.set(r.reduce(0, +) / Double(r.count), forKey: "introsAvgSecs")
            }
            if ev != "error" || e["name"] != nil { done += 1 }
            var text: String
            if ev == "done" {
                var parts = [(e["cues"] as? [String] ?? []).joined(separator: " · ")]
                if e["intro"] != nil {
                    let ic = (e["intro_cues"] as? [String] ?? []).joined(separator: " · ")
                    parts.append(ic.isEmpty ? "Intro version" : "Intro version: \(ic)")
                }
                if let n = e["note"] as? String { parts.append(n) }
                text = parts.filter { !$0.isEmpty }.joined(separator: "   —   ")
            } else {
                text = e["message"] as? String ?? ev
            }
            results.insert(ToolResult(name: name.isEmpty ? "Library" : name, text: text, ok: ev == "done",
                                      path: e["intro"] as? String, madeIntro: e["intro"] != nil,
                                      song: e["path"] as? String), at: 0)
            if results.count > 200 { results.removeLast() }
            if ev == "error" && e["name"] == nil { status = text }
        case "finished":
            let now = Date()
            let t = DateFormatter(); t.dateFormat = "h:mm a"
            let d = DateFormatter(); d.dateFormat = "MMM d, h:mm a"
            status = "Completed at \(t.string(from: now))"
            lastRun = "\(runLabel) · completed \(d.string(from: now))"
            UserDefaults.standard.set(lastRun, forKey: "introsLastRun")
        default: break
        }
    }
}

/// Jokes for the empty pages: a new pair every time the app opens and after Clear, never the same
/// line on both pages at once.
final class EmptyJoke: ObservableObject {
    static let shared = EmptyJoke()
    static let lines = [
        "Quieter in here than a DJ who forgot the aux cord.",
        "This dance floor is emptier than the 8 a.m. set.",
        "Nothing here yet. Even the bass dropped out.",
        "So empty, you can hear the crowd waiting for the drop.",
        "No songs yet. This is the part where the DJ looks busy.",
        "Dead air. Every DJ's worst nightmare.",
        "Emptier than the dance floor during the slow song.",
        "Silence. Somebody check if the needle's on the record.",
        "The crowd is waiting. No pressure.",
        "Emptier than the requests box at a wedding.",
        "Nothing playing. Even the lights stopped flashing.",
        "Track list: blank. Vibes: pending.",
    ]
    @Published var grab = ""
    @Published var intros = ""
    init() { shuffle() }
    func shuffle() {
        let pick = Array(EmptyJoke.lines.filter { $0 != grab && $0 != intros }.shuffled().prefix(2))
        grab = pick[0]; intros = pick[1]
    }
}

/// Seconds as words, to the nearest 10 seconds: "Less than 10 seconds", "About 40 seconds",
/// "About 1 minute 20 seconds", "About 2 hours 5 minutes"
func spokenTime(_ s: Double) -> String {
    if s < 10 { return "Less than 10 seconds" }
    let r = Int((s / 10).rounded()) * 10
    if r < 60 { return "About \(r) seconds" }
    if r < 3600 {
        let m = r / 60, sec = r % 60
        return "About \(m) minute\(m == 1 ? "" : "s")" + (sec > 0 ? " \(sec) seconds" : "")
    }
    let h = r / 3600, m = (r % 3600) / 60
    return "About \(h) hour\(h == 1 ? "" : "s")" + (m > 0 ? " \(m) minute\(m == 1 ? "" : "s")" : "")
}

/// "Song.mp3" -> "Song (Intro).mp3", if it exists
func introFile(_ song: String?) -> String? {
    guard let s = song, s.lowercased().hasSuffix(".mp3") else { return nil }
    let ip = String(s.dropLast(4)) + " (Intro).mp3"
    return FileManager.default.fileExists(atPath: ip) ? ip : nil
}

/// The menu bar icon: the logo's claw holding a record, drawn as a template so it matches the menu bar
/// (white in dark mode, black in light). While songs download, the record fills in.
func menuBarIcon(busy: Bool) -> NSImage {
    let img = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
        NSColor.black.set()
        // cable and claw head
        NSBezierPath(rect: NSRect(x: 8.4, y: 0, width: 1.2, height: 3)).fill()
        NSBezierPath(roundedRect: NSRect(x: 6, y: 2.6, width: 6, height: 2.4), xRadius: 1, yRadius: 1).fill()
        // record
        let rec = NSBezierPath(ovalIn: NSRect(x: 4.5, y: 7.5, width: 9, height: 9))
        if busy { rec.fill() } else { rec.lineWidth = 1.3; rec.stroke() }
        let label = NSBezierPath(ovalIn: NSRect(x: 7.6, y: 10.6, width: 2.8, height: 2.8))
        if busy { NSGraphicsContext.current?.compositingOperation = .clear }
        label.fill()
        NSGraphicsContext.current?.compositingOperation = .sourceOver
        // arms gripping the record's sides
        let arms = NSBezierPath()
        arms.move(to: NSPoint(x: 6.6, y: 4.6)); arms.line(to: NSPoint(x: 2.2, y: 8.6)); arms.line(to: NSPoint(x: 2.2, y: 12.8)); arms.line(to: NSPoint(x: 4.4, y: 15))
        arms.move(to: NSPoint(x: 11.4, y: 4.6)); arms.line(to: NSPoint(x: 15.8, y: 8.6)); arms.line(to: NSPoint(x: 15.8, y: 12.8)); arms.line(to: NSPoint(x: 13.6, y: 15))
        arms.lineWidth = 1.5
        arms.lineCapStyle = .round; arms.lineJoinStyle = .round
        arms.stroke()
        return true
    }
    img.isTemplate = true
    return img
}

/// Intros & Cues ▸ search one song in your library and run just that song (no folder run). If it already
/// has an intro version, Run makes a different one (the old one goes to _To Delete).
/// On the Grab page: shows when Intros & Cues is working in the background (auto-process after a download)
struct IntroActivity: View {
    @ObservedObject private var tools = ToolsStore.shared
    var body: some View {
        if tools.running {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(tools.current.isEmpty ? "Starting: \(tools.runLabel.isEmpty ? "loading the tools" : tools.runLabel)…"
                         : "Making intro & cues: \(tools.current)" + (tools.songEta.isEmpty ? "" : " · \(tools.songEta)"))
                        .font(.caption).foregroundStyle(Theme.current.accent2).lineLimit(1).truncationMode(.middle)
                }
                if tools.current.isEmpty || tools.pct < 16 { ProgressView().progressViewStyle(.linear) }
                else { ProgressView(value: min(tools.pct, 100), total: 100) }
            }
            .help("Intros & Cues is working on this song in the background")
        }
    }
}

/// The songs in a folder, to listen to (and to their intro versions) without running the folder
struct FolderSongs: View {
    let folder: String
    @ObservedObject private var tools = ToolsStore.shared
    @State private var songs: [String] = []
    var root: String {
        if folder.hasPrefix("/") { return folder }
        let base = isAdmin ? tools.libraryRoot : Store.shared.outputFolder
        return folder.isEmpty ? base : base + "/" + folder
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(songs.count) songs in \(folder.isEmpty ? "your library" : folder)").font(.caption).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(songs, id: \.self) { p in
                        HStack {
                            VStack(alignment: .leading, spacing: 0) {
                                Text(((p as NSString).lastPathComponent as NSString).deletingPathExtension).lineLimit(1)
                                Text(((p as NSString).deletingLastPathComponent as NSString).lastPathComponent)
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            VStack(spacing: 1) { PlayButton(path: p); Text("Original").font(.system(size: 9)).foregroundStyle(.secondary) }
                            if let ip = introFile(p) {
                                VStack(spacing: 1) { PlayButton(path: ip); Text("Intro").font(.system(size: 9)).foregroundStyle(.secondary) }
                                Button { tools.noIntro(song: p, intro: ip); load() } label: {
                                    VStack(spacing: 1) {
                                        Image(systemName: "arrow.uturn.backward.circle").font(.system(size: 18))
                                        Text("Use original").font(.system(size: 9))
                                    }
                                }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                                .help("Remove this song's intro version (moved to _To Delete) and keep only the original")
                            }
                        }
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        SeekBar(path: p).padding(.horizontal, 8)
                        if let ip = introFile(p) { SeekBar(path: ip).padding(.horizontal, 8) }
                    }
                }
            }
            .frame(maxHeight: 260)
        }
        .onAppear(perform: load)
        .onChange(of: folder) { _ in load() }
    }
    private func load() {
        let r = root
        DispatchQueue.global().async {
            var out: [String] = []
            if let en = FileManager.default.enumerator(atPath: r) {
                for case let f as String in en {
                    let name = (f as NSString).lastPathComponent
                    if f.hasPrefix("_") || f.contains("/_") || name.hasPrefix(".") { continue }
                    if !name.lowercased().hasSuffix(".mp3") || name.hasSuffix(" (Intro).mp3") || name.hasSuffix(" (Outro).mp3") { continue }
                    out.append(r + "/" + f)
                }
            }
            out.sort { ($0 as NSString).lastPathComponent.localizedCaseInsensitiveCompare(($1 as NSString).lastPathComponent) == .orderedAscending }
            DispatchQueue.main.async { songs = out }
        }
    }
}

struct SongSearch: View {
    @ObservedObject private var tools = ToolsStore.shared
    @State private var q = ""
    @State private var hits: [String] = []
    @State private var all: [String] = []
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Find a song in your library to run…", text: $q)
                    .textFieldStyle(.plain)
                    .onChange(of: q) { _ in search() }
                if !q.isEmpty { Button { q = ""; hits = [] } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.borderless).foregroundStyle(.secondary) }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.current.panel))
            ForEach(hits, id: \.self) { p in
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(((p as NSString).lastPathComponent as NSString).deletingPathExtension).lineLimit(1)
                        Text(((p as NSString).deletingLastPathComponent as NSString).lastPathComponent)
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    PlayButton(path: p).help("Play the song")
                    if let ip = introFile(p) { PlayButton(path: ip).help("Play its intro version").opacity(0.7) }
                    let has = introFile(p) != nil
                    Button(tools.running ? (tools.waiting.contains(p) ? "Queued" : "Queue") : (has ? "Redo intro" : "Run")) {
                        if has && !tools.running { tools.start(files: [p], quiet: false, mode: "another") } else { tools.start(file: p) }
                        q = ""; hits = []
                    }
                    .disabled(tools.waiting.contains(p))
                    .help(tools.running ? "Add this song to the list: it runs as soon as the current one is done"
                          : has ? "It has an intro version: make a different one (the old one goes to _To Delete)"
                              : "Beatgrid, cues and (if it needs one) an intro for this song only")
                }
                .padding(.horizontal, 8)
                SeekBar(path: p).padding(.horizontal, 8)
                if let ip = introFile(p) { SeekBar(path: ip).padding(.horizontal, 8) }
            }
        }
    }
    private func search() {
        let words = q.lowercased().split(separator: " ").map(String.init).filter { $0.count >= 2 }
        guard !words.isEmpty else { hits = []; return }
        if all.isEmpty {                      // list the library once (songs only, not intros or _To Delete)
            let root = tools.libraryRoot
            var out: [String] = []
            if let en = FileManager.default.enumerator(atPath: root) {
                for case let f as String in en {
                    let l = f.lowercased()
                    if l.hasSuffix(".mp3") && !l.contains("(intro)") && !l.contains("_to delete") && !l.hasPrefix("._") && !l.contains("/._") {
                        out.append(root + "/" + f)
                    }
                }
            }
            all = out
        }
        hits = Array(all.filter { p in
            let n = (p as NSString).lastPathComponent.lowercased()
            return words.allSatisfy { n.contains($0) }
        }.prefix(6))
    }
}

struct ToolsView: View {
    @EnvironmentObject var store: Store
    @ObservedObject private var jokes = EmptyJoke.shared
    @AppStorage("uiLook") private var look = "neon"
    @StateObject private var tools = ToolsStore.shared
    @StateObject private var ratings = RatingStore.shared
    @State private var noteFor: ToolResult? = nil
    @AppStorage("makeIntros") private var makeIntros = isAdmin      // others: off until they turn it on
    @AppStorage("autoIntros") private var autoIntros = true
    @AppStorage("introFolder") private var folder = ""
    @State private var browsing = false
    @State private var choices: [String] = []
    @State private var lastFolder = ""
    @State private var targeted = false
    @State private var showInfo = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Intros & Cue Points").font(.title3.weight(.semibold))
                Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                    .buttonStyle(.borderless)
                    .popover(isPresented: $showInfo, arrowEdge: .bottom) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("• Cues fill pads 1–7 in song order; pad 8 stays free.")
                            Text("• Intro versions only for songs with vocals in the first 8 bars.")
                            Text("• Start on a ✓ folder redoes it.")
                            Text("• Pauses while Serato is open or the USB is unplugged.")
                        }
                        .font(.callout)
                        .padding(14)
                    }
            }
            HStack(spacing: 16) {
                Toggle("Make intro versions", isOn: $makeIntros)
                    .help("Songs that start with vocals get a copy with a DJ-style beat intro added, so you can mix into them. Saved next to the song as \"(Intro)\".")
                Toggle("Auto-process new downloads", isOn: $autoIntros)
                    .help("Each song you download gets its beat grid, cue points and (if needed) an intro version on its own, without starting a folder run.")
                if tools.notStarted > 0 {
                    Text("\(tools.notStarted) queued").font(.caption).foregroundStyle(.orange)
                }
            }
            HStack {
                Picker("Folder", selection: $folder) {
                    if !choices.isEmpty {
                        Text("Entire Library").tag("")
                        ForEach(choices, id: \.self) { c in
                            Text(tools.label(for: c)).tag(c)
                        }
                    }
                    if !tools.recentFolders.isEmpty {
                        Divider()
                        ForEach(tools.recentFolders, id: \.self) { f in
                            Text(tools.label(for: f)).tag(f)
                        }
                    }
                    if folder.hasPrefix("/") && !tools.recentFolders.contains(folder) {
                        Text(tools.label(for: folder)).tag(folder)
                    }
                    Divider()
                    Text("Choose Folder…").tag("__choose__")
                }
                .frame(minWidth: 150, maxWidth: 360)        // gives up room when more buttons show (Paused), so the window never overflows
                .layoutPriority(-1)
                .disabled(tools.running)
                .onChange(of: folder) { v in
                    guard v == "__choose__" else { lastFolder = v; return }
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true
                    panel.canChooseFiles = false
                    panel.allowsMultipleSelection = false
                    panel.prompt = "Choose"
                    panel.message = "Pick a folder of songs. DJ Grab works on the songs in it and in its subfolders."
                    if panel.runModal() == .OK, let u = panel.url {
                        tools.remember(u.path)
                        folder = u.path
                        tools.refreshStatus()
                    } else {
                        folder = lastFolder
                    }
                }
                if tools.running {
                    Button { tools.restart() } label: { Image(systemName: "arrow.clockwise") }
                        .fixedSize()
                        .help("Restart this run (picks up updates, carries on where it left off)")
                    Button("Pause") { tools.pause() }.fixedSize()
                } else {
                    // Resume carries on the paused folder, so it only shows while that folder is the one picked;
                    // pick another folder and Start runs that one instead
                    let resumable = tools.userPaused && (tools.lastFilesPending || tools.folder == folder)
                    if resumable {
                        Button { tools.resume() } label: { Label("Resume", systemImage: "play.fill") }
                            .buttonStyle(.borderedProminent).fixedSize()
                            .help("Carry on with the paused run where it left off")
                    }
                    if resumable {
                        Button(tools.isComplete(folder) ? "Redo" : "Start") { tools.folder = folder; tools.start() }
                            .fixedSize()
                    } else {
                        Button(tools.isComplete(folder) ? "Redo" : "Start") { tools.folder = folder; tools.start() }
                            .buttonStyle(.borderedProminent).fixedSize()
                    }
                }
                if !tools.running && !tools.isComplete(folder) {
                    Button("Redo all") { tools.folder = folder; tools.start(files: nil, quiet: false, forceRedo: true) }
                        .fixedSize()
                        .help("Run every song in this folder again with the newest rules, including songs already done (old intros that aren't needed anymore move to _To Delete)")
                }
                Button(browsing ? "Hide songs" : "Songs") { browsing.toggle() }
                    .fixedSize()
                    .help("List the songs in this folder to listen to them (and their intros) without running anything")
                if tools.total > 0 {
                    Text("\(tools.done) / \(tools.total)").foregroundStyle(.secondary).monospacedDigit().fixedSize()
                }
                Spacer(minLength: 4)
            }
            if !tools.eta.isEmpty {                 // its own line, so it's never squeezed or cut off
                Text(tools.eta).font(.caption).foregroundStyle(.secondary)
            }
            if tools.running && !tools.current.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text(tools.current).font(.body.weight(.medium)).lineLimit(1)
                    if tools.pct < 16 { ProgressView().progressViewStyle(.linear) }
                    else { ProgressView(value: min(tools.pct, 100), total: 100) }
                    HStack {
                        Text(tools.stage).font(.caption).foregroundStyle(.secondary)
                        if let cp = tools.currentPath, makeIntros {
                            Button("No intro for this song") { tools.noIntro(song: cp) }
                                .buttonStyle(.borderless).font(.caption)
                                .help("Skip the intro version for this song (now and in future runs). It still gets cue points.")
                        }
                        Spacer()
                        if tools.running { ProgressView().controlSize(.small) }
                        Text(tools.songEta).font(.caption.weight(.medium)).foregroundStyle(Theme.current.accent2)
                    }
                }
            }
            if !tools.status.isEmpty {
                Text(tools.status).font(.callout).foregroundStyle(tools.status.hasPrefix("Paused") ? Color.orange : Color.secondary)
            }
            if tools.current.isEmpty { IntroActivity() }   // only before the song card shows (tools loading)
            if !tools.waiting.isEmpty {
                Text("Up next: " + tools.waiting.map { (($0 as NSString).lastPathComponent as NSString).deletingPathExtension }.joined(separator: ", "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            SongSearch()
            if browsing { FolderSongs(folder: folder) }
            Text("Drop an MP3 to process one song")
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 34)
                .background(RoundedRectangle(cornerRadius: 8).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5]))
                    .foregroundStyle(targeted ? Theme.current.accent : (Theme.current.look == .neon ? Theme.current.accent2.opacity(0.5) : Color.secondary.opacity(0.4))))
                .onDrop(of: [UTType.fileURL], isTargeted: $targeted) { providers in
                    guard let p = providers.first, p.canLoadObject(ofClass: URL.self) else { return false }
                    _ = p.loadObject(ofClass: URL.self) { url, _ in
                        guard let u = url, u.pathExtension.lowercased() == "mp3" else { return }
                        DispatchQueue.main.async { tools.start(file: u.path) }
                    }
                    return true
                }
            if !tools.results.isEmpty {
                HStack {
                    Text("\(tools.results.count) result\(tools.results.count == 1 ? "" : "s")")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Clear") { tools.results.removeAll() }
                        .buttonStyle(.borderless)
                        .font(.caption)
                        .disabled(tools.running)
                        .help("Clear this list (doesn't touch your songs)")
                        .sheet(item: $noteFor) { r in
                            RateNote(path: r.path ?? "", name: r.name,
                                     song: r.song ?? (r.path ?? "").replacingOccurrences(of: " (Intro).mp3", with: ".mp3"))
                        }
                }
            }
            List(tools.results) { r in
                HStack(alignment: .top) {
                    Image(systemName: r.icon.0)
                        .foregroundStyle(r.icon.1)
                        .help(r.icon.2)
                    VStack(alignment: .leading) {
                        Text(r.name).lineLimit(1)
                        Text(r.text).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        if r.madeIntro, let ip = r.path { SeekBar(path: ip) }
                        if let song = r.song ?? r.path?.replacingOccurrences(of: " (Intro).mp3", with: ".mp3"), song != r.path {
                            SeekBar(path: song)                  // the original playing: its time bar too
                        }
                        if r.madeIntro, let ip = r.path, ratings.ratings[ip] == -1 {
                            RatingComment(path: ip, name: r.name)
                        }
                    }
                    if !r.madeIntro, let song = r.song, FileManager.default.fileExists(atPath: song) {
                        Spacer()
                        PlayButton(path: song).help("Play the song (it didn't need an intro)")
                        if r.ok && !song.lowercased().contains("intro") {
                            Button { tools.makeIntro(song: song) } label: {
                                VStack(spacing: 1) {
                                    Image(systemName: "plus.circle").font(.system(size: 18))
                                    Text("Make intro").font(.system(size: 9))
                                }
                            }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                            .help("DJ Grab thought this song didn't need an intro. Make one anyway (and always for this song)")
                        }
                    }
                    if r.madeIntro && r.path != nil {
                        Spacer()
                        if let song = r.song ?? r.path?.replacingOccurrences(of: " (Intro).mp3", with: ".mp3"),
                           FileManager.default.fileExists(atPath: song) {
                            VStack(spacing: 1) {
                                PlayButton(path: song)
                                Text("Original").font(.system(size: 9)).foregroundStyle(.secondary)
                            }
                            .help("Play the original song, to compare")
                        }
                        if let ip = r.path, FileManager.default.fileExists(atPath: ip) {
                            VStack(spacing: 1) {
                                PlayButton(path: ip)
                                Text("Intro").font(.system(size: 9)).foregroundStyle(.secondary)
                            }
                            .help("Play the intro version from the start")
                        }
                        if let ip = r.path, let song = r.song ?? Optional(ip.replacingOccurrences(of: " (Intro).mp3", with: ".mp3")) {
                            Button { tools.noIntro(song: song, intro: ip) } label: {
                                VStack(spacing: 1) {
                                    Image(systemName: "arrow.uturn.backward.circle").font(.system(size: 18))
                                    Text("Use original").font(.system(size: 9))
                                }
                            }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                            .help("Don't use an intro for this song: the intro version moves to _To Delete (nothing is deleted) and this song won't get one again")
                        }
                        let v = ratings.ratings[r.path!] ?? 0
                        Button { withAnimation(.spring(response: 0.3, dampingFraction: 0.6)) { ratings.rate(r, 1) } } label: {
                            Text("👍").font(.system(size: 22))
                                .saturation(v == 1 ? 1 : 0.35).opacity(v == 1 ? 1 : 0.7)
                                .scaleEffect(v == 1 ? 1.15 : 1)
                                .padding(4)
                                .background(Circle().fill(v == 1 ? Color.green.opacity(0.25) : Color.clear))
                        }
                        .buttonStyle(.plain)
                        .help("Good intro")
                        if v == -1 {
                            // 👎 slides over into a redo button: makes a different intro that still fits
                            Button { tools.another(r) } label: {
                                Label(tools.redoing == r.path ? "Making…" : "Try another", systemImage: "arrow.triangle.2.circlepath")
                                    .font(.caption.weight(.semibold))
                                    .padding(.horizontal, 10).padding(.vertical, 5)
                                    .foregroundStyle(.white)
                                    .background(Capsule().fill(Color.red.opacity(0.85)))
                            }
                            .buttonStyle(.plain)
                            .disabled(tools.running)
                            .help(tools.running ? "Wait for the current run to finish" : "Make a different intro for this song (the old one goes to _To Delete)")
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                            .contextMenu { Button("Undo 👎") { ratings.rate(r, -1) } }
                        } else {
                            Button { withAnimation(.spring(response: 0.35, dampingFraction: 0.75)) { ratings.rate(r, -1) }; noteFor = r } label: {
                                Text("👎").font(.system(size: 22))
                                    .saturation(0.35).opacity(0.7)
                                    .padding(4)
                            }
                            .buttonStyle(.plain)
                            .help("Bad intro: turns into a Try another button")
                            .transition(.opacity)
                        }
                    }
                }
                .onDrag {
                    if let p = r.path { return NSItemProvider(object: URL(fileURLWithPath: p) as NSURL) }
                    return NSItemProvider()
                }
                .contextMenu {
                    if r.madeIntro, let song = r.song {
                        Button("Remove intro (this song doesn't need one)") { tools.noIntro(song: song, intro: r.path) }
                    }
                }
            }
            .listStyle(.inset(alternatesRowBackgrounds: Theme.current.look != .neon))
            .scrollContentBackground(Theme.current.look != .neon ? .automatic : .hidden)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.current.panel))
            .overlay {
                if tools.results.isEmpty && !tools.running {
                    VStack(spacing: 8) {
                        Image(systemName: "hifispeaker.2").font(.system(size: 30)).foregroundStyle(.tertiary)
                        Text(jokes.intros).font(Theme.current.font(14, .medium)).foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Text("Pick a folder and press Start to make some intros.")
                            .font(Theme.current.font(12)).foregroundStyle(.tertiary)
                    }
                    .padding(30)
                }
            }
            HStack {
                Text("v\(shownVersion(appVersion))").foregroundStyle(.tertiary).font(.caption)
                if !tools.lastRun.isEmpty {
                    Text("Last run: \(tools.lastRun)").foregroundStyle(.secondary).font(.caption).lineLimit(1)
                        .padding(.leading, 8)
                }
                Spacer()
                if store.driveName != nil {
                    if store.driveConnected && !usbFree(store.driveName).isEmpty {
                        Text(usbFree(store.driveName)).foregroundStyle(Color.blue).lineLimit(1)
                    }
                    Label(store.driveConnected ? (store.driveName ?? "USB") : "\(store.driveName ?? "USB") not connected",
                          systemImage: store.driveConnected ? "externaldrive.fill.badge.checkmark" : "externaldrive.badge.xmark")
                        .foregroundStyle(store.driveConnected ? Color.green : Color.red)
                        .lineLimit(1)
                    if store.driveConnected {
                        Button { store.eject() } label: {
                            if store.ejecting { ProgressView().controlSize(.small) } else { Image(systemName: "eject.fill") }
                        }
                        .buttonStyle(.borderless)
                        .help("Eject \(store.driveName ?? "USB")")
                        .disabled(store.ejecting)
                    }
                }
            }
            .font(.callout)
        }
        .padding(16)
        .onAppear { choices = tools.folderChoices(); lastFolder = folder; tools.refreshStatus() }
    }
}

struct UpdateBar: View {
    @EnvironmentObject var store: Store
    @ObservedObject var tools = ToolsStore.shared
    var blocked: Bool { store.busy || tools.running }
    var body: some View {
        if store.updating {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Installing update…")
                    .font(.callout)
                Spacer()
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.12)))
        } else if store.updateAvailable {
            HStack {
                Image(systemName: "arrow.down.app.fill").foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 1) {
                    Text(store.updatePreparing ? "Update v\(shownVersion(store.availableVersion)) is still being prepared…"
                                               : "Update v\(shownVersion(store.availableVersion)) is ready").font(.callout.weight(.semibold))
                    Text("You have v\(shownVersion(appVersion))").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Install update") { store.installUpdate() }
                    .buttonStyle(.borderedProminent)
                    .disabled(blocked || store.updatePreparing)
                    .help(store.updatePreparing ? "Wait a moment: the update is still being added" :
                          store.busy ? "Finish downloads first" : tools.running ? "Pause Intros & Cues first" : "Takes about a minute")
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.12)))
        }
    }
}

/// While intros or downloads are running, the Mac doesn't go to sleep on its own (the screen still can).
/// Settings ▸ General ▸ "Keep the computer awake while working", on by default.
enum KeepAwake {
    private static var token: NSObjectProtocol?
    static func update(busy: Bool) {
        let want = busy && (UserDefaults.standard.object(forKey: "preventSleep") as? Bool ?? true)
        if want && token == nil {
            token = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled, .userInitiated],
                                                          reason: "DJ Grab is working on your songs")
        } else if !want, let t = token {
            ProcessInfo.processInfo.endActivity(t)
            token = nil
        }
    }
}

struct StopSoundButton: View {
    @ObservedObject private var preview = Preview.shared
    var body: some View {
        if preview.playing != nil {
            Button { preview.stop() } label: {
                Label("Stop", systemImage: "stop.fill")
                    .font(.system(size: 12, weight: .semibold))
            }
            .buttonStyle(.borderedProminent)
            .tint(.pink)
            .help("Stop the song that's playing: " + ((preview.playing! as NSString).lastPathComponent as NSString).deletingPathExtension)
        }
    }
}

struct RootView: View {
    @EnvironmentObject var store: Store
    @State private var showSettings = false
    @State private var showWelcome = !isAdmin && !UserDefaults.standard.bool(forKey: "welcomed")
    @ObservedObject private var setup = SetupStore.shared
    @AppStorage("uiLook") private var look = "neon"
    /// The gear in a thin, subtle circle (no pill)
    var gearButton: some View {
        Button { showSettings = true } label: {
            Image(systemName: "gearshape")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 26, height: 26)
                .overlay(Circle().strokeBorder(Color.secondary.opacity(0.35), lineWidth: 1))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help("Settings (⌘,)")
    }

    var body: some View {
        VStack(spacing: 0) {
            UpdateBar()
                .padding(.horizontal, 16)
                .padding(.top, store.updateAvailable || store.updating ? 12 : 0)
            TabView {
                ContentView()
                    .tabItem { Label("Grab", systemImage: "arrow.down.circle") }
                ToolsView()
                    .tabItem { Label("Intros & Cues", systemImage: "waveform") }
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {   // a song preview is playing: stop it from anywhere
                    StopSoundButton()
                }
                .hiddenGlass()
                ToolbarItem(placement: .primaryAction) {   // settings in the title bar, top right (the Mac spot)
                    gearButton
                }
                .hiddenGlass()
            }
        }
        .looked()
        .frame(minWidth: 680, minHeight: 560)
        .sheet(isPresented: Binding(get: { setup.needed }, set: { _ in })) {
            SetupView().looked()
        }
        .sheet(isPresented: Binding(get: { showWelcome && !setup.needed }, set: { showWelcome = $0 })) {
            WelcomeView { showWelcome = false }.looked()
        }
        .sheet(isPresented: $showSettings) {
            // the same settings as DJ Grab ▸ Settings… (⌘,), opened right over the window
            VStack(spacing: 0) {
                SettingsView()
                HStack {
                    Spacer()
                    Button("Done") { showSettings = false }
                        .keyboardShortcut(.defaultAction)
                }
                .padding(12)
            }
            .looked()
        }
    }
}

@main
struct DJGrabApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var store = Store.shared
    @AppStorage("menuBarIcon") private var menuBar = true

    var body: some Scene {
        Window("DJ Grab", id: "main") {
            RootView().environmentObject(store)
        }
        .windowResizability(.contentMinSize)
        Settings {
            SettingsView().looked()
        }
        MenuBarExtra(isInserted: $menuBar) {
            MiniGrab().environmentObject(store)
        } label: {
            Image(nsImage: menuBarIcon(busy: store.busy))
        }
        .menuBarExtraStyle(.window)
    }
}
