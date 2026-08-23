import CoreMIDI
import Foundation

/// CoreMIDI input, the same shape as the desktop backend.
///
/// iOS has CoreMIDI too, so a class compliant USB pad connected through the
/// USB-C or Lightning adapter shows up as a source here exactly as it does on
/// the Mac. What differs is that the ctypes bindings the desktop app uses
/// cannot be loaded inside an iOS sandbox, so this is the same protocol
/// reimplemented rather than shared code.
final class MIDIInput: ObservableObject {

    /// Note number and velocity for every note on, on the main queue.
    var onNote: ((UInt8, UInt8) -> Void)?
    var onNoteOff: ((UInt8) -> Void)?

    @Published private(set) var sourceNames: [String] = []
    @Published private(set) var lastEventAt: Date?

    private var client = MIDIClientRef()
    private var port = MIDIPortRef()

    func start() {
        guard client == 0 else { return }
        var status = MIDIClientCreateWithBlock("Starrypad" as CFString, &client) { [weak self] _ in
            // Sources come and go while the app runs; reconnect on any change.
            DispatchQueue.main.async { self?.connectAllSources() }
        }
        guard status == noErr else {
            print("MIDIClientCreate: \(status)")
            return
        }
        status = MIDIInputPortCreateWithProtocol(
            client, "Starrypad in" as CFString, ._1_0, &port
        ) { [weak self] eventList, _ in
            self?.handle(eventList)
        }
        guard status == noErr else {
            print("MIDIInputPortCreate: \(status)")
            return
        }
        connectAllSources()
    }

    private func connectAllSources() {
        var names: [String] = []
        for index in 0..<MIDIGetNumberOfSources() {
            let source = MIDIGetSource(index)
            guard source != 0 else { continue }
            MIDIPortConnectSource(port, source, nil)
            names.append(Self.name(of: source))
        }
        sourceNames = names
    }

    private func handle(_ eventList: UnsafePointer<MIDIEventList>) {
        // The read block runs on a high priority MIDI thread; nothing here
        // touches UI state directly.
        for packet in eventList.unsafeSequence() {
            for word in packet.words() {
                let type = UInt8((word >> 20) & 0x0F)
                let status = UInt8((word >> 20) & 0xFF)
                let data1 = UInt8((word >> 8) & 0x7F)
                let data2 = UInt8(word & 0x7F)
                guard (word >> 28) & 0x0F == 2 else { continue }   // channel voice
                if type == 0x9 && data2 > 0 {
                    DispatchQueue.main.async { [weak self] in
                        self?.lastEventAt = Date()
                        self?.onNote?(data1, data2)
                    }
                } else if type == 0x8 || (type == 0x9 && data2 == 0) {
                    DispatchQueue.main.async { [weak self] in self?.onNoteOff?(data1) }
                }
                _ = status
            }
        }
    }

    private static func name(of endpoint: MIDIEndpointRef) -> String {
        var reference: Unmanaged<CFString>?
        let status = MIDIObjectGetStringProperty(endpoint, kMIDIPropertyDisplayName, &reference)
        guard status == noErr, let value = reference?.takeRetainedValue() else { return "MIDI" }
        return value as String
    }
}
