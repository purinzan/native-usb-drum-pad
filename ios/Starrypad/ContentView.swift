import SwiftUI

struct ContentView: View {
    @StateObject private var midi = MIDIInput()
    @State private var player = SamplePlayer()

    @State private var lit: [Int: Double] = [:]          // pad id -> energy
    @State private var selected = 0
    @State private var velocityFloor = 1
    @State private var lastHit = "—"
    @State private var lastVelocity = 0
    @State private var loaded = 0

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 4)
    /// Pad 0 is bottom left, so the grid is drawn from the top row down.
    private var rows: [[Pad]] {
        stride(from: 12, through: 0, by: -4).map { Array(Kit.pads[$0..<($0 + 4)]) }
    }

    var body: some View {
        VStack(spacing: 12) {
            header
            VStack(spacing: 8) {
                ForEach(rows.indices, id: \.self) { row in
                    HStack(spacing: 8) {
                        ForEach(rows[row]) { pad in padView(pad) }
                    }
                    .frame(maxHeight: .infinity)
                }
            }
            .frame(maxHeight: .infinity)
            floorControl
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.ground)
        .preferredColorScheme(.dark)
        .onAppear(perform: begin)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("STARRYPAD").font(.system(size: 12, weight: .semibold)).kerning(2.4)
                    .foregroundStyle(Palette.accent)
                Text(midi.sourceNames.first ?? "No MIDI in")
                    .font(.system(size: 12)).foregroundStyle(Palette.ink3)
            }
            Spacer()
            readout("Now", lastHit, accent: false)
            readout("Vel", lastVelocity > 0 ? "\(lastVelocity)" : "—", accent: true)
            readout("Out", String(format: "%.1f ms", player.outputLatencyMilliseconds), accent: false)
        }
    }

    private func readout(_ caption: String, _ value: String, accent: Bool) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(caption.uppercased()).font(.system(size: 9, weight: .semibold)).kerning(1.4)
                .foregroundStyle(Palette.ink3)
            Text(value).font(.system(size: 15, weight: .medium, design: .monospaced))
                .foregroundStyle(accent ? Palette.accent : Palette.ink)
                .lineLimit(1)
        }
        .frame(minWidth: 62, alignment: .leading)
    }

    private func padView(_ pad: Pad) -> some View {
        let energy = lit[pad.id] ?? 0
        let isSelected = selected == pad.id
        let sounding = energy > 0
        return GeometryReader { geometry in
            ZStack(alignment: .top) {
                RoundedRectangle(cornerRadius: 8)
                    .fill(sounding ? Palette.padHit : Palette.pad)
                RoundedRectangle(cornerRadius: 8)
                    .fill(Palette.accentSoft.opacity(energy * 0.85))
                Rectangle()
                    .fill(Palette.hueHint(pad.hue))
                    .frame(height: 3)
                    .clipShape(UnevenRoundedRectangle(topLeadingRadius: 8, topTrailingRadius: 8))
                Text(pad.sound.uppercased())
                    .font(.system(size: 11, weight: .semibold)).kerning(0.8)
                    .foregroundStyle(sounding ? Palette.accent : isSelected ? Palette.ink : Palette.ink2)
                    .multilineTextAlignment(.center)
                    .minimumScaleFactor(0.7)
                    .lineLimit(2)
                    .padding(.horizontal, 6)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(sounding || isSelected ? Palette.accent : Palette.rule,
                                  lineWidth: sounding || isSelected ? 2 : 1)
            )
            .shadow(color: Palette.accent.opacity(energy * 0.55), radius: 4 + 12 * energy)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onEnded { touch in
                        // Touch carries no velocity, so the pad does: the
                        // higher up you hit it, the harder it lands.
                        let depth = 1.0 - min(max(touch.location.y / geometry.size.height, 0), 1)
                        strike(pad, velocity: 24 + Int(depth * 103))
                    }
            )
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var floorControl: some View {
        HStack(spacing: 10) {
            Text("VELOCITY FLOOR").font(.system(size: 10, weight: .semibold)).kerning(1.4)
                .foregroundStyle(Palette.ink3)
            Text("\(velocityFloor)").font(.system(size: 15, weight: .medium, design: .monospaced))
                .foregroundStyle(Palette.ink).frame(minWidth: 28)
            Stepper("", value: $velocityFloor, in: 1...120).labelsHidden().tint(Palette.accent)
            Spacer()
            Text("\(loaded) sounds").font(.system(size: 11)).foregroundStyle(Palette.ink3)
        }
        .padding(.horizontal, 4)
    }

    private func begin() {
        loaded = player.preload(Kit.pads)
        player.start()
        midi.onNote = { note, velocity in
            guard let pad = Kit.pad(forNote: note) else { return }
            strike(pad, velocity: Int(velocity))
        }
        midi.start()
    }

    private func strike(_ pad: Pad, velocity: Int) {
        let expanded = Velocity.expand(velocity, floor: velocityFloor)
        player.play(pad, velocity: expanded)
        selected = pad.id
        lastHit = pad.sound
        lastVelocity = expanded
        // Brightness and hold both scale with velocity, as on the desktop.
        let energy = Double(expanded) / 127.0
        lit[pad.id] = energy
        let hold = 0.075 + 0.11 * energy
        DispatchQueue.main.asyncAfter(deadline: .now() + hold) {
            withAnimation(.easeOut(duration: 0.12)) { lit[pad.id] = 0 }
        }
    }
}
