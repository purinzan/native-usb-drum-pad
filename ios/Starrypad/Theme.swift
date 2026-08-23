import SwiftUI

/// Colour tokens generated from theme.py. Colour is a role, not a decoration:
/// pads are graphite and the accent marks what is happening now.
enum Palette {
    static let ground = Color(red: 0.078, green: 0.090, blue: 0.094)
    static let panel = Color(red: 0.114, green: 0.129, blue: 0.133)
    static let panel2 = Color(red: 0.149, green: 0.169, blue: 0.173)
    static let pad = Color(red: 0.141, green: 0.165, blue: 0.169)
    static let padHit = Color(red: 0.212, green: 0.243, blue: 0.247)
    static let rule = Color(red: 0.192, green: 0.224, blue: 0.227)
    static let ruleSoft = Color(red: 0.149, green: 0.176, blue: 0.180)
    static let ink = Color(red: 0.929, green: 0.937, blue: 0.925)
    static let ink2 = Color(red: 0.569, green: 0.600, blue: 0.584)
    static let ink3 = Color(red: 0.392, green: 0.420, blue: 0.408)
    static let accent = Color(red: 0.914, green: 0.635, blue: 0.290)
    static let accentSoft = Color(red: 0.227, green: 0.173, blue: 0.094)
    static let onAccent = Color(red: 0.094, green: 0.075, blue: 0.071)
    static let signal = Color(red: 0.271, green: 0.722, blue: 0.651)
    static let danger = Color(red: 0.851, green: 0.345, blue: 0.306)

    /// Damp a kit colour down to the 2px identity stripe on a pad.
    static func hueHint(_ color: Color) -> Color { color.opacity(0.72) }
}
