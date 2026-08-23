import SwiftUI

/// Pad layout and kit contents, generated from the desktop app so the two do
/// not drift. Sounds are the CC0 TR-808 set; see LICENSE-SAMPLES.
struct Pad: Identifiable {
    let id: Int
    let name: String
    let sound: String
    let file: String
    let hue: Color
    let note: UInt8?
}

enum Kit {
    /// Pad 0 is bottom left, matching the hardware and the desktop grid.
    static let pads: [Pad] = [
        Pad(id: 0, name: "Kick", sound: "808 Kick Long",
            file: "kick8_long.wav",
            hue: Color(red: 0.847, green: 0.345, blue: 0.247), note: 35),
        Pad(id: 1, name: "Snare", sound: "808 Snare",
            file: "snare8.wav",
            hue: Color(red: 0.208, green: 0.435, blue: 0.702), note: 38),
        Pad(id: 2, name: "Closed Hat", sound: "808 Hat",
            file: "hat8.wav",
            hue: Color(red: 0.180, green: 0.490, blue: 0.357), note: 42),
        Pad(id: 3, name: "Open Hat", sound: "808 Open Hat",
            file: "openhat8.wav",
            hue: Color(red: 0.110, green: 0.482, blue: 0.569), note: 46),
        Pad(id: 4, name: "Low Tom", sound: "808 Low Tom",
            file: "lowtom8.wav",
            hue: Color(red: 0.467, green: 0.322, blue: 0.639), note: 45),
        Pad(id: 5, name: "Mid Tom", sound: "808 Mid Tom",
            file: "midtom8.wav",
            hue: Color(red: 0.816, green: 0.604, blue: 0.141), note: 48),
        Pad(id: 6, name: "High Tom", sound: "808 High Tom",
            file: "hitom8.wav",
            hue: Color(red: 0.690, green: 0.424, blue: 0.180), note: 50),
        Pad(id: 7, name: "Floor Tom", sound: "808 Conga",
            file: "conga8.wav",
            hue: Color(red: 0.373, green: 0.353, blue: 0.635), note: 41),
        Pad(id: 8, name: "Clap", sound: "808 Clap",
            file: "clap8.wav",
            hue: Color(red: 0.608, green: 0.310, blue: 0.247), note: 39),
        Pad(id: 9, name: "Rim", sound: "808 Rim",
            file: "rim8.wav",
            hue: Color(red: 0.702, green: 0.227, blue: 0.337), note: 37),
        Pad(id: 10, name: "Cowbell", sound: "808 Cowbell",
            file: "cowbell8.wav",
            hue: Color(red: 0.502, green: 0.443, blue: 0.165), note: 56),
        Pad(id: 11, name: "Crash", sound: "Glass Shatter",
            file: "glass_shatter_1.wav",
            hue: Color(red: 0.376, green: 0.651, blue: 0.729), note: 49),
        Pad(id: 12, name: "Ride", sound: "808 Cymbal",
            file: "cymbal8.wav",
            hue: Color(red: 0.337, green: 0.439, blue: 0.435), note: 51),
        Pad(id: 13, name: "Tamb", sound: "Glass Break",
            file: "glass_break_1.wav",
            hue: Color(red: 0.471, green: 0.729, blue: 0.784), note: 54),
        Pad(id: 14, name: "Shaker", sound: "808 Maraca",
            file: "maraca8.wav",
            hue: Color(red: 0.298, green: 0.549, blue: 0.455), note: 70),
        Pad(id: 15, name: "Clave", sound: "Glass Tap",
            file: "glass_tap_1.wav",
            hue: Color(red: 0.588, green: 0.804, blue: 0.839), note: 75),
    ]

    static func pad(forNote note: UInt8) -> Pad? {
        pads.first { $0.note == note }
    }
}
