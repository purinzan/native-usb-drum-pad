import Foundation
import QuartzCore

/// A bar-length loop you play into, ported from the desktop looper.
///
/// The desktop keeps its loop on a dedicated audio thread and stores events as
/// beat positions rather than seconds, so the tempo can move without rewriting
/// the take. This does the same on a timer queue: positions are beats, and the
/// only thing that turns them into time is the tempo read at the moment a
/// scheduling tick happens.
final class Looper: ObservableObject {

    struct Event: Identifiable {
        let id = UUID()
        var beat: Double
        var padID: Int
        var velocity: Int
    }

    enum State: Equatable {
        case idle
        case recording        // recording over the top, always also playing
        case playing
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var events: [Event] = []
    @Published var bars: Int = 2 {
        didSet { events = events.filter { $0.beat < totalBeats } }
    }
    @Published var bpm: Double = 120

    /// Called on the main queue when a recorded hit comes back round.
    var onFire: ((Int, Int) -> Void)?

    var totalBeats: Double { Double(bars * 4) }
    var canUndo: Bool { !history.isEmpty }

    private var startedAt: CFTimeInterval?
    private var lastPosition: Double = 0
    private var timer: DispatchSourceTimer?
    private var history: [[Event]] = []
    private let queue = DispatchQueue(label: "starrypad.looper", qos: .userInteractive)

    /// Where the playhead is, in beats. Read every frame by the UI, so it
    /// computes rather than publishes: a 60 Hz publish would redraw the world.
    var position: Double {
        guard let startedAt else { return 0 }
        let beats = (CACurrentMediaTime() - startedAt) * bpm / 60.0
        return beats.truncatingRemainder(dividingBy: totalBeats)
    }

    // MARK: - Transport

    func toggleRecord() {
        switch state {
        case .recording:
            state = .playing                 // drop out of record, keep looping
        case .playing:
            pushHistory()
            state = .recording
        case .idle:
            pushHistory()
            begin()
            state = .recording
        }
    }

    func togglePlay() {
        switch state {
        case .idle:
            guard !events.isEmpty else { return }
            begin()
            state = .playing
        case .playing, .recording:
            stop()
        }
    }

    func stop() {
        timer?.cancel()
        timer = nil
        startedAt = nil
        lastPosition = 0
        state = .idle
    }

    func clear() {
        guard !events.isEmpty else { return }
        pushHistory()
        events = []
        stop()
    }

    func undo() {
        guard let previous = history.popLast() else { return }
        events = previous
        if events.isEmpty { stop() }
    }

    // MARK: - Recording

    /// Stamp a hit at the playhead. Silent unless recording.
    func capture(padID: Int, velocity: Int) {
        guard state == .recording else { return }
        events.append(Event(beat: position, padID: padID, velocity: velocity))
    }

    // MARK: - Scheduling

    private func begin() {
        startedAt = CACurrentMediaTime()
        lastPosition = 0
        let timer = DispatchSource.makeTimerSource(queue: queue)
        // 4 ms is well under the shortest gap a person plays and far cheaper
        // than waking for every frame.
        timer.schedule(deadline: .now(), repeating: .milliseconds(4), leeway: .milliseconds(1))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    private func tick() {
        let now = position
        let previous = lastPosition
        lastPosition = now
        guard !events.isEmpty else { return }

        // The window is normally tiny and forward; at the loop point it wraps,
        // and then it is two windows, not one.
        let due: [Event]
        if now >= previous {
            due = events.filter { $0.beat > previous && $0.beat <= now }
        } else {
            due = events.filter { $0.beat > previous || $0.beat <= now }
        }
        guard !due.isEmpty else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, let onFire = self.onFire else { return }
            for event in due { onFire(event.padID, event.velocity) }
        }
    }

    private func pushHistory() {
        history.append(events)
        if history.count > 20 { history.removeFirst() }
    }
}
