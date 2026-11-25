import ImageIO
import SwiftUI

enum SpriteFrame: Int, CaseIterable {
    case idle, draw, sword, investigate, present, send, union, sheathe
}

struct SpritePose {
    let frame: SpriteFrame
    var angle: Double = 0
    var rise: Double = 0
    var scale: Double = 1
    var slash: Double = 0

    static func at(_ reaction: Reaction, elapsed: Double, still: Bool) -> Self {
        let t = max(0, elapsed)
        let hover = still ? 0 : sin(t * 2.4) * 1.5
        switch reaction {
        case .appeared:
            if still || t >= 1.2 { return Self(frame: .idle) }
            if t < 0.28 { return Self(frame: .idle, rise: -4 + t * 14, scale: 0.9 + t * 0.35) }
            return Self(frame: .draw, rise: hover, scale: 1 + sin((t - 0.28) * 4) * 0.05)
        case .running, .coding:
            if still || t >= 1.15 { return Self(frame: .sword, rise: hover) }
            if t < 0.45 { return Self(frame: .draw, angle: -14 + t * 32, rise: hover) }
            let roll = sin((t - 0.45) / 0.7 * .pi)
            return Self(frame: .sword, angle: roll * 14, rise: hover, slash: roll * 0.5)
        case .debugging:
            let trace = still || t >= 0.95 ? 0 : sin(t / 0.95 * .pi)
            return Self(frame: .investigate, rise: hover, slash: trace * 0.65)
        case .approval, .draftApproval, .question:
            return Self(frame: .present, rise: hover, scale: still ? 1 : 1 + max(0, 0.4 - t) * 0.16)
        case .sending, .messageSent, .message, .reminder:
            if still { return Self(frame: .send) }
            if t < 0.18 { return Self(frame: .present, angle: -5, rise: hover) }
            return Self(frame: .send, angle: max(0, 0.5 - t) * 18, rise: hover)
        case .waitingReply:
            return Self(frame: .idle, rise: hover)
        case .blocked, .ciFailed, .sendUncertain:
            if still || t >= 1.15 { return Self(frame: .union) }
            if t < 0.3 { return Self(frame: .draw, rise: hover) }
            let sweep = sin((t - 0.3) / 0.85 * .pi)
            return Self(frame: .union, angle: -sweep * 7, rise: -sweep * 3,
                        scale: 1 + sweep * 0.09, slash: sweep)
        case .ready, .ciPassed:
            if still { return Self(frame: .sheathe) }
            if t < 0.26 { return Self(frame: .sword, angle: -12, rise: hover) }
            return Self(frame: .sheathe, rise: hover, slash: max(0, 0.85 - t) * 0.8)
        case .idle, .alert:
            return Self(frame: .idle, rise: hover)
        }
    }
}

enum PetSprites {
    static func frames(from url: URL?) -> [CGImage] {
        guard let url, let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let atlas = CGImageSourceCreateImageAtIndex(source, 0, nil),
              atlas.width == atlas.height * 2, atlas.width.isMultiple(of: 4),
              atlas.height.isMultiple(of: 2) else { return [] }
        let width = atlas.width / 4
        let height = atlas.height / 2
        return SpriteFrame.allCases.compactMap { frame in
            atlas.cropping(to: CGRect(x: frame.rawValue % 4 * width, y: frame.rawValue / 4 * height,
                                      width: width, height: height))
        }
    }

    static let bundled = frames(from: Bundle.main.url(forResource: "asta-sprites", withExtension: "png"))
}

struct AstaSprite: View {
    let kind: Reaction
    let started: Date
    let still: Bool
    var frames = PetSprites.bundled

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24, paused: still || kind == .idle)) { timeline in
            let pose = SpritePose.at(kind, elapsed: timeline.date.timeIntervalSince(started), still: still)
            ZStack {
                if frames.indices.contains(pose.frame.rawValue) {
                    Image(decorative: frames[pose.frame.rawValue], scale: 1)
                        .resizable().interpolation(.high).scaledToFit()
                        .rotationEffect(.degrees(pose.angle))
                        .scaleEffect(pose.scale)
                        .offset(y: pose.rise)
                } else {
                    Image(systemName: "book.closed.fill").foregroundStyle(.red)
                }
                if pose.slash > 0 {
                    Path { path in
                        path.move(to: CGPoint(x: 9, y: 71))
                        path.addQuadCurve(to: CGPoint(x: 78, y: 22), control: CGPoint(x: 56, y: 52))
                    }
                    .stroke(Color.black.opacity(pose.slash), style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .shadow(color: .red.opacity(pose.slash), radius: 2)
                }
            }
            .frame(width: 88, height: 88)
        }
        .accessibilityHidden(true)
    }
}
