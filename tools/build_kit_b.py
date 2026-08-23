"""Build the sounds behind Kit B: a TR-808 core plus synthesised glass.

Two sources, for two different reasons.

The drum machine sounds come from Michael Fischer's TR-808 Sample Set, released
under CC0 by the TidalCycles project. They arrive as 16 bit 44.1kHz mono at about
half scale, so this trims each one to its transient, resamples to 48kHz in the
frequency domain rather than by linear interpolation, normalises it, and writes
24 bit stereo to match the rest of the library.

The glass is generated here instead of downloaded. A drum pad needs a one shot
that starts on sample zero and stops when the pad is released, which a field
recording of breaking glass is not; and generating it means the licence is ours
and the velocity layers can actually differ rather than being one recording at
three volumes.

    python tools/build_kit_b.py --pack /path/to/sounds-tr808-fischer
    python tools/build_kit_b.py --glass-only
"""

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
TR808_OUT = SAMPLES / "tr808"
GLASS_OUT = SAMPLES / "impact"

RATE = 48000
ONSET_THRESHOLD = 0.02
PRE_ROLL_MS = 0.3
FADE_OUT_MS = 6.0

# A drum machine plays the same sample every time, so the pack's tone and decay
# positions are separate sounds to choose between, not round robin variants.
TR808_PLAN = {
    "kick8":         ("bd8", "BD2510", 0.95),
    "kick8_tight":   ("bd8", "BD0000", 0.95),
    "kick8_long":    ("bd8", "BD7550", 0.95),
    "snare8":        ("sd8", "SD2525", 0.90),
    "snare8_bright": ("sd8", "SD7500", 0.90),
    "hat8":          ("ch8", "CH", 0.72),
    "openhat8":      ("oh8", "OH25", 0.72),
    "openhat8_long": ("oh8", "OH75", 0.72),
    "clap8":         ("cp8", "CP", 0.88),
    "rim8":          ("rs8", "RS", 0.80),
    "cowbell8":      ("cb8", "CB", 0.78),
    "cymbal8":       ("cy8", "CY2525", 0.68),
    "hitom8":        ("ht8", "HT50", 0.88),
    "midtom8":       ("mt8", "MT50", 0.88),
    "lowtom8":       ("lt8", "LT50", 0.90),
    "conga8":        ("hc8", "HC50", 0.85),
    "maraca8":       ("ma8", "MA", 0.62),
    "clave8":        ("cl8", "CL", 0.72),
}


def read_wave(path):
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        raw = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16 bit, got {width * 8}")
    audio = numpy.frombuffer(raw, dtype="<i2").astype(numpy.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def write_wave(path, audio):
    """Write float mono in the library's format: 24 bit, 48kHz, stereo."""
    audio = numpy.clip(audio, -1.0, 1.0)
    scaled = numpy.round(audio * 8388607.0).astype(numpy.int32)
    packed = numpy.empty((len(scaled), 3), dtype=numpy.uint8)
    packed[:, 0] = scaled & 0xFF
    packed[:, 1] = (scaled >> 8) & 0xFF
    packed[:, 2] = (scaled >> 16) & 0xFF
    stereo = numpy.repeat(packed[:, None, :], 2, axis=1).reshape(-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(3)
        target.setframerate(RATE)
        target.writeframes(stereo.tobytes())
    return path


def resample(audio, source_rate, target_rate=RATE):
    """Band limited resample in the frequency domain.

    Linear interpolation is what the app uses for user drops, where speed
    matters more; for files that ship with the app it is worth spending the FFT
    to keep the hats and cymbals from aliasing.
    """
    if source_rate == target_rate or not len(audio):
        return audio
    target_length = max(1, round(len(audio) * target_rate / source_rate))
    spectrum = numpy.fft.rfft(audio)
    keep = min(len(spectrum), target_length // 2 + 1)
    resampled_spectrum = numpy.zeros(target_length // 2 + 1, dtype=complex)
    resampled_spectrum[:keep] = spectrum[:keep]
    return numpy.fft.irfft(resampled_spectrum, target_length).astype(numpy.float32) * (
        target_length / len(audio)
    )


def trim_to_onset(audio, rate):
    """Drop everything before the transient, keeping a hair of pre-roll."""
    if not len(audio):
        return audio
    peak = float(numpy.max(numpy.abs(audio))) or 1.0
    loud = numpy.flatnonzero(numpy.abs(audio) > peak * ONSET_THRESHOLD)
    if not len(loud):
        return audio
    start = max(0, int(loud[0] - rate * PRE_ROLL_MS / 1000.0))
    end = int(loud[-1] + 1)
    return audio[start:end]


def fade_tail(audio, rate, milliseconds=FADE_OUT_MS):
    frames = min(len(audio), int(rate * milliseconds / 1000.0))
    if frames > 1:
        audio = audio.copy()
        audio[-frames:] *= numpy.linspace(1.0, 0.0, frames, dtype=numpy.float32)
    return audio


def normalise(audio, target_peak):
    peak = float(numpy.max(numpy.abs(audio)))
    return audio * (target_peak / peak) if peak else audio


def build_808(pack):
    written = []
    for name, (folder, stem, gain) in TR808_PLAN.items():
        candidates = [pack / folder / f"{stem}.WAV", pack / folder / f"{stem}.wav"]
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            print(f"  missing {folder}/{stem}")
            continue
        audio, rate = read_wave(source)
        audio = trim_to_onset(audio, rate)
        audio = fade_tail(audio, rate)
        audio = resample(audio, rate)
        audio = normalise(audio, gain)
        audio = fade_tail(audio, RATE, 3.0)
        target = write_wave(TR808_OUT / f"{name}.wav", audio)
        written.append(target)
        print(f"  {source.name:12s} -> {target.name:20s} {len(audio) / RATE:.3f}s")
    return written


# --- glass ----------------------------------------------------------------

def _noise(rng, frames):
    return rng.standard_normal(frames).astype(numpy.float32)


def _highpass(audio, amount=0.85):
    """One pole difference filter: enough to take the body out of noise."""
    out = numpy.empty_like(audio)
    out[0] = audio[0]
    out[1:] = audio[1:] - amount * audio[:-1]
    return out


def _partials(rng, frames, count, low, high, decay_low, decay_high):
    """Inharmonic ringing, which is what makes glass sound like glass."""
    time_axis = numpy.arange(frames, dtype=numpy.float32) / RATE
    out = numpy.zeros(frames, dtype=numpy.float32)
    for _ in range(count):
        frequency = math.exp(rng.uniform(math.log(low), math.log(high)))
        decay = rng.uniform(decay_low, decay_high)
        phase = rng.uniform(0.0, math.tau)
        amplitude = rng.uniform(0.35, 1.0) / math.sqrt(frequency / low)
        out += amplitude * numpy.sin(math.tau * frequency * time_axis + phase) * numpy.exp(
            -time_axis / decay
        )
    return out


def _debris(rng, frames, grains, spread, decay):
    """Scattered shards: short pings thinning out over time."""
    out = numpy.zeros(frames, dtype=numpy.float32)
    time_axis = numpy.arange(frames, dtype=numpy.float32) / RATE
    for _ in range(grains):
        start = int(abs(rng.exponential(spread * 0.35)) * RATE)
        if start >= frames - 64:
            continue
        length = min(frames - start, int(rng.uniform(0.004, 0.03) * RATE))
        local = time_axis[:length]
        frequency = math.exp(rng.uniform(math.log(2200), math.log(11000)))
        grain = numpy.sin(math.tau * frequency * local) * numpy.exp(-local / rng.uniform(0.002, 0.012))
        out[start:start + length] += grain * rng.uniform(0.05, 0.3) * math.exp(-start / RATE / decay)
    return out


GLASS_PLAN = {
    # name: (seconds, partials, partial decay range, grains, debris spread, transient)
    "glass_tap": (0.55, 22, (0.05, 0.22), 24, 0.10, 0.35),
    "glass_break": (1.10, 30, (0.08, 0.45), 130, 0.30, 0.75),
    "glass_shatter": (2.10, 38, (0.12, 0.80), 320, 0.75, 1.00),
}


def build_glass(variants=3):
    written = []
    for name, (seconds, partial_count, decay_range, grains, spread, transient) in GLASS_PLAN.items():
        for variant in range(1, variants + 1):
            rng = numpy.random.default_rng(abs(hash((name, variant))) % (2**32))
            frames = int(seconds * RATE)
            time_axis = numpy.arange(frames, dtype=numpy.float32) / RATE

            body = _partials(rng, frames, partial_count, 1400.0, 9000.0, *decay_range)
            crack = _highpass(_noise(rng, frames)) * numpy.exp(-time_axis / 0.006) * transient
            shards = _debris(rng, frames, grains, spread, max(0.12, seconds * 0.45))

            audio = body * 0.5 + crack * 0.9 + shards * 1.1
            audio *= numpy.minimum(1.0, time_axis / 0.0004)      # no click on frame zero
            audio = fade_tail(audio, RATE, 25.0)
            audio = normalise(audio, 0.88)
            target = write_wave(GLASS_OUT / f"{name}_{variant}.wav", audio)
            written.append(target)
            print(f"  {target.name:22s} {seconds:.2f}s  {partial_count} partials  {grains} shards")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, help="unpacked sounds-tr808-fischer checkout")
    parser.add_argument("--glass-only", action="store_true")
    args = parser.parse_args()

    if not args.glass_only:
        if not args.pack or not args.pack.exists():
            parser.error("--pack is required unless --glass-only is given")
        print(f"TR-808 from {args.pack}")
        built = build_808(args.pack)
        print(f"{len(built)} files -> {TR808_OUT.relative_to(ROOT)}\n")

    print("Glass")
    built = build_glass()
    print(f"{len(built)} files -> {GLASS_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
