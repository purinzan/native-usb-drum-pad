"""Render a track with a known tempo, for testing the import and chop path.

Dropping a real song on the window proves nothing about whether the tempo was
detected correctly, because there is nothing to check the answer against. This
renders a loop at a tempo it prints, long enough to trigger the long import
framing, so detection and slicing can be graded rather than eyeballed.

Built from the CC0 TR-808 set already in the repository plus a synthesised bass,
so it carries no licence of its own.

    python tools/make_test_track.py                       # 96 bpm, 96 seconds
    python tools/make_test_track.py --bpm 140 --seconds 60
    python tools/make_test_track.py --out ~/Desktop
"""

import argparse
import math
import subprocess
import shutil
import sys
import wave
from pathlib import Path

import numpy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RATE = 48000

# Sixteen steps per bar. Each entry is the 808 sound and the steps it lands on.
#
# Deliberately square. An earlier version put the kick on 0, 6 and 10, which
# reads as a 1.5 beat period and had the detector answering 96 for a 120 bpm
# file -- correctly, in that the material really was ambiguous. Test material
# that cannot be graded is worse than none, so the kick sits on the beat.
PATTERN = {
    "kick8_long": (0, 8),
    "snare8": (4, 12),
    "hat8": (0, 2, 4, 6, 8, 10, 12, 14),
    "openhat8": (14,),
    "clap8": (12,),
    "cowbell8": (),
}
# Every fourth bar, so chopping different regions gives different material.
# The extra hits land on the beat grid too, leaving the period intact.
FILL = {
    "kick8_long": (0, 8, 12),
    "snare8": (4, 12, 14),
    "hat8": (0, 1, 2, 3, 4, 6, 8, 10, 12, 14),
    "cowbell8": (6, 10),
}
BASS_ROOTS = (55.0, 55.0, 73.42, 65.41)      # A1 A1 D2 C2, one per bar of four


def load_sample(name):
    path = ROOT / "samples" / "tr808" / f"{name}.wav"
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        raw = source.readframes(source.getnframes())
    packed = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, 3)
    values = (
        packed[:, 0].astype(numpy.int32)
        | (packed[:, 1].astype(numpy.int32) << 8)
        | (packed[:, 2].astype(numpy.int32) << 16)
    )
    values = numpy.where(values & 0x800000, values - 0x1000000, values)
    return (values.reshape(-1, channels)[:, 0] / 8388608.0).astype(numpy.float32)


def bass_note(frequency, seconds):
    """A plucked sine with a little grit, so the transient is findable."""
    frames = int(seconds * RATE)
    time_axis = numpy.arange(frames, dtype=numpy.float32) / RATE
    envelope = numpy.exp(-time_axis / (seconds * 0.4))
    tone = numpy.sin(math.tau * frequency * time_axis)
    tone += 0.3 * numpy.sin(math.tau * frequency * 2 * time_axis)
    return (tone * envelope * 0.5).astype(numpy.float32)


def render(bpm, seconds):
    step = 60.0 / bpm / 4.0
    bar = step * 16
    total = int(seconds * RATE)
    track = numpy.zeros(total + RATE, dtype=numpy.float32)
    voices = {name: load_sample(name) for name in set(PATTERN) | set(FILL)}

    def place(audio, at_seconds, gain):
        start = int(at_seconds * RATE)
        end = min(len(track), start + len(audio))
        if start < len(track):
            track[start:end] += audio[: end - start] * gain

    for index in range(int(seconds / bar) + 1):
        origin = index * bar
        grid = FILL if index % 4 == 3 else PATTERN
        for name, steps in grid.items():
            for position in steps:
                gain = 0.9 if position % 4 == 0 else 0.62
                place(voices[name], origin + position * step, gain)
        root = BASS_ROOTS[index % len(BASS_ROOTS)]
        for beat in (0, 2):
            place(bass_note(root, step * 6), origin + beat * step * 4, 0.8)

    track = track[:total]
    peak = float(numpy.max(numpy.abs(track))) or 1.0
    track *= 0.89 / peak
    fade = int(0.05 * RATE)
    track[:fade] *= numpy.linspace(0.0, 1.0, fade)
    track[-fade:] *= numpy.linspace(1.0, 0.0, fade)
    return numpy.repeat(track[:, None], 2, axis=1)


def write_wav(path, stereo):
    scaled = numpy.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(scaled.tobytes())
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bpm", type=float, default=96.0)
    parser.add_argument("--seconds", type=float, default=96.0)
    parser.add_argument("--out", type=Path, default=Path.home() / "Desktop")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    stereo = render(args.bpm, args.seconds)
    stem = f"test-track-{int(args.bpm)}bpm"
    wav = write_wav(args.out / f"{stem}.wav", stereo)
    print(f"wrote {wav}  ({args.seconds:.0f}s at {args.bpm:g} bpm)")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        # An m4a as well, since that is the format SDL cannot read on its own.
        m4a = args.out / f"{stem}.m4a"
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "192k", str(m4a)],
            check=False, timeout=180,
        )
        if m4a.exists():
            print(f"wrote {m4a}")

    bar = 4 * 60.0 / args.bpm
    print(f"\nOne bar is {bar:.2f}s, so four bars is {bar * 4:.2f}s.")
    print("Drop either file on the window: the tempo it reports should match, and")
    print("the region it selects should be four bars long.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
