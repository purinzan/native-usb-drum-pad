import Foundation

/// The velocity decisions from the desktop app, carried over unchanged.
///
/// These are the parts worth porting exactly rather than reinventing: the curve
/// was tuned by ear against real pads, and the floor expansion exists because a
/// controller whose softest hit is 50 otherwise starts a third of the way up it
/// and can never reach a ghost note.
enum Velocity {

    /// Gain for a velocity, matching `velocity_gain` in drum_pad_native.py.
    static func gain(_ raw: Int) -> Float {
        let velocity = Float(max(1, min(127, raw)))
        if velocity <= 50 {
            return 0.12 + pow(velocity / 50.0, 1.6) * 0.2
        }
        if velocity <= 70 {
            return 0.32 + pow((velocity - 50.0) / 20.0, 1.3) * 0.1
        }
        if velocity <= 100 {
            return 0.42 + pow((velocity - 70.0) / 30.0, 1.2) * 0.46
        }
        return 0.88 + pow((velocity - 100.0) / 27.0, 0.85) * 0.12
    }

    /// Stretch the range a controller actually produces back out to 1...127.
    ///
    /// Matches `expand_velocity`. A floor of 1 is a no-op; anything at or below
    /// the floor is as soft as the pad goes.
    static func expand(_ raw: Int, floor: Int) -> Int {
        let velocity = max(1, min(127, raw))
        let floor = max(1, min(120, floor))
        if floor <= 1 { return velocity }
        if velocity <= floor { return 1 }
        let scaled = 1.0 + Double(velocity - floor) * 126.0 / Double(127 - floor)
        return max(1, min(127, Int(scaled.rounded())))
    }

    /// Which layer band a hit lands in, matching `split_layer_ranges`.
    static func layerRanges(count: Int) -> [ClosedRange<Int>] {
        let count = max(1, min(4, count))
        var edges = [1]
        for step in 0..<count {
            edges.append(Int((1.0 + 126.0 * Double(step + 1) / Double(count)).rounded()))
        }
        return (0..<count).map { step in
            let upper = edges[step + 1] - (step + 1 < count ? 1 : 0)
            return edges[step]...max(edges[step], upper)
        }
    }

    /// The five tiers the kit layers are keyed on, matching `velocity_tier`.
    static func tier(_ velocity: Int) -> String {
        switch velocity {
        case ..<45: return "ghost"
        case ..<78: return "soft"
        case ..<100: return "mid"
        case ..<116: return "hard"
        default: return "accent"
        }
    }
}
