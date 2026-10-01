// Asta Voice — the menu-bar half of voice mode (see app/voice_mode.py).
//
// Deliberately thin: two global hotkeys, the microphone, playing what Asta
// says, and an icon. Everything that DECIDES anything lives in Asta's Python,
// where it is tested. This only moves sound and key presses.
//
//   ⌃⌥A  Asta's voice on/off   (speaks answers and the updates that matter)
//   ⌃⌥M  Asta's mic on/off     (an open conversation while on)
//
// Icon: 🔊/🔇 for the voice, 🎙/· for the mic, ⚠︎ when Asta is not reachable.
// The mic is only open while the mic switch is on — macOS's orange dot is the
// proof. Asta's own speech is not heard back: the input uses the Mac's voice
// processing (echo cancelling), and listening pauses while Asta talks.

import AppKit
import AVFoundation
import Carbon.HIToolbox
import CoreAudio

// MARK: - settings

let serverURL: URL = {
    let env = ProcessInfo.processInfo.environment
    var token = env["ASTA_TOKEN"] ?? ""
    if token.isEmpty, let path = env["ASTA_ENV_FILE"] ?? CommandLine.arguments.dropFirst().first,
       let text = try? String(contentsOfFile: path, encoding: .utf8) {
        for line in text.split(separator: "\n") where line.hasPrefix("ASTA_TOKEN=") {
            token = String(line.dropFirst("ASTA_TOKEN=".count)).trimmingCharacters(in: .whitespaces)
        }
    }
    let port = env["ASTA_PORT"] ?? "8321"
    var parts = URLComponents(string: "ws://127.0.0.1:\(port)/ws/voice-mode")!
    parts.queryItems = [URLQueryItem(name: "token", value: token)]
    return parts.url!
}()

func log(_ s: String) {
    FileHandle.standardError.write(("[asta-voice] " + s + "\n").data(using: .utf8)!)
}

// MARK: - the link to Asta

final class Link {
    var task: URLSessionWebSocketTask?
    var connected = false
    var onMessage: (([String: Any]) -> Void)?
    var onState: ((Bool) -> Void)?

    func connect() {
        task = URLSession.shared.webSocketTask(with: serverURL)
        task?.resume()
        send(["type": "hello"])
        receive()
    }

    func send(_ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { [weak self] err in
            if err != nil { self?.dropped() }
        }
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure:
                self.dropped()
            case .success(let msg):
                if !self.connected { self.connected = true; DispatchQueue.main.async { self.onState?(true) } }
                if case .string(let text) = msg,
                   let data = text.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    DispatchQueue.main.async { self.onMessage?(obj) }
                }
                self.receive()
            }
        }
    }

    private func dropped() {
        guard task != nil else { return }
        task?.cancel()
        task = nil
        connected = false
        DispatchQueue.main.async { self.onState?(false) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.connect() }
    }
}

// MARK: - saying things

final class Mouth: NSObject, AVAudioPlayerDelegate {
    var player: AVAudioPlayer?
    let synth = AVSpeechSynthesizer()
    var speaking = false
    var onDone: (() -> Void)?

    func say(text: String, audio: Data?, chime: Bool) {
        let go = {
            self.speaking = true
            if let audio = audio, let p = try? AVAudioPlayer(data: audio) {
                self.player = p
                p.delegate = self
                p.play()
            } else {
                // No Asta voice (Voicebox down): the Mac's own, never silence.
                self.synth.speak(AVSpeechUtterance(string: text))
                DispatchQueue.main.asyncAfter(deadline: .now() + Double(text.count) / 14.0 + 0.5) {
                    self.finished()
                }
            }
        }
        if chime {
            NSSound(named: "Tink")?.play()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: go)
        } else {
            go()
        }
    }

    func stop() {
        player?.stop()
        synth.stopSpeaking(at: .immediate)
        finished()
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        finished()
    }

    private func finished() {
        // A short tail: the room's echo of the last word is not him talking.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            self.speaking = false
            self.onDone?()
        }
    }
}

// MARK: - hearing things

final class Ears {
    let engine = AVAudioEngine()
    var running = false
    var mouth: Mouth?
    var onUtterance: ((Data) -> Void)?

    // Voice activity, on 16 kHz mono frames.
    private let rate: Double = 16000
    private var converter: AVAudioConverter?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                          channels: 1, interleaved: true)!
    private var floorDb: Float = -55
    private var loudFrames = 0
    private var quietMs: Double = 0
    private var inSpeech = false
    private var speech: [Int16] = []
    private var preroll: [Int16] = []
    private let queue = DispatchQueue(label: "asta.ears")

    func start() {
        guard !running else { return }
        let input = engine.inputNode
        try? input.setVoiceProcessingEnabled(true)        // echo cancelling
        let inFormat = input.outputFormat(forBus: 0)
        converter = AVAudioConverter(from: inFormat, to: outFormat)
        input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] buf, _ in
            self?.queue.async { self?.take(buf) }
        }
        do {
            try engine.start()
            running = true
            log("mic open")
        } catch {
            input.removeTap(onBus: 0)
            log("mic failed: \(error)")
        }
    }

    func stop() {
        guard running else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        running = false
        queue.async { self.reset() }
        log("mic closed")
    }

    private func reset() {
        inSpeech = false; loudFrames = 0; quietMs = 0; speech = []; preroll = []
    }

    private func take(_ buf: AVAudioPCMBuffer) {
        guard let converter = converter else { return }
        let ratio = rate / buf.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buf.frameLength) * ratio + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var given = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buf
        }
        guard err == nil, let p = out.int16ChannelData?[0], out.frameLength > 0 else { return }
        let frames = Array(UnsafeBufferPointer(start: p, count: Int(out.frameLength)))
        if mouth?.speaking == true { reset(); return }      // half duplex while Asta talks
        var sum: Float = 0
        for s in frames { let f = Float(s) / 32768; sum += f * f }
        let rms = sqrt(sum / Float(max(frames.count, 1)))
        let db = 20 * log10(max(rms, 1e-6))
        let ms = Double(frames.count) / rate * 1000
        if !inSpeech {
            floorDb = min(-30, max(-75, floorDb * 0.97 + db * 0.03))
            preroll += frames
            if preroll.count > Int(rate * 0.3) { preroll.removeFirst(preroll.count - Int(rate * 0.3)) }
            loudFrames = (db > floorDb + 10 && db > -50) ? loudFrames + 1 : 0
            if loudFrames >= 3 {
                inSpeech = true
                speech = preroll
                quietMs = 0
            }
            return
        }
        speech += frames
        quietMs = (db < floorDb + 6) ? quietMs + ms : 0
        let seconds = Double(speech.count) / rate
        if quietMs >= 900 || seconds >= 30 {
            let take = speech
            reset()
            if seconds >= 0.5 {
                let wav = Ears.wav(take, rate: Int(rate))
                DispatchQueue.main.async { self.onUtterance?(wav) }
            }
        }
    }

    static func wav(_ samples: [Int16], rate: Int) -> Data {
        var d = Data()
        func put32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func put16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        let bytes = UInt32(samples.count * 2)
        d.append("RIFF".data(using: .ascii)!); put32(36 + bytes)
        d.append("WAVEfmt ".data(using: .ascii)!); put32(16); put16(1); put16(1)
        put32(UInt32(rate)); put32(UInt32(rate * 2)); put16(2); put16(16)
        d.append("data".data(using: .ascii)!); put32(bytes)
        samples.withUnsafeBufferPointer { d.append(Data(buffer: $0)) }
        return d
    }
}

// MARK: - is another app on a call?

func micInUseElsewhere() -> Bool {
    var device = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
                                     &size, &device) == noErr else { return false }
    var running = UInt32(0)
    size = UInt32(MemoryLayout<UInt32>.size)
    addr.mSelector = kAudioDevicePropertyDeviceIsRunningSomewhere
    guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &running) == noErr else { return false }
    return running != 0
}

// MARK: - the app

var hotkeyHandler: ((UInt32) -> Void)?

final class App: NSObject, NSApplicationDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let link = Link()
    let mouth = Mouth()
    let ears = Ears()
    var speaker = false
    var mic = false
    var busy = false

    func applicationDidFinishLaunching(_ note: Notification) {
        ears.mouth = mouth
        ears.onUtterance = { [weak self] wav in
            self?.link.send(["type": "utterance", "wav": wav.base64EncodedString()])
        }
        link.onState = { [weak self] _ in self?.redraw() }
        link.onMessage = { [weak self] msg in self?.handle(msg) }
        buildMenu()
        registerHotkeys()
        watchScreenLock()
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.checkBusy() }
        link.connect()
        redraw()
    }

    func handle(_ msg: [String: Any]) {
        switch msg["type"] as? String {
        case "state":
            let s = msg["speaker"] as? Bool ?? false
            let m = msg["mic"] as? Bool ?? false
            let why = msg["why"] as? String ?? ""
            if why != "connected" && why != "hello" && (s != speaker || m != mic) {
                NSSound(named: (s && !speaker) || (m && !mic) ? "Pop" : "Bottle")?.play()
            }
            speaker = s
            if m && !mic { openMic() }
            if !m && mic { ears.stop() }
            mic = m
            if !speaker { mouth.stop() }
        case "say":
            guard speaker else { return }
            let audio = (msg["audio"] as? String).flatMap { Data(base64Encoded: $0) }
            mouth.say(text: msg["text"] as? String ?? "", audio: (audio?.isEmpty ?? true) ? nil : audio,
                      chime: msg["chime"] as? Bool ?? false)
        case "error":
            log("asta: \(msg["text"] as? String ?? "")")
        default:
            break
        }
        redraw()
    }

    func openMic() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            ears.start()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                DispatchQueue.main.async {
                    if ok { self.ears.start() } else { self.link.send(["type": "toggle", "what": "mic"]) }
                }
            }
        default:
            log("microphone permission denied — System Settings › Privacy › Microphone")
            link.send(["type": "toggle", "what": "mic"])
        }
    }

    func checkBusy() {
        guard !ears.running else { return }         // our own mic would read as "in use"
        let now = micInUseElsewhere()
        if now != busy {
            busy = now
            link.send(["type": "busy", "value": now])
        }
    }

    func watchScreenLock() {
        DistributedNotificationCenter.default().addObserver(
            forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.link.send(["type": "locked"])
        }
    }

    // MARK: menu and icon

    func buildMenu() {
        let menu = NSMenu()
        let v = NSMenuItem(title: "Asta voice  ⌃⌥A", action: #selector(toggleSpeaker), keyEquivalent: "")
        let m = NSMenuItem(title: "Asta mic  ⌃⌥M", action: #selector(toggleMic), keyEquivalent: "")
        v.target = self; m.target = self
        menu.addItem(v); menu.addItem(m)
        menu.addItem(.separator())
        let q = NSMenuItem(title: "Quit Asta Voice", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        menu.addItem(q)
        item.menu = menu
    }

    @objc func toggleSpeaker() { link.send(["type": "toggle", "what": "speaker"]) }
    @objc func toggleMic() { link.send(["type": "toggle", "what": "mic"]) }

    func redraw() {
        if !link.connected {
            item.button?.title = "Asta ⚠︎"
            item.button?.toolTip = "Asta is not reachable — voice and mic are unavailable"
            return
        }
        item.button?.title = (speaker ? "🔊" : "🔇") + (mic ? "🎙" : "·")
        item.button?.toolTip = "Asta voice \(speaker ? "on" : "off") (⌃⌥A) · mic \(mic ? "on" : "off") (⌃⌥M)"
        if let menu = item.menu, menu.items.count >= 2 {
            menu.items[0].state = speaker ? .on : .off
            menu.items[1].state = mic ? .on : .off
        }
    }

    // MARK: hotkeys — Carbon's RegisterEventHotKey needs no extra permission

    func registerHotkeys() {
        hotkeyHandler = { [weak self] id in
            DispatchQueue.main.async {
                if id == 1 { self?.toggleSpeaker() } else { self?.toggleMic() }
            }
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hk = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
            hotkeyHandler?(hk.id)
            return noErr
        }, 1, &spec, nil, nil)
        let mods = UInt32(controlKey | optionKey)
        var ref1: EventHotKeyRef?
        var ref2: EventHotKeyRef?
        RegisterEventHotKey(UInt32(kVK_ANSI_A), mods, EventHotKeyID(signature: OSType(0x41535441), id: 1),
                            GetApplicationEventTarget(), 0, &ref1)
        RegisterEventHotKey(UInt32(kVK_ANSI_M), mods, EventHotKeyID(signature: OSType(0x41535441), id: 2),
                            GetApplicationEventTarget(), 0, &ref2)
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
