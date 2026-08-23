import AVFoundation

/// A fixed pool of player nodes fed from preloaded buffers.
///
/// The desktop app gives SDL 96 mixer channels and picks a free one per hit;
/// this is the same shape in AVAudioEngine terms. Buffers are decoded once at
/// launch so a hit never waits on file I/O, and the session asks for the
/// shortest buffer iOS will give, since the output path is where the latency
/// lives on any platform.
final class SamplePlayer {

    private let engine = AVAudioEngine()
    private var voices: [AVAudioPlayerNode] = []
    private var buffers: [String: AVAudioPCMBuffer] = [:]
    private var nextVoice = 0
    private let format: AVAudioFormat

    init(voiceCount: Int = 24) {
        format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
        configureSession()
        for _ in 0..<voiceCount {
            let node = AVAudioPlayerNode()
            engine.attach(node)
            engine.connect(node, to: engine.mainMixerNode, format: format)
            voices.append(node)
        }
    }

    private func configureSession() {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            // iOS clamps this to what the hardware allows; asking is still the
            // only lever an app has over the output buffer.
            try session.setPreferredIOBufferDuration(0.003)
            try session.setPreferredSampleRate(48000)
            try session.setActive(true)
        } catch {
            print("audio session: \(error)")
        }
    }

    /// Decode every kit sample up front. Returns how many loaded.
    @discardableResult
    func preload(_ pads: [Pad]) -> Int {
        for pad in pads where buffers[pad.file] == nil {
            guard let url = Self.url(for: pad.file), let buffer = Self.buffer(at: url, as: format) else {
                print("missing sample: \(pad.file)")
                continue
            }
            buffers[pad.file] = buffer
        }
        return buffers.count
    }

    func start() {
        guard !engine.isRunning else { return }
        engine.prepare()
        do {
            try engine.start()
            voices.forEach { $0.play() }
        } catch {
            print("engine: \(error)")
        }
    }

    /// Play one hit. Velocity shapes gain through the ported curve.
    func play(_ pad: Pad, velocity: Int) {
        guard let buffer = buffers[pad.file] else { return }
        let voice = voices[nextVoice]
        nextVoice = (nextVoice + 1) % voices.count
        voice.volume = Velocity.gain(velocity)
        voice.scheduleBuffer(buffer, at: nil, options: .interrupts, completionHandler: nil)
    }

    /// Measured output latency, the number that actually matters to a player.
    var outputLatencyMilliseconds: Double {
        let session = AVAudioSession.sharedInstance()
        return (session.outputLatency + session.ioBufferDuration) * 1000.0
    }

    private static func url(for file: String) -> URL? {
        let name = (file as NSString).deletingPathExtension
        return Bundle.main.url(forResource: name, withExtension: "wav")
    }

    private static func buffer(at url: URL, as format: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard let file = try? AVAudioFile(forReading: url) else { return nil }
        let frames = AVAudioFrameCount(file.length)
        guard frames > 0,
              let source = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: frames),
              (try? file.read(into: source)) != nil
        else { return nil }
        guard file.processingFormat != format else { return source }

        // The kit is 48 kHz stereo already, but a converted buffer keeps every
        // voice on one format so the engine never resamples mid hit.
        guard let converter = AVAudioConverter(from: file.processingFormat, to: format),
              let target = AVAudioPCMBuffer(
                  pcmFormat: format,
                  frameCapacity: AVAudioFrameCount(Double(frames) * format.sampleRate / file.processingFormat.sampleRate) + 64
              )
        else { return source }
        var supplied = false
        var error: NSError?
        converter.convert(to: target, error: &error) { _, status in
            if supplied { status.pointee = .endOfStream; return nil }
            supplied = true
            status.pointee = .haveData
            return source
        }
        return error == nil ? target : source
    }
}
