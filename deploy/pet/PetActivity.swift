import Foundation

enum Reaction: String, Decodable {
    case idle, running, approval, question, ready, blocked, reminder, message, alert
    case ciFailed = "ci_failed"
    case ciPassed = "ci_passed"

    var priority: Int {
        switch self {
        case .approval, .question: return 5
        case .blocked, .ciFailed: return 4
        case .reminder: return 3
        case .ready, .ciPassed, .message, .alert: return 2
        case .running: return 1
        case .idle: return 0
        }
    }

    var label: String {
        switch self {
        case .idle: return "All caught up"
        case .running: return "Asta is working"
        case .approval: return "Approval needed"
        case .question: return "Question for you"
        case .ready: return "Result ready"
        case .blocked: return "Task blocked"
        case .reminder: return "Reminder due"
        case .message: return "New message"
        case .alert: return "Asta update"
        case .ciFailed: return "CI failed"
        case .ciPassed: return "CI passed"
        }
    }

    var line: String {
        switch self {
        case .idle: return ""
        case .running: return "I’m on it. I’ll call you when there’s news."
        case .approval: return "Your call. There’s something ready for you to review."
        case .question: return "Quick question before I keep going."
        case .ready: return "Done! I’ve got the result ready."
        case .blocked: return "I hit a snag. The details are in Asta."
        case .reminder: return "Right on cue. You have a reminder."
        case .message: return "You’ve got a new message."
        case .alert: return "You have an Asta update."
        case .ciFailed: return "Red flag. The C I check failed."
        case .ciPassed: return "Good news. The C I check passed."
        }
    }

    var important: Bool { priority >= 3 || self == .ready }
}

enum SpeechMode: String, CaseIterable {
    case silent = "Silent"
    case important = "Important only"
    case talkative = "Talkative"

    func includes(_ reaction: Reaction) -> Bool {
        reaction != .idle && (self == .talkative || (self == .important && reaction.important))
    }
}

struct PetNotice: Decodable, Identifiable {
    let id: Int
    let kind: Reaction
    let level: String
    let seen: Bool
    let createdAt: Double
    let taskId: Int?
    let runUrl: URL?
}

struct PetTask: Decodable, Identifiable {
    let id: Int
    let kind: Reaction
    let changedAt: Double
}

struct PetActivity: Decodable {
    let items: [PetNotice]
    let unseen: Int
    let seenThrough: Int
    let latest: Int
    let cursor: Int
    let tasks: [PetTask]
    let speechAvailable: Bool
    let speech: [SpeechEvent]
}

struct SpeechEvent: Decodable {
    let id: Int
    let kind: Reaction
}

struct PetFocus {
    var kind: Reaction = .idle
    var notice: PetNotice?
    var task: PetTask?

    static func choose(notices: [PetNotice], tasks: [PetTask], since: Double, seenThrough: Int) -> Self {
        let notice = notices.filter { $0.id > seenThrough && !$0.seen && $0.createdAt >= since }
            .max { ($0.kind.priority, $0.id) < ($1.kind.priority, $1.id) }
        let task = tasks.max { ($0.kind.priority, $0.changedAt) < ($1.kind.priority, $1.changedAt) }
        if let task, task.kind.priority >= (notice?.kind.priority ?? 0) {
            return Self(kind: task.kind, task: task)
        }
        if let notice { return Self(kind: notice.kind, notice: notice) }
        return Self()
    }
}

private final class LocalSession: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession, task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

final class PetAPI {
    enum Problem: LocalizedError {
        case disabled, authentication, unavailable

        var errorDescription: String? {
            switch self {
            case .disabled: return "Asta Pet is off on the server"
            case .authentication: return "Asta token required"
            case .unavailable: return "Waiting for Asta"
            }
        }
    }

    let base: URL
    private let environment: [String: String]
    private let session: URLSession

    init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.environment = environment
        let supplied = Int(environment["ASTA_PET_PORT"] ?? "")
        let port = supplied.flatMap { (1...65535).contains($0) ? $0 : nil } ?? 8321
        base = URL(string: "http://127.0.0.1:\(port)")!
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        session = URLSession(configuration: config, delegate: LocalSession(), delegateQueue: nil)
    }

    private func token() -> String? {
        if let token = environment["ASTA_TOKEN"], !token.isEmpty { return token }
        let path = environment["ASTA_PET_TOKEN_FILE"]
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/asta/pet-token").path
        guard let token = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        return token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func activity(after: Int? = nil) async throws -> PetActivity {
        guard let token = token(), !token.isEmpty else { throw Problem.authentication }
        var target = URLComponents(url: base.appendingPathComponent("api/pet/activity"), resolvingAgainstBaseURL: false)!
        if let after { target.queryItems = [URLQueryItem(name: "after", value: String(after))] }
        var request = URLRequest(url: target.url!)
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        switch (response as? HTTPURLResponse)?.statusCode {
        case 200:
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(PetActivity.self, from: data)
        case 401: throw Problem.authentication
        case 404: throw Problem.disabled
        default: throw Problem.unavailable
        }
    }

    func destination(notice: PetNotice? = nil, task: PetTask? = nil) -> URL {
        if let run = notice?.runUrl, run.scheme == "https", run.host == "github.com",
           run.user == nil, run.password == nil, run.port == nil,
           run.path.range(of: #"^/[\w.-]+/[\w.-]+/actions/runs/\d+$"#, options: .regularExpression) != nil {
            return run
        }
        var target = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        if let id = task?.id ?? notice?.taskId {
            target.queryItems = [URLQueryItem(name: "pet_task", value: String(id))]
        } else if let notice {
            target.queryItems = [URLQueryItem(name: "pet_notification", value: String(notice.id))]
        }
        return target.url!
    }
}
