import AppKit
import AVFoundation
import SwiftUI

@MainActor
final class PetModel: ObservableObject {
    @Published private(set) var focus = PetFocus()
    @Published private(set) var notices: [PetNotice] = []
    @Published private(set) var unseen = 0
    @Published private(set) var seenThrough = 0
    @Published private(set) var status: String?
    @Published private(set) var snoozed = false
    @Published private(set) var reactionSince = Date()
    @Published private(set) var speechMode: SpeechMode
    @Published var showingActivity = false

    let api = PetAPI()
    private let defaults = UserDefaults.standard
    private let synth = AVSpeechSynthesizer()
    private var loop: Task<Void, Never>?
    private var cursor: Int?
    private var tasks: [Int: Reaction]?
    private var snoozeUntil = Date.distantPast
    private var lastSpeech = Date.distantPast
    private var pending = [SpeechEvent]()
    private var speechIDs = [Int: Date]()
    private var connectionFailures = 0
    private var greeted = false

    init() {
        speechMode = SpeechMode(rawValue: UserDefaults.standard.string(forKey: "petSpeech") ?? "") ?? .silent
    }

    private var cursorKey: String { "petCursor.\(api.base.port ?? 8321)" }

    func start() {
        guard loop == nil else { return }
        reactionSince = Date()
        focus = PetFocus(kind: .appeared)
        loop = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(1200))
            while let self, !Task.isCancelled {
                await self.poll()
                let delay = self.connectionFailures == 0 ? 5 : min(60, 5 * self.connectionFailures)
                try? await Task.sleep(for: .seconds(delay))
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
        cursor = nil
        tasks = nil
        speechIDs = [:]
        greeted = false
        notices = []
        focus = PetFocus()
        unseen = 0
        showingActivity = false
        stopSpeech()
    }

    func setSpeech(_ mode: SpeechMode) {
        speechMode = mode
        defaults.set(mode.rawValue, forKey: "petSpeech")
        stopSpeech()
    }

    func snooze() {
        snoozed.toggle()
        snoozeUntil = snoozed ? Date().addingTimeInterval(15 * 60) : .distantPast
        stopSpeech()
    }

    func openCurrent() {
        open(notice: focus.notice, task: focus.task)
    }

    func open(notice: PetNotice? = nil, task: PetTask? = nil) {
        NSWorkspace.shared.open(api.destination(notice: notice, task: task))
    }

    private func stopSpeech() {
        pending.removeAll()
        synth.stopSpeaking(at: .immediate)
    }

    private func merge(_ items: [PetNotice]) {
        var merged = Dictionary(uniqueKeysWithValues: notices.map { ($0.id, $0) })
        for item in items { merged[item.id] = item }
        notices = Array(merged.values.sorted { $0.id > $1.id }.prefix(300))
    }

    private func poll() async {
        do {
            let now = Date()
            snoozed = now < snoozeUntil
            var page = try await api.activity(after: cursor)
            try Task.checkCancellation()
            let firstPoll = cursor == nil
            if firstPoll {
                let previous = defaults.object(forKey: cursorKey) as? Int
                if let previous, previous >= 0, previous < page.latest {
                    merge(page.items)
                    page = try await api.activity(after: previous)
                    try Task.checkCancellation()
                }
            } else if let cursor, cursor > page.latest {
                notices = []
                tasks = nil
            }
            while true {
                merge(page.items)
                cursor = page.cursor
                if page.cursor >= page.latest { break }
                page = try await api.activity(after: page.cursor)
                try Task.checkCancellation()
            }
            defaults.set(page.cursor, forKey: cursorKey)
            speechIDs = speechIDs.filter { now.timeIntervalSince($0.value) < 120 }
            var reactions = [SpeechEvent]()
            for event in page.speech where speechIDs[event.id] == nil {
                speechIDs[event.id] = now
                // Catch-up after a start or reconnect updates the list silently.
                if !firstPoll && connectionFailures == 0 { reactions.append(event) }
            }
            let stamp = now.timeIntervalSince1970
            if let tasks, connectionFailures == 0 {
                for task in page.tasks where task.kind.isOngoing && tasks[task.id] != task.kind
                    && task.changedAt >= stamp - 60 {
                    reactions.append(SpeechEvent(id: -task.id, kind: task.kind))
                }
            }
            if !greeted {
                greeted = true
                if speechMode == .talkative && reactions.isEmpty {
                    reactions.append(SpeechEvent(id: 0, kind: .appeared))
                }
            }
            tasks = Dictionary(uniqueKeysWithValues: page.tasks.map { ($0.id, $0.kind) })
            unseen = page.unseen
            seenThrough = page.seenThrough
            let next = PetFocus.choose(notices: notices, tasks: page.tasks, now: stamp, seenThrough: seenThrough)
            if focus.kind != next.kind || focus.sourceID != next.sourceID {
                reactionSince = Date()
            }
            focus = next
            status = nil
            connectionFailures = 0
            speak(reactions, at: Date(), available: page.speechAvailable, eligible: Set(page.speech.map(\.id)))
        } catch {
            guard !Task.isCancelled else { return }
            status = (error as? PetAPI.Problem)?.errorDescription ?? "Waiting for Asta"
            focus = PetFocus()
            unseen = 0
            notices = []
            cursor = nil
            tasks = nil
            connectionFailures = min(connectionFailures + 1, 12)
            stopSpeech()
        }
    }

    private func speak(_ reactions: [SpeechEvent], at now: Date, available: Bool, eligible: Set<Int>) {
        guard available, !snoozed, speechMode != .silent else {
            stopSpeech()
            return
        }
        let sinceLast = now.timeIntervalSince(lastSpeech)
        if sinceLast > 60 { pending.removeAll() }
        pending.removeAll { $0.id > 0 && !eligible.contains($0.id) }
        pending += reactions.filter { speechMode.includes($0.kind) }
        guard sinceLast >= 15, !synth.isSpeaking,
              let important = pending.max(by: { $0.kind.priority < $1.kind.priority }) else { return }
        let extra = pending.count - 1
        let summary = extra == 1 ? " There is one more update." : " There are \(extra) more updates."
        let line = important.kind.line + (extra > 0 ? summary : "")
        pending.removeAll()
        lastSpeech = now
        synth.speak(AVSpeechUtterance(string: line))
    }
}

private struct PetView: View {
    @ObservedObject var model: PetModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let hide: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Button(action: model.openCurrent) {
                    AstaSprite(kind: model.focus.kind, started: model.reactionSince,
                               still: reduceMotion || model.snoozed)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Open in Asta")
                VStack(alignment: .leading, spacing: 7) {
                    Text(model.status ?? model.focus.kind.label).font(.headline).lineLimit(2)
                    Button { model.showingActivity.toggle() } label: {
                        Label(model.unseen == 0 ? "Activity" : "Activity (\(model.unseen))",
                              systemImage: "bell")
                    }
                    .popover(isPresented: $model.showingActivity, arrowEdge: .bottom) {
                        activity
                    }
                }
                Spacer(minLength: 0)
                Menu {
                    ForEach(SpeechMode.allCases, id: \.self) { mode in
                        Button { model.setSpeech(mode) } label: {
                            if model.speechMode == mode { Label(mode.rawValue, systemImage: "checkmark") }
                            else { Text(mode.rawValue) }
                        }
                    }
                    Divider()
                    Button(model.snoozed ? "Resume pet" : "Snooze for 15 minutes", action: model.snooze)
                    Button("Hide", action: hide)
                } label: {
                    Image(systemName: "ellipsis.circle").accessibilityLabel("Pet controls")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
        }
        .padding(.horizontal, 9)
        .frame(width: 264, height: 100)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24))
    }

    private var activity: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Asta activity").font(.headline)
            if model.notices.isEmpty {
                Text(model.status ?? "Nothing yet").foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(model.notices) { notice in
                            Button {
                                model.showingActivity = false
                                model.open(notice: notice)
                            } label: {
                                HStack {
                                    Image(systemName: notice.id > model.seenThrough && !notice.seen ? "circle.fill" : "circle")
                                        .font(.system(size: 6))
                                    VStack(alignment: .leading) {
                                        Text(notice.kind.label)
                                        Text(notice.level).font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text(Date(timeIntervalSince1970: notice.createdAt), style: .relative)
                                        .font(.caption).foregroundStyle(.secondary)
                                }.padding(.vertical, 6).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .padding(14)
        .frame(width: 280, height: 245, alignment: .topLeading)
    }
}

private final class PetPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

@MainActor
final class PetApp: NSObject, NSApplicationDelegate {
    private let model = PetModel()
    private var item: NSStatusItem?
    private var panel: NSPanel?
    private let defaults = UserDefaults.standard

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "book.closed.fill", accessibilityDescription: "Asta Pet")
        self.item = item
        if !defaults.bool(forKey: "petDisabled") {
            model.start()
            show()
        }
        updateMenu()
    }

    private func updateMenu() {
        let menu = NSMenu()
        let disabled = defaults.bool(forKey: "petDisabled")
        if !disabled {
            append(panel?.isVisible == true ? "Hide mascot" : "Show mascot", #selector(togglePanel), to: menu)
        }
        append(disabled ? "Enable Asta Pet" : "Disable Asta Pet", #selector(toggleEnabled), to: menu)
        append("Quit Asta Pet", #selector(quit), to: menu, key: "q")
        item?.menu = menu
    }

    private func append(_ title: String, _ selector: Selector, to menu: NSMenu, key: String = "") {
        let entry = menu.addItem(withTitle: title, action: selector, keyEquivalent: key)
        entry.target = self
    }

    private func show() {
        if panel == nil {
            let panel = PetPanel(contentRect: NSRect(x: 0, y: 0, width: 264, height: 100),
                                 styleMask: [.borderless, .nonactivatingPanel],
                                 backing: .buffered, defer: false)
            panel.level = .floating
            panel.becomesKeyOnlyIfNeeded = true
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.hidesOnDeactivate = false
            panel.isMovableByWindowBackground = true
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.contentView = NSHostingView(rootView: PetView(model: model, hide: { [weak self] in
                self?.hide()
            }))
            if let screen = NSScreen.main?.visibleFrame {
                panel.setFrameOrigin(NSPoint(x: screen.maxX - 284, y: screen.minY + 24))
            }
            self.panel = panel
        }
        panel?.orderFrontRegardless()
        updateMenu()
    }

    private func hide() {
        model.showingActivity = false
        panel?.orderOut(nil)
        updateMenu()
    }

    @objc private func togglePanel() {
        if panel?.isVisible == true { hide() } else { show() }
    }

    @objc private func toggleEnabled() {
        let disable = !defaults.bool(forKey: "petDisabled")
        defaults.set(disable, forKey: "petDisabled")
        if disable {
            model.stop()
            hide()
        } else {
            model.start()
            show()
        }
    }

    @objc private func quit() {
        model.stop()
        NSApp.terminate(nil)
    }
}

@main
enum AstaPet {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = PetApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
