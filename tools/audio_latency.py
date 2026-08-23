"""Measure what each audio output actually costs in latency.

The figure a driver advertises is an estimate and was out by a factor of four on
this machine, so this opens a stream on every output and reports what CoreAudio
negotiated. Run it, plug or unplug headphones, and run it again: the built-in
device keeps one entry and switches its output source, so the only way to know
whether the headphone jack is faster than the speakers is to measure both.

    python tools/audio_latency.py
    python tools/audio_latency.py --blocks 64 128 256
"""

import argparse
import subprocess
import sys

BLOCKS = (64, 128, 256)
RATE = 48000


def output_source():
    """What the built-in device is currently driving, speakers or headphones."""
    try:
        report = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    sources = {}
    device = None
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(("Devices", "Audio")):
            device = stripped[:-1]
        elif stripped.startswith("Output Source:") and device:
            sources[device] = stripped.split(":", 1)[1].strip()
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, nargs="+", default=list(BLOCKS))
    parser.add_argument("--rate", type=int, default=RATE)
    args = parser.parse_args()

    import sounddevice

    sources = output_source()
    results = []
    print(f"{'output':30s} {'source':22s} " + "".join(f"{block:>10d}" for block in args.blocks))
    for index, info in enumerate(sounddevice.query_devices()):
        if not info["max_output_channels"]:
            continue
        name = info["name"]
        row = []
        for block in args.blocks:
            try:
                with sounddevice.OutputStream(
                    device=index, samplerate=args.rate, channels=2,
                    blocksize=block, latency="low",
                ) as stream:
                    row.append(stream.latency * 1000.0)
            except Exception:
                row.append(None)
        measured = [value for value in row if value is not None]
        if measured:
            results.append((min(measured), name))
        cells = "".join(f"{value:9.2f}m" if value is not None else f"{'--':>10s}" for value in row)
        print(f"{name[:30]:30s} {sources.get(name, '-')[:22]:22s} {cells}")

    if results:
        results.sort()
        best, name = results[0]
        print(f"\nFastest: {name} at {best:.1f} ms")
        if len(results) > 1:
            print(f"Slowest costs {results[-1][0] - best:.1f} ms more than that.")
    print("\nBlock size moves this by a few ms at most; the device buffer is the rest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
