import SwiftUI

@main
enum PetSpriteSmoke {
    @MainActor
    static func main() throws {
        let atlas = URL(fileURLWithPath: CommandLine.arguments[1])
        let frames = PetSprites.frames(from: atlas)
        precondition(frames.count == SpriteFrame.allCases.count)
        precondition(frames.allSatisfy { $0.width == 224 && $0.height == 224 })

        func frame(_ reaction: Reaction, _ time: Double, still: Bool = false) -> SpriteFrame {
            SpritePose.at(reaction, elapsed: time, still: still).frame
        }

        precondition(frame(.appeared, 0) == .idle && frame(.appeared, 0.5) == .draw)
        precondition(frame(.appeared, 2) == .idle)
        precondition(frame(.coding, 0) == .draw && frame(.coding, 30) == .sword)
        precondition(SpritePose.at(.coding, elapsed: 0.8, still: false).angle > 10)
        precondition(SpritePose.at(.coding, elapsed: 30, still: false).angle == 0)
        precondition(frame(.debugging, 0.4) == .investigate)
        precondition(SpritePose.at(.debugging, elapsed: 0.4, still: false).slash > 0)
        precondition(frame(.draftApproval, 0) == .present && frame(.messageSent, 0.4) == .send)
        precondition(frame(.waitingReply, 0) == .idle)
        precondition(frame(.ciFailed, 0.1) == .draw && frame(.ciFailed, 0.8) == .union)
        precondition(SpritePose.at(.blocked, elapsed: 2, still: false).slash == 0)
        precondition(frame(.ready, 0.1) == .sword && frame(.ready, 0.6) == .sheathe)
        precondition(!SpeechMode.silent.includes(.coding))
        precondition(SpeechMode.talkative.includes(.debugging))
        precondition(SpeechMode.important.includes(.sendUncertain))

        let running = PetTask(id: 7, kind: .coding, changedAt: 950)
        let olderResult = PetTask(id: 8, kind: .ready, changedAt: 970)
        let awaitingReply = PetTask(id: 9, kind: .waitingReply, changedAt: 980)
        let tasks = [running, olderResult, awaitingReply]
        precondition(PetFocus.choose(notices: [], tasks: tasks, now: 1000, seenThrough: 0).kind == .coding)
        let draft = PetTask(id: 10, kind: .draftApproval, changedAt: 998)
        precondition(PetFocus.choose(notices: [], tasks: tasks + [draft], now: 1000, seenThrough: 0).kind == .draftApproval)

        let reactions: [Reaction] = [.appeared, .coding, .debugging, .draftApproval,
                                     .messageSent, .waitingReply, .blocked, .ready]
        for reaction in reactions {
            let pose = SpritePose.at(reaction, elapsed: 0.4, still: true)
            precondition(pose.angle == 0 && pose.rise == 0 && pose.slash == 0 && pose.scale == 1)
            let renderer = ImageRenderer(content: AstaSprite(kind: reaction, started: Date(), still: true, frames: frames))
            renderer.scale = 2
            guard let image = renderer.cgImage else { preconditionFailure("Could not render \(reaction)") }
            precondition(image.width == 176 && image.height == 176)
        }
        print("Verified Asta sprite timing, focus, speech modes, and 88-point reduced-motion rendering")
    }
}
