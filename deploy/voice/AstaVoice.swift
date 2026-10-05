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
import Speech

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

let logClock: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm:ss.SSS"; return f }()

func log(_ s: String) {
    FileHandle.standardError.write(("[asta-voice \(logClock.string(from: Date()))] " + s + "\n").data(using: .utf8)!)
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
    private var currentText = ""
    // Lines wait their turn: "Let me check." is not cut off by the answer.
    private var waiting: [(String, Data?, Bool)] = []

    func say(text: String, audio: Data?, chime: Bool) {
        if speaking || held {
            waiting.append((text, audio, chime))
            return
        }
        play(text: text, audio: audio, chime: chime)
    }

    /// He is talking: Asta waits. 2 Oct 13:46: an answer started while he was
    /// mid-sentence — Asta talked over him, and its words were recorded as his,
    /// came back as a request, and were answered again.
    private(set) var held = false
    /// When Asta last fell silent: echo can only be in a recording made while,
    /// or just after, Asta was audible.
    private(set) var silentSince = Date.distantPast

    /// Waiting is capped: room sound reads as "him talking" too — with echo
    /// cancelling off, an answer ready at 17:50:39 waited 10 s while he said
    /// "Hello? Hello?" because nothing came (2 Oct). After this long Asta speaks.
    static let holdMax: TimeInterval = 2.5
    private var heldAt = Date.distantPast

    func hold(_ on: Bool) {
        guard on != held else { return }
        held = on
        if on {
            heldAt = Date()
            let mine = heldAt
            DispatchQueue.main.asyncAfter(deadline: .now() + Mouth.holdMax) {
                if self.held && self.heldAt == mine { self.hold(false) }
            }
            return
        }
        if !speaking && !waiting.isEmpty {
            let (t, a, c) = waiting.removeFirst()
            play(text: t, audio: a, chime: c)
        }
    }

    /// Sound while Asta talks: turned down, not off — it may be Asta's own voice
    /// coming back through the mic (2 Oct: on speakers, it cut itself off every
    /// answer). Asta checks the words and then says "hush" (him) or "unduck" (echo).
    private(set) var ducked = false
    private var duckedAt = Date.distantPast

    func flush() {
        if !waiting.isEmpty { log("dropped \(waiting.count) queued line(s) — he moved on") }
        waiting.removeAll()
    }

    func duck() {
        ducked = true
        duckedAt = Date()
        player?.volume = 0.15
        log("ducked")
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
            if self.ducked && Date().timeIntervalSince(self.duckedAt) >= 10 { self.unduck() }
        }
    }

    func unduck() {
        guard ducked else { return }
        ducked = false
        player?.volume = 1.0
        log("unducked")
    }

    /// He started talking: Asta stops at once and forgets what it was about to say.
    func interrupt() {
        ducked = false
        silentSince = Date()
        waiting.removeAll()
        player?.stop()
        synth.stopSpeaking(at: .immediate)
        speaking = false
    }

    private func play(text: String, audio: Data?, chime: Bool) {
        speaking = true                     // from the chime on: the next line queues
        currentText = text
        let go = {
            guard self.speaking else { return }    // interrupted during the chime
            if let audio = audio, let p = try? AVAudioPlayer(data: audio) {
                self.player = p
                p.delegate = self
                p.volume = self.ducked ? 0.15 : 1.0
                if p.play() {
                    log("saying: \(text.prefix(400))")
                } else {
                    log("voice playback did not start — using Mac speech")
                    self.player = nil
                    self.useMacVoice(text)
                }
            } else {
                // No Asta voice (Voicebox down): the Mac's own, never silence.
                self.useMacVoice(text)
            }
        }
        if chime {
            NSSound(named: "Tink")?.play()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: go)
        } else {
            go()
        }
    }

    private func useMacVoice(_ text: String) {
        synth.speak(AVSpeechUtterance(string: text))
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(text.count) / 14.0 + 0.5) {
            self.finished()
        }
    }

    func stop() {
        waiting.removeAll()
        player?.stop()
        synth.stopSpeaking(at: .immediate)
        finished()
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        guard self.player === player else { return }
        if !flag && speaking {
            log("voice playback failed — using Mac speech")
            self.player = nil
            useMacVoice(currentText)
            return
        }
        finished()
    }

    private func finished() {
        guard speaking else { return }
        if !waiting.isEmpty && !held {
            let (t, a, c) = waiting.removeFirst()
            play(text: t, audio: a, chime: c)
            return
        }
        // A short tail: the room's echo of the last word is not him talking.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            self.speaking = false
            self.silentSince = Date()
            self.onDone?()
        }
    }
}

// MARK: - understanding words, on this Mac

/// Apple's on-device speech recognition, fed while he talks — so the words are
/// ready about a fifth of a second after he stops, instead of after a 1.4 s
/// Whisper pass over the whole clip. Primed with his vocabulary (the people he
/// talks to, his services, ATA/ATD...) so names come out right — 2 Oct:
/// "Rajendra shared" came back from Whisper as "Raja Shad". Runs on the Neural
/// Engine. When it is not allowed or not there, Asta's Whisper does the hearing:
/// the clip is always sent too.
final class Recognizer {
    var vocab: [String] = []
    private let queue: DispatchQueue
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var best: SFSpeechRecognitionResult?
    private var failed = false
    private var waiting: ((String, Float) -> Void)?
    private var gen = 0
    private let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    private var asked = false

    init(queue: DispatchQueue) { self.queue = queue }

    func authorize() {
        guard !asked else { return }
        asked = true
        SFSpeechRecognizer.requestAuthorization { status in
            guard status == .authorized else {
                log("speech recognition not allowed (\(status.rawValue)) — Whisper does the hearing")
                return
            }
            for id in ["en-IN", "en-US"] {
                if let r = SFSpeechRecognizer(locale: Locale(identifier: id)), r.supportsOnDeviceRecognition {
                    self.queue.async { self.recognizer = r }
                    log("on-device speech recognition: \(id)")
                    return
                }
            }
            log("no on-device speech recognition here — Whisper does the hearing")
        }
    }

    /// Speech started: a new request, with what came just before it.
    func begin(_ samples: [Int16]) {
        cancel()
        guard let r = recognizer, r.isAvailable else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true
        req.shouldReportPartialResults = false
        req.contextualStrings = vocab
        req.addsPunctuation = true
        req.taskHint = .dictation
        request = req
        let g = gen
        task = r.recognitionTask(with: req) { [weak self] result, error in
            self?.queue.async {
                guard let self = self, self.gen == g else { return }
                if let result = result { self.best = result }
                if error != nil && result == nil { self.failed = true }
                if result?.isFinal == true || error != nil { self.complete() }
            }
        }
        feed(samples)
    }

    func feed(_ samples: [Int16]) {
        guard let req = request, !samples.isEmpty,
              let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)),
              let ch = buf.floatChannelData else { return }
        buf.frameLength = AVAudioFrameCount(samples.count)
        for i in 0..<samples.count { ch[0][i] = Float(samples[i]) / 32768 }
        req.append(buf)
    }

    /// He stopped: the words, and how sure the recognizer is (0 when unknown).
    func finish(_ then: @escaping (String, Float) -> Void) {
        guard let req = request, !failed else { cancel(); then("", 0); return }
        waiting = then
        req.endAudio()
        let g = gen
        queue.asyncAfter(deadline: .now() + 0.7) { [weak self] in
            guard let self = self, self.gen == g else { return }
            self.complete()
        }
    }

    private func complete() {
        guard let then = waiting else { return }
        let words = Recognizer.words(best)
        cancel()
        then(words.0, words.1)
    }

    func cancel() {
        task?.cancel()
        request = nil; task = nil; best = nil; waiting = nil; failed = false
        gen += 1
    }

    static func words(_ result: SFSpeechRecognitionResult?) -> (String, Float) {
        guard let t = result?.bestTranscription else { return ("", 0) }
        let segs = t.segments
        let conf = segs.isEmpty ? 0 : segs.map { $0.confidence }.reduce(0, +) / Float(segs.count)
        return (t.formattedString, conf)
    }

    /// A whole clip, for Asta's calls: a second ear when Whisper heard nothing.
    func transcribe(_ wav: Data, then: @escaping (String, Float) -> Void) {
        guard let r = recognizer, r.isAvailable else { then("", 0); return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("asta-\(UUID().uuidString).wav")
        guard (try? wav.write(to: url)) != nil else { then("", 0); return }
        let req = SFSpeechURLRecognitionRequest(url: url)
        req.requiresOnDeviceRecognition = true
        req.contextualStrings = vocab
        req.addsPunctuation = true
        var sent = false
        let done: (String, Float) -> Void = { text, conf in
            DispatchQueue.main.async {
                guard !sent else { return }
                sent = true
                try? FileManager.default.removeItem(at: url)
                then(text, conf)
            }
        }
        r.recognitionTask(with: req) { result, error in
            if let result = result, result.isFinal {
                let w = Recognizer.words(result)
                done(w.0, w.1)
            } else if error != nil {
                done("", 0)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { done("", 0) }
    }
}

// MARK: - hearing things

final class Ears {
    let engine = AVAudioEngine()
    var running = false
    var mouth: Mouth?
    var onUtterance: ((Data, String, Float, Bool) -> Void)?
    /// Was Asta audible at any point while this recording was made? If not, it
    /// cannot be Asta's echo — 2 Oct: "Can you explain the booking service?",
    /// said in silence, was dropped as echo of an earlier answer.
    private var astaAudible = false
    private func astaNow() -> Bool {
        guard let m = mouth else { return false }
        return m.speaking || Date().timeIntervalSince(m.silentSince) < 0.8
    }
    lazy var recognizer = Recognizer(queue: queue)
    var onBargeIn: (() -> Void)?
    var onInputError: ((String) -> Void)?
    /// He started talking (true), or a sound too short to be speech ended (false).
    /// Asta holds a half-said turn while he talks instead of answering the half.
    var onSpeaking: ((Bool) -> Void)?
    private var bargeFrames = 0
    private var bargeWindow: [Bool] = []

    // Voice activity, on 16 kHz mono frames.
    private let rate: Double = 16000
    private var converter: AVAudioConverter?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                          channels: 1, interleaved: true)!
    private var floorDb: Float = -55
    private var loudFrames = 0
    private var quietMs: Double = 0
    private var inSpeech = false
    var inSpeechNow: Bool { queue.sync { inSpeech } }
    private var speech: [Int16] = []
    private var preroll: [Int16] = []
    private let queue = DispatchQueue(label: "asta.ears")

    // What the mic is actually delivering, logged every few seconds while open —
    // 2 Oct: hotkeys worked, the mic "opened", and not one sentence arrived.
    private var peakDb: Float = -120
    private var lastReport = Date()
    private var openedAt = Date()
    private var lastGoodAudioAt = Date.distantPast
    var useVoiceProcessing = true
    private var retriedProcessing = false

    func start() {
        guard !running else { return }
        // Echo cancelling is tried again every time the mic opens: once it gave
        // silence it stayed off for the session, and Asta heard itself (2 Oct
        // 17:49) — its own lines came back as requests and cut it off.
        if !useVoiceProcessing && !retriedProcessing { useVoiceProcessing = true }
        retriedProcessing = false
        let input = engine.inputNode
        try? input.setVoiceProcessingEnabled(useVoiceProcessing)   // echo cancelling
        let inFormat = input.outputFormat(forBus: 0)
        log("mic format: \(inFormat.sampleRate) Hz, \(inFormat.channelCount) ch, voice processing \(useVoiceProcessing)")
        openedAt = Date()
        queue.sync { lastGoodAudioAt = .distantPast }
        peakDb = -120
        // Echo cancelling hands over 9 channels on a Mac; the first is the
        // cleaned voice. One channel goes to the converter — a 9-to-1 convert
        // produced nothing at all (2 Oct).
        monoFormat = AVAudioFormat(standardFormatWithSampleRate: inFormat.sampleRate, channels: 1)
        converter = AVAudioConverter(from: monoFormat!, to: outFormat)
        input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] buf, _ in
            guard let mono = self?.firstChannel(buf) else { return }
            self?.queue.async { self?.take(mono) }
        }
        do {
            try engine.start()
            running = true
            log("mic open")
            let opened = openedAt
            queue.asyncAfter(deadline: .now() + 8) { [weak self] in
                guard let self = self, self.lastGoodAudioAt < opened else { return }
                DispatchQueue.main.async {
                    guard self.running, self.openedAt == opened else { return }
                    if self.useVoiceProcessing && !self.retriedProcessing {
                        log("mic produced no usable audio — retrying without voice processing")
                        self.stop()
                        self.engine.reset()
                        self.useVoiceProcessing = false
                        self.retriedProcessing = true
                        self.start()
                    } else {
                        log("mic produced no usable audio after retry — stopping")
                        self.stop()
                        self.onInputError?("Microphone input produced no usable audio after retry")
                    }
                }
            }
        } catch {
            input.removeTap(onBus: 0)
            log("mic failed: \(error)")
            if useVoiceProcessing {
                log("reopening the mic without echo cancelling")
                engine.reset()
                useVoiceProcessing = false
                start()
            }
        }
    }

    private var monoFormat: AVAudioFormat?

    /// Channel one of whatever the input delivers, as a mono float buffer.
    private func firstChannel(_ buf: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let mono = monoFormat, let src = buf.floatChannelData,
              let out = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buf.frameLength),
              let dst = out.floatChannelData else { return nil }
        out.frameLength = buf.frameLength
        dst[0].update(from: src[0], count: Int(buf.frameLength))
        return out
    }

    func stop() {
        guard running else { return }
        mouth?.hold(false)
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        running = false
        queue.async { self.reset() }
        log("mic closed")
    }

    private func reset() {
        inSpeech = false; loudFrames = 0; quietMs = 0; speech = []; preroll = []
        recognizer.cancel()
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
        var sum: Float = 0
        for s in frames { let f = Float(s) / 32768; sum += f * f }
        let rms = sqrt(sum / Float(max(frames.count, 1)))
        let db = 20 * log10(max(rms, 1e-6))
        if db > -90 { lastGoodAudioAt = Date() }
        let ms = Double(frames.count) / rate * 1000
        peakDb = max(peakDb, db)
        if Date().timeIntervalSince(lastReport) > 3 {
            log(String(format: "level: peak %.0f dB, room %.0f dB%@", peakDb, floorDb, inSpeech ? ", hearing speech" : ""))
            // Echo-cancelled input that delivers nothing at all: reopen plain.
            if peakDb < -90 && useVoiceProcessing && Date().timeIntervalSince(openedAt) > 5 {
                DispatchQueue.main.async {
                    log("voice processing gives silence — reopening the mic without it")
                    self.stop()
                    self.engine.reset()
                    self.useVoiceProcessing = false
                    self.retriedProcessing = true      // this reopen stays without it
                    self.start()
                }
            }
            peakDb = -120
            lastReport = Date()
        }
        // Once he is heard over Asta, Asta is only turned down — his words are
        // gathered below as usual, not taken for more of Asta's own sound.
        if mouth?.speaking == true && !inSpeech {
            // Barge-in. A wrong guess only turns Asta down until its words are
            // checked, so the bar can be low enough to catch him: 12 dB over the
            // room in 10 of the last 16 frames (speech has gaps — 2 Oct, the old
            // 14-in-a-row bar let Asta talk straight over him).
            bargeWindow.append(db > floorDb + 12 && db > -42)
            if bargeWindow.count > 16 { bargeWindow.removeFirst() }
            bargeFrames = bargeWindow.filter { $0 }.count
            if bargeFrames >= 10 {
                bargeWindow.removeAll()
                bargeFrames = 0
                inSpeech = true
                speech = preroll + frames
                quietMs = 0
                astaAudible = true
                recognizer.begin(speech)
                DispatchQueue.main.async { self.onBargeIn?() }
            } else {
                preroll += frames
                if preroll.count > Int(rate * 0.3) { preroll.removeFirst(preroll.count - Int(rate * 0.3)) }
            }
            return
        }
        bargeFrames = 0
        bargeWindow.removeAll()
        if !inSpeech {
            floorDb = min(-30, max(-75, floorDb * 0.97 + db * 0.03))
            preroll += frames
            if preroll.count > Int(rate * 0.3) { preroll.removeFirst(preroll.count - Int(rate * 0.3)) }
            loudFrames = (db > floorDb + 10 && db > -50) ? loudFrames + 1 : 0
            if loudFrames >= 3 {
                inSpeech = true
                speech = preroll
                quietMs = 0
                astaAudible = astaNow()
                recognizer.begin(speech)
                DispatchQueue.main.async { self.mouth?.hold(true); self.onSpeaking?(true) }
            }
            return
        }
        speech += frames
        recognizer.feed(frames)
        if !astaAudible && astaNow() { astaAudible = true }
        quietMs = (db < floorDb + 6) ? quietMs + ms : 0
        let seconds = Double(speech.count) / rate
        if quietMs >= 700 || seconds >= 30 {
            let take = speech
            inSpeech = false; loudFrames = 0; quietMs = 0; speech = []; preroll = []
            if seconds >= 0.5 {
                let wav = Ears.wav(take, rate: Int(rate))
                let audible = astaAudible
                recognizer.finish { text, conf in
                    DispatchQueue.main.async { self.onUtterance?(wav, text, conf, audible) }
                }
                // Asta may speak again once his answer is in: a moment's grace
                // in case he goes on.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                    if !self.inSpeechNow { self.mouth?.hold(false) }
                }
            } else {
                recognizer.cancel()
                DispatchQueue.main.async { self.mouth?.hold(false); self.onSpeaking?(false) }
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
        ears.onUtterance = { [weak self] wav, text, conf, audible in
            self?.link.send(["type": "utterance", "wav": wav.base64EncodedString(),
                             "text": text, "confidence": Double(conf), "asta": audible])
        }
        ears.onSpeaking = { [weak self] now in
            if !now { self?.mouth.unduck() }       // too short to be him
            self?.link.send(["type": "speaking", "value": now])
        }
        ears.onBargeIn = { [weak self] in
            self?.mouth.duck()
            self?.link.send(["type": "barge"])
        }
        ears.onInputError = { [weak self] reason in
            self?.link.send(["type": "mic_error", "reason": reason])
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
        case "hush":
            log("hushed — he is talking")
            mouth.interrupt()
        case "flush":
            // He has moved on: what was still queued from earlier answers is
            // dropped; the line playing now finishes.
            mouth.flush()
        case "unduck":
            mouth.unduck()
        case "vocab":
            let words = (msg["words"] as? [String]) ?? []
            ears.recognizer.vocab = words
            ears.recognizer.authorize()
        case "transcribe":
            // A call's clip Whisper heard nothing in: a second ear.
            let id = msg["id"] as? Int ?? 0
            let wav = (msg["wav"] as? String).flatMap { Data(base64Encoded: $0) } ?? Data()
            ears.recognizer.transcribe(wav) { [weak self] text, conf in
                self?.link.send(["type": "transcript", "id": id, "text": text, "confidence": Double(conf)])
            }
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
