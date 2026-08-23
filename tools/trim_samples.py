"""Trim dead air from the front of the shipped samples.

The Salamander set was trimmed once for low latency, but not all the way: a
third of the files still open with silence, up to 26ms of it. Because that
padding differs between round robin variants, the same pad flams by a different
amount on successive hits, which reads as the app being late rather than as the
files being uneven.

Trimming is measured against each file's own peak, so a soft ghost note is cut
where it becomes audible rather than being held to a loud file's threshold.

    python tools/trim_samples.py --check     # report only
    python tools/trim_samples.py             # rewrite anything over the budget
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drum_pad_native as app  # noqa: E402

ONSET_THRESHOLD = 0.02
PRE_ROLL_MS = 0.5
BUDGET_MS = 1.5


def read24(path):
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        raw = source.readframes(source.getnframes())
    if width != 3:
        raise ValueError(f"{path.name}: expected 24 bit, got {width * 8}")
    packed = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, 3)
    values = (
        packed[:, 0].astype(numpy.int32)
        | (packed[:, 1].astype(numpy.int32) << 8)
        | (packed[:, 2].astype(numpy.int32) << 16)
    )
    values = numpy.where(values & 0x800000, values - 0x1000000, values)
    return values.reshape(-1, channels), rate, channels


def write24(path, frames, rate, channels):
    values = frames.reshape(-1).astype(numpy.int32)
    packed = numpy.empty((len(values), 3), dtype=numpy.uint8)
    packed[:, 0] = values & 0xFF
    packed[:, 1] = (values >> 8) & 0xFF
    packed[:, 2] = (values >> 16) & 0xFF
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(3)
        target.setframerate(rate)
        target.writeframes(packed.tobytes())


def onset_frames(frames, threshold=ONSET_THRESHOLD):
    mono = numpy.max(numpy.abs(frames), axis=1).astype(numpy.float32)
    peak = float(mono.max()) or 1.0
    loud = numpy.flatnonzero(mono > peak * threshold)
    return int(loud[0]) if len(loud) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report without rewriting")
    parser.add_argument("--budget", type=float, default=BUDGET_MS)
    args = parser.parse_args()

    trimmed = 0
    worst_before = 0.0
    worst_after = 0.0
    for name in app.all_sample_files():
        path = app.sample_path(name)
        frames, rate, channels = read24(path)
        start = onset_frames(frames)
        before = start / rate * 1000.0
        worst_before = max(worst_before, before)
        if before <= args.budget:
            worst_after = max(worst_after, before)
            continue

        keep = max(0, start - int(rate * PRE_ROLL_MS / 1000.0))
        after = (start - keep) / rate * 1000.0
        worst_after = max(worst_after, after)
        print(f"  {before:6.2f} -> {after:4.2f} ms  {name}")
        trimmed += 1
        if not args.check:
            write24(path, frames[keep:], rate, channels)

    verb = "would trim" if args.check else "trimmed"
    print(f"\n{verb} {trimmed} files; worst onset {worst_before:.2f} -> {worst_after:.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
