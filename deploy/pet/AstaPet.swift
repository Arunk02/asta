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

    init() {
        speechMode = SpeechMode(rawValue: UserDefaults.standard.string(forKey: "petSpeech") ?? "") ?? .silent
    }

    private var cursorKey: String { "petCursor.\(api.base.port ?? 8321)" }

    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
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
                for task in page.tasks where task.kind == .running && tasks[task.id] != .running
                    && task.changedAt >= stamp - 60 {
                    reactions.append(SpeechEvent(id: -task.id, kind: .running))
                }
            }
            tasks = Dictionary(uniqueKeysWithValues: page.tasks.map { ($0.id, $0.kind) })
            unseen = page.unseen
            seenThrough = page.seenThrough
            focus = .choose(notices: notices, tasks: page.tasks, since: stamp - 300, seenThrough: seenThrough)
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

private struct Clown: View {
    let kind: Reaction
    let still: Bool

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24, paused: still || kind == .idle)) { timeline in
            let t = still ? 0 : timeline.date.timeIntervalSinceReferenceDate
            ZStack {
                Circle().fill(Color(red: 0.96, green: 0.89, blue: 0.77)).frame(width: 78, height: 78)
                HStack(spacing: 22) {
                    Circle().fill(.orange).frame(width: 23)
                    Circle().fill(.orange).frame(width: 23)
                }.offset(y: -3)
                Ellipse().fill(Color(red: 1, green: 0.96, blue: 0.89))
                    .frame(width: 46, height: 48).offset(y: 3)
                HStack(spacing: 12) {
                    Capsule().frame(width: 3, height: 6)
                    Capsule().frame(width: 3, height: 6)
                }.foregroundStyle(Color(red: 0.19, green: 0.13, blue: 0.23)).offset(y: -1)
                Circle().fill(.red).frame(width: 9, height: 9).offset(y: 7)
                Path { path in
                    path.move(to: CGPoint(x: 1, y: 1))
                    path.addQuadCurve(to: CGPoint(x: 15, y: 1), control: CGPoint(x: 8, y: 12))
                }.stroke(.pink, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .frame(width: 16, height: 8).offset(y: 17)
                VStack(spacing: -3) {
                    RoundedRectangle(cornerRadius: 3).fill(.purple).frame(width: 19, height: 15)
                    Capsule().fill(.purple).frame(width: 30, height: 5)
                }.rotationEffect(.degrees(-14)).offset(x: -4, y: -23)
                Image(systemName: "bowtie.fill").font(.system(size: 14, weight: .bold))
                    .foregroundStyle(.teal).offset(y: 27)
                if kind == .running {
                    ForEach(0..<3) { index in
                        let angle = t * 3 + Double(index) * 2.1
                        Circle().fill([Color.teal, .pink, .yellow][index]).frame(width: 6, height: 6)
                            .offset(x: cos(angle) * 31, y: -28 + sin(angle) * 11)
                    }
                } else if let symbol {
                    Image(systemName: symbol).font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(kind == .blocked || kind == .ciFailed ? Color.red : Color.purple)
                        .padding(5).background(.white, in: Circle())
                        .rotationEffect(.degrees(sin(t * 5) * 8))
                        .offset(x: 29, y: 22 + sin(t * 4) * 3)
                }
            }
            .rotationEffect(.degrees(kind == .ready || kind == .ciPassed ? sin(t * 3) * 11 : 0))
        }
        .frame(width: 88, height: 88)
        .accessibilityHidden(true)
    }

    private var symbol: String? {
        switch kind {
        case .approval: return "list.clipboard"
        case .question: return "questionmark"
        case .blocked, .ciFailed: return "flag.fill"
        case .ready, .ciPassed: return "sparkles"
        case .reminder: return "clock.fill"
        case .message: return "envelope.fill"
        case .alert: return "bell.fill"
        case .idle, .running: return nil
        }
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
                    Clown(kind: model.focus.kind, still: reduceMotion || model.snoozed)
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
        item.button?.image = NSImage(systemSymbolName: "face.smiling", accessibilityDescription: "Asta Pet")
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
