import os
import random
import shutil
import collections
import heapq
import json
import queue
import struct
import statistics
import sys
import tempfile
import threading
import time
import wave
import zipfile
from pathlib import Path

import icons
import theme
import typeface
from platform_backend import (
    AUDIO_DRIVER,
    IS_MACOS,
    MidiInput,
    MidiOutput,
    acquire_single_instance,
    enable_audio_thread_priority,
    enable_process_priority,
    release_audio_thread_priority,
    release_single_instance,
)

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
if AUDIO_DRIVER:
    os.environ.setdefault("SDL_AUDIODRIVER", AUDIO_DRIVER)

import pygame


ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "samples" / "salamander-lowlatency" / "OH"
SETTINGS_FILE = ROOT / "drum_pad_settings.json"
EXPORT_DIR = ROOT / "exports"
USER_SAMPLE_DIR = ROOT / "user-samples"
PROJECT_DIR = ROOT / "projects"
PROJECT_EXTENSION = ".starrypad.json"
GRAIN_FILE = ROOT / "assets" / "tex" / "grain-256.png"
APP_ICON_FILE = ROOT / "assets" / "brand" / "icon-64.png"
GRAIN_OPACITY = 10

WINDOW_SIZE = (1040, 820)
MIXER_FREQUENCY = 48000
MIXER_BUFFER = 128
UI_FPS = 60
DIAGNOSTIC_WINDOW = 256
MIDI_HEALTH_CHECK_SECONDS = 2.0
PERFORMANCE_BUFFER_SECONDS = 120.0
PERFORMANCE_PHRASE_GAP_SECONDS = 2.0
MIDI_SILENCE_HINT_SECONDS = 4.0
PAD_DRAG_THRESHOLD = 10
PAD_SWAP_FLASH_SECONDS = 0.45
HIT_FLASH_MIN = 0.075
HIT_FLASH_RANGE = 0.11
PAD_GHOST_SCALE = 0.6
PAD_GHOST_OFFSET = (16, 14)
PAD_MOVE_DELTAS = {
    pygame.K_LEFT: -1, pygame.K_RIGHT: 1, pygame.K_UP: 4, pygame.K_DOWN: -4,
}
# macOS puts app shortcuts on Command; accept Control there too rather than
# retiring a chord anyone may already have in their fingers.
COMMAND_MODIFIER = (pygame.KMOD_META | pygame.KMOD_CTRL) if IS_MACOS else pygame.KMOD_CTRL
QUIT_SHORTCUT = "Cmd-Q" if IS_MACOS else "Ctrl-Q"
AUDIO_HEALTH_CHECK_SECONDS = 2.0
AUDIO_MODES = ("Low latency", "Stable")
AUDIO_RATES = (48000, 44100)
AUDIO_BUFFERS = (64, 128, 256)
SAMPLE_PLAY_MODES = ("One-shot", "Gate", "Toggle", "Loop")
FEEL_PRESETS = {
    "Tight": {"strength": 100, "swing": 50, "nudge_ms": 0, "humanize_ms": 0},
    "Natural": {"strength": 50, "swing": 50, "nudge_ms": 0, "humanize_ms": 0},
    "Loose": {"strength": 20, "swing": 54, "nudge_ms": 0, "humanize_ms": 3},
}
PATTERN_COUNT = 8
PATTERN_LAUNCH_MODES = ("Next beat", "Next bar", "Pattern end")


def audio_mode_config(mode):
    if mode == "Stable":
        return 48000, 256
    return 48000, 128


def apply_loop_feel(events, bars, grid, strength, swing, nudge_ms, humanize_ms, bpm):
    total_beats = max(1.0, float(bars) * 4.0)
    grid = max(1 / 32, float(grid))
    amount = max(0.0, min(1.0, float(strength) / 100.0))
    swing_shift = ((max(50.0, min(75.0, float(swing))) - 50.0) / 25.0) * (grid / 2.0)
    nudge_beats = float(nudge_ms) * max(BPM_MIN, min(BPM_MAX, int(bpm))) / 60000.0
    humanize_beats = max(0.0, min(20.0, float(humanize_ms))) * max(BPM_MIN, min(BPM_MAX, int(bpm))) / 60000.0
    result = []
    for index, (beat, pad, velocity) in enumerate(events):
        beat = float(beat)
        target = round(beat / grid) * grid
        moved = beat + (target - beat) * amount
        step_index = round(target / grid)
        if step_index % 2 == 1:
            moved += swing_shift
        if humanize_beats:
            deterministic = (((index + 1) * 37 + (int(pad) + 1) * 17) % 101) / 50.0 - 1.0
            moved += deterministic * humanize_beats
        moved += nudge_beats
        result.append((moved % total_beats, int(pad), max(1, min(127, int(velocity)))))
    return sorted(result)


def event_meta_key(pad, beat):
    return f"{int(pad)}:{float(beat):.6f}"


# What a pad plays travels when the layout is rearranged. Sensitivity and
# calibration do not: they describe the physical rubber, not the sound on it.
SOUND_PAD_FIELDS = (
    "pad_synths", "custom_sample_files", "sample_edits",
    "pad_volume", "pad_pan", "pad_tune",
    "pad_punch", "pad_air", "pad_space", "pad_bus", "pad_mute",
)


def swap_event_pads(events, first, second):
    swapped = []
    for beat, pad, velocity in events:
        pad = int(pad)
        pad = second if pad == first else first if pad == second else pad
        swapped.append((beat, pad, velocity))
    return sorted(swapped)


def swap_event_meta_pads(meta, first, second):
    remapped = {}
    for key, value in meta.items():
        pad_text, _, beat_text = str(key).partition(":")
        try:
            pad, beat = int(pad_text), float(beat_text)
        except ValueError:
            remapped[key] = value
            continue
        pad = second if pad == first else first if pad == second else pad
        remapped[event_meta_key(pad, beat)] = value
    return remapped


def sanitize_event_meta(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, meta in value.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        try:
            chance = max(0, min(100, int(meta.get("chance", 100))))
            ratchet = max(1, min(4, int(meta.get("ratchet", 1))))
        except (TypeError, ValueError):
            continue
        if chance != 100 or ratchet != 1:
            result[key] = {"chance": chance, "ratchet": ratchet}
    return result


def deterministic_event_roll(cycle, pad, beat):
    return ((int(cycle) + 1) * 37 + (int(pad) + 1) * 17 + round(float(beat) * 1000) * 13) % 100 + 1


def stereo_pan_gains(pan):
    pan = max(-1.0, min(1.0, float(pan)))
    return (1.0, 1.0 + pan) if pan < 0 else (1.0 - pan, 1.0)


def pitch_shift_array(samples, semitones):
    import numpy

    values = numpy.asarray(samples)
    if values.ndim == 0 or len(values) == 0:
        return numpy.ascontiguousarray(values)
    semitones = max(-12.0, min(12.0, float(semitones)))
    if semitones == 0 or len(values) < 2:
        return numpy.ascontiguousarray(values)
    ratio = 2.0 ** (semitones / 12.0)
    output_length = max(1, round(len(values) / ratio))
    positions = numpy.arange(output_length, dtype=numpy.float64) * ratio
    source_positions = numpy.arange(len(values), dtype=numpy.float64)
    if values.ndim == 1:
        shifted = numpy.interp(positions, source_positions, values)
    else:
        shifted = numpy.column_stack([
            numpy.interp(positions, source_positions, values[:, channel])
            for channel in range(values.shape[1])
        ])
    return numpy.ascontiguousarray(numpy.clip(shifted, -32768, 32767).astype(values.dtype))


def default_sample_edit():
    return {
        "start": 0.0, "end": 1.0, "normalize": False, "reverse": False,
        "attack_ms": 0, "release_ms": 8, "tune": 0, "mode": "One-shot",
        "choke_group": None,
        "source_bpm": None, "source_bars": None, "stretch_mode": "Off",
    }


def sanitize_sample_edit(value):
    fallback = default_sample_edit()
    if not isinstance(value, dict):
        return fallback
    try:
        start = max(0.0, min(0.99, float(value.get("start", 0.0))))
        end = max(start + 0.01, min(1.0, float(value.get("end", 1.0))))
        mode = str(value.get("mode", "One-shot"))
        choke_group = value.get("choke_group")
        choke_group = choke_group[:32] if isinstance(choke_group, str) and choke_group else None
        source_bpm = value.get("source_bpm")
        source_bpm = round(max(40.0, min(240.0, float(source_bpm))), 1) if source_bpm is not None else None
        source_bars = value.get("source_bars")
        source_bars = int(source_bars) if source_bars in (1, 2, 4, 8, 16) else None
        stretch_mode = str(value.get("stretch_mode", "Off"))
        return {
            "start": round(start, 3), "end": round(end, 3),
            "normalize": bool(value.get("normalize", False)),
            "reverse": bool(value.get("reverse", False)),
            "attack_ms": max(0, min(500, int(value.get("attack_ms", 0)))),
            "release_ms": max(0, min(1000, int(value.get("release_ms", 8)))),
            "tune": max(-12, min(12, int(value.get("tune", 0)))),
            "mode": mode if mode in SAMPLE_PLAY_MODES else "One-shot",
            "choke_group": choke_group,
            "source_bpm": source_bpm, "source_bars": source_bars,
            "stretch_mode": stretch_mode if stretch_mode in ("Off", "Repitch", "Stretch") else "Off",
        }
    except (TypeError, ValueError):
        return fallback


def apply_sample_edits(samples, edit, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples)
    if values.ndim == 1:
        values = numpy.repeat(values[:, None], 2, axis=1)
    settings = sanitize_sample_edit(edit)
    start = min(len(values) - 1, round(len(values) * settings["start"]))
    end = max(start + 1, min(len(values), round(len(values) * settings["end"])))
    output = values[start:end].astype(numpy.float32, copy=True)
    if settings["reverse"]:
        output = output[::-1].copy()
    if settings["normalize"] and len(output):
        peak = float(numpy.max(numpy.abs(output)))
        if peak > 0:
            output *= 30000.0 / peak
    attack_ms = settings["attack_ms"]
    release_ms = settings["release_ms"]
    if settings["mode"] in ("Toggle", "Loop"):
        attack_ms = max(3, attack_ms)
        release_ms = max(3, release_ms)
    attack = min(len(output), round(sample_rate * attack_ms / 1000.0))
    release = min(len(output), round(sample_rate * release_ms / 1000.0))
    if attack:
        output[:attack] *= numpy.linspace(0.0, 1.0, attack, dtype=numpy.float32)[:, None]
    if release:
        output[-release:] *= numpy.linspace(1.0, 0.0, release, dtype=numpy.float32)[:, None]
    return numpy.ascontiguousarray(numpy.clip(output, -32768, 32767).astype(values.dtype))


def equal_slice_markers(frame_count, slice_count):
    frame_count = max(1, int(frame_count))
    slice_count = max(1, min(16, int(slice_count)))
    return [round(index * frame_count / slice_count) for index in range(slice_count + 1)]


def transient_slice_markers(samples, slice_count, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples, dtype=numpy.float32)
    if values.ndim > 1:
        values = numpy.max(numpy.abs(values), axis=1)
    else:
        values = numpy.abs(values)
    target = max(1, min(16, int(slice_count)))
    if len(values) < target * 2:
        return equal_slice_markers(len(values), target)
    hop = max(1, round(sample_rate * 0.005))
    envelope = numpy.array([
        float(numpy.sqrt(numpy.mean(values[index:index + hop] ** 2)))
        for index in range(0, len(values), hop)
    ])
    novelty = numpy.maximum(0.0, numpy.diff(envelope, prepend=envelope[0]))
    minimum_gap = max(1, len(envelope) // (target * 3))
    selected = []
    for candidate in numpy.argsort(novelty)[::-1]:
        candidate = int(candidate)
        if candidate == 0 or any(abs(candidate - other) < minimum_gap for other in selected):
            continue
        selected.append(candidate)
        if len(selected) >= target - 1:
            break
    markers = [0] + sorted(min(len(values) - 1, value * hop) for value in selected) + [len(values)]
    if len(markers) != target + 1:
        return equal_slice_markers(len(values), target)
    return markers


def slice_sample_audio(samples, markers, play_through=False, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples)
    clean = sorted(set(max(0, min(len(values), int(value))) for value in markers))
    if not clean or clean[0] != 0:
        clean.insert(0, 0)
    if clean[-1] != len(values):
        clean.append(len(values))
    result = []
    fade = max(1, round(sample_rate * 0.002))
    for index, start in enumerate(clean[:-1]):
        end = len(values) if play_through else clean[index + 1]
        part = values[start:end].copy()
        edge = min(fade, len(part) // 2)
        if edge:
            part[:edge] = (part[:edge].astype(numpy.float32) * numpy.linspace(0, 1, edge)[:, None]).astype(part.dtype)
            part[-edge:] = (part[-edge:].astype(numpy.float32) * numpy.linspace(1, 0, edge)[:, None]).astype(part.dtype)
        result.append(numpy.ascontiguousarray(part))
    return result


def detect_sample_tempo(samples, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples, dtype=numpy.float32)
    if values.ndim > 1:
        values = numpy.mean(values, axis=1)
    hop = max(1, round(sample_rate * 0.01))
    energy = numpy.array([
        float(numpy.sqrt(numpy.mean(values[index:index + hop] ** 2)))
        for index in range(0, len(values), hop)
    ])
    onset = numpy.maximum(0.0, numpy.diff(energy, prepend=energy[0]))
    onset -= numpy.mean(onset)
    correlation = numpy.correlate(onset, onset, mode="full")[len(onset) - 1:]
    minimum_lag = max(1, round(60.0 / 200.0 / 0.01))
    maximum_lag = min(len(correlation) - 1, round(60.0 / 40.0 / 0.01))
    if maximum_lag <= minimum_lag or not numpy.any(correlation[minimum_lag:maximum_lag + 1] > 0):
        return None
    lag = minimum_lag + int(numpy.argmax(correlation[minimum_lag:maximum_lag + 1]))
    raw_bpm = 60.0 / (lag * 0.01)
    duration = len(values) / float(sample_rate)
    candidates = []
    for multiplier in (0.5, 1.0, 2.0, 4.0):
        bpm = raw_bpm * multiplier
        if 40 <= bpm <= 240:
            beats = duration * bpm / 60.0
            nearest_bars = min((1, 2, 4, 8, 16), key=lambda bars: abs(beats - bars * 4))
            bar_error = abs(beats - nearest_bars * 4)
            range_penalty = 0 if 85 <= bpm <= 145 else 0.75
            candidates.append((bar_error + range_penalty, bpm, nearest_bars))
    _score, bpm, bars = min(candidates)
    confidence = float(correlation[lag] / max(1e-9, correlation[0]))
    return round(bpm, 1), bars, max(0.0, min(1.0, confidence))


def time_stretch_array(samples, speed):
    import numpy
    from audiotsm import wsola
    from audiotsm.io.array import ArrayReader, ArrayWriter

    values = numpy.asarray(samples)
    speed = max(0.25, min(4.0, float(speed)))
    if abs(speed - 1.0) < 0.001 or len(values) < 2048:
        return numpy.ascontiguousarray(values)
    stereo = values if values.ndim > 1 else numpy.repeat(values[:, None], 2, axis=1)
    scale = 32768.0 if numpy.issubdtype(stereo.dtype, numpy.integer) else max(1.0, float(numpy.max(numpy.abs(stereo))))
    reader = ArrayReader((stereo.astype(numpy.float32) / scale).T.copy())
    writer = ArrayWriter(stereo.shape[1])
    processor = wsola(stereo.shape[1], speed=speed)
    processor.run(reader, writer)
    output = (writer.data.T * scale)
    expected_length = max(1, round(len(stereo) / speed))
    if len(output) < expected_length:
        output = numpy.pad(output, ((0, expected_length - len(output)), (0, 0)))
    elif len(output) > expected_length:
        output = output[:expected_length]
    edge = min(len(output), round(MIXER_FREQUENCY * 0.008))
    if edge:
        output[-edge:] *= numpy.linspace(1.0, 0.0, edge, dtype=numpy.float32)[:, None]
    return numpy.ascontiguousarray(numpy.clip(output, -32768, 32767).astype(values.dtype))


def apply_sample_tempo(samples, edit, target_bpm):
    settings = sanitize_sample_edit(edit)
    source_bpm = settings.get("source_bpm")
    mode = settings.get("stretch_mode", "Off")
    if not source_bpm or mode == "Off":
        return samples
    speed = float(target_bpm) / source_bpm
    if mode == "Repitch":
        import math
        return pitch_shift_array(samples, 12.0 * math.log2(speed))
    return time_stretch_array(samples, speed)


def apply_sound_macros(samples, punch=0, air=0, space=0, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples)
    stereo = values if values.ndim > 1 else numpy.repeat(values[:, None], 2, axis=1)
    scale = 32768.0 if numpy.issubdtype(stereo.dtype, numpy.integer) or numpy.max(numpy.abs(stereo), initial=0) > 2 else 1.0
    audio = stereo.astype(numpy.float32) / scale
    punch_amount = max(0.0, min(1.0, float(punch) / 100.0))
    air_amount = max(0.0, min(1.0, float(air) / 100.0))
    space_amount = max(0.0, min(1.0, float(space) / 100.0))
    if punch_amount:
        transient = numpy.diff(audio, axis=0, prepend=audio[:1])
        audio += transient * (0.42 * punch_amount)
        drive = 1.0 + 2.4 * punch_amount
        audio = numpy.tanh(audio * drive) / numpy.tanh(drive)
    if air_amount:
        high = numpy.diff(audio, axis=0, prepend=audio[:1])
        audio += high * (0.32 * air_amount)
    if space_amount:
        delays = ((0.037, 0.34), (0.061, 0.22), (0.113, 0.13))
        tail = round(sample_rate * delays[-1][0])
        wet = numpy.zeros((len(audio) + tail, audio.shape[1]), dtype=numpy.float32)
        wet[:len(audio)] += audio
        for seconds, gain in delays:
            offset = round(sample_rate * seconds)
            wet[offset:offset + len(audio)] += audio * (gain * space_amount)
        audio = wet
    result = numpy.clip(audio * scale, -32768, 32767)
    return numpy.ascontiguousarray(result.astype(values.dtype))


def apply_master_limiter(samples, ceiling=0.98):
    import numpy

    values = numpy.asarray(samples, dtype=numpy.float32)
    limit = 32767.0 * max(0.5, min(1.0, float(ceiling)))
    return numpy.clip(values, -limit, limit)


def apply_perform_fx(samples, filter_amount=0, delay=0, stutter=0, crush=0, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples)
    stereo = values if values.ndim > 1 else numpy.repeat(values[:, None], 2, axis=1)
    scale = 32768.0 if numpy.issubdtype(stereo.dtype, numpy.integer) or numpy.max(numpy.abs(stereo), initial=0) > 2 else 1.0
    audio = stereo.astype(numpy.float32) / scale
    filter_mix = max(0.0, min(1.0, float(filter_amount) / 100.0))
    delay_mix = max(0.0, min(1.0, float(delay) / 100.0))
    stutter_mix = max(0.0, min(1.0, float(stutter) / 100.0))
    crush_mix = max(0.0, min(1.0, float(crush) / 100.0))
    if filter_mix and len(audio):
        width = 1 + round(filter_mix * 31)
        kernel = numpy.ones(width, dtype=numpy.float32) / width
        low = numpy.column_stack([
            numpy.convolve(audio[:, channel], kernel, mode="same") for channel in range(audio.shape[1])
        ])
        audio = audio * (1.0 - filter_mix) + low * filter_mix
    if delay_mix and len(audio):
        wet = audio.copy()
        for seconds, gain in ((0.125, 0.38), (0.25, 0.22)):
            offset = round(sample_rate * seconds)
            if offset < len(audio):
                wet[offset:] += audio[:-offset] * gain * delay_mix
        audio = wet
    if stutter_mix and len(audio):
        slice_frames = max(1, round(sample_rate * 0.0625))
        phase = (numpy.arange(len(audio)) // slice_frames) % 4
        gate = numpy.where(phase == 3, 1.0 - stutter_mix, 1.0).astype(numpy.float32)
        audio *= gate[:, None]
    if crush_mix:
        levels = max(16, round(32768 ** (1.0 - crush_mix * 0.72)))
        crushed = numpy.round(audio * levels) / levels
        audio = audio * (1.0 - crush_mix) + crushed * crush_mix
    result = numpy.clip(audio * scale, -32768, 32767)
    return numpy.ascontiguousarray(result.astype(values.dtype))


def apply_perform_fx_automation(samples, events, bpm, initial=None, sample_rate=MIXER_FREQUENCY):
    import numpy

    values = numpy.asarray(samples)
    if not events:
        state = dict(initial or {})
        return apply_perform_fx(values, *(state.get(field, 0) for field in ("filter", "delay", "stutter", "crush")), sample_rate)
    output = values.copy()
    state = {field: 0 for field in ("filter", "delay", "stutter", "crush")}
    state.update(initial or {})
    grouped = []
    for beat, field, value in sorted(events):
        frame = max(0, min(len(values), round(float(beat) * sample_rate * 60.0 / float(bpm))))
        if grouped and grouped[-1][0] == frame:
            grouped[-1][1].append((field, value))
        else:
            grouped.append((frame, [(field, value)]))
    cursor = 0
    for frame, changes in grouped:
        if frame > cursor:
            output[cursor:frame] = apply_perform_fx(
                values[cursor:frame], *(state[field] for field in ("filter", "delay", "stutter", "crush")), sample_rate
            )
        for field, value in changes:
            if field in state:
                state[field] = max(0, min(100, int(value)))
        cursor = frame
    if cursor < len(values):
        output[cursor:] = apply_perform_fx(
            values[cursor:], *(state[field] for field in ("filter", "delay", "stutter", "crush")), sample_rate
        )
    return numpy.ascontiguousarray(output)


def realize_loop_events(events, event_meta, bars, cycle=0):
    total_beats = max(1.0, float(bars) * 4.0)
    realized = []
    metadata = sanitize_event_meta(event_meta)
    for beat, pad, velocity in events:
        meta = metadata.get(event_meta_key(pad, beat), {"chance": 100, "ratchet": 1})
        if deterministic_event_roll(cycle, pad, beat) > meta["chance"]:
            continue
        for repeat_index in range(meta["ratchet"]):
            repeat_beat = beat + repeat_index * (0.25 / meta["ratchet"])
            if repeat_beat < total_beats:
                realized.append((repeat_beat, pad, velocity))
    return sorted(realized)

KIT_SLOTS = ("A", "B", "C", "D")
REPEAT_RATES = {
    "1/8": 2.0,
    "1/16": 4.0,
    "1/16T": 6.0,
    "1/32": 8.0,
}
BPM_MIN = 40
BPM_MAX = 240
LOOP_BAR_OPTIONS = (1, 2, 4)
RECORD_START_MODES = ("Instant", "Next bar", "Count 1 bar")
SAMPLE_START_MODES = ("Auto", "Manual")
MIDI_TICKS_PER_BEAT = 480


PADS = [
    {"name": "Kick", "color": (216, 88, 63), "synth": "kick"},
    {"name": "Snare", "color": (53, 111, 179), "synth": "snare"},
    {"name": "Closed Hat", "color": (46, 125, 91), "synth": "hat"},
    {"name": "Open Hat", "color": (28, 123, 145), "synth": "open_hat"},
    {"name": "Low Tom", "color": (119, 82, 163), "synth": "low_tom"},
    {"name": "Mid Tom", "color": (208, 154, 36), "synth": "mid_tom"},
    {"name": "High Tom", "color": (176, 108, 46), "synth": "high_tom"},
    {"name": "Floor Tom", "color": (95, 90, 162), "synth": "floor_tom"},
    {"name": "Clap", "color": (155, 79, 63), "synth": "clap"},
    {"name": "Rim", "color": (179, 58, 86), "synth": "rim"},
    {"name": "Cowbell", "color": (128, 113, 42), "synth": "cowbell"},
    {"name": "Crash", "color": (86, 112, 111), "synth": "crash"},
    {"name": "Ride", "color": (108, 127, 153), "synth": "ride"},
    {"name": "Tamb", "color": (192, 104, 138), "synth": "tambourine"},
    {"name": "Shaker", "color": (76, 140, 116), "synth": "shaker"},
    {"name": "Clave", "color": (138, 98, 68), "synth": "clave"},
]


S = {
    "kick": [
        "kick_OH_FF_1.wav",
        "kick_OH_FF_3.wav",
        "kick_OH_FF_6.wav",
        "kick_OH_FF_9.wav",
        "kick_OH_F_2.wav",
        "kick_OH_F_5.wav",
        "kick2_OH_FF_1.wav",
        "kick2_OH_FF_4.wav",
        "kick2_OH_FF_8.wav",
        "kick2_OH_F_3.wav",
    ],
    "snare": [
        "snare_OH_FF_1.wav",
        "snare_OH_FF_3.wav",
        "snare_OH_FF_5.wav",
        "snare_OH_FF_7.wav",
        "snare_OH_FF_9.wav",
        "snare2_OH_FF_2.wav",
        "snare2_OH_FF_4.wav",
        "snare2_OH_FF_6.wav",
        "snare2_OH_FF_8.wav",
    ],
    "stick": [
        "snareStick_OH_F_1.wav",
        "snareStick_OH_F_3.wav",
        "snareStick_OH_F_5.wav",
        "snareStick_OH_F_7.wav",
    ],
    "closed_hat": [
        "hihatClosed_OH_F_1.wav",
        "hihatClosed_OH_F_5.wav",
        "hihatClosed_OH_F_10.wav",
        "hihatClosed_OH_F_15.wav",
        "hihatClosed_OH_F_20.wav",
    ],
    "open_hat": [
        "hihatOpen_OH_FF_1.wav",
        "hihatOpen_OH_FF_3.wav",
        "hihatOpen_OH_FF_5.wav",
        "hihatOpen_OH_FF_6.wav",
    ],
    "semi_hat": [
        "hihatSemiOpen4_OH_F_1.wav",
        "hihatSemiOpen5_OH_F_2.wav",
        "hihatSemiOpen6_OH_F_1.wav",
        "hihatSemiOpen7_OH_F_4.wav",
    ],
    "low_tom": [
        "loTom_OH_FF_1.wav",
        "loTom_OH_FF_4.wav",
        "loTom_OH_FF_7.wav",
        "loTom_OH_MP_2.wav",
        "loTom_OH_MP_5.wav",
    ],
    "high_tom": [
        "hiTom_OH_FF_1.wav",
        "hiTom_OH_FF_4.wav",
        "hiTom_OH_FF_7.wav",
        "hiTom_OH_FF_10.wav",
        "hiTom_OH_F_2.wav",
        "hiTom_OH_F_5.wav",
    ],
    "ride": [
        "ride1_OH_FF_1.wav",
        "ride1_OH_FF_2.wav",
        "ride1_OH_FF_3.wav",
        "ride1_OH_FF_4.wav",
        "ride2_OH_FF_1.wav",
        "ride2_OH_FF_2.wav",
        "ride2_OH_FF_3.wav",
        "ride2_OH_FF_4.wav",
        "ride2_OH_FF_5.wav",
    ],
    "bell": ["ride1Bell_OH_F_2.wav", "ride1Bell_OH_F_4.wav", "ride1Bell_OH_F_6.wav"],
    "crash": [
        "crash1_OH_FF_1.wav",
        "crash1_OH_FF_3.wav",
        "crash1_OH_FF_5.wav",
        "crash1_OH_FF_6.wav",
        "crash2_OH_FF_2.wav",
        "crash2_OH_FF_4.wav",
        "crash2_OH_FF_6.wav",
        "crash2_OH_FF_8.wav",
        "crash3_OH_FF_1.wav",
        "crash3_OH_FF_5.wav",
    ],
    "splash": ["splash1_OH_F_1.wav", "splash1_OH_F_3.wav", "splash1_OH_F_5.wav"],
    "cowbell": [
        "cowbell_FF_1.wav",
        "cowbell_FF_3.wav",
        "cowbell_FF_5.wav",
        "cowbell_FF_7.wav",
        "cowbell_FF_9.wav",
    ],
    "kick_soft": ["kick_OH_P_6.wav", "kick_OH_P_9.wav", "kick2_OH_P_3.wav", "kick2_OH_P_6.wav"],
    "kick_mid": ["kick_OH_F_2.wav", "kick_OH_F_5.wav", "kick2_OH_F_3.wav"],
    "kick_hard": ["kick_OH_FF_1.wav", "kick_OH_FF_3.wav", "kick_OH_FF_6.wav", "kick_OH_FF_9.wav", "kick2_OH_FF_1.wav", "kick2_OH_FF_4.wav", "kick2_OH_FF_8.wav"],
    "snare_soft": ["snare_OH_Ghost_7.wav", "snare_OH_Ghost_9.wav", "snare2_OH_Ghost_3.wav", "snare2_OH_Ghost_4.wav"],
    "snare_mid": ["snare_OH_MP_13.wav", "snare2_OH_MP_3.wav", "snare2_OH_MP_5.wav", "snare2_OH_MP_12.wav", "snare2_OH_MP_13.wav"],
    "snare_hard": ["snare_OH_FF_1.wav", "snare_OH_FF_3.wav", "snare_OH_FF_5.wav", "snare_OH_FF_7.wav", "snare_OH_FF_9.wav", "snare2_OH_FF_2.wav", "snare2_OH_FF_4.wav", "snare2_OH_FF_6.wav", "snare2_OH_FF_8.wav"],
    "closed_hat_soft": ["hihatClosed_OH_P_3.wav", "hihatClosed_OH_P_8.wav", "hihatClosed_OH_P_15.wav", "hihatClosed_OH_P_18.wav"],
    "open_hat_soft": ["hihatOpen_OH_P_3.wav", "hihatOpen_OH_P_4.wav", "hihatOpen_OH_P_7.wav"],
    "semi_hat_soft": ["hihatSemiOpen1_OH_P_1.wav", "hihatSemiOpen1_OH_P_4.wav", "hihatSemiOpen2_OH_P_2.wav", "hihatSemiOpen2_OH_P_4.wav"],
    "low_tom_soft": ["loTom_OH_PP_1.wav", "loTom_OH_MP_2.wav", "loTom_OH_MP_3.wav", "loTom_OH_MP_5.wav", "loTom_OH_MP_7.wav"],
    "high_tom_soft": ["hiTom_OH_P_2.wav", "hiTom_OH_F_2.wav", "hiTom_OH_F_4.wav", "hiTom_OH_F_5.wav", "hiTom_OH_F_10.wav"],
    "ride_soft": ["ride1_OH_MP_1.wav", "ride1_OH_MP_4.wav", "ride2_OH_MP_2.wav"],
    "crash_soft": ["crash1_OH_P_1.wav", "crash1_OH_P_2.wav", "crash1_OH_P_3.wav", "splash1_OH_P_1.wav", "splash1_OH_P_3.wav", "splash1_OH_P_5.wav"],
    "splash_soft": ["splash1_OH_P_1.wav", "splash1_OH_P_3.wav", "splash1_OH_P_5.wav"],
    "cowbell_soft": ["cowbell_P_4.wav", "cowbell_P_6.wav", "cowbell_P_7.wav", "cowbell_MP_2.wav", "cowbell_MP_6.wav"],
}

# The source library contains two snares and two rides. Keep their takes grouped so
# timbre changes retain velocity response and round-robin variation.
S["snare1_soft"] = [name for name in S["snare_soft"] if name.startswith("snare_")]
S["snare2_soft"] = [name for name in S["snare_soft"] if name.startswith("snare2_")]
S["snare1_mid"] = [name for name in S["snare_mid"] if name.startswith("snare_")]
S["snare2_mid"] = [name for name in S["snare_mid"] if name.startswith("snare2_")]
S["snare1_hard"] = [name for name in S["snare_hard"] if name.startswith("snare_")]
S["snare2_hard"] = [name for name in S["snare_hard"] if name.startswith("snare2_")]
S["ride1_soft"] = [name for name in S["ride_soft"] if name.startswith("ride1_")]
S["ride2_soft"] = [name for name in S["ride_soft"] if name.startswith("ride2_")]
S["ride1_hard"] = [name for name in S["ride"] if name.startswith("ride1_")]
S["ride2_hard"] = [name for name in S["ride"] if name.startswith("ride2_")]
S["snare1_mid"] += S["snare1_soft"][:2]
S["ride2_soft"] += S["ride2_hard"][:1]


KIT = {
    "kick": [{"files": S["kick_hard"], "velocity_files": {"soft": S["kick_soft"], "mid": S["kick_mid"], "hard": S["kick_hard"]}, "gain": 1.0}],
    "snare": [{"files": S["snare1_hard"], "velocity_files": {"soft": S["snare1_soft"], "mid": S["snare1_mid"], "hard": S["snare1_hard"]}, "gain": 0.98}],
    "hat": [{"files": S["closed_hat"], "velocity_files": {"soft": S["closed_hat_soft"], "mid": S["closed_hat"], "hard": S["closed_hat"]}, "gain": 0.78, "choke": "hat", "duration_ms": 130}],
    "open_hat": [{"files": S["open_hat"], "velocity_files": {"soft": S["open_hat_soft"], "mid": S["open_hat_soft"], "hard": S["open_hat"]}, "gain": 0.78, "choke": "hat"}],
    "floor_tom": [{"files": S["low_tom"], "velocity_files": {"soft": S["low_tom_soft"], "mid": S["low_tom"], "hard": S["low_tom"]}, "gain": 1.02}],
    "low_tom": [{"files": S["low_tom"], "velocity_files": {"soft": S["low_tom_soft"], "mid": S["low_tom"], "hard": S["low_tom"]}, "gain": 0.98}],
    "mid_tom": [{"files": S["high_tom"], "velocity_files": {"soft": S["high_tom_soft"], "mid": S["high_tom_soft"], "hard": S["high_tom"]}, "gain": 0.94}],
    "high_tom": [{"files": S["high_tom"], "velocity_files": {"soft": S["high_tom_soft"], "mid": S["high_tom_soft"], "hard": S["high_tom"]}, "gain": 0.92}],
    "clap": [
        {"files": S["snare_hard"], "velocity_files": {"soft": S["snare_mid"], "mid": S["snare_mid"], "hard": S["snare_hard"]}, "gain": 0.34, "duration_ms": 120},
        {"files": S["snare_hard"], "velocity_files": {"soft": S["stick"], "mid": S["snare_mid"], "hard": S["snare_hard"]}, "gain": 0.28, "duration_ms": 120},
        {"files": S["stick"], "gain": 0.24, "duration_ms": 90},
    ],
    "rim": [{"files": S["stick"], "gain": 0.72, "duration_ms": 85}],
    "cowbell": [{"files": S["cowbell"], "velocity_files": {"soft": S["cowbell_soft"], "mid": S["cowbell_soft"], "hard": S["cowbell"]}, "gain": 0.82}],
    "crash": [{"files": S["crash"], "velocity_files": {"soft": S["crash_soft"], "mid": S["crash_soft"], "hard": S["crash"]}, "gain": 0.82}],
    "ride": [{"files": S["ride1_hard"], "velocity_files": {"soft": S["ride1_soft"], "mid": S["ride1_soft"], "hard": S["ride1_hard"]}, "gain": 0.72, "duration_ms": 1500}],
    "tambourine": [
        {"files": S["semi_hat"], "velocity_files": {"soft": S["semi_hat_soft"], "mid": S["semi_hat_soft"], "hard": S["semi_hat"]}, "gain": 0.4, "duration_ms": 95},
        {"files": S["splash"], "velocity_files": {"soft": S["splash_soft"], "mid": S["splash_soft"], "hard": S["splash"]}, "gain": 0.22, "duration_ms": 110},
    ],
    "shaker": [{"files": S["closed_hat"], "velocity_files": {"soft": S["closed_hat_soft"], "mid": S["closed_hat_soft"], "hard": S["closed_hat"]}, "gain": 0.5, "duration_ms": 60}],
    "clave": [{"files": S["stick"], "gain": 0.52, "duration_ms": 55}],
}

KIT.update({
    "snare_warm": [{"files": S["snare2_hard"], "velocity_files": {"soft": S["snare2_soft"], "mid": S["snare2_mid"], "hard": S["snare2_hard"]}, "gain": 0.98}],
    "snare_deep": [{"files": S["snare2_hard"], "velocity_files": {"soft": S["snare2_soft"], "mid": S["snare2_mid"], "hard": S["snare2_hard"]}, "gain": 0.94, "tune": -2}],
    "snare_bright": [{"files": S["snare1_hard"], "velocity_files": {"soft": S["snare1_soft"], "mid": S["snare1_mid"], "hard": S["snare1_hard"]}, "gain": 0.94, "tune": 2}],
    "hat_dry": [{"files": S["closed_hat"], "velocity_files": {"soft": S["closed_hat_soft"], "mid": S["closed_hat"], "hard": S["closed_hat"]}, "gain": 0.8, "choke": "hat", "duration_ms": 95, "tune": -1}],
    "hat_bright": [{"files": S["closed_hat"], "velocity_files": {"soft": S["closed_hat_soft"], "mid": S["closed_hat"], "hard": S["closed_hat"]}, "gain": 0.88, "choke": "hat", "duration_ms": 120, "tune": 2}],
    "hat_dark": [{"files": S["closed_hat"], "velocity_files": {"soft": S["closed_hat_soft"], "mid": S["closed_hat"], "hard": S["closed_hat"]}, "gain": 0.88, "choke": "hat", "duration_ms": 160, "tune": -3}],
    "open_hat_dry": [{"files": S["open_hat"], "velocity_files": {"soft": S["open_hat_soft"], "mid": S["open_hat_soft"], "hard": S["open_hat"]}, "gain": 0.56, "choke": "hat", "duration_ms": 520, "tune": -1}],
    "open_hat_bright": [{"files": S["open_hat"], "velocity_files": {"soft": S["open_hat_soft"], "mid": S["open_hat_soft"], "hard": S["open_hat"]}, "gain": 0.92, "choke": "hat", "tune": 2}],
    "open_hat_dark": [{"files": S["open_hat"], "velocity_files": {"soft": S["open_hat_soft"], "mid": S["open_hat_soft"], "hard": S["open_hat"]}, "gain": 0.84, "choke": "hat", "tune": -3}],
    "ride_bright": [{"files": S["ride1_hard"], "velocity_files": {"soft": S["ride1_soft"], "mid": S["ride1_soft"], "hard": S["ride1_hard"]}, "gain": 0.94, "tune": 2}],
    "ride_dark": [{"files": S["ride2_hard"], "velocity_files": {"soft": S["ride2_soft"], "mid": S["ride2_soft"], "hard": S["ride2_hard"]}, "gain": 0.64, "tune": -2}],
    "ride_washy": [{"files": S["ride2_hard"], "velocity_files": {"soft": S["ride2_soft"], "mid": S["ride2_soft"], "hard": S["ride2_hard"]}, "gain": 0.64}],
    "hat_semi": [{"files": S["semi_hat"], "velocity_files": {"soft": S["semi_hat_soft"], "mid": S["semi_hat_soft"], "hard": S["semi_hat"]}, "gain": 0.76, "choke": "hat", "duration_ms": 340}],
})

PAD_NAME_TO_INDEX = {pad["name"]: index for index, pad in enumerate(PADS)}

GM_NOTE_TO_PAD = {
    35: PAD_NAME_TO_INDEX["Kick"],
    36: PAD_NAME_TO_INDEX["Kick"],
    37: PAD_NAME_TO_INDEX["Rim"],
    38: PAD_NAME_TO_INDEX["Snare"],
    39: PAD_NAME_TO_INDEX["Clap"],
    40: PAD_NAME_TO_INDEX["Snare"],
    41: PAD_NAME_TO_INDEX["Floor Tom"],
    42: PAD_NAME_TO_INDEX["Closed Hat"],
    43: PAD_NAME_TO_INDEX["Floor Tom"],
    44: PAD_NAME_TO_INDEX["Closed Hat"],
    45: PAD_NAME_TO_INDEX["Low Tom"],
    46: PAD_NAME_TO_INDEX["Open Hat"],
    47: PAD_NAME_TO_INDEX["Low Tom"],
    48: PAD_NAME_TO_INDEX["Mid Tom"],
    49: PAD_NAME_TO_INDEX["Crash"],
    50: PAD_NAME_TO_INDEX["High Tom"],
    51: PAD_NAME_TO_INDEX["Ride"],
    52: PAD_NAME_TO_INDEX["Crash"],
    53: PAD_NAME_TO_INDEX["Ride"],
    54: PAD_NAME_TO_INDEX["Tamb"],
    55: PAD_NAME_TO_INDEX["Crash"],
    56: PAD_NAME_TO_INDEX["Cowbell"],
    57: PAD_NAME_TO_INDEX["Crash"],
    59: PAD_NAME_TO_INDEX["Ride"],
    70: PAD_NAME_TO_INDEX["Shaker"],
    75: PAD_NAME_TO_INDEX["Clave"],
    76: PAD_NAME_TO_INDEX["Clave"],
    77: PAD_NAME_TO_INDEX["Clave"],
}

# Lowest GM note that reaches each pad, shown on the pad face as a reference.
PAD_TO_GM_NOTE = {}
for _note, _pad in sorted(GM_NOTE_TO_PAD.items()):
    PAD_TO_GM_NOTE.setdefault(_pad, _note)

MAPPING_MODES = ("DONNER Mini", "GM Drums", "Learn")
KIT_ORDER = tuple(KIT)
# The kit hue belongs to the sound, so it travels when pads are rearranged.
SYNTH_COLORS = {pad["synth"]: pad["color"] for pad in PADS}


def synth_color(synth, fallback):
    if synth in SYNTH_COLORS:
        return SYNTH_COLORS[synth]
    base = max(
        (name for name in SYNTH_COLORS if str(synth).startswith(name + "_")),
        key=len, default=None,
    )
    return SYNTH_COLORS[base] if base else fallback


SYNTH_LABELS = {pad["synth"]: pad["name"] for pad in PADS}
SYNTH_LABELS.update({
    "snare": "Snare Tight", "snare_warm": "Snare Warm",
    "snare_deep": "Snare Deep", "snare_bright": "Snare Bright",
    "hat": "Hat Tight", "hat_dry": "Hat Dry",
    "hat_bright": "Hat Bright", "hat_dark": "Hat Dark",
    "open_hat": "Open Hat Tight", "open_hat_dry": "Open Hat Dry",
    "open_hat_bright": "Open Hat Bright", "open_hat_dark": "Open Hat Dark",
    "hat_semi": "Hat Semi",
    "ride": "Ride Dry", "ride_bright": "Ride Bright",
    "ride_dark": "Ride Dark", "ride_washy": "Ride Washy",
})
TIMBRE_FAMILIES = (
    ("snare", "snare_warm", "snare_deep", "snare_bright"),
    ("hat", "hat_dry", "hat_bright", "hat_dark"),
    ("open_hat", "open_hat_dry", "open_hat_bright", "open_hat_dark"),
    ("ride", "ride_bright", "ride_dark", "ride_washy"),
)
HAT_OPEN_PAIRS = dict(zip(TIMBRE_FAMILIES[1], TIMBRE_FAMILIES[2]))
DEFAULT_PAD_SYNTHS = tuple(pad["synth"] for pad in PADS)
SYNTH_TO_GM_NOTE = {
    "kick": 36,
    "snare": 38,
    "hat": 42,
    "open_hat": 46,
    "floor_tom": 41,
    "low_tom": 45,
    "mid_tom": 48,
    "high_tom": 50,
    "clap": 39,
    "rim": 37,
    "cowbell": 56,
    "crash": 49,
    "ride": 51,
    "tambourine": 54,
    "shaker": 70,
    "clave": 75,
}
for family in TIMBRE_FAMILIES:
    note = SYNTH_TO_GM_NOTE[family[0]]
    for synth in family[1:]:
        SYNTH_TO_GM_NOTE[synth] = note
SYNTH_TO_GM_NOTE["hat_semi"] = 44


def all_sample_files():
    files = set()
    for layers in KIT.values():
        for layer in layers:
            files.update(layer["files"])
            for velocity_files in layer.get("velocity_files", {}).values():
                files.update(velocity_files)
    return sorted(files)


def velocity_gain(raw_velocity):
    velocity = max(1, min(127, raw_velocity))
    if velocity <= 50:
        return 0.12 + ((velocity / 50.0) ** 1.6) * 0.2
    if velocity <= 70:
        return 0.32 + (((velocity - 50.0) / 20.0) ** 1.3) * 0.1
    if velocity <= 100:
        return 0.42 + (((velocity - 70.0) / 30.0) ** 1.2) * 0.46
    return 0.88 + (((velocity - 100.0) / 27.0) ** 0.85) * 0.12


def velocity_tier(raw_velocity):
    if raw_velocity < 45:
        return "ghost"
    if raw_velocity < 78:
        return "soft"
    if raw_velocity < 100:
        return "mid"
    if raw_velocity < 116:
        return "hard"
    return "accent"


def velocity_layer_mix(raw_velocity, blend_width=8):
    velocity = max(1, min(127, int(raw_velocity)))
    half = max(1, blend_width // 2)
    for boundary, lower, upper in (
        (45, "ghost", "soft"),
        (78, "soft", "mid"),
        (100, "mid", "hard"),
        (116, "hard", "accent"),
    ):
        start = boundary - half
        end = boundary + half
        if start <= velocity <= end:
            upper_weight = (velocity - start) / max(1, end - start)
            return ((lower, 1.0 - upper_weight), (upper, upper_weight))
    return ((velocity_tier(velocity), 1.0),)


def layer_files_for_tier(layer, tier):
    velocity_files = layer.get("velocity_files", {})
    fallback_tier = "soft" if tier == "ghost" else "hard" if tier == "accent" else tier
    return velocity_files.get(tier, velocity_files.get(fallback_tier, layer["files"]))


def choose_nonrepeating_sample(files, previous=None):
    choices = tuple(files)
    if not choices:
        raise ValueError("Sample layer has no files")
    if len(choices) == 1:
        return choices[0]
    candidates = tuple(file for file in choices if file != previous)
    return random.choice(candidates or choices)


VELOCITY_TIER_GAIN = {"ghost": 1.22, "soft": 1.18, "mid": 1.05, "hard": 1.0, "accent": 0.98}


def repeat_interval_seconds(rate, bpm):
    divisions_per_quarter = REPEAT_RATES.get(rate, REPEAT_RATES["1/16"])
    safe_bpm = max(BPM_MIN, min(BPM_MAX, int(bpm)))
    return 60.0 / safe_bpm / divisions_per_quarter


def midi_clock_bpm(intervals_ns):
    values = [int(value) for value in intervals_ns if int(value) > 0]
    if not values:
        return None
    interval = statistics.median(values[-96:])
    return max(BPM_MIN, min(BPM_MAX, 60_000_000_000.0 / (interval * 24.0)))


def clamp_sensitivity(value):
    return round(max(0.6, min(1.6, float(value))), 2)


def default_pad_calibration():
    return {"enabled": False, "soft": 50, "natural": 75, "hard": 100, "dead_time_ms": 10}


def sanitize_pad_calibration(value):
    fallback = default_pad_calibration()
    if not isinstance(value, dict):
        return fallback
    try:
        soft = max(1, min(117, int(value.get("soft", fallback["soft"]))))
        natural = max(soft + 4, min(122, int(value.get("natural", fallback["natural"]))))
        hard = max(natural + 4, min(127, int(value.get("hard", fallback["hard"]))))
        dead_time_ms = max(0, min(40, int(value.get("dead_time_ms", fallback["dead_time_ms"]))))
    except (TypeError, ValueError):
        return fallback
    return {
        "enabled": bool(value.get("enabled", False)),
        "soft": soft,
        "natural": natural,
        "hard": hard,
        "dead_time_ms": dead_time_ms,
    }


def calibrated_velocity(raw_velocity, calibration):
    raw = max(1, min(127, int(raw_velocity)))
    profile = sanitize_pad_calibration(calibration)
    if not profile["enabled"]:
        return raw
    anchors = (1, profile["soft"], profile["natural"], profile["hard"], 127)
    targets = (1, 42, 76, 116, 127)
    for start, end, target_start, target_end in zip(anchors, anchors[1:], targets, targets[1:]):
        if raw <= end:
            ratio = (raw - start) / max(1, end - start)
            return max(1, min(127, round(target_start + ratio * (target_end - target_start))))
    return 127


def encode_midi_varlen(value):
    value = max(0, int(value))
    buffer = value & 0x7F
    encoded = bytearray([buffer])
    while value > 0x7F:
        value >>= 7
        buffer = (value & 0x7F) | 0x80
        encoded.insert(0, buffer)
    return bytes(encoded)


def build_midi_file(events, bars, bpm, pad_synths):
    total_ticks = int(bars * 4 * MIDI_TICKS_PER_BEAT)
    tempo = int(60_000_000 / max(BPM_MIN, min(BPM_MAX, bpm)))
    track = bytearray()
    track.extend(b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big"))
    track.extend(b"\x00\xff\x58\x04\x04\x02\x18\x08")

    timed_messages = []
    for beat, pad_index, velocity in events:
        if not 0 <= pad_index < len(pad_synths):
            continue
        note = SYNTH_TO_GM_NOTE.get(pad_synths[pad_index], 36)
        on_tick = max(0, min(total_ticks - 1, round(beat * MIDI_TICKS_PER_BEAT)))
        off_tick = min(total_ticks - 1, on_tick + 60)
        safe_velocity = max(1, min(127, int(velocity)))
        timed_messages.append((on_tick, 1, bytes((0x99, note, safe_velocity))))
        timed_messages.append((off_tick, 0, bytes((0x89, note, 0))))

    last_tick = 0
    for tick, _order, message in sorted(timed_messages):
        track.extend(encode_midi_varlen(tick - last_tick))
        track.extend(message)
        last_tick = tick
    track.extend(encode_midi_varlen(total_ticks - last_tick))
    track.extend(b"\xff\x2f\x00")

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, MIDI_TICKS_PER_BEAT)
    return header + b"MTrk" + struct.pack(">I", len(track)) + bytes(track)


def audio_input_devices():
    try:
        import sounddevice

        return [
            (index, str(device["name"]), int(round(device["default_samplerate"])))
            for index, device in enumerate(sounddevice.query_devices())
            if device["max_input_channels"] > 0
        ]
    except Exception:
        return []


def prepare_sample_audio(samples, source_rate, target_rate=MIXER_FREQUENCY):
    import numpy

    audio = numpy.asarray(samples, dtype=numpy.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = numpy.nan_to_num(audio.reshape(-1), copy=False)
    if len(audio) < 32 or source_rate <= 0:
        raise ValueError("Recording is too short")

    peak = float(numpy.max(numpy.abs(audio)))
    if peak < 0.003:
        raise ValueError("No usable audio detected")

    block_size = max(32, round(source_rate * 0.005))
    padded_length = ((len(audio) + block_size - 1) // block_size) * block_size
    padded = numpy.pad(audio, (0, padded_length - len(audio)))
    rms = numpy.sqrt(numpy.mean(padded.reshape(-1, block_size) ** 2, axis=1))
    threshold = max(0.003, peak * 0.025)
    active_blocks = numpy.flatnonzero(rms >= threshold)
    if not len(active_blocks):
        raise ValueError("No usable audio detected")

    start = max(0, int(active_blocks[0] * block_size - source_rate * 0.01))
    end = min(len(audio), int((active_blocks[-1] + 1) * block_size + source_rate * 0.15))
    audio = audio[start:end]

    if source_rate != target_rate:
        target_length = max(1, round(len(audio) * target_rate / source_rate))
        source_positions = numpy.linspace(0.0, 1.0, len(audio), endpoint=False)
        target_positions = numpy.linspace(0.0, 1.0, target_length, endpoint=False)
        audio = numpy.interp(target_positions, source_positions, audio).astype(numpy.float32)

    peak = float(numpy.max(numpy.abs(audio)))
    audio *= 0.92 / peak
    fade_in = min(len(audio) // 2, round(target_rate * 0.002))
    fade_out = min(len(audio) // 2, round(target_rate * 0.008))
    if fade_in:
        audio[:fade_in] *= numpy.linspace(0.0, 1.0, fade_in, dtype=numpy.float32)
    if fade_out:
        audio[-fade_out:] *= numpy.linspace(1.0, 0.0, fade_out, dtype=numpy.float32)

    mono = numpy.clip(audio * 32767.0, -32768, 32767).astype(numpy.int16)
    return numpy.repeat(mono[:, None], 2, axis=1)


class AudioSampler:
    def __init__(self, max_seconds=20, pre_roll_seconds=0.2, silence_stop_seconds=1.2):
        self.max_seconds = max_seconds
        self.pre_roll_seconds = pre_roll_seconds
        self.silence_stop_seconds = silence_stop_seconds
        self.lock = threading.Lock()
        self.stream = None
        self.chunks = []
        self.pre_roll_chunks = collections.deque()
        self.samplerate = 0
        self.total_frames = 0
        self.pre_roll_frames = 0
        self.silence_frames = 0
        self.level = 0.0
        self.auto_stop = False
        self.auto_start = True
        self.triggered = False
        self.clipped = False
        self.stop_reason = None

    def _reset_capture_state(self, samplerate, auto_start):
        with self.lock:
            self.chunks = []
            self.pre_roll_chunks = collections.deque()
            self.samplerate = int(samplerate)
            self.total_frames = 0
            self.pre_roll_frames = 0
            self.silence_frames = 0
            self.level = 0.0
            self.auto_stop = False
            self.auto_start = bool(auto_start)
            self.triggered = not self.auto_start
            self.clipped = False
            self.stop_reason = None

    def start(self, device=None, auto_start=True, monitor=False):
        import sounddevice

        info = sounddevice.query_devices(device, "input") if device is not None else sounddevice.query_devices(kind="input")
        samplerate = int(round(info["default_samplerate"]))
        self._reset_capture_state(samplerate, auto_start)
        if monitor:
            try:
                self.stream = sounddevice.Stream(
                    device=(device, None), samplerate=samplerate,
                    channels=(1, 2), dtype="float32", blocksize=256,
                    latency="low", callback=self._duplex_callback,
                )
            except Exception:
                self.stream = None
        if self.stream is None:
            self.stream = sounddevice.InputStream(
                device=device, samplerate=samplerate, channels=1,
                dtype="float32", blocksize=256, latency="low",
                callback=self._callback,
            )
        self.stream.start()

    def _duplex_callback(self, input_data, output_data, frames, time_info, status):
        output_data[:, 0] = input_data[:, 0] * 0.7
        output_data[:, 1] = input_data[:, 0] * 0.7
        self._callback(input_data, frames, time_info, status)

    def _callback(self, input_data, frames, _time_info, _status):
        import numpy

        block = input_data[:, 0].copy()
        current_level = float(numpy.max(numpy.abs(input_data))) if frames else 0.0
        with self.lock:
            if self.auto_stop:
                return
            self.level = max(current_level, self.level * 0.78)
            self.clipped = self.clipped or current_level >= 0.98

            if not self.triggered:
                self.pre_roll_chunks.append(block)
                self.pre_roll_frames += frames
                pre_roll_limit = max(1, round(self.samplerate * self.pre_roll_seconds))
                while self.pre_roll_chunks and self.pre_roll_frames - len(self.pre_roll_chunks[0]) >= pre_roll_limit:
                    self.pre_roll_frames -= len(self.pre_roll_chunks.popleft())
                if current_level < 0.015:
                    return
                self.triggered = True
                self.chunks.extend(self.pre_roll_chunks)
                self.total_frames = self.pre_roll_frames
                self.pre_roll_chunks.clear()
                self.pre_roll_frames = 0
            else:
                self.chunks.append(block)
                self.total_frames += frames

            if current_level < 0.006:
                self.silence_frames += frames
            else:
                self.silence_frames = 0

            if (
                self.total_frames >= self.samplerate * 0.25
                and self.silence_frames >= self.samplerate * self.silence_stop_seconds
            ):
                self.auto_stop = True
                self.stop_reason = "silence"
            if self.total_frames >= self.samplerate * self.max_seconds:
                self.auto_stop = True
                self.stop_reason = "limit"

    def stop(self):
        import numpy

        stream = self.stream
        self.stream = None
        if stream is not None:
            stream.stop()
            stream.close()
        with self.lock:
            audio = numpy.concatenate(self.chunks) if self.chunks else numpy.zeros(0, dtype=numpy.float32)
            samplerate = self.samplerate
            self.chunks = []
            self.pre_roll_chunks.clear()
            self.level = 0.0
            self.auto_stop = False
        return audio, samplerate

    def snapshot(self):
        with self.lock:
            return self.stream is not None, self.level, self.auto_stop

    def detail_snapshot(self):
        with self.lock:
            return {
                "active": self.stream is not None,
                "level": self.level,
                "auto_stop": self.auto_stop,
                "auto_start": self.auto_start,
                "triggered": self.triggered,
                "clipped": self.clipped,
                "stop_reason": self.stop_reason,
            }


class DrumPadNative:
    def __init__(self, settings_path=SETTINGS_FILE):
        self.screen = None
        self.clock = None
        self.grain = None
        self.audio_inputs_available = False
        self.last_midi_event_ns = 0
        self.midi_opened_at = 0.0
        self.faces = {}
        self.font = None
        self.small_font = None
        self.big_font = None
        self.label_font = None
        self.head_font = None
        self.data_font = None
        self.data_font_lg = None
        self.data_font_sm = None
        self.samples = {}
        self.midi_input = None
        self.midi_device_id = None
        self.midi_device_name = "No MIDI"
        self.preferred_midi_name = "STARRYPAD MINI"
        self.next_midi_health_check_at = 0.0
        self.midi_disconnect_notified = False
        self.midi_output = None
        self.midi_output_name = None
        self.clock_source = "Auto"
        self.clock_active_source = "Internal"
        self.clock_output_enabled = False
        self.clock_correction_ms = 0
        self.clock_intervals_ns = collections.deque(maxlen=96)
        self.last_midi_clock_ns = None
        self.external_clock_ticks = 0
        self.external_transport_running = False
        self.next_midi_clock_out_ns = None
        self.audio_events = queue.SimpleQueue()
        self.audio_thread = None
        self.audio_running = threading.Event()
        self.audio_output_name = None
        self.audio_mode = "Low latency"
        self.audio_rate = MIXER_FREQUENCY
        self.audio_buffer = MIXER_BUFFER
        self.audio_setup_open = False
        self.sync_setup_open = False
        self.audio_advanced = False
        self.ui_scale = 1.0
        self.display_surface = None
        self.display_size = WINDOW_SIZE
        self.display_viewport = pygame.Rect(0, 0, *WINDOW_SIZE)
        self.mouse_logical = (-100, -100)
        self.tooltip_key = None
        self.tooltip_since = 0.0
        self.keyboard_focus_name = None
        self.next_audio_health_check_at = 0.0
        self.audio_recovering = False
        self.last_ui_heartbeat = None
        self.audio_test_active = False
        self.audio_test_deadline = 0.0
        self.audio_test_next_hit = 0.0
        self.audio_test_hit_index = 0
        self.audio_test_baseline = None
        self.audio_test_mixer_ok = True
        self.audio_test_result = ""
        self.assignments = {}
        self.pad_notes = [None] * len(PADS)
        self.mapping_mode = 0
        self.mapping_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.loop_lock = threading.RLock()
        self.performance_lock = threading.Lock()
        self.settings_lock = threading.Lock()
        self.hit_until = [0.0] * len(PADS)
        self.hit_energy = [0.0] * len(PADS)
        self.last_hit = "--"
        self.last_velocity = "--"
        self.last_velocity_value = 0
        self.status = "Starting"
        self.logs = []
        self.volume = 0.82
        self.settings_path = Path(settings_path) if settings_path else None
        self.project_dir = (
            PROJECT_DIR
            if self.settings_path and self.settings_path.resolve() == SETTINGS_FILE.resolve()
            else self.settings_path.parent / "projects"
            if self.settings_path
            else None
        )
        self.project_path = None
        self.project_name = "Current"
        self.recent_projects = []
        self.project_initialized = False
        self.project_menu_open = False
        self.project_buttons = {}
        self.project_lock = threading.Lock()
        self.project_history = collections.deque(maxlen=20)
        self.project_redo = collections.deque(maxlen=20)
        self.active_kit = "A"
        self.kit_slots = {
            slot: self.default_kit_profile()
            for slot in KIT_SLOTS
        }
        self.pad_synths = list(DEFAULT_PAD_SYNTHS)
        self.pad_sensitivity = [1.0] * len(PADS)
        self.pad_volume = [1.0] * len(PADS)
        self.pad_pan = [0.0] * len(PADS)
        self.pad_tune = [0] * len(PADS)
        self.pad_mute = [False] * len(PADS)
        self.pad_punch = [0] * len(PADS)
        self.pad_air = [0] * len(PADS)
        self.pad_space = [0] * len(PADS)
        self.pad_bus = [0] * len(PADS)
        self.pad_selection = {0}
        self.pad_drag_from = None
        self.pad_drag_over = None
        self.pad_drag_origin = None
        self.pad_drag_active = False
        self.pad_swap_flash = {}
        self.accent_name = theme.DEFAULT_ACCENT
        self.solo_pads = set()
        self.mixer_bypass = False
        self.mixer_open = False
        self.mixer_fx_view = False
        self.mixer_buttons = {}
        self.perform_fx_open = False
        self.perform_fx_buttons = {}
        self.perform_fx = {"filter": 0, "delay": 0, "stutter": 0, "crush": 0}
        self.perform_fx_events = []
        self.perform_fx_pending = []
        self.perform_fx_bypass = False
        self.bounce_processing = False
        self.bounce_results = queue.SimpleQueue()
        self.bounce_thread = None
        self.tuned_sound_cache = {}
        self.processed_sound_cache = {}
        self.master_peak_warning_until = 0.0
        self.master_peak_window_ns = 0
        self.master_peak_sum = 0.0
        self.pad_calibrations = [default_pad_calibration() for _ in PADS]
        self.last_pad_trigger_ns = [0] * len(PADS)
        self.calibration_active = False
        self.calibration_pad = None
        self.calibration_stage = 0
        self.calibration_hits = []
        self.calibration_duplicate_ms = []
        self.calibration_last_raw_ns = None
        self.calibration_prompted = False
        self.custom_sample_files = [None] * len(PADS)
        self.sample_edits = [default_sample_edit() for _ in PADS]
        self.custom_sound_cache = {}
        self.edited_sound_cache = {}
        self.sample_channels = {}
        self.sample_editor_open = False
        self.sample_editor_buttons = {}
        self.sample_edit_bypass = False
        self.sample_wave_zoom = False
        self.chop_open = False
        self.chop_buttons = {}
        self.chop_mode = "Transient"
        self.chop_count = 8
        self.chop_markers = []
        self.chop_keep_original = True
        self.chop_play_through = False
        self.chop_choke = False
        self.chop_lazy_active = False
        self.chop_lazy_started_at = None
        self.chop_wave_rect = None
        self.browser_open = False
        self.browser_buttons = {}
        self.browser_query = ""
        self.browser_type = "All"
        self.browser_source = "All"
        self.browser_kit = "All Kits"
        self.browser_view = "All"
        self.browser_selected = None
        self.browser_page = 0
        self.browser_row_ids = []
        self.sample_favorites = []
        self.recent_samples = []
        self.waveform_cache = {}
        self.sample_choice_history = {}
        self.selected_pad = 0
        self.repeat_enabled = False
        self.repeat_rate = "1/16"
        self.bpm = 120
        self.metronome_enabled = False
        self.record_start_mode = "Count 1 bar"
        self.sample_input_name = None
        self.sample_start_mode = "Auto"
        self.sample_monitor_enabled = False
        self.sample_continuous_enabled = False
        self.sample_continuous_active = False
        self.clip_prompt_open = False
        self.clip_prompt_buttons = {}
        self.pending_clipped_sample = None
        self.sample_was_clipped = False
        self.sampler = AudioSampler()
        self.sample_results = queue.SimpleQueue()
        self.sample_processing = False
        self.sample_worker = None
        self.sample_target_pad = None
        self.sample_status = ""
        self.surface_notice = ""
        self.surface_notice_until = 0.0
        self.settings_open = False
        self.settings_buttons = {}
        self.tap_times = collections.deque(maxlen=5)
        self.held_triggers = {}
        self.hat_openness = 0.0
        self.next_metronome_ns = None
        self.metronome_beat = 0
        self.loop_events = []
        self.loop_source_events = None
        self.feel_preset = "Natural"
        self.feel_strength = 50
        self.feel_swing = 50
        self.feel_nudge_ms = 0
        self.feel_humanize_ms = 0
        self.feel_open = False
        self.feel_advanced = False
        self.feel_buttons = {}
        self.scene_buttons = {}
        self.view_mode = "Perform"
        self.sequence_bar_page = 0
        self.sequence_selected = None
        self.sequence_selection = set()
        self.sequence_velocity = 100
        self.sequence_cells = {}
        self.sequence_step_input = False
        self.sequence_step_cursor = 0
        self.loop_event_meta = {}
        self.loop_cycle_index = 0
        self.patterns = [None] * PATTERN_COUNT
        self.active_pattern = 0
        self.pending_pattern = None
        self.pattern_switch_deadline_ns = None
        self.pattern_launch_mode = "Next bar"
        self.scene_order = []
        self.scene_position = 0
        self.song_playing = False
        self.scene_open = False
        self.scene_buttons = {}
        self.loop_history = collections.deque(maxlen=20)
        self.loop_redo = collections.deque(maxlen=20)
        self.loop_bars = 1
        self.loop_playing = False
        self.loop_recording = False
        self.loop_overdub = False
        self.loop_record_pending = False
        self.loop_record_deadline_ns = None
        self.loop_count_next_ns = None
        self.loop_count_remaining = 0
        self.loop_start_ns = None
        self.loop_pending = []
        self.loop_schedule_bpm = self.bpm
        self.loop_exporting = False
        self.performance_events = collections.deque(maxlen=8192)
        self.export_thread = None
        self.last_export = "--"
        self.share_open = False
        self.share_buttons = {}
        self.buttons = {}
        self.chokes = {}
        self.trigger_latencies = collections.deque(maxlen=DIAGNOSTIC_WINDOW)
        self.trigger_count = 0
        self.ignored_event_count = 0
        self.audio_error_count = 0
        self.max_queue_depth = 0
        self.load_settings()
        self.initialize_project()
        self.loop_schedule_bpm = self.bpm
        self.apply_mapping_mode()

    @staticmethod
    def default_kit_profile():
        return {
            "pad_synths": list(DEFAULT_PAD_SYNTHS),
            "pad_sensitivity": [1.0] * len(PADS),
            "custom_samples": [None] * len(PADS),
            "sample_edits": [default_sample_edit() for _ in PADS],
            "pad_volume": [1.0] * len(PADS),
            "pad_pan": [0.0] * len(PADS),
            "pad_tune": [0] * len(PADS),
            "pad_mute": [False] * len(PADS),
            "pad_punch": [0] * len(PADS),
            "pad_air": [0] * len(PADS),
            "pad_space": [0] * len(PADS),
            "pad_bus": [0] * len(PADS),
        }

    @staticmethod
    def sanitize_kit_profile(profile):
        fallback = DrumPadNative.default_kit_profile()
        if not isinstance(profile, dict):
            return fallback

        synths = profile.get("pad_synths", fallback["pad_synths"])
        if not isinstance(synths, list) or len(synths) != len(PADS):
            synths = fallback["pad_synths"]
        synths = [synth if synth in KIT else DEFAULT_PAD_SYNTHS[index] for index, synth in enumerate(synths)]

        sensitivity = profile.get("pad_sensitivity", fallback["pad_sensitivity"])
        if not isinstance(sensitivity, list) or len(sensitivity) != len(PADS):
            sensitivity = fallback["pad_sensitivity"]
        try:
            sensitivity = [clamp_sensitivity(value) for value in sensitivity]
        except (TypeError, ValueError):
            sensitivity = fallback["pad_sensitivity"]

        custom_samples = profile.get("custom_samples", fallback["custom_samples"])
        if not isinstance(custom_samples, list) or len(custom_samples) != len(PADS):
            custom_samples = fallback["custom_samples"]
        custom_samples = [
            value
            if isinstance(value, str)
            and Path(value).name == value
            and Path(value).suffix.lower() == ".wav"
            else None
            for value in custom_samples
        ]
        sample_edits = profile.get("sample_edits", fallback["sample_edits"])
        if not isinstance(sample_edits, list) or len(sample_edits) != len(PADS):
            sample_edits = fallback["sample_edits"]
        sample_edits = [sanitize_sample_edit(value) for value in sample_edits]

        def sanitize_list(name, transform):
            values = profile.get(name, fallback[name])
            if not isinstance(values, list) or len(values) != len(PADS):
                return list(fallback[name])
            try:
                return [transform(value) for value in values]
            except (TypeError, ValueError):
                return list(fallback[name])

        pad_volume = sanitize_list("pad_volume", lambda value: round(max(0.0, min(1.5, float(value))), 2))
        pad_pan = sanitize_list("pad_pan", lambda value: round(max(-1.0, min(1.0, float(value))), 2))
        pad_tune = sanitize_list("pad_tune", lambda value: max(-12, min(12, int(value))))
        pad_mute = sanitize_list("pad_mute", bool)
        pad_punch = sanitize_list("pad_punch", lambda value: max(0, min(100, int(value))))
        pad_air = sanitize_list("pad_air", lambda value: max(0, min(100, int(value))))
        pad_space = sanitize_list("pad_space", lambda value: max(0, min(100, int(value))))
        pad_bus = sanitize_list("pad_bus", lambda value: max(0, min(3, int(value))))

        return {
            "pad_synths": list(synths),
            "pad_sensitivity": list(sensitivity),
            "custom_samples": list(custom_samples),
            "sample_edits": sample_edits,
            "pad_volume": pad_volume,
            "pad_pan": pad_pan,
            "pad_tune": pad_tune,
            "pad_mute": pad_mute,
            "pad_punch": pad_punch, "pad_air": pad_air,
            "pad_space": pad_space, "pad_bus": pad_bus,
        }

    def settings_backup_path(self):
        if not self.settings_path:
            return None
        return self.settings_path.with_suffix(self.settings_path.suffix + ".bak")

    @staticmethod
    def write_text_atomic(path, text):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def apply_settings_data(self, data):
        self.volume = max(0.0, min(1.0, float(data.get("volume", self.volume))))
        self.mapping_mode = max(0, min(len(MAPPING_MODES) - 1, int(data.get("mapping_mode", 0))))
        active_kit = str(data.get("active_kit", "A"))
        self.active_kit = active_kit if active_kit in KIT_SLOTS else "A"
        self.repeat_enabled = bool(data.get("repeat_enabled", False))
        repeat_rate = str(data.get("repeat_rate", "1/16"))
        self.repeat_rate = repeat_rate if repeat_rate in REPEAT_RATES else "1/16"
        self.bpm = max(BPM_MIN, min(BPM_MAX, int(data.get("bpm", 120))))
        self.metronome_enabled = bool(data.get("metronome_enabled", False))
        self.calibration_prompted = bool(data.get("calibration_prompted", False))
        record_start_mode = str(data.get("record_start_mode", "Count 1 bar"))
        self.record_start_mode = record_start_mode if record_start_mode in RECORD_START_MODES else "Count 1 bar"
        sample_input_name = data.get("sample_input_name")
        self.sample_input_name = sample_input_name if isinstance(sample_input_name, str) else None
        sample_start_mode = str(data.get("sample_start_mode", "Auto"))
        self.sample_start_mode = sample_start_mode if sample_start_mode in SAMPLE_START_MODES else "Auto"
        self.sample_monitor_enabled = bool(data.get("sample_monitor_enabled", False))
        self.sample_continuous_enabled = bool(data.get("sample_continuous_enabled", False))
        audio_output_name = data.get("audio_output_name")
        self.audio_output_name = audio_output_name if isinstance(audio_output_name, str) else None
        audio_mode = str(data.get("audio_mode", "Low latency"))
        self.audio_mode = audio_mode if audio_mode in AUDIO_MODES else "Low latency"
        clock_source = str(data.get("clock_source", "Auto"))
        self.clock_source = clock_source if clock_source in ("Auto", "Internal", "External") else "Auto"
        self.clock_output_enabled = bool(data.get("clock_output_enabled", False))
        midi_output_name = data.get("midi_output_name")
        self.midi_output_name = midi_output_name if isinstance(midi_output_name, str) else None
        try:
            self.clock_correction_ms = max(-100, min(100, int(data.get("clock_correction_ms", 0))))
        except (TypeError, ValueError):
            self.clock_correction_ms = 0
        try:
            self.audio_rate = int(data.get("audio_rate", audio_mode_config(self.audio_mode)[0]))
            self.audio_buffer = int(data.get("audio_buffer", audio_mode_config(self.audio_mode)[1]))
        except (TypeError, ValueError):
            self.audio_rate, self.audio_buffer = audio_mode_config(self.audio_mode)
        if self.audio_rate not in AUDIO_RATES:
            self.audio_rate = audio_mode_config(self.audio_mode)[0]
        if self.audio_buffer not in AUDIO_BUFFERS:
            self.audio_buffer = audio_mode_config(self.audio_mode)[1]
        accent = str(data.get("accent", theme.DEFAULT_ACCENT))
        self.accent_name = accent if accent in theme.ACCENT_NAMES else theme.DEFAULT_ACCENT
        theme.set_accent(self.accent_name)
        try:
            saved_scale = float(data.get("ui_scale", 1.0))
            self.ui_scale = min((1.0, 1.25, 1.5), key=lambda value: abs(value - saved_scale))
        except (TypeError, ValueError):
            self.ui_scale = 1.0
        recent_projects = data.get("recent_projects", [])
        if isinstance(recent_projects, list):
            self.recent_projects = [str(value) for value in recent_projects if isinstance(value, str)][:8]
        favorites = data.get("sample_favorites", [])
        if isinstance(favorites, list):
            self.sample_favorites = [str(value) for value in favorites if isinstance(value, str)][:128]
        recent_samples = data.get("recent_samples", [])
        if isinstance(recent_samples, list):
            self.recent_samples = [str(value) for value in recent_samples if isinstance(value, str)][:32]
        current_project = data.get("current_project")
        if isinstance(current_project, str):
            self.project_path = Path(current_project)
        calibrations = data.get("pad_calibrations", [])
        if isinstance(calibrations, list) and len(calibrations) == len(PADS):
            self.pad_calibrations = [sanitize_pad_calibration(value) for value in calibrations]
        loop_bars = int(data.get("loop_bars", 1))
        self.loop_bars = loop_bars if loop_bars in LOOP_BAR_OPTIONS else 1
        self.loop_events = self.sanitize_loop_events(data.get("loop_events", []), self.loop_bars)
        self.perform_fx_events = self.sanitize_perform_fx_events(
            data.get("perform_fx_events", []), self.loop_bars
        )

        saved_kits = data.get("kits", {})
        if isinstance(saved_kits, dict):
            for slot in KIT_SLOTS:
                self.kit_slots[slot] = self.sanitize_kit_profile(saved_kits.get(slot))
        self.apply_kit_profile(self.kit_slots[self.active_kit])

    def load_settings(self):
        if not self.settings_path:
            return
        backup_path = self.settings_backup_path()
        candidates = [self.settings_path, backup_path]
        errors = []
        for candidate in candidates:
            if candidate is None or not candidate.exists():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Saved data must be an object")
                self.apply_settings_data(data)
                if candidate == backup_path:
                    self.write_text_atomic(self.settings_path, raw)
                    self.status = "Recovered saved session"
                    self.log(self.status)
                return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"{candidate.name}: {exc}")
        if errors:
            self.status = f"Settings ignored: {'; '.join(errors)}"
            self.log(self.status)

    def settings_payload(self):
        return {
            "version": 2,
            "volume": round(self.volume, 2),
            "mapping_mode": self.mapping_mode,
            "repeat_enabled": self.repeat_enabled,
            "repeat_rate": self.repeat_rate,
            "metronome_enabled": self.metronome_enabled,
            "record_start_mode": self.record_start_mode,
            "sample_input_name": self.sample_input_name,
            "sample_start_mode": self.sample_start_mode,
            "sample_monitor_enabled": self.sample_monitor_enabled,
            "sample_continuous_enabled": self.sample_continuous_enabled,
            "audio_output_name": self.audio_output_name,
            "audio_mode": self.audio_mode,
            "audio_rate": self.audio_rate,
            "audio_buffer": self.audio_buffer,
            "ui_scale": self.ui_scale,
            "accent": self.accent_name,
            "clock_source": self.clock_source,
            "clock_output_enabled": self.clock_output_enabled,
            "midi_output_name": self.midi_output_name,
            "clock_correction_ms": self.clock_correction_ms,
            "pad_calibrations": self.pad_calibrations,
            "calibration_prompted": self.calibration_prompted,
            "current_project": str(self.project_path) if self.project_path else None,
            "recent_projects": list(self.recent_projects[:8]),
            "sample_favorites": list(self.sample_favorites[:128]),
            "recent_samples": list(self.recent_samples[:32]),
        }

    def persist_settings(self):
        if not self.settings_path:
            return
        try:
            with self.settings_lock:
                payload = json.dumps(self.settings_payload(), indent=2, ensure_ascii=True) + "\n"
                self.write_text_atomic(self.settings_path, payload)
                self.write_text_atomic(self.settings_backup_path(), payload)
            self.persist_project()
        except OSError as exc:
            self.status = f"Settings save failed: {exc}"
            self.log(self.status)

    def persist_settings_async(self):
        if not self.settings_path:
            return
        threading.Thread(target=self.persist_settings, name="DrumSettingsSave", daemon=True).start()

    def project_backup_path(self, path=None):
        target = Path(path or self.project_path) if path or self.project_path else None
        return target.with_suffix(target.suffix + ".bak") if target else None

    def current_pattern_data_locked(self):
        return {
            "bars": self.loop_bars,
            "events": [list(event) for event in self.loop_events],
            "source_events": [list(event) for event in self.loop_source_events] if self.loop_source_events is not None else None,
            "event_meta": json.loads(json.dumps(self.loop_event_meta)),
            "feel_preset": self.feel_preset,
            "feel_strength": self.feel_strength,
            "feel_swing": self.feel_swing,
            "feel_nudge_ms": self.feel_nudge_ms,
            "feel_humanize_ms": self.feel_humanize_ms,
            "perform_fx_events": [list(event) for event in self.perform_fx_events],
        }

    def sanitize_pattern_data(self, value):
        if not isinstance(value, dict):
            return None
        try:
            bars = int(value.get("bars", 1))
            bars = bars if bars in LOOP_BAR_OPTIONS else 1
            events = self.sanitize_loop_events(value.get("events", []), bars)
            source_value = value.get("source_events")
            source = self.sanitize_loop_events(source_value, bars) if isinstance(source_value, list) else None
            preset = str(value.get("feel_preset", "Natural"))
            preset = preset if preset in (*FEEL_PRESETS, "Custom") else "Natural"
            defaults = FEEL_PRESETS.get(preset, FEEL_PRESETS["Natural"])
            return {
                "bars": bars,
                "events": events,
                "source_events": source,
                "event_meta": sanitize_event_meta(value.get("event_meta", {})),
                "feel_preset": preset,
                "feel_strength": max(0, min(100, int(value.get("feel_strength", defaults["strength"])))),
                "feel_swing": max(50, min(75, int(value.get("feel_swing", defaults["swing"])))),
                "feel_nudge_ms": max(-50, min(50, int(value.get("feel_nudge_ms", defaults["nudge_ms"])))),
                "feel_humanize_ms": max(0, min(20, int(value.get("feel_humanize_ms", defaults["humanize_ms"])))),
                "perform_fx_events": self.sanitize_perform_fx_events(value.get("perform_fx_events", []), bars),
            }
        except (TypeError, ValueError):
            return None

    def save_active_pattern_locked(self):
        self.patterns[self.active_pattern] = self.current_pattern_data_locked()

    def apply_pattern_data_locked(self, value):
        pattern = self.sanitize_pattern_data(value) or {
            "bars": 1,
            "events": [],
            "source_events": None,
            "event_meta": {},
            "feel_preset": "Natural",
            "feel_strength": FEEL_PRESETS["Natural"]["strength"],
            "feel_swing": FEEL_PRESETS["Natural"]["swing"],
            "feel_nudge_ms": FEEL_PRESETS["Natural"]["nudge_ms"],
            "feel_humanize_ms": FEEL_PRESETS["Natural"]["humanize_ms"],
            "perform_fx_events": [],
        }
        self.loop_bars = pattern["bars"]
        self.loop_events = list(pattern["events"])
        self.loop_source_events = list(pattern["source_events"]) if pattern["source_events"] is not None else None
        self.loop_event_meta = json.loads(json.dumps(pattern["event_meta"]))
        self.feel_preset = pattern["feel_preset"]
        self.feel_strength = pattern["feel_strength"]
        self.feel_swing = pattern["feel_swing"]
        self.feel_nudge_ms = pattern["feel_nudge_ms"]
        self.feel_humanize_ms = pattern["feel_humanize_ms"]
        self.perform_fx_events = list(pattern.get("perform_fx_events", []))
        self.sequence_bar_page = min(self.sequence_bar_page, self.loop_bars - 1)
        self.sequence_selected = None
        self.sequence_selection.clear()

    def project_payload(self):
        self.save_current_kit()
        with self.loop_lock:
            self.save_active_pattern_locked()
            loop_events = [list(event) for event in self.loop_events]
            loop_bars = self.loop_bars
        return {
            "version": 1,
            "name": self.project_name,
            "active_kit": self.active_kit,
            "bpm": self.bpm,
            "loop_bars": loop_bars,
            "loop_events": loop_events,
            "loop_source_events": [list(event) for event in self.loop_source_events] if self.loop_source_events is not None else None,
            "feel_preset": self.feel_preset,
            "feel_strength": self.feel_strength,
            "feel_swing": self.feel_swing,
            "feel_nudge_ms": self.feel_nudge_ms,
            "feel_humanize_ms": self.feel_humanize_ms,
            "loop_event_meta": self.loop_event_meta,
            "perform_fx_events": [list(event) for event in self.perform_fx_events],
            "patterns": self.patterns,
            "active_pattern": self.active_pattern,
            "pattern_launch_mode": self.pattern_launch_mode,
            "scene_order": self.scene_order,
            "kits": self.kit_slots,
        }

    def apply_project_data(self, data):
        if not isinstance(data, dict):
            raise ValueError("Project must be an object")
        active_kit = str(data.get("active_kit", "A"))
        self.active_kit = active_kit if active_kit in KIT_SLOTS else "A"
        self.bpm = max(BPM_MIN, min(BPM_MAX, int(data.get("bpm", 120))))
        loop_bars = int(data.get("loop_bars", 1))
        self.loop_bars = loop_bars if loop_bars in LOOP_BAR_OPTIONS else 1
        self.loop_events = self.sanitize_loop_events(data.get("loop_events", []), self.loop_bars)
        self.perform_fx_events = self.sanitize_perform_fx_events(
            data.get("perform_fx_events", []), self.loop_bars
        )
        source_events = data.get("loop_source_events")
        self.loop_source_events = (
            self.sanitize_loop_events(source_events, self.loop_bars)
            if isinstance(source_events, list)
            else None
        )
        feel_preset = str(data.get("feel_preset", "Natural"))
        self.feel_preset = feel_preset if feel_preset in (*FEEL_PRESETS, "Custom") else "Natural"
        try:
            self.feel_strength = max(0, min(100, int(data.get("feel_strength", FEEL_PRESETS[self.feel_preset]["strength"]))))
            self.feel_swing = max(50, min(75, int(data.get("feel_swing", FEEL_PRESETS[self.feel_preset]["swing"]))))
            self.feel_nudge_ms = max(-50, min(50, int(data.get("feel_nudge_ms", 0))))
            self.feel_humanize_ms = max(0, min(20, int(data.get("feel_humanize_ms", 0))))
        except (TypeError, ValueError):
            preset = FEEL_PRESETS[self.feel_preset]
            self.feel_strength = preset["strength"]
            self.feel_swing = preset["swing"]
            self.feel_nudge_ms = preset["nudge_ms"]
            self.feel_humanize_ms = preset["humanize_ms"]
        self.loop_event_meta = sanitize_event_meta(data.get("loop_event_meta", {}))
        pattern_values = data.get("patterns")
        if isinstance(pattern_values, list) and len(pattern_values) == PATTERN_COUNT:
            self.patterns = [self.sanitize_pattern_data(value) for value in pattern_values]
            self.active_pattern = max(0, min(PATTERN_COUNT - 1, int(data.get("active_pattern", 0))))
            self.apply_pattern_data_locked(self.patterns[self.active_pattern])
        else:
            self.patterns = [None] * PATTERN_COUNT
            self.patterns[0] = self.current_pattern_data_locked()
            self.active_pattern = 0
        launch_mode = str(data.get("pattern_launch_mode", "Next bar"))
        self.pattern_launch_mode = launch_mode if launch_mode in PATTERN_LAUNCH_MODES else "Next bar"
        scene_order = data.get("scene_order", [])
        self.scene_order = [int(value) for value in scene_order if isinstance(value, int) and 0 <= value < PATTERN_COUNT][:64] if isinstance(scene_order, list) else []
        self.loop_playing = False
        self.loop_recording = False
        self.loop_overdub = False
        self.loop_record_pending = False
        self.loop_start_ns = None
        self.loop_pending = []
        self.loop_history.clear()
        self.loop_redo.clear()
        saved_kits = data.get("kits", {})
        if isinstance(saved_kits, dict):
            for slot in KIT_SLOTS:
                self.kit_slots[slot] = self.sanitize_kit_profile(saved_kits.get(slot))
        self.apply_kit_profile(self.kit_slots[self.active_kit])
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            self.project_name = name.strip()[:48]

    def initialize_project(self):
        if not self.project_dir:
            return
        self.project_dir.mkdir(parents=True, exist_ok=True)
        candidate = self.project_path if self.project_path and self.project_path.exists() else self.project_dir / f"Current{PROJECT_EXTENSION}"
        self.project_initialized = True
        if candidate.exists() and self.load_project(candidate, update_settings=False):
            self.persist_settings()
            return
        self.project_path = candidate
        self.project_name = candidate.name.removesuffix(PROJECT_EXTENSION)
        self.persist_settings()

    def load_project(self, path, update_settings=True):
        target = Path(path)
        for candidate in (target, self.project_backup_path(target)):
            if candidate is None or not candidate.exists():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8")
                self.apply_project_data(json.loads(raw))
                self.project_path = target
                if candidate != target:
                    self.write_text_atomic(target, raw)
                    self.status = "Recovered project autosave"
                self.remember_project(target)
                if update_settings:
                    self.persist_settings()
                return True
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.log(f"Project skipped: {exc}")
        return False

    def persist_project(self):
        if not self.project_initialized or not self.project_path:
            return
        try:
            with self.project_lock:
                payload = json.dumps(self.project_payload(), indent=2, ensure_ascii=True) + "\n"
                self.project_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_text_atomic(self.project_path, payload)
                self.write_text_atomic(self.project_backup_path(), payload)
        except OSError as exc:
            self.status = f"Project save failed: {exc}"
            self.log(self.status)

    def remember_project(self, path):
        value = str(Path(path).resolve())
        self.recent_projects = [value] + [item for item in self.recent_projects if item != value]
        del self.recent_projects[8:]

    def project_snapshot(self):
        return json.loads(json.dumps(self.project_payload()))

    def push_project_history(self):
        self.project_history.append(self.project_snapshot())
        self.project_redo.clear()

    def restore_project_snapshot(self, snapshot):
        now_ns = time.perf_counter_ns()
        with self.loop_lock:
            was_playing = self.loop_playing
            phase = 0.0
            if was_playing and self.loop_start_ns is not None:
                quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
                phase = max(0.0, (now_ns - self.loop_start_ns) / quarter_ns)
        self.apply_project_data(snapshot)
        if was_playing and self.loop_events:
            with self.loop_lock:
                self.loop_playing = True
                self.loop_schedule_bpm = self.bpm
                quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
                self.loop_start_ns = now_ns - int((phase % (self.loop_bars * 4.0)) * quarter_ns)
                self.rebuild_loop_pending_locked(now_ns)
        self.custom_sound_cache = {}
        if pygame.mixer.get_init():
            self.load_custom_samples()
        self.persist_settings()

    def undo_project_edit(self):
        if not self.project_history:
            self.status = "Nothing to undo"
            return False
        self.project_redo.append(self.project_snapshot())
        self.restore_project_snapshot(self.project_history.pop())
        self.status = "Edit undone"
        return True

    def redo_project_edit(self):
        if not self.project_redo:
            self.status = "Nothing to redo"
            return False
        self.project_history.append(self.project_snapshot())
        snapshot = self.project_redo.pop()
        self.restore_project_snapshot(snapshot)
        self.status = "Edit redone"
        return True

    @staticmethod
    def choose_project_file(save=False):
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            options = {
                "title": "Save STARRYPAD Project" if save else "Open STARRYPAD Project",
                "filetypes": [("STARRYPAD Project", f"*{PROJECT_EXTENSION}")],
            }
            if save:
                value = filedialog.asksaveasfilename(defaultextension=PROJECT_EXTENSION, **options)
            else:
                value = filedialog.askopenfilename(**options)
            root.destroy()
            return Path(value) if value else None
        except Exception:
            return None

    def new_project(self):
        if not self.project_dir:
            return
        index = 1
        while True:
            name = "Untitled" if index == 1 else f"Untitled {index}"
            target = self.project_dir / f"{name}{PROJECT_EXTENSION}"
            if not target.exists():
                break
            index += 1
        if pygame.mixer.get_init():
            self.stop_all_sounds()
        self.solo_pads.clear()
        self.mixer_bypass = False
        self.mixer_open = False
        self.selected_pad = 0
        self.pad_selection = {0}
        self.active_kit = "A"
        self.kit_slots = {slot: self.default_kit_profile() for slot in KIT_SLOTS}
        self.apply_kit_profile(self.kit_slots[self.active_kit])
        self.bpm = 120
        with self.loop_lock:
            self.loop_events = []
            self.loop_source_events = None
            self.loop_event_meta = {}
            self.loop_bars = 1
            self.loop_history.clear()
            self.loop_redo.clear()
            self.loop_playing = False
            self.loop_recording = False
            self.loop_overdub = False
            self.loop_start_ns = None
            self.loop_pending = []
        self.feel_preset = "Natural"
        self.feel_strength = 50
        self.feel_swing = 50
        self.feel_nudge_ms = 0
        self.feel_humanize_ms = 0
        self.patterns = [None] * PATTERN_COUNT
        self.active_pattern = 0
        with self.loop_lock:
            self.save_active_pattern_locked()
        self.pending_pattern = None
        self.pattern_switch_deadline_ns = None
        self.scene_order = []
        self.scene_position = 0
        self.song_playing = False
        self.project_path = target
        self.project_name = name
        self.project_history.clear()
        self.project_redo.clear()
        self.remember_project(target)
        self.persist_settings()
        self.status = "New project"

    def save_project_as(self, path=None):
        target = Path(path) if path else self.choose_project_file(save=True)
        if target is None:
            return False
        if not str(target).lower().endswith(PROJECT_EXTENSION):
            target = Path(str(target) + PROJECT_EXTENSION)
        self.project_path = target
        self.project_name = target.name.removesuffix(PROJECT_EXTENSION)[:48]
        self.remember_project(target)
        self.persist_settings()
        self.status = "Project saved"
        return True

    def open_project(self, path=None):
        target = Path(path) if path else self.choose_project_file(save=False)
        if target is None or not self.load_project(target):
            if target is not None:
                self.status = "Project could not be opened"
            return False
        if pygame.mixer.get_init():
            self.stop_all_sounds()
        self.solo_pads.clear()
        self.mixer_bypass = False
        self.mixer_open = False
        self.pad_selection = {self.selected_pad}
        self.custom_sound_cache = {}
        self.project_history.clear()
        self.project_redo.clear()
        if pygame.mixer.get_init():
            self.load_custom_samples()
        self.loop_schedule_bpm = self.bpm
        self.status = "Project opened"
        return True

    def project_sample_dir(self, path=None):
        target = Path(path or self.project_path) if path or self.project_path else None
        return target.parent / f"{target.name.removesuffix(PROJECT_EXTENSION)}.samples" if target else None

    def custom_sample_path(self, filename):
        collected = self.project_sample_dir()
        if collected:
            candidate = collected / filename
            if candidate.exists():
                return candidate
        return USER_SAMPLE_DIR / filename

    def collect_project_samples(self):
        target_dir = self.project_sample_dir()
        if target_dir is None:
            return False
        referenced = {
            filename
            for profile in self.kit_slots.values()
            for filename in profile.get("custom_samples", [])
            if filename
        }
        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for filename in referenced:
            source = self.custom_sample_path(filename)
            destination = target_dir / filename
            if source.exists() and source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
                copied += 1
        self.persist_project()
        self.status = f"Collected {copied} samples"
        return True

    def save_current_kit(self):
        self.kit_slots[self.active_kit] = {
            "pad_synths": list(self.pad_synths),
            "pad_sensitivity": list(self.pad_sensitivity),
            "custom_samples": list(self.custom_sample_files),
            "sample_edits": json.loads(json.dumps(self.sample_edits)),
            "pad_volume": list(self.pad_volume),
            "pad_pan": list(self.pad_pan),
            "pad_tune": list(self.pad_tune),
            "pad_mute": list(self.pad_mute),
            "pad_punch": list(self.pad_punch), "pad_air": list(self.pad_air),
            "pad_space": list(self.pad_space), "pad_bus": list(self.pad_bus),
        }

    def apply_kit_profile(self, profile):
        sanitized = self.sanitize_kit_profile(profile)
        self.pad_synths = sanitized["pad_synths"]
        self.pad_sensitivity = sanitized["pad_sensitivity"]
        self.custom_sample_files = sanitized["custom_samples"]
        self.sample_edits = sanitized["sample_edits"]
        self.edited_sound_cache.clear()
        self.waveform_cache.clear()
        self.pad_volume = sanitized["pad_volume"]
        self.pad_pan = sanitized["pad_pan"]
        self.pad_tune = sanitized["pad_tune"]
        self.pad_mute = sanitized["pad_mute"]
        self.pad_punch = sanitized["pad_punch"]
        self.pad_air = sanitized["pad_air"]
        self.pad_space = sanitized["pad_space"]
        self.pad_bus = sanitized["pad_bus"]
        self.processed_sound_cache.clear()

    def switch_kit(self):
        self.push_project_history()
        self.save_current_kit()
        current_index = KIT_SLOTS.index(self.active_kit)
        self.active_kit = KIT_SLOTS[(current_index + 1) % len(KIT_SLOTS)]
        self.apply_kit_profile(self.kit_slots[self.active_kit])
        self.prewarm_pad_tuning(range(len(PADS)))
        self.solo_pads.clear()
        self.mixer_bypass = False
        self.pad_selection = {self.selected_pad}
        self.persist_settings()
        self.log(f"Kit: {self.active_kit}")

    def adjust_pad_sensitivity(self, amount):
        self.push_project_history()
        index = self.selected_pad
        self.pad_sensitivity[index] = clamp_sensitivity(self.pad_sensitivity[index] + amount)
        self.persist_settings()
        self.log(f"Pad {index + 1} sensitivity {int(self.pad_sensitivity[index] * 100)}%")

    def mixer_targets(self):
        return sorted(self.pad_selection or {self.selected_pad})

    def adjust_pad_mix(self, field, amount):
        if field not in ("volume", "pan", "tune"):
            return False
        self.push_project_history()
        targets = self.mixer_targets()
        if field == "volume":
            for index in targets:
                self.pad_volume[index] = round(max(0.0, min(1.5, self.pad_volume[index] + amount)), 2)
        elif field == "pan":
            for index in targets:
                self.pad_pan[index] = round(max(-1.0, min(1.0, self.pad_pan[index] + amount)), 2)
        else:
            for index in targets:
                self.pad_tune[index] = max(-12, min(12, self.pad_tune[index] + int(amount)))
        self.persist_settings_async()
        if field == "tune":
            self.prewarm_pad_tuning(self.mixer_targets())
        return True

    def toggle_pad_mute(self):
        self.push_project_history()
        targets = self.mixer_targets()
        new_value = not all(self.pad_mute[index] for index in targets)
        for index in targets:
            self.pad_mute[index] = new_value
        self.persist_settings_async()
        return new_value

    def toggle_pad_solo(self):
        targets = set(self.mixer_targets())
        if targets and targets.issubset(self.solo_pads):
            self.solo_pads.difference_update(targets)
        else:
            self.solo_pads.update(targets)
        return bool(self.solo_pads)

    def reset_pad_mix(self):
        self.push_project_history()
        for index in self.mixer_targets():
            self.pad_volume[index] = 1.0
            self.pad_pan[index] = 0.0
            self.pad_tune[index] = 0
            self.pad_mute[index] = False
            self.solo_pads.discard(index)
        self.persist_settings_async()

    def adjust_pad_fx(self, field, amount):
        values = {"punch": self.pad_punch, "air": self.pad_air, "space": self.pad_space}.get(field)
        if values is None:
            return False
        self.push_project_history()
        for index in self.mixer_targets():
            values[index] = max(0, min(100, values[index] + int(amount)))
        self.processed_sound_cache.clear()
        self.persist_settings_async()
        return True

    def cycle_pad_bus(self):
        self.push_project_history()
        for index in self.mixer_targets():
            self.pad_bus[index] = (self.pad_bus[index] + 1) % 4
        self.persist_settings_async()

    def reset_pad_fx(self):
        self.push_project_history()
        for index in self.mixer_targets():
            self.pad_punch[index] = 0
            self.pad_air[index] = 0
            self.pad_space[index] = 0
        self.processed_sound_cache.clear()
        self.persist_settings_async()

    def tuned_sound(self, sound, semitones):
        semitones = round(float(semitones), 1)
        if semitones == 0:
            return sound
        key = (id(sound), semitones)
        cached = self.tuned_sound_cache.get(key)
        if cached is not None:
            return cached
        samples = pygame.sndarray.array(sound)
        shifted = pitch_shift_array(samples, semitones)
        tuned = pygame.sndarray.make_sound(shifted)
        self.tuned_sound_cache[key] = tuned
        return tuned

    def processed_sound(self, sound, pad_index, bypass=False):
        if sound is None:
            return sound
        perform_values = tuple(self.perform_fx.values()) if not self.perform_fx_bypass else (0, 0, 0, 0)
        pad_values = (0, 0, 0) if bypass else (
            self.pad_punch[pad_index], self.pad_air[pad_index], self.pad_space[pad_index]
        )
        values = (*pad_values, *perform_values)
        if not any(values):
            return sound
        key = (id(sound), *values)
        cached = self.processed_sound_cache.get(key)
        if cached is not None:
            return cached
        samples = pygame.sndarray.array(sound)
        processed_samples = apply_sound_macros(samples, *values[:3])
        processed_samples = apply_perform_fx(processed_samples, *perform_values)
        processed = pygame.sndarray.make_sound(processed_samples)
        self.processed_sound_cache[key] = processed
        return processed

    def track_master_peak(self, gain):
        now_ns = time.perf_counter_ns()
        if now_ns - self.master_peak_window_ns > 12_000_000:
            self.master_peak_window_ns = now_ns
            self.master_peak_sum = 0.0
        self.master_peak_sum += max(0.0, float(gain))
        if self.master_peak_sum > 1.0:
            self.master_peak_warning_until = time.perf_counter() + 1.2

    def start_pad_calibration(self):
        self.calibration_active = True
        self.calibration_pad = self.selected_pad
        self.calibration_stage = 0
        self.calibration_hits = []
        self.calibration_duplicate_ms = []
        self.calibration_last_raw_ns = None
        self._calibration_medians = []
        self.status = "Calibration: play 3 soft hits"

    def cancel_pad_calibration(self):
        self.calibration_active = False
        self.calibration_pad = None
        self.calibration_hits = []
        self.calibration_duplicate_ms = []
        self.calibration_last_raw_ns = None
        self._calibration_medians = []
        self.status = "Calibration cancelled"

    def reset_pad_calibration(self):
        self.pad_calibrations[self.selected_pad] = default_pad_calibration()
        self.calibration_active = False
        self.calibration_pad = None
        self.calibration_hits = []
        self.calibration_duplicate_ms = []
        self.calibration_last_raw_ns = None
        self._calibration_medians = []
        self.persist_settings()
        self.status = f"Pad {self.selected_pad + 1} calibration reset"

    def cycle_audio_output(self, direction):
        devices = [None] + self.audio_output_devices()
        try:
            index = devices.index(self.audio_output_name)
        except ValueError:
            index = 0
        candidate = devices[(index + direction) % len(devices)]
        self.apply_audio_setup(candidate, self.audio_mode, self.audio_rate, self.audio_buffer)

    def set_audio_mode(self, mode):
        rate, buffer_size = audio_mode_config(mode)
        self.apply_audio_setup(self.audio_output_name, mode, rate, buffer_size)

    def cycle_audio_rate(self):
        index = AUDIO_RATES.index(self.audio_rate)
        rate = AUDIO_RATES[(index + 1) % len(AUDIO_RATES)]
        self.apply_audio_setup(self.audio_output_name, self.audio_mode, rate, self.audio_buffer)

    def cycle_audio_buffer(self):
        index = AUDIO_BUFFERS.index(self.audio_buffer)
        buffer_size = AUDIO_BUFFERS[(index + 1) % len(AUDIO_BUFFERS)]
        self.apply_audio_setup(self.audio_output_name, self.audio_mode, self.audio_rate, buffer_size)

    def collect_calibration_hit(self, pad_index, velocity):
        if not self.calibration_active or pad_index != self.calibration_pad:
            return False
        self.calibration_hits.append(max(1, min(127, int(velocity))))
        if len(self.calibration_hits) < 3:
            return True
        self._calibration_medians.append(int(statistics.median(self.calibration_hits)))
        self.calibration_hits = []
        self.calibration_stage += 1
        if self.calibration_stage < 3:
            labels = ("soft", "natural", "hard")
            self.status = f"Calibration: play 3 {labels[self.calibration_stage]} hits"
            return True

        soft, natural, hard = self._calibration_medians
        if natural < soft + 4 or hard < natural + 4:
            self.calibration_stage = 0
            self._calibration_medians = []
            self.status = "Hits were too similar - try again"
            return True
        auto_dead_time = 10
        if self.calibration_duplicate_ms:
            auto_dead_time = max(6, min(30, round(max(self.calibration_duplicate_ms) + 2)))
        self.pad_calibrations[pad_index] = sanitize_pad_calibration({
            "enabled": True,
            "soft": soft,
            "natural": natural,
            "hard": hard,
            "dead_time_ms": auto_dead_time,
        })
        self.calibration_active = False
        self.calibration_pad = None
        self.persist_settings()
        self.status = f"Pad {pad_index + 1} calibrated"
        return True

    def cycle_pad_sound(self, direction):
        self.push_project_history()
        index = self.selected_pad
        current = self.pad_synths[index]
        family = next((values for values in TIMBRE_FAMILIES if current in values), None)
        choices = family or KIT_ORDER
        sound_index = choices.index(current)
        self.pad_synths[index] = choices[(sound_index + direction) % len(choices)]
        self.custom_sample_files[index] = None
        self.prewarm_pad_tuning((index,))
        self.persist_settings()
        self.log(f"Pad {index + 1}: {SYNTH_LABELS[self.pad_synths[index]]}")

    @staticmethod
    def sanitize_loop_events(events, bars):
        total_beats = bars * 4.0
        sanitized = []
        if not isinstance(events, list):
            return sanitized
        for event in events:
            if not isinstance(event, (list, tuple)) or len(event) != 3:
                continue
            try:
                beat = float(event[0]) % total_beats
                pad_index = int(event[1])
                velocity = max(1, min(127, int(event[2])))
            except (TypeError, ValueError):
                continue
            if 0 <= pad_index < len(PADS):
                sanitized.append((beat, pad_index, velocity))
        return sorted(sanitized)

    @staticmethod
    def sanitize_perform_fx_events(events, bars):
        sanitized = []
        total_beats = max(1.0, float(bars) * 4.0)
        if not isinstance(events, list):
            return sanitized
        for event in events:
            if not isinstance(event, (list, tuple)) or len(event) != 3:
                continue
            try:
                beat, field, value = float(event[0]) % total_beats, str(event[1]), int(event[2])
            except (TypeError, ValueError):
                continue
            if field in ("filter", "delay", "stutter", "crush"):
                sanitized.append((beat, field, max(0, min(100, value))))
        return sorted(sanitized)

    def run(self):
        enable_process_priority()
        self.init_pygame()
        self.load_samples()
        self.start_audio_worker()
        try:
            self.open_preferred_midi()
            self.open_preferred_midi_output()
            self.main_loop()
        finally:
            self.close_midi()
            self.close_midi_output()
            self.stop_audio_worker()
            if self.sampler.snapshot()[0]:
                self.stop_sampling()
            self.wait_for_sample_worker()
            self.poll_sample_results()
            if self.bounce_thread and self.bounce_thread.is_alive():
                self.bounce_thread.join(timeout=10.0)
            self.persist_settings()
            self.wait_for_export()
            pygame.quit()

    def init_pygame(self):
        pygame.mixer.pre_init(
            self.audio_rate,
            -16,
            2,
            self.audio_buffer,
            devicename=self.audio_output_name,
        )
        pygame.init()
        if not pygame.mixer.get_init():
            try:
                self.initialize_mixer(self.audio_output_name, self.audio_rate, self.audio_buffer)
            except pygame.error:
                self.audio_output_name = None
                self.initialize_mixer(None, self.audio_rate, self.audio_buffer)
                self.status = "Audio output changed - using default"
        pygame.mixer.set_num_channels(96)
        self.display_size = tuple(round(value * self.ui_scale) for value in WINDOW_SIZE)
        self.display_surface = pygame.display.set_mode(self.display_size, pygame.RESIZABLE)
        self.screen = pygame.Surface(WINDOW_SIZE).convert()
        self.update_display_viewport(self.display_size)
        pygame.display.set_caption("STARRYPAD")
        self.set_window_icon()
        self.grain = self.build_grain()
        self.clock = pygame.time.Clock()
        self.audio_inputs_available = bool(audio_input_devices())
        self.faces = typeface.build()
        self.font = self.faces["ui"]
        self.small_font = self.faces["small"]
        self.big_font = self.faces["big"]
        self.label_font = self.faces["label"]
        self.head_font = self.faces["head"]
        self.data_font = self.faces["data"]
        self.data_font_lg = self.faces["data_lg"]
        self.data_font_sm = self.faces["data_sm"]

    @staticmethod
    def set_window_icon():
        try:
            pygame.display.set_icon(pygame.image.load(str(APP_ICON_FILE)))
        except (pygame.error, FileNotFoundError):
            pass

    @staticmethod
    def build_grain():
        """One pre-tiled noise layer, so the ground is a coated panel, not a flat fill."""
        try:
            tile = pygame.image.load(str(GRAIN_FILE)).convert_alpha()
        except (pygame.error, FileNotFoundError):
            return None
        tile.fill((255, 255, 255, GRAIN_OPACITY), special_flags=pygame.BLEND_RGBA_MULT)
        layer = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        for y in range(0, WINDOW_SIZE[1], tile.get_height()):
            for x in range(0, WINDOW_SIZE[0], tile.get_width()):
                layer.blit(tile, (x, y))
        return layer

    def update_display_viewport(self, size):
        width, height = max(640, int(size[0])), max(500, int(size[1]))
        self.display_size = (width, height)
        ratio = min(width / WINDOW_SIZE[0], height / WINDOW_SIZE[1])
        viewport_size = (max(1, round(WINDOW_SIZE[0] * ratio)), max(1, round(WINDOW_SIZE[1] * ratio)))
        self.display_viewport = pygame.Rect(
            (width - viewport_size[0]) // 2, (height - viewport_size[1]) // 2, *viewport_size
        )

    def set_ui_scale(self, scale):
        scale = min((1.0, 1.25, 1.5), key=lambda value: abs(value - float(scale)))
        self.ui_scale = scale
        self.display_size = tuple(round(value * scale) for value in WINDOW_SIZE)
        self.display_surface = pygame.display.set_mode(self.display_size, pygame.RESIZABLE)
        self.update_display_viewport(self.display_size)
        self.persist_settings_async()

    def window_to_logical(self, pos):
        viewport = self.display_viewport
        x = (pos[0] - viewport.x) * WINDOW_SIZE[0] / max(1, viewport.width)
        y = (pos[1] - viewport.y) * WINDOW_SIZE[1] / max(1, viewport.height)
        return round(x), round(y)

    def present_screen(self):
        if self.display_surface is self.screen:
            pygame.display.flip()
            return
        self.display_surface.fill(theme.GROUND)
        scaled = pygame.transform.smoothscale(self.screen, self.display_viewport.size)
        self.display_surface.blit(scaled, self.display_viewport)
        pygame.display.flip()

    @staticmethod
    def initialize_mixer(device_name, rate, buffer_size):
        pygame.mixer.init(
            int(rate),
            -16,
            2,
            int(buffer_size),
            devicename=device_name,
            allowedchanges=0,
        )
        pygame.mixer.set_num_channels(96)

    @staticmethod
    def audio_output_devices():
        try:
            from pygame._sdl2 import audio as sdl_audio

            return list(sdl_audio.get_audio_device_names(False))
        except (ImportError, pygame.error):
            return []

    def reload_mixer_sounds(self):
        self.samples = {}
        self.custom_sound_cache = {}
        self.tuned_sound_cache = {}
        self.processed_sound_cache = {}
        self.load_samples()

    def apply_audio_setup(self, output_name=None, mode=None, rate=None, buffer_size=None):
        previous = (self.audio_output_name, self.audio_mode, self.audio_rate, self.audio_buffer)
        requested_mode = mode if mode in AUDIO_MODES else self.audio_mode
        default_rate, default_buffer = audio_mode_config(requested_mode)
        requested_rate = int(rate if rate is not None else default_rate)
        requested_buffer = int(buffer_size if buffer_size is not None else default_buffer)
        if requested_rate not in AUDIO_RATES or requested_buffer not in AUDIO_BUFFERS:
            raise ValueError("Unsupported audio setup")

        self.stop_audio_worker()
        try:
            pygame.mixer.stop()
            pygame.mixer.quit()
            self.initialize_mixer(output_name, requested_rate, requested_buffer)
            self.audio_output_name = output_name
            self.audio_mode = requested_mode
            self.audio_rate = requested_rate
            self.audio_buffer = requested_buffer
            self.reload_mixer_sounds()
            self.status = f"Audio ready: {requested_mode}"
            self.persist_settings()
            return True
        except (pygame.error, OSError, ValueError) as exc:
            try:
                pygame.mixer.quit()
                self.initialize_mixer(previous[0], previous[2], previous[3])
            except pygame.error:
                pygame.mixer.quit()
                self.initialize_mixer(None, MIXER_FREQUENCY, MIXER_BUFFER)
                previous = (None, "Low latency", MIXER_FREQUENCY, MIXER_BUFFER)
            self.audio_output_name, self.audio_mode, self.audio_rate, self.audio_buffer = previous
            self.reload_mixer_sounds()
            self.status = "Audio change failed - previous setup restored"
            self.log(f"Audio setup failed: {exc}")
            return False
        finally:
            self.start_audio_worker()

    def maintain_audio_connection(self, now=None):
        now = time.perf_counter() if now is None else now
        if now < self.next_audio_health_check_at or self.audio_recovering:
            return
        self.next_audio_health_check_at = now + AUDIO_HEALTH_CHECK_SECONDS
        # Sampling controls are only offered when something can actually record.
        self.audio_inputs_available = bool(audio_input_devices())
        configured_missing = (
            self.audio_output_name is not None
            and self.audio_output_name not in self.audio_output_devices()
        )
        if pygame.mixer.get_init() and not configured_missing:
            return
        self.audio_recovering = True
        try:
            fallback = None if configured_missing else self.audio_output_name
            if self.apply_audio_setup(fallback, self.audio_mode, self.audio_rate, self.audio_buffer):
                self.status = "Audio reconnected"
        finally:
            self.audio_recovering = False

    def handle_system_resume(self, now=None):
        now = time.perf_counter() if now is None else float(now)
        self.audio_events.put(("PANIC",))
        self.next_midi_health_check_at = 0.0
        self.next_audio_health_check_at = 0.0
        self.next_metronome_ns = None
        self.next_midi_clock_out_ns = None
        self.set_surface_notice("Checking connections")
        self.maintain_midi_connection(now)
        self.maintain_audio_connection(now)

    def force_reconnect(self):
        self.audio_events.put(("PANIC",))
        self.next_midi_health_check_at = 0.0
        self.next_audio_health_check_at = 0.0
        self.maintain_midi_connection(0.0)
        self.maintain_audio_connection(0.0)
        self.set_surface_notice("Reconnect requested")

    def start_audio_test(self, now=None):
        if self.audio_test_active:
            return False
        now = time.perf_counter() if now is None else float(now)
        self.audio_test_active = True
        self.audio_test_deadline = now + 10.0
        self.audio_test_next_hit = now
        self.audio_test_hit_index = 0
        with self.metrics_lock:
            self.trigger_latencies.clear()
            self.max_queue_depth = 0
            baseline_errors = self.audio_error_count
        self.audio_test_baseline = (0.0, 0.0, 0, 0, baseline_errors, 0)
        self.audio_test_mixer_ok = bool(pygame.mixer.get_init())
        self.audio_test_result = ""
        self.set_surface_notice("Testing audio")
        return True

    def update_audio_test(self, now=None):
        if not self.audio_test_active:
            return None
        now = time.perf_counter() if now is None else float(now)
        self.audio_test_mixer_ok = self.audio_test_mixer_ok and bool(pygame.mixer.get_init())
        while self.audio_test_next_hit <= min(now, self.audio_test_deadline):
            self.queue_pad(self.audio_test_hit_index % len(PADS), 84)
            self.audio_test_hit_index += 1
            self.audio_test_next_hit += 0.125
        if now < self.audio_test_deadline:
            return None
        _p95, p99, _hits, _ignored, errors, queue_depth = self.diagnostic_snapshot()
        baseline_errors = self.audio_test_baseline[4] if self.audio_test_baseline else 0
        stable = self.audio_test_mixer_ok and errors == baseline_errors and p99 <= 5.0 and queue_depth <= 8
        self.audio_test_result = "Low latency passed" if stable else "Use Stable mode"
        self.audio_test_active = False
        self.set_surface_notice(self.audio_test_result, duration=4.0)
        return stable

    def load_samples(self):
        missing = []
        for file in all_sample_files():
            path = SAMPLE_DIR / file
            if not path.exists():
                missing.append(file)
                continue
            self.samples[file] = pygame.mixer.Sound(str(path))

        if missing:
            self.status = f"Missing {len(missing)} samples"
            self.log(self.status)
        else:
            self.status = f"Loaded {len(self.samples)} samples"
            self.log(self.status)
        self.load_custom_samples()
        self.prewarm_pad_tuning(range(len(PADS)))

    def prewarm_pad_tuning(self, pad_indices):
        for index in pad_indices:
            tune = self.pad_tune[index]
            for layer in KIT.get(self.pad_synths[index], ()):
                total_tune = tune + int(layer.get("tune", 0))
                files = set(layer.get("files", ()))
                for values in layer.get("velocity_files", {}).values():
                    files.update(values)
                for filename in files:
                    sound = self.samples.get(filename)
                    if sound is not None:
                        self.tuned_sound(sound, total_tune)

    def load_custom_samples(self):
        referenced = {
            filename
            for profile in self.kit_slots.values()
            for filename in profile.get("custom_samples", [])
            if filename
        }
        referenced.update(filename for filename in self.custom_sample_files if filename)
        for filename in referenced:
            path = self.custom_sample_path(filename)
            if not path.exists():
                continue
            try:
                self.custom_sound_cache[filename] = pygame.mixer.Sound(str(path))
            except pygame.error as exc:
                self.log(f"Sample skipped: {exc}")

    def resolve_sample_input(self):
        devices = audio_input_devices()
        if not devices:
            return None, "No audio input"
        if self.sample_input_name:
            for index, name, _rate in devices:
                if name == self.sample_input_name:
                    return index, name
        try:
            import sounddevice

            default_input = int(sounddevice.default.device[0])
            for index, name, _rate in devices:
                if index == default_input:
                    self.sample_input_name = name
                    return index, name
        except Exception:
            pass
        self.sample_input_name = devices[0][1]
        return devices[0][0], devices[0][1]

    def cycle_sample_input(self, direction):
        devices = audio_input_devices()
        if not devices:
            self.sample_status = "No audio input"
            return
        names = [device[1] for device in devices]
        try:
            current = names.index(self.sample_input_name)
        except ValueError:
            current = -1
        selected = devices[(current + direction) % len(devices)]
        self.sample_input_name = selected[1]
        self.persist_settings()

    def cycle_sample_start_mode(self):
        current = SAMPLE_START_MODES.index(self.sample_start_mode)
        self.sample_start_mode = SAMPLE_START_MODES[(current + 1) % len(SAMPLE_START_MODES)]
        self.persist_settings()

    def toggle_sample_monitor(self):
        if self.sampler.snapshot()[0]:
            return
        self.sample_monitor_enabled = not self.sample_monitor_enabled
        self.persist_settings()

    def toggle_continuous_sampling(self):
        if self.sampler.snapshot()[0] or self.sample_processing:
            return
        self.sample_continuous_enabled = not self.sample_continuous_enabled
        self.persist_settings()

    def toggle_sampling(self):
        recording, _level, _auto_stop = self.sampler.snapshot()
        if recording:
            self.sample_continuous_active = False
            self.stop_sampling()
        else:
            self.sample_continuous_active = self.sample_continuous_enabled
            self.start_sampling()

    def start_sampling(self):
        if self.sample_processing:
            return
        device_index, device_name = self.resolve_sample_input()
        if device_index is None:
            self.sample_status = device_name
            return
        try:
            self.sample_target_pad = self.selected_pad
            auto_start = self.sample_start_mode == "Auto"
            self.sampler.start(device_index, auto_start=auto_start, monitor=self.sample_monitor_enabled)
            self.sample_was_clipped = False
            self.sample_status = "Waiting for sound" if auto_start else "Recording"
            self.log(f"Sampling: {device_name}")
        except Exception as exc:
            self.sample_continuous_active = False
            self.sample_status = "Input unavailable"
            self.log(f"Sampling failed: {exc}")

    def stop_sampling(self):
        detail = self.sampler.detail_snapshot()
        try:
            audio, samplerate = self.sampler.stop()
        except Exception as exc:
            self.sample_status = "Recording failed"
            self.log(f"Sampling failed: {exc}")
            return
        if not len(audio):
            self.sample_continuous_active = False
            self.sample_status = "No sound captured"
            self.sample_target_pad = None
            return
        self.sample_was_clipped = detail["clipped"]
        target_pad = self.sample_target_pad if self.sample_target_pad is not None else self.selected_pad
        self.sample_processing = True
        self.sample_status = "Processing"
        self.sample_worker = threading.Thread(
            target=self.process_sample_audio,
            args=(audio, samplerate, target_pad),
            name="DrumSampleProcessor",
            daemon=True,
        )
        self.sample_worker.start()

    def process_sample_audio(self, audio, samplerate, target_pad):
        try:
            prepared = prepare_sample_audio(audio, samplerate)
            USER_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
            filename = f"pad-{target_pad + 1:02d}-{timestamp}.wav"
            path = USER_SAMPLE_DIR / filename
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(MIXER_FREQUENCY)
                wav_file.writeframes(prepared.tobytes())
            self.sample_results.put(("OK", target_pad, filename))
        except Exception as exc:
            self.sample_results.put(("ERROR", target_pad, str(exc)))

    def import_sample_file(self, path):
        if self.sample_processing or self.sampler.snapshot()[0]:
            return
        try:
            sound = pygame.mixer.Sound(str(path))
            samples = pygame.sndarray.array(sound).astype("float32") / 32768.0
            target_pad = self.selected_pad
            self.sample_processing = True
            self.sample_status = "Processing"
            self.sample_worker = threading.Thread(
                target=self.process_sample_audio,
                args=(samples, MIXER_FREQUENCY, target_pad),
                name="DrumSampleImport",
                daemon=True,
            )
            self.sample_worker.start()
        except Exception as exc:
            self.sample_status = "Import failed"
            self.log(f"Sample import failed: {exc}")

    def poll_sample_results(self):
        while True:
            try:
                result, target_pad, value = self.sample_results.get_nowait()
            except queue.Empty:
                break
            self.sample_processing = False
            if result == "ERROR":
                self.sample_continuous_active = False
                self.sample_status = "No sample captured"
                self.log(f"Sample failed: {value}")
                continue
            try:
                sound = pygame.mixer.Sound(str(USER_SAMPLE_DIR / value))
                if self.sample_was_clipped:
                    self.pending_clipped_sample = (target_pad, value, sound)
                    self.clip_prompt_open = True
                    self.sample_status = "Level was too high"
                else:
                    self.accept_processed_sample(target_pad, value, sound)
            except pygame.error as exc:
                self.sample_continuous_active = False
                self.sample_status = "Sample load failed"
                self.log(f"Sample load failed: {exc}")

    def accept_processed_sample(self, target_pad, filename, sound):
        self.push_project_history()
        self.custom_sound_cache[filename] = sound
        self.custom_sample_files[target_pad] = filename
        self.sample_edits[target_pad] = default_sample_edit()
        self.edited_sound_cache.clear()
        self.waveform_cache.pop(filename, None)
        self.sample_status = "Sample ready"
        self.persist_settings()
        self.log(f"Sample assigned to pad {target_pad + 1}")
        if not self.sample_continuous_active:
            return
        next_pad = next(
            (index for index in range(target_pad + 1, len(PADS)) if not self.custom_sample_files[index]),
            None,
        )
        if next_pad is None:
            self.sample_continuous_active = False
            self.sample_status = "All following pads are filled"
            return
        self.selected_pad = next_pad
        self.pad_selection = {next_pad}
        self.start_sampling()

    def resolve_clipped_sample(self, keep):
        pending = self.pending_clipped_sample
        if pending is None:
            return False
        target_pad, filename, sound = pending
        self.pending_clipped_sample = None
        self.clip_prompt_open = False
        self.sample_was_clipped = False
        if keep:
            self.accept_processed_sample(target_pad, filename, sound)
        else:
            try: (USER_SAMPLE_DIR / filename).unlink(missing_ok=True)
            except OSError: pass
            self.selected_pad = target_pad
            self.pad_selection = {target_pad}
            self.start_sampling()
        return True

    def clear_custom_sample(self):
        if self.custom_sample_files[self.selected_pad]:
            self.push_project_history()
            self.custom_sample_files[self.selected_pad] = None
            self.sample_edits[self.selected_pad] = default_sample_edit()
            self.edited_sound_cache.clear()
            self.sample_status = "Sample cleared"
            self.persist_settings()

    @staticmethod
    def browser_sound_type(synth):
        if synth == "kick": return "Kick"
        if synth.startswith("snare"): return "Snare"
        if "hat" in synth: return "Hat"
        if synth.startswith(("ride", "crash")): return "Cymbal"
        if "tom" in synth: return "Toms"
        return "Perc"

    def sample_browser_candidates(self):
        candidates = [
            {"id": f"synth:{synth}", "label": SYNTH_LABELS[synth], "source": "Built-in", "type": self.browser_sound_type(synth), "value": synth, "missing": False}
            for synth in KIT_ORDER
            if synth != "hat_semi"
        ]
        referenced = {
            filename
            for profile in self.kit_slots.values()
            for filename in profile.get("custom_samples", [])
            if filename
        }
        if USER_SAMPLE_DIR.exists():
            referenced.update(path.name for path in USER_SAMPLE_DIR.glob("*.wav"))
        for filename in sorted(referenced):
            candidates.append({
                "id": f"file:{filename}", "label": Path(filename).stem[:34],
                "source": "User", "type": "Samples", "value": filename,
                "missing": not self.custom_sample_path(filename).exists(),
            })
        query = self.browser_query.casefold().strip()
        candidates = [candidate for candidate in candidates if not query or query in candidate["label"].casefold()]
        if self.browser_type != "All":
            candidates = [candidate for candidate in candidates if candidate["type"] == self.browser_type]
        if self.browser_source != "All":
            candidates = [candidate for candidate in candidates if candidate["source"] == self.browser_source]
        if self.browser_kit != "All Kits":
            slot = self.browser_kit.removeprefix("Kit ")
            profile = self.kit_slots.get(slot, self.default_kit_profile())
            allowed = {f"synth:{value}" for value in profile.get("pad_synths", [])}
            allowed.update(f"file:{value}" for value in profile.get("custom_samples", []) if value)
            candidates = [candidate for candidate in candidates if candidate["id"] in allowed]
        if self.browser_view == "Favorites":
            candidates = [candidate for candidate in candidates if candidate["id"] in self.sample_favorites]
        elif self.browser_view == "Recent":
            lookup = {candidate["id"]: candidate for candidate in candidates}
            candidates = [lookup[value] for value in self.recent_samples if value in lookup]
        return candidates

    def remember_sample_candidate(self, candidate_id):
        self.recent_samples = [candidate_id] + [value for value in self.recent_samples if value != candidate_id]
        del self.recent_samples[32:]

    def preview_sample_candidate(self, candidate_id):
        candidate = next((value for value in self.sample_browser_candidates() if value["id"] == candidate_id), None)
        if candidate is None or candidate["missing"]:
            self.sample_status = "Sample is missing"
            return False
        index = self.selected_pad
        original = (self.pad_synths[index], self.custom_sample_files[index], self.sample_edits[index])
        if candidate["source"] == "Built-in":
            self.pad_synths[index] = candidate["value"]
            self.custom_sample_files[index] = None
        else:
            filename = candidate["value"]
            if filename not in self.custom_sound_cache:
                self.custom_sound_cache[filename] = pygame.mixer.Sound(str(self.custom_sample_path(filename)))
            self.custom_sample_files[index] = filename
            self.sample_edits[index] = default_sample_edit()
        self.play_pad(index, 100, "Preview")
        self.pad_synths[index], self.custom_sample_files[index], self.sample_edits[index] = original
        self.browser_selected = candidate_id
        self.remember_sample_candidate(candidate_id)
        self.persist_settings_async()
        return True

    def assign_sample_candidate(self, candidate_id):
        candidate = next((value for value in self.sample_browser_candidates() if value["id"] == candidate_id), None)
        if candidate is None or candidate["missing"]:
            return False
        self.push_project_history()
        index = self.selected_pad
        if candidate["source"] == "Built-in":
            self.pad_synths[index] = candidate["value"]
            self.custom_sample_files[index] = None
            self.sample_edits[index] = default_sample_edit()
        else:
            filename = candidate["value"]
            if filename not in self.custom_sound_cache:
                self.custom_sound_cache[filename] = pygame.mixer.Sound(str(self.custom_sample_path(filename)))
            self.custom_sample_files[index] = filename
            self.sample_edits[index] = default_sample_edit()
        self.edited_sound_cache.clear()
        self.remember_sample_candidate(candidate_id)
        self.persist_settings()
        self.browser_open = False
        self.sample_status = "Sound assigned"
        return True

    def toggle_sample_favorite(self, candidate_id):
        if candidate_id in self.sample_favorites:
            self.sample_favorites.remove(candidate_id)
        else:
            self.sample_favorites.append(candidate_id)
        self.persist_settings_async()

    def choose_relink_folder(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            value = filedialog.askdirectory(title="Relink missing samples")
            root.destroy()
            return Path(value) if value else None
        except Exception:
            return None

    def relink_missing_samples(self, folder=None):
        root = Path(folder) if folder else self.choose_relink_folder()
        if root is None or not root.exists():
            return 0
        referenced = {
            filename for profile in self.kit_slots.values()
            for filename in profile.get("custom_samples", []) if filename
        }
        missing = {name for name in referenced if not self.custom_sample_path(name).exists()}
        wanted = {name.casefold(): name for name in missing}
        found = {}
        for path in root.rglob("*"):
            if path.is_file() and path.name.casefold() in wanted and path.name.casefold() not in found:
                found[path.name.casefold()] = path
                if len(found) == len(wanted): break
        USER_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        for folded, source in found.items():
            shutil.copy2(source, USER_SAMPLE_DIR / wanted[folded])
        self.load_custom_samples()
        self.persist_settings()
        self.sample_status = f"Relinked {len(found)} samples"
        return len(found)

    def adjust_sample_edit(self, field, amount=None):
        if not self.custom_sample_files[self.selected_pad]:
            return False
        self.push_project_history()
        edit = dict(self.sample_edits[self.selected_pad])
        if field == "start":
            edit[field] = max(0.0, min(edit["end"] - 0.01, edit[field] + amount))
        elif field == "end":
            edit[field] = min(1.0, max(edit["start"] + 0.01, edit[field] + amount))
        elif field in ("attack_ms", "release_ms"):
            edit[field] += int(amount)
        elif field == "tune":
            edit[field] += int(amount)
        elif field in ("normalize", "reverse"):
            edit[field] = not edit[field]
        elif field == "mode":
            current = SAMPLE_PLAY_MODES.index(edit["mode"])
            edit["mode"] = SAMPLE_PLAY_MODES[(current + int(amount or 1)) % len(SAMPLE_PLAY_MODES)]
        else:
            self.project_history.pop()
            return False
        self.sample_edits[self.selected_pad] = sanitize_sample_edit(edit)
        self.edited_sound_cache.clear()
        self.persist_settings_async()
        return True

    def reset_sample_edit(self):
        if not self.custom_sample_files[self.selected_pad]:
            return False
        self.push_project_history()
        self.sample_edits[self.selected_pad] = default_sample_edit()
        self.edited_sound_cache.clear()
        self.persist_settings_async()
        return True

    def edited_custom_sound(self, index, bypass=False):
        filename = self.custom_sample_files[index]
        source = self.custom_sound_cache.get(filename) if filename else None
        if source is None or bypass:
            return source
        edit = self.sample_edits[index]
        key = (filename, json.dumps(edit, sort_keys=True), self.bpm)
        cached = self.edited_sound_cache.get(key)
        if cached is not None:
            return cached
        try:
            samples = pygame.sndarray.array(source)
            edited = apply_sample_edits(samples, edit)
            edited = apply_sample_tempo(edited, edit, self.bpm)
            sound = pygame.sndarray.make_sound(edited)
        except (pygame.error, TypeError, ValueError):
            return source
        self.edited_sound_cache[key] = sound
        return sound

    def current_sample_array(self):
        filename = self.custom_sample_files[self.selected_pad]
        sound = self.custom_sound_cache.get(filename) if filename else None
        if sound is None:
            return None
        edit = self.sample_edits[self.selected_pad]
        return apply_sample_tempo(apply_sample_edits(pygame.sndarray.array(sound), edit), edit, self.bpm)

    def detect_selected_sample_tempo(self):
        filename = self.custom_sample_files[self.selected_pad]
        sound = self.custom_sound_cache.get(filename) if filename else None
        if sound is None:
            return False
        result = detect_sample_tempo(pygame.sndarray.array(sound))
        if result is None:
            self.sample_status = "Tempo not detected"
            return False
        self.push_project_history()
        bpm, bars, _confidence = result
        edit = dict(self.sample_edits[self.selected_pad])
        edit.update({"source_bpm": bpm, "source_bars": bars, "stretch_mode": "Stretch"})
        self.sample_edits[self.selected_pad] = sanitize_sample_edit(edit)
        self.edited_sound_cache.clear()
        self.persist_settings_async()
        self.sample_status = f"Fits {bpm:g} BPM"
        return True

    def adjust_sample_source_bpm(self, multiplier):
        edit = self.sample_edits[self.selected_pad]
        if not edit.get("source_bpm"):
            return False
        self.push_project_history()
        updated = dict(edit)
        updated["source_bpm"] = edit["source_bpm"] * multiplier
        self.sample_edits[self.selected_pad] = sanitize_sample_edit(updated)
        self.edited_sound_cache.clear()
        self.persist_settings_async()
        return True

    def cycle_sample_stretch_mode(self):
        edit = self.sample_edits[self.selected_pad]
        if not edit.get("source_bpm"):
            return False
        self.push_project_history()
        modes = ("Off", "Repitch", "Stretch")
        updated = dict(edit)
        updated["stretch_mode"] = modes[(modes.index(edit["stretch_mode"]) + 1) % len(modes)]
        self.sample_edits[self.selected_pad] = sanitize_sample_edit(updated)
        self.edited_sound_cache.clear()
        self.persist_settings_async()
        return True

    def current_chop_markers(self, samples):
        if self.chop_mode == "Equal":
            return equal_slice_markers(len(samples), self.chop_count)
        if self.chop_mode == "Transient":
            return transient_slice_markers(samples, self.chop_count)
        ratios = sorted(set(value for value in self.chop_markers if 0.01 <= value <= 0.99))
        return [0] + [round(len(samples) * value) for value in ratios] + [len(samples)]

    def start_lazy_chop(self):
        samples = self.current_sample_array()
        if samples is None:
            return False
        self.chop_mode = "Lazy"
        self.chop_markers = []
        self.chop_lazy_active = True
        self.chop_lazy_started_at = time.perf_counter()
        self.queue_pad(self.selected_pad, 100)
        self.sample_status = "Tap pads for slices"
        return True

    def add_lazy_chop_marker(self):
        if not self.chop_lazy_active or self.chop_lazy_started_at is None:
            return False
        sound = self.edited_custom_sound(self.selected_pad)
        duration = sound.get_length() if sound is not None else 0.0
        if duration <= 0:
            return False
        ratio = (time.perf_counter() - self.chop_lazy_started_at) / duration
        if ratio >= 1.0:
            self.chop_lazy_active = False
            return False
        if ratio >= 0.01 and all(abs(ratio - value) >= 0.01 for value in self.chop_markers):
            self.chop_markers.append(ratio)
            self.chop_markers.sort()
        return True

    def execute_chop(self):
        samples = self.current_sample_array()
        if samples is None:
            self.sample_status = "No sample to chop"
            return False
        markers = self.current_chop_markers(samples)
        slices = slice_sample_audio(samples, markers, self.chop_play_through)
        start = self.selected_pad + (1 if self.chop_keep_original else 0)
        targets = [] if self.chop_keep_original else [self.selected_pad]
        targets.extend(
            index for index in range(start, len(PADS))
            if index != self.selected_pad and not self.custom_sample_files[index]
        )
        targets = targets[:len(slices)]
        if len(targets) < len(slices):
            self.sample_status = f"Need {len(slices) - len(targets)} more empty pads"
            return False
        self.push_project_history()
        USER_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        choke_group = f"chop-{timestamp}" if self.chop_choke else None
        for slice_index, (target, audio) in enumerate(zip(targets, slices), 1):
            filename = f"chop-{target + 1:02d}-{slice_index:02d}-{timestamp}.wav"
            path = USER_SAMPLE_DIR / filename
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(MIXER_FREQUENCY)
                wav_file.writeframes(audio.astype("int16").tobytes())
            self.custom_sample_files[target] = filename
            self.custom_sound_cache[filename] = pygame.mixer.Sound(str(path))
            edit = default_sample_edit()
            edit["choke_group"] = choke_group
            self.sample_edits[target] = edit
        self.edited_sound_cache.clear()
        self.chop_lazy_active = False
        self.chop_open = False
        self.sample_editor_open = False
        self.selected_pad = targets[0]
        self.pad_selection = {targets[0]}
        self.persist_settings()
        self.sample_status = f"Chopped to {len(slices)} pads"
        return True

    def wait_for_sample_worker(self):
        sample_worker = self.sample_worker
        if sample_worker and sample_worker.is_alive():
            sample_worker.join(timeout=10.0)

    def midi_devices(self):
        return [(device_id, name, 0) for device_id, name in MidiInput.devices()]

    def open_preferred_midi(self):
        devices = self.midi_devices()
        if not devices:
            self.status = "No MIDI input"
            self.log(self.status)
            return False

        # CoreMIDI splits one USB device into several endpoints, and the vendor
        # "-Private" port carries editor traffic rather than pad hits.
        playable = [d for d in devices if "PRIVATE" not in d[1].upper()]
        exact = [d for d in playable if d[1] == "STARRYPAD MINI"]
        contains = [d for d in playable if "STARRYPAD" in d[1].upper()]
        ordered = []
        for group in (exact, contains, playable, devices):
            for device in group:
                if device[0] not in [item[0] for item in ordered]:
                    ordered.append(device)

        last_error = None
        for device_id, _name, _opened in ordered:
            if self.open_midi(device_id):
                return True
            last_error = self.status

        self.status = last_error or "MIDI open failed"
        self.log(self.status)
        return False

    def open_midi(self, device_id):
        self.close_midi()
        try:
            self.midi_input = MidiInput(device_id, self.audio_events)
            self.midi_device_id = device_id
            self.midi_opened_at = time.perf_counter()
            self.last_midi_event_ns = 0
            self.midi_device_name = dict(MidiInput.devices()).get(device_id, f"Input {device_id}")
            self.preferred_midi_name = self.midi_device_name
            self.midi_disconnect_notified = False
            self.status = f"MIDI: {self.midi_device_name}"
            self.log(self.status)
            if not self.calibration_prompted and not any(value["enabled"] for value in self.pad_calibrations):
                self.calibration_prompted = True
                self.status = "MIDI ready - calibrate pads in Settings"
                self.set_surface_notice("Calibrate pads in Settings", 4.0)
                self.persist_settings()
            return True
        except Exception as exc:
            self.midi_input = None
            self.midi_device_id = None
            self.status = f"MIDI open failed: {exc}"
            self.log(self.status)
            return False

    def close_midi(self):
        if self.midi_input:
            try:
                self.midi_input.close()
            except Exception:
                pass
        self.midi_input = None
        self.midi_device_id = None

    def open_preferred_midi_output(self):
        self.close_midi_output()
        if not self.clock_output_enabled:
            return False
        devices = MidiOutput.devices()
        if not devices:
            self.status = "No MIDI output"
            return False
        playable = [device for device in devices if "PRIVATE" not in device[1].upper()]
        selected = next(
            (device for device in devices if device[1] == self.midi_output_name),
            (playable or devices)[0],
        )
        try:
            self.midi_output = MidiOutput(selected[0])
            self.midi_output_name = selected[1]
            return True
        except Exception as exc:
            self.midi_output = None
            self.log(f"MIDI output failed: {exc}")
            return False

    def close_midi_output(self):
        if self.midi_output:
            try: self.midi_output.close()
            except Exception: pass
        self.midi_output = None

    def cycle_midi_output(self):
        devices = MidiOutput.devices()
        if not devices:
            self.midi_output_name = None
            self.clock_output_enabled = False
            self.close_midi_output()
            return
        names = [name for _device_id, name in devices]
        try: current = names.index(self.midi_output_name)
        except ValueError: current = -1
        self.midi_output_name = names[(current + 1) % len(names)]
        self.open_preferred_midi_output()
        self.persist_settings()

    def toggle_clock_output(self):
        self.clock_output_enabled = not self.clock_output_enabled
        if self.clock_output_enabled: self.open_preferred_midi_output()
        else: self.close_midi_output()
        self.persist_settings()

    def maintain_midi_connection(self, now=None):
        now = time.perf_counter() if now is None else float(now)
        if now < self.next_midi_health_check_at:
            return
        self.next_midi_health_check_at = now + MIDI_HEALTH_CHECK_SECONDS

        devices = self.midi_devices()
        available_ids = {device_id for device_id, _name, _opened in devices}
        if self.midi_input is not None and self.midi_device_id in available_ids:
            return

        if self.midi_input is not None:
            self.close_midi()
            self.audio_events.put(("PANIC",))

        if not self.midi_disconnect_notified:
            self.status = "MIDI disconnected"
            self.log(self.status)
            self.midi_disconnect_notified = True

        preferred = self.preferred_midi_name.casefold()
        reconnect_candidates = [
            device
            for device in devices
            if device[1].casefold() == preferred
        ]
        if "starrypad" in preferred:
            reconnect_candidates.extend(
                device
                for device in devices
                if "starrypad" in device[1].casefold()
                and device not in reconnect_candidates
            )

        for device_id, _name, _opened in reconnect_candidates:
            if self.open_midi(device_id):
                self.log("MIDI reconnected")
                return

    def next_midi_device(self):
        devices = self.midi_devices()
        if not devices:
            self.status = "No MIDI input"
            return
        ids = [device[0] for device in devices]
        if self.midi_device_id not in ids:
            self.open_midi(ids[0])
            return
        next_index = (ids.index(self.midi_device_id) + 1) % len(ids)
        for offset in range(len(ids)):
            device_id = ids[(next_index + offset) % len(ids)]
            if self.open_midi(device_id):
                return

    def reset_mapping(self):
        with self.mapping_lock:
            self.assignments.clear()
            self.apply_mapping_mode()
        self.log("Map reset")

    def cycle_mapping_mode(self):
        with self.mapping_lock:
            self.mapping_mode = (self.mapping_mode + 1) % len(MAPPING_MODES)
            self.assignments.clear()
            self.apply_mapping_mode()
        self.persist_settings()
        self.log(f"Preset: {MAPPING_MODES[self.mapping_mode]}")

    def apply_mapping_mode(self):
        mode = MAPPING_MODES[self.mapping_mode]
        if mode == "DONNER Mini":
            self.pad_notes = [f"N{20 + index}/N{36 + index}" for index in range(len(PADS))]
        elif mode == "GM Drums":
            labels = [""] * len(PADS)
            for note, index in sorted(GM_NOTE_TO_PAD.items()):
                if not labels[index]:
                    labels[index] = f"N{note}"
            self.pad_notes = [label or "--" for label in labels]
        else:
            self.pad_notes = [None] * len(PADS)

    def main_loop(self):
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    running = self.handle_key(event.key, event.unicode)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.handle_mouse(self.window_to_logical(event.pos), pygame.key.get_mods())
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    self.finish_pad_drag(self.window_to_logical(event.pos))
                elif event.type == pygame.MOUSEMOTION and event.buttons[0] and self.perform_fx_open:
                    self.mouse_logical = self.window_to_logical(event.pos)
                    self.handle_perform_fx_drag(self.mouse_logical)
                elif event.type == pygame.MOUSEMOTION and event.buttons[0] and self.pad_drag_from is not None:
                    self.mouse_logical = self.window_to_logical(event.pos)
                    self.update_pad_drag(self.mouse_logical)
                elif event.type == pygame.MOUSEMOTION:
                    self.mouse_logical = self.window_to_logical(event.pos)
                elif event.type == pygame.VIDEORESIZE:
                    self.display_surface = pygame.display.set_mode(event.size, pygame.RESIZABLE)
                    self.update_display_viewport(event.size)
                elif event.type == pygame.DROPFILE:
                    self.import_sample_file(event.file)

            sample_detail = self.sampler.detail_snapshot()
            if sample_detail["active"]:
                if sample_detail["clipped"]:
                    self.sample_status = "Recording - level high"
                elif sample_detail["triggered"]:
                    self.sample_status = "Recording"
                else:
                    self.sample_status = "Waiting for sound"
            if sample_detail["active"] and sample_detail["auto_stop"]:
                self.stop_sampling()
            health_now = time.perf_counter()
            if self.last_ui_heartbeat is not None and health_now - self.last_ui_heartbeat >= 8.0:
                self.handle_system_resume(health_now)
            self.last_ui_heartbeat = health_now
            self.maintain_midi_connection(health_now)
            self.maintain_audio_connection(health_now)
            self.update_audio_test(health_now)
            self.poll_sample_results()
            self.poll_bounce_results()
            self.draw()
            self.clock.tick(UI_FPS)

    def handle_key(self, key, text=""):
        modifiers = pygame.key.get_mods()
        if modifiers & COMMAND_MODIFIER:
            if key == pygame.K_q:
                return False
            if key in PAD_MOVE_DELTAS and self.view_mode == "Perform":
                self.move_selected_pad(PAD_MOVE_DELTAS[key])
                return True
            if key == pygame.K_z:
                self.redo_project_edit() if modifiers & pygame.KMOD_SHIFT else self.undo_project_edit()
            elif key == pygame.K_y:
                self.redo_project_edit()
            elif key == pygame.K_s:
                self.persist_settings()
                self.set_surface_notice("Project saved")
            elif key == pygame.K_o:
                self.open_project()
            elif key == pygame.K_n:
                self.new_project()
            elif key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                scales = (1.0, 1.25, 1.5)
                self.set_ui_scale(scales[min(len(scales) - 1, scales.index(self.ui_scale) + 1)])
            elif key in (pygame.K_MINUS, pygame.K_UNDERSCORE, pygame.K_KP_MINUS):
                scales = (1.0, 1.25, 1.5)
                self.set_ui_scale(scales[max(0, scales.index(self.ui_scale) - 1)])
            elif key == pygame.K_0:
                self.set_ui_scale(1.0)
            return True
        if self.browser_open and key != pygame.K_ESCAPE:
            if key == pygame.K_BACKSPACE:
                self.browser_query = self.browser_query[:-1]
            elif text and text.isprintable() and len(self.browser_query) < 32:
                self.browser_query += text
            self.browser_page = 0
            return True
        if key == pygame.K_ESCAPE:
            if self.clip_prompt_open:
                return True
            if self.browser_open:
                self.browser_open = False
                return True
            if self.chop_open:
                self.chop_open = False
                self.chop_lazy_active = False
                return True
            if self.sample_editor_open:
                self.sample_editor_open = False
                self.sample_edit_bypass = False
                return True
            if self.share_open:
                self.share_open = False
                return True
            if self.perform_fx_open:
                self.perform_fx_open = False
                return True
            if self.mixer_open:
                self.mixer_open = False
                self.mixer_bypass = False
                return True
            if self.scene_open:
                self.scene_open = False
                return True
            if self.feel_open:
                self.feel_open = False
                return True
            if self.project_menu_open:
                self.project_menu_open = False
                return True
            if self.settings_open:
                if self.audio_setup_open:
                    self.audio_setup_open = False
                    return True
                if self.sync_setup_open:
                    self.sync_setup_open = False
                    return True
                self.settings_open = False
                return True
            self.status = f"Press {QUIT_SHORTCUT} to quit"
            return True
        if key == pygame.K_TAB:
            controls = self.focusable_controls()
            if controls:
                names = [name for name, _rect in controls]
                direction = -1 if modifiers & pygame.KMOD_SHIFT else 1
                current = names.index(self.keyboard_focus_name) if self.keyboard_focus_name in names else (-1 if direction > 0 else 0)
                self.keyboard_focus_name = names[(current + direction) % len(names)]
            return True
        if key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN) and self.view_mode == "Perform":
            # Pad 0 is bottom left, so a higher index is higher on screen.
            delta = {pygame.K_LEFT: -1, pygame.K_RIGHT: 1, pygame.K_UP: 4, pygame.K_DOWN: -4}[key]
            self.selected_pad = (self.selected_pad + delta) % len(PADS)
            self.pad_selection = {self.selected_pad}
            return True
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.keyboard_focus_name:
            control = dict(self.focusable_controls()).get(self.keyboard_focus_name)
            if control:
                self.handle_mouse(control.center, modifiers)
                return True
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.view_mode == "Perform":
            self.queue_pad(self.selected_pad, 112)
            return True
        if key == pygame.K_r:
            self.reset_mapping()
        elif key in (pygame.K_PLUS, pygame.K_EQUALS):
            self.volume = min(1.0, self.volume + 0.04)
            self.persist_settings()
        elif key in (pygame.K_MINUS, pygame.K_UNDERSCORE):
            self.volume = max(0.0, self.volume - 0.04)
            self.persist_settings()
        elif key == pygame.K_d:
            self.next_midi_device()
        elif key == pygame.K_p:
            self.cycle_mapping_mode()
        elif key == pygame.K_n:
            self.toggle_repeat()
        elif key == pygame.K_m:
            self.toggle_metronome()
        elif key == pygame.K_SPACE:
            self.request_loop_command("PLAY")
        elif key == pygame.K_l:
            self.request_loop_command("RECORD")
        elif key == pygame.K_o:
            self.request_loop_command("OVERDUB")
        elif key == pygame.K_u:
            self.request_loop_command("UNDO")
        elif key == pygame.K_y:
            self.request_loop_command("REDO")
        elif key == pygame.K_c:
            self.request_loop_command("CAPTURE")
        elif key == pygame.K_q:
            self.request_loop_command("QUANTIZE")
        elif key == pygame.K_s:
            self.toggle_sampling()
        elif pygame.K_1 <= key <= pygame.K_9:
            self.queue_pad(key - pygame.K_1, 112)
        return True

    def handle_mouse(self, pos, modifiers=0):
        if self.clip_prompt_open:
            for name, rect in self.clip_prompt_buttons.items():
                if rect.collidepoint(pos):
                    self.resolve_clipped_sample(name == "clip_keep")
                    return
            return

        if self.browser_open:
            for name, rect in self.browser_buttons.items():
                if not rect.collidepoint(pos): continue
                if name == "browser_close": self.browser_open = False
                elif name == "browser_type":
                    values = ("All", "Kick", "Snare", "Hat", "Cymbal", "Toms", "Perc", "Samples")
                    self.browser_type = values[(values.index(self.browser_type) + 1) % len(values)]; self.browser_page = 0
                elif name == "browser_source":
                    values = ("All", "Built-in", "User")
                    self.browser_source = values[(values.index(self.browser_source) + 1) % len(values)]; self.browser_page = 0
                elif name == "browser_kit":
                    values = ("All Kits", "Kit A", "Kit B", "Kit C", "Kit D")
                    self.browser_kit = values[(values.index(self.browser_kit) + 1) % len(values)]; self.browser_page = 0
                elif name == "browser_view":
                    values = ("All", "Favorites", "Recent")
                    self.browser_view = values[(values.index(self.browser_view) + 1) % len(values)]; self.browser_page = 0
                elif name == "browser_prev": self.browser_page = max(0, self.browser_page - 1)
                elif name == "browser_next": self.browser_page += 1
                elif name == "browser_relink": self.relink_missing_samples()
                elif name == "browser_use" and self.browser_selected: self.assign_sample_candidate(self.browser_selected)
                elif name.startswith("browser_preview_"):
                    self.preview_sample_candidate(self.browser_row_ids[int(name.rsplit("_", 1)[1])])
                elif name.startswith("browser_favorite_"):
                    self.toggle_sample_favorite(self.browser_row_ids[int(name.rsplit("_", 1)[1])])
                return

            return

        if self.chop_open:
            for name, rect in self.chop_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "chop_close":
                    self.chop_open = False
                    self.chop_lazy_active = False
                elif name == "chop_mode":
                    modes = ("Transient", "Equal", "Manual")
                    self.chop_mode = modes[(modes.index(self.chop_mode) + 1) % len(modes)] if self.chop_mode in modes else "Transient"
                    self.chop_markers = []
                elif name == "chop_count":
                    if self.chop_mode in ("Transient", "Equal"):
                        counts = (2, 4, 8, 16)
                        self.chop_count = counts[(counts.index(self.chop_count) + 1) % len(counts)]
                elif name == "chop_keep": self.chop_keep_original = not self.chop_keep_original
                elif name == "chop_through": self.chop_play_through = not self.chop_play_through
                elif name == "chop_choke": self.chop_choke = not self.chop_choke
                elif name == "chop_clear": self.chop_markers = []; self.chop_mode = "Manual"
                elif name == "chop_lazy":
                    if self.chop_lazy_active:
                        self.chop_lazy_active = False
                    else:
                        self.start_lazy_chop()
                elif name == "chop_apply": self.execute_chop()
                return
            if self.chop_wave_rect and self.chop_wave_rect.collidepoint(pos):
                ratio = (pos[0] - self.chop_wave_rect.left) / self.chop_wave_rect.width
                if 0.01 <= ratio <= 0.99:
                    self.chop_mode = "Manual"
                    if all(abs(ratio - value) >= 0.01 for value in self.chop_markers):
                        self.chop_markers.append(ratio)
                        self.chop_markers.sort()
                return
            return

        if self.sample_editor_open:
            for name, rect in self.sample_editor_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "sample_editor_close":
                    self.sample_editor_open = False
                    self.sample_edit_bypass = False
                elif name == "sample_start_down": self.adjust_sample_edit("start", -0.01)
                elif name == "sample_start_up": self.adjust_sample_edit("start", 0.01)
                elif name == "sample_end_down": self.adjust_sample_edit("end", -0.01)
                elif name == "sample_end_up": self.adjust_sample_edit("end", 0.01)
                elif name == "sample_tune_down": self.adjust_sample_edit("tune", -1)
                elif name == "sample_tune_up": self.adjust_sample_edit("tune", 1)
                elif name == "sample_attack_down": self.adjust_sample_edit("attack_ms", -5)
                elif name == "sample_attack_up": self.adjust_sample_edit("attack_ms", 5)
                elif name == "sample_release_down": self.adjust_sample_edit("release_ms", -5)
                elif name == "sample_release_up": self.adjust_sample_edit("release_ms", 5)
                elif name == "sample_normalize": self.adjust_sample_edit("normalize")
                elif name == "sample_reverse": self.adjust_sample_edit("reverse")
                elif name == "sample_mode": self.adjust_sample_edit("mode", 1)
                elif name == "sample_ab": self.sample_edit_bypass = not self.sample_edit_bypass
                elif name == "sample_zoom": self.sample_wave_zoom = not self.sample_wave_zoom
                elif name == "sample_preview": self.queue_pad(self.selected_pad, 100)
                elif name == "sample_reset": self.reset_sample_edit()
                elif name == "sample_undo": self.undo_project_edit()
                elif name == "sample_chop":
                    self.chop_open = True
                    self.chop_markers = []
                elif name == "sample_detect": self.detect_selected_sample_tempo()
                elif name == "sample_half": self.adjust_sample_source_bpm(0.5)
                elif name == "sample_double": self.adjust_sample_source_bpm(2.0)
                elif name == "sample_stretch": self.cycle_sample_stretch_mode()
                return
            return

        if self.share_open:
            for name, rect in self.share_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "share_close":
                    self.share_open = False
                elif name == "share_wav":
                    self.start_loop_export("WAV")
                    self.share_open = False
                elif name == "share_midi":
                    self.start_loop_export("MIDI")
                    self.share_open = False
                elif name == "share_stems":
                    self.start_loop_export("STEMS")
                    self.share_open = False
                elif name == "share_bundle":
                    self.start_loop_export("BUNDLE")
                    self.share_open = False
                return
            return

        if self.perform_fx_open:
            for name, rect in self.perform_fx_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "perform_fx_close":
                    self.perform_fx_open = False
                elif name == "perform_fx_bypass":
                    self.perform_fx_bypass = not self.perform_fx_bypass
                    self.processed_sound_cache.clear()
                elif name == "perform_fx_reset":
                    self.reset_perform_fx()
                elif name == "perform_fx_bounce":
                    self.start_loop_bounce()
                elif name.startswith("perform_fx_track_"):
                    self.set_perform_fx_from_position(name.removeprefix("perform_fx_track_"), pos[0], rect)
                elif name.endswith("_down"):
                    self.adjust_perform_fx(name.removeprefix("perform_fx_").removesuffix("_down"), -10)
                elif name.endswith("_up"):
                    self.adjust_perform_fx(name.removeprefix("perform_fx_").removesuffix("_up"), 10)
                return
            return

        if self.mixer_open:
            for name, rect in self.mixer_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "mixer_close":
                    self.mixer_open = False
                    self.mixer_bypass = False
                elif name == "mixer_mix_tab":
                    self.mixer_fx_view = False
                elif name == "mixer_fx_tab":
                    self.mixer_fx_view = True
                elif name == "mixer_volume_down":
                    self.adjust_pad_mix("volume", -0.05)
                elif name == "mixer_volume_up":
                    self.adjust_pad_mix("volume", 0.05)
                elif name == "mixer_pan_down":
                    self.adjust_pad_mix("pan", -0.1)
                elif name == "mixer_pan_up":
                    self.adjust_pad_mix("pan", 0.1)
                elif name == "mixer_tune_down":
                    self.adjust_pad_mix("tune", -1)
                elif name == "mixer_tune_up":
                    self.adjust_pad_mix("tune", 1)
                elif name == "mixer_mute":
                    self.toggle_pad_mute()
                elif name == "mixer_solo":
                    self.toggle_pad_solo()
                elif name == "mixer_bypass":
                    self.mixer_bypass = not self.mixer_bypass
                elif name == "mixer_reset":
                    self.reset_pad_fx() if self.mixer_fx_view else self.reset_pad_mix()
                elif name.endswith("_down") and name.startswith("mixer_"):
                    self.adjust_pad_fx(name.removeprefix("mixer_").removesuffix("_down"), -10)
                elif name.endswith("_up") and name.startswith("mixer_"):
                    self.adjust_pad_fx(name.removeprefix("mixer_").removesuffix("_up"), 10)
                elif name == "mixer_bus":
                    self.cycle_pad_bus()
                elif name == "mixer_undo":
                    self.undo_project_edit()
                elif name == "mixer_all":
                    self.pad_selection = set(range(len(PADS)))
                return
            return

        if self.scene_open:
            for name, rect in self.scene_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "scene_close":
                    self.scene_open = False
                elif name.startswith("scene_add_"):
                    self.add_scene_pattern(int(name.rsplit("_", 1)[1]))
                elif name == "scene_remove":
                    self.remove_scene_step()
                elif name == "scene_play":
                    self.toggle_song_playback()
                return
            return

        if self.feel_open:
            for name, rect in self.feel_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "feel_close":
                    self.feel_open = False
                elif name.startswith("feel_preset_"):
                    self.set_feel_preset(name.removeprefix("feel_preset_").title())
                elif name == "feel_reset":
                    self.reset_loop_feel()
                elif name == "feel_advanced":
                    self.feel_advanced = not self.feel_advanced
                elif name == "feel_grid":
                    self.cycle_feel_grid()
                elif name.endswith("_down"):
                    field = name.removeprefix("feel_").removesuffix("_down")
                    self.adjust_feel(field, -5 if field in ("strength", "swing") else -1)
                elif name.endswith("_up"):
                    field = name.removeprefix("feel_").removesuffix("_up")
                    self.adjust_feel(field, 5 if field in ("strength", "swing") else 1)
                return
            return

        if self.project_menu_open:
            for name, rect in self.project_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "project_close":
                    self.project_menu_open = False
                elif name == "project_new":
                    self.new_project()
                    self.project_menu_open = False
                elif name == "project_open":
                    self.open_project()
                    self.project_menu_open = False
                elif name == "project_save_as":
                    self.save_project_as()
                    self.project_menu_open = False
                elif name == "project_collect":
                    self.collect_project_samples()
                elif name == "project_undo":
                    self.undo_project_edit()
                elif name == "project_redo":
                    self.redo_project_edit()
                elif name.startswith("recent_"):
                    self.open_project(self.recent_projects[int(name.split("_")[1])])
                    self.project_menu_open = False
                return
            return

        if self.settings_open:
            for name, rect in self.settings_buttons.items():
                if not rect.collidepoint(pos):
                    continue
                if name == "settings_close":
                    self.settings_open = False
                    self.audio_setup_open = False
                    self.sync_setup_open = False
                elif name == "device":
                    self.next_midi_device()
                elif name == "preset":
                    self.cycle_mapping_mode()
                elif name == "reset":
                    self.reset_mapping()
                elif name == "input_prev":
                    self.cycle_sample_input(-1)
                elif name == "input_next":
                    self.cycle_sample_input(1)
                elif name == "sample_start":
                    self.cycle_sample_start_mode()
                elif name == "sample_monitor":
                    self.toggle_sample_monitor()
                elif name == "sample_continuous":
                    self.toggle_continuous_sampling()
                elif name == "record_start":
                    self.cycle_record_start_mode()
                elif name == "calibrate":
                    self.start_pad_calibration()
                elif name == "audio_setup":
                    self.audio_setup_open = True
                elif name == "sync_setup":
                    self.sync_setup_open = True
                elif name == "ui_scale":
                    scales = (1.0, 1.25, 1.5)
                    self.set_ui_scale(scales[(scales.index(self.ui_scale) + 1) % len(scales)])
                elif name.startswith("accent_"):
                    self.set_accent(name.removeprefix("accent_"))
                elif name == "sync_back":
                    self.sync_setup_open = False
                elif name.startswith("sync_source_"):
                    self.clock_source = name.removeprefix("sync_source_").capitalize()
                    if self.clock_source == "Internal": self.clock_active_source = "Internal"
                    self.persist_settings()
                elif name == "sync_output": self.toggle_clock_output()
                elif name == "sync_port": self.cycle_midi_output()
                elif name == "sync_correction_down":
                    self.clock_correction_ms = max(-100, self.clock_correction_ms - 1); self.persist_settings()
                elif name == "sync_correction_up":
                    self.clock_correction_ms = min(100, self.clock_correction_ms + 1); self.persist_settings()
                elif name == "audio_back":
                    self.audio_setup_open = False
                elif name == "audio_output_prev":
                    self.cycle_audio_output(-1)
                elif name == "audio_output_next":
                    self.cycle_audio_output(1)
                elif name == "audio_low":
                    self.set_audio_mode("Low latency")
                elif name == "audio_stable":
                    self.set_audio_mode("Stable")
                elif name == "audio_advanced":
                    self.audio_advanced = not self.audio_advanced
                elif name == "audio_test":
                    self.start_audio_test()
                elif name == "audio_rate":
                    self.cycle_audio_rate()
                elif name == "audio_buffer":
                    self.cycle_audio_buffer()
                elif name == "calibration_cancel":
                    self.cancel_pad_calibration()
                elif name == "calibration_reset":
                    self.reset_pad_calibration()
                return
            return

        for name, rect in self.buttons.items():
            if rect.collidepoint(pos):
                if name == "view_perform":
                    self.view_mode = "Perform"
                elif name == "view_sequence":
                    self.view_mode = "Sequence"
                elif name == "sequence_play":
                    self.request_loop_command("PLAY")
                elif name == "sequence_undo":
                    self.request_loop_command("UNDO")
                elif name == "sequence_redo":
                    self.request_loop_command("REDO")
                elif name == "sequence_clear":
                    self.request_loop_command("CLEAR")
                elif name == "sequence_bars":
                    self.request_loop_command("BARS")
                    self.sequence_bar_page = min(self.sequence_bar_page, self.loop_bars - 1)
                elif name == "sequence_page_prev":
                    self.sequence_bar_page = (self.sequence_bar_page - 1) % self.loop_bars
                elif name == "sequence_page_next":
                    self.sequence_bar_page = (self.sequence_bar_page + 1) % self.loop_bars
                elif name == "sequence_velocity_down":
                    self.adjust_sequence_velocity(-5)
                elif name == "sequence_velocity_up":
                    self.adjust_sequence_velocity(5)
                elif name == "sequence_nudge_left":
                    self.nudge_sequence_event(-5)
                elif name == "sequence_nudge_right":
                    self.nudge_sequence_event(5)
                elif name == "sequence_copy":
                    self.copy_sequence_event()
                elif name == "sequence_chance_down":
                    self.adjust_sequence_meta("chance", -5)
                elif name == "sequence_chance_up":
                    self.adjust_sequence_meta("chance", 5)
                elif name == "sequence_ratchet_down":
                    self.adjust_sequence_meta("ratchet", -1)
                elif name == "sequence_ratchet_up":
                    self.adjust_sequence_meta("ratchet", 1)
                elif name.startswith("pattern_") and name.removeprefix("pattern_").isdigit():
                    self.request_pattern(int(name.removeprefix("pattern_")))
                elif name == "pattern_duplicate":
                    self.duplicate_pattern()
                elif name == "pattern_double":
                    self.double_pattern()
                elif name == "pattern_launch":
                    self.cycle_pattern_launch_mode()
                elif name == "pattern_scenes":
                    self.scene_open = True
                elif name == "sequence_step_input":
                    self.sequence_step_input = not self.sequence_step_input
                elif name == "sequence_cursor_prev":
                    self.sequence_step_cursor = (self.sequence_step_cursor - 1) % 16
                elif name == "sequence_cursor_next":
                    self.sequence_step_cursor = (self.sequence_step_cursor + 1) % 16
                elif name == "reset":
                    self.reset_mapping()
                elif name == "preset":
                    self.cycle_mapping_mode()
                elif name == "device":
                    self.next_midi_device()
                elif name == "kit":
                    self.switch_kit()
                elif name == "settings":
                    self.settings_open = True
                elif name == "reconnect":
                    self.force_reconnect()
                elif name == "mixer":
                    self.mixer_open = True
                elif name == "project":
                    self.project_menu_open = True
                elif name == "vol_down":
                    self.volume = max(0.0, self.volume - 0.04)
                    self.persist_settings()
                elif name == "vol_up":
                    self.volume = min(1.0, self.volume + 0.04)
                    self.persist_settings()
                elif name == "sound_prev":
                    self.cycle_pad_sound(-1)
                elif name == "sound_next":
                    self.cycle_pad_sound(1)
                elif name == "sens_down":
                    self.adjust_pad_sensitivity(-0.05)
                elif name == "sens_up":
                    self.adjust_pad_sensitivity(0.05)
                elif name == "sample":
                    self.toggle_sampling()
                elif name == "sample_clear":
                    self.clear_custom_sample()
                elif name == "sample_edit":
                    if self.custom_sample_files[self.selected_pad]:
                        self.sample_editor_open = True
                elif name == "browser":
                    self.browser_open = True
                    self.browser_selected = None
                    self.browser_page = 0
                elif name == "repeat":
                    self.toggle_repeat()
                elif name == "repeat_rate":
                    self.cycle_repeat_rate()
                elif name == "metro":
                    self.toggle_metronome()
                elif name == "bpm_down":
                    self.adjust_bpm(-2)
                elif name == "bpm_up":
                    self.adjust_bpm(2)
                elif name == "tap":
                    self.tap_tempo()
                elif name == "loop_record":
                    self.request_loop_command("RECORD")
                elif name == "loop_play":
                    self.request_loop_command("PLAY")
                elif name == "loop_overdub":
                    self.request_loop_command("OVERDUB")
                elif name == "loop_undo":
                    self.request_loop_command("UNDO")
                elif name == "loop_redo":
                    self.request_loop_command("REDO")
                elif name == "loop_capture":
                    self.request_loop_command("CAPTURE")
                elif name == "loop_clear":
                    self.request_loop_command("CLEAR")
                elif name == "loop_quantize":
                    self.feel_open = True
                elif name == "loop_bars":
                    self.request_loop_command("BARS")
                elif name == "share":
                    self.share_open = True
                elif name == "perform_fx":
                    self.perform_fx_open = True
                return

        if self.view_mode == "Sequence":
            for (pad_index, step), rect in self.sequence_cells.items():
                if rect.collidepoint(pos):
                    self.sequence_step_cursor = step
                    if modifiers & pygame.KMOD_SHIFT:
                        self.select_sequence_range(pad_index, self.sequence_bar_page, step)
                        return
                    with self.loop_lock:
                        indices = self.sequence_cell_events_locked(pad_index, self.sequence_bar_page, step)
                        selected_index = self.selected_sequence_index_locked()
                        is_selected = selected_index in indices if selected_index is not None else False
                    if indices and not is_selected:
                        self.select_sequence_step(pad_index, self.sequence_bar_page, step)
                    else:
                        self.toggle_sequence_step(pad_index, self.sequence_bar_page, step)
                    return
            return

        for index, rect in self.pad_rects().items():
            if rect.collidepoint(pos):
                self.selected_pad = index
                if modifiers & pygame.KMOD_SHIFT:
                    if index in self.pad_selection and len(self.pad_selection) > 1:
                        self.pad_selection.remove(index)
                    else:
                        self.pad_selection.add(index)
                else:
                    self.pad_selection = {index}
                self.queue_pad(index, 112)
                self.begin_pad_drag(index, pos)
                return

    def set_accent(self, name):
        """Repoint the one colour the panel uses to mean "now"."""
        if name not in theme.ACCENT_NAMES:
            return False
        self.accent_name = name
        theme.set_accent(name)
        self.status = f"Pad light: {name}"
        self.persist_settings_async()
        return True

    def swap_pads(self, first, second):
        """Move one pad's sound onto another and bring its music along.

        Rearranging the layout should not rewrite the take, so recorded hits and
        sequenced steps follow the sound to its new pad. One undo puts the whole
        thing back.
        """
        first, second = int(first), int(second)
        if first == second or not (0 <= first < len(PADS) and 0 <= second < len(PADS)):
            return False

        self.push_project_history()

        for field in SOUND_PAD_FIELDS:
            values = getattr(self, field)
            values[first], values[second] = values[second], values[first]

        was_solo = (first in self.solo_pads, second in self.solo_pads)
        self.solo_pads.difference_update({first, second})
        if was_solo[0]:
            self.solo_pads.add(second)
        if was_solo[1]:
            self.solo_pads.add(first)

        with self.loop_lock:
            self.loop_events = swap_event_pads(self.loop_events, first, second)
            if self.loop_source_events is not None:
                self.loop_source_events = swap_event_pads(self.loop_source_events, first, second)
            self.loop_event_meta = swap_event_meta_pads(self.loop_event_meta, first, second)
            self.rebuild_loop_pending_locked(time.perf_counter_ns())

        for pattern in self.patterns:
            if not pattern:
                continue
            pattern["events"] = swap_event_pads(pattern["events"], first, second)
            if pattern["source_events"] is not None:
                pattern["source_events"] = swap_event_pads(pattern["source_events"], first, second)
            pattern["event_meta"] = swap_event_meta_pads(pattern["event_meta"], first, second)

        flash_until = time.perf_counter() + PAD_SWAP_FLASH_SECONDS
        self.pad_swap_flash = {first: flash_until, second: flash_until}

        # Either pad may still be sounding its old voice. The derived sound
        # caches key on content rather than pad index, so nothing else to clear.
        self.audio_events.put(("PANIC",))
        self.status = f"Swapped {PADS[first]['name']} and {PADS[second]['name']}  \u00b7  Undo with U"
        self.persist_settings_async()
        return True

    def move_selected_pad(self, delta):
        """Swap the selected pad with a neighbour and follow it."""
        target = (self.selected_pad + delta) % len(PADS)
        if self.swap_pads(self.selected_pad, target):
            self.selected_pad = target
            self.pad_selection = {target}
            return True
        return False

    def begin_pad_drag(self, index, pos):
        self.pad_drag_from = index
        self.pad_drag_origin = pos
        self.pad_drag_over = None
        self.pad_drag_active = False

    def update_pad_drag(self, pos):
        if self.pad_drag_from is None or self.pad_drag_origin is None:
            return
        moved = abs(pos[0] - self.pad_drag_origin[0]) + abs(pos[1] - self.pad_drag_origin[1])
        if moved < PAD_DRAG_THRESHOLD:
            return
        if not self.pad_drag_active:
            self.pad_drag_active = True
            self.status = "Drop on another pad to swap the sounds"
        over = next((index for index, rect in self.pad_rects().items() if rect.collidepoint(pos)), None)
        self.pad_drag_over = over if over != self.pad_drag_from else None

    def finish_pad_drag(self, pos):
        source, target = self.pad_drag_from, self.pad_drag_over
        self.pad_drag_from = self.pad_drag_over = self.pad_drag_origin = None
        self.pad_drag_active = False
        if source is None or target is None:
            return False
        if self.swap_pads(source, target):
            self.selected_pad = target
            self.pad_selection = {target}
            return True
        return False

    def focusable_controls(self):
        if self.clip_prompt_open: collection = self.clip_prompt_buttons
        elif self.browser_open: collection = self.browser_buttons
        elif self.chop_open: collection = self.chop_buttons
        elif self.sample_editor_open: collection = self.sample_editor_buttons
        elif self.share_open: collection = self.share_buttons
        elif self.perform_fx_open: collection = self.perform_fx_buttons
        elif self.mixer_open: collection = self.mixer_buttons
        elif self.scene_open: collection = self.scene_buttons
        elif self.feel_open: collection = self.feel_buttons
        elif self.project_menu_open: collection = self.project_buttons
        elif self.settings_open: collection = self.settings_buttons
        else: collection = self.buttons
        return [(name, rect) for name, rect in collection.items() if rect.width >= 20 and rect.height >= 20]

    def start_audio_worker(self):
        if self.audio_thread and self.audio_thread.is_alive():
            return
        self.audio_running.set()
        self.audio_thread = threading.Thread(
            target=self.audio_loop,
            name="DrumAudioTrigger",
            daemon=True,
        )
        self.audio_thread.start()

    def stop_audio_worker(self):
        if not self.audio_thread:
            return
        self.audio_running.clear()
        self.audio_events.put(("STOP",))
        self.audio_thread.join(timeout=2.0)
        self.audio_thread = None

    def audio_loop(self):
        task_handle = enable_audio_thread_priority()
        try:
            while self.audio_running.is_set():
                timeout = self.scheduler_timeout()
                try:
                    event = self.audio_events.get(timeout=timeout)
                except queue.Empty:
                    event = None

                if event:
                    if event[0] == "STOP":
                        break
                    try:
                        try:
                            queue_depth = self.audio_events.qsize()
                        except NotImplementedError:
                            queue_depth = 0
                        with self.metrics_lock:
                            self.max_queue_depth = max(self.max_queue_depth, queue_depth)
                        if event[0] == "MIDI":
                            _, kind, number, velocity, received_ns = event
                            self.handle_midi_trigger(kind, number, velocity, received_ns)
                        elif event[0] == "MIDI_OFF":
                            _, kind, number, _velocity, _received_ns = event
                            self.handle_midi_release(kind, number)
                        elif event[0] == "MIDI_CLOCK":
                            _, status, received_ns = event
                            self.handle_midi_clock(status, received_ns)
                        elif event[0] == "PAD":
                            _, index, velocity, received_ns = event
                            self.record_performance_hit(index, velocity, received_ns)
                            self.record_loop_hit(index, velocity, received_ns)
                            self.play_pad(index, velocity, None)
                            self.record_trigger_latency(received_ns)
                        elif event[0] == "TIMING":
                            self.reschedule_timing()
                        elif event[0] == "LOOP_CMD":
                            self.handle_loop_command(event[1])
                        elif event[0] == "PANIC":
                            self.stop_all_sounds()
                    except Exception as exc:
                        with self.metrics_lock:
                            self.audio_error_count += 1
                        self.status = f"Audio trigger failed: {exc}"
                        self.log(self.status)

                try:
                    self.process_scheduled_events()
                except Exception as exc:
                    with self.metrics_lock:
                        self.audio_error_count += 1
                    self.status = f"Audio scheduler failed: {exc}"
                    self.log(self.status)
        finally:
            release_audio_thread_priority(task_handle)

    def scheduler_timeout(self):
        now_ns = time.perf_counter_ns()
        deadlines = []
        if self.repeat_enabled:
            deadlines.extend(state["next_ns"] for state in self.held_triggers.values())
        if self.metronome_enabled:
            deadlines.append(self.next_metronome_ns or now_ns)
        external_sync = self.clock_active_source == "External" and self.external_transport_running
        loop_deadline = None if external_sync else self.loop_next_deadline()
        if loop_deadline is not None:
            deadlines.append(loop_deadline)
        if self.clock_output_enabled and self.midi_output and self.loop_playing and self.clock_active_source == "Internal":
            deadlines.append(self.next_midi_clock_out_ns or now_ns)
        if not deadlines:
            return None
        return max(0.0, (min(deadlines) - now_ns) / 1_000_000_000.0)

    def process_scheduled_events(self):
        now_ns = time.perf_counter_ns()

        if not self.repeat_enabled:
            self.held_triggers.clear()
        else:
            interval_ns = int(repeat_interval_seconds(self.repeat_rate, self.bpm) * 1_000_000_000)
            for state in list(self.held_triggers.values()):
                if now_ns < state["next_ns"]:
                    continue
                self.record_performance_hit(state["index"], state["velocity"], now_ns)
                self.record_loop_hit(state["index"], state["velocity"], now_ns)
                self.play_pad(state["index"], state["velocity"], state["label"])
                while state["next_ns"] <= now_ns:
                    state["next_ns"] += interval_ns

        if self.loop_record_pending:
            self.next_metronome_ns = None
            self.metronome_beat = 0
        elif not self.metronome_enabled:
            self.next_metronome_ns = None
            self.metronome_beat = 0
        else:
            beat_interval_ns = int((60.0 / self.bpm) * 1_000_000_000)
            if self.next_metronome_ns is None:
                self.next_metronome_ns = now_ns
            if now_ns >= self.next_metronome_ns:
                self.play_metronome_click(self.metronome_beat == 0)
                self.metronome_beat = (self.metronome_beat + 1) % 4
                while self.next_metronome_ns <= now_ns:
                    self.next_metronome_ns += beat_interval_ns

        external_sync = self.clock_active_source == "External" and self.external_transport_running
        if not external_sync:
            self.process_pending_record(now_ns)
            self.process_pattern_switch(now_ns)
            self.process_loop_scheduler(now_ns)
        if self.clock_active_source == "External" and self.clock_source == "Auto":
            if self.last_midi_clock_ns and now_ns - self.last_midi_clock_ns > 1_000_000_000:
                self.clock_active_source = "Internal"
                self.external_transport_running = False
                self.last_midi_clock_ns = None

        self.process_midi_clock_output(now_ns)

    def reschedule_timing(self):
        now_ns = time.perf_counter_ns()
        repeat_interval_ns = int(repeat_interval_seconds(self.repeat_rate, self.bpm) * 1_000_000_000)
        for state in self.held_triggers.values():
            state["next_ns"] = now_ns + repeat_interval_ns
        if self.metronome_enabled:
            self.next_metronome_ns = now_ns + int((60.0 / self.bpm) * 1_000_000_000)
        with self.loop_lock:
            if self.loop_record_pending:
                quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
                if self.loop_count_remaining:
                    self.loop_count_next_ns = now_ns + quarter_ns
                    self.loop_record_deadline_ns = now_ns + self.loop_count_remaining * quarter_ns
                else:
                    self.loop_record_deadline_ns = now_ns + quarter_ns
            if self.loop_start_ns is not None and (self.loop_playing or self.loop_recording):
                old_quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
                total_beats = self.loop_bars * 4.0
                elapsed_beats = max(0.0, (now_ns - self.loop_start_ns) / old_quarter_ns)
                phase_beats = elapsed_beats % total_beats
                new_quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
                self.loop_start_ns = now_ns - int(phase_beats * new_quarter_ns)
                self.loop_schedule_bpm = self.bpm
                self.rebuild_loop_pending_locked(now_ns)

    def process_midi_clock_output(self, now_ns):
        if not (self.clock_output_enabled and self.midi_output and self.loop_playing and self.clock_active_source == "Internal"):
            self.next_midi_clock_out_ns = None
            return
        interval_ns = max(1, round(60_000_000_000 / (self.bpm * 24)))
        if self.next_midi_clock_out_ns is None:
            self.next_midi_clock_out_ns = now_ns
        sent = 0
        while self.next_midi_clock_out_ns <= now_ns and sent < 4:
            self.midi_output.send(0xF8)
            self.next_midi_clock_out_ns += interval_ns
            sent += 1

    def play_metronome_click(self, accent):
        file = S["stick"][0 if accent else 1]
        sound = self.samples.get(file)
        if not sound:
            return
        channel = pygame.mixer.find_channel(True)
        if channel is None:
            return
        channel.set_volume(min(1.0, self.volume * (0.72 if accent else 0.46)))
        channel.play(sound, maxtime=55)

    def loop_length_ns_locked(self):
        quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
        return self.loop_bars * 4 * quarter_ns

    def loop_next_deadline(self):
        with self.loop_lock:
            deadlines = []
            if self.loop_record_pending:
                if self.loop_count_next_ns is not None:
                    deadlines.append(self.loop_count_next_ns)
                if self.loop_record_deadline_ns is not None:
                    deadlines.append(self.loop_record_deadline_ns)
            if self.loop_recording and not self.loop_overdub and self.loop_start_ns is not None:
                deadlines.append(self.loop_start_ns + self.loop_length_ns_locked())
            if self.loop_playing and self.loop_start_ns is not None:
                deadlines.append(self.loop_start_ns + self.loop_length_ns_locked())
                if self.loop_pending:
                    deadlines.append(self.loop_pending[0][0])
                if self.perform_fx_pending:
                    deadlines.append(self.perform_fx_pending[0][0])
            if self.pattern_switch_deadline_ns is not None:
                deadlines.append(self.pattern_switch_deadline_ns)
            return min(deadlines) if deadlines else None

    def clear_record_pending_locked(self):
        self.loop_record_pending = False
        self.loop_record_deadline_ns = None
        self.loop_count_next_ns = None
        self.loop_count_remaining = 0

    def start_loop_recording_locked(self, now_ns):
        self.push_loop_history_locked()
        self.loop_events.clear()
        self.loop_source_events = None
        self.loop_event_meta = {}
        self.perform_fx_events = [(0.0, field, value) for field, value in self.perform_fx.items()]
        self.loop_pending.clear()
        self.perform_fx_pending.clear()
        self.loop_playing = False
        self.loop_recording = True
        self.loop_overdub = False
        self.loop_start_ns = now_ns
        self.loop_schedule_bpm = self.bpm
        self.clear_record_pending_locked()

    def schedule_loop_recording_locked(self, now_ns):
        if self.record_start_mode == "Instant":
            self.start_loop_recording_locked(now_ns)
            return "Recording"

        quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
        if self.record_start_mode == "Next bar" and self.loop_playing and self.loop_start_ns is not None:
            elapsed_beats = max(0.0, (now_ns - self.loop_start_ns) / quarter_ns)
            beats_to_boundary = 4.0 - (elapsed_beats % 4.0)
            self.loop_record_pending = True
            self.loop_record_deadline_ns = now_ns + max(1, round(beats_to_boundary * quarter_ns))
            self.loop_count_next_ns = None
            self.loop_count_remaining = 0
            return "Record next bar"
        if self.record_start_mode == "Next bar":
            self.start_loop_recording_locked(now_ns)
            return "Recording"

        self.loop_record_pending = True
        self.loop_record_deadline_ns = now_ns + 4 * quarter_ns
        self.loop_count_next_ns = now_ns + quarter_ns
        self.loop_count_remaining = 4
        self.play_metronome_click(True)
        return "Count 4"

    def process_pending_record(self, now_ns):
        message = None
        with self.loop_lock:
            if not self.loop_record_pending or self.loop_record_deadline_ns is None:
                return
            while (
                self.loop_count_remaining > 1
                and self.loop_count_next_ns is not None
                and now_ns >= self.loop_count_next_ns
            ):
                self.loop_count_remaining -= 1
                self.play_metronome_click(False)
                quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
                self.loop_count_next_ns += quarter_ns
                message = f"Count {self.loop_count_remaining}"
            if now_ns >= self.loop_record_deadline_ns:
                self.start_loop_recording_locked(self.loop_record_deadline_ns)
                message = "Recording"
        if message:
            self.set_surface_notice(message, duration=1.0)

    def push_loop_history_locked(self, clear_redo=True):
        self.loop_history.append(self.loop_history_snapshot_locked())
        if clear_redo:
            self.loop_redo.clear()

    def loop_history_snapshot_locked(self):
        return (
            self.loop_bars,
            list(self.loop_events),
            list(self.loop_source_events) if self.loop_source_events is not None else None,
            self.feel_preset,
            self.feel_strength,
            self.feel_swing,
            self.feel_nudge_ms,
            self.feel_humanize_ms,
            json.loads(json.dumps(self.loop_event_meta)),
            list(self.perform_fx_events),
        )

    def restore_loop_snapshot_locked(self, snapshot, now_ns):
        if len(snapshot) == 2:
            self.loop_bars, events = snapshot
            source = None
            event_meta = {}
            perform_fx_events = []
        else:
            values = tuple(snapshot)
            (
                self.loop_bars,
                events,
                source,
                self.feel_preset,
                self.feel_strength,
                self.feel_swing,
                self.feel_nudge_ms,
                self.feel_humanize_ms,
            ) = values[:8]
            event_meta = values[8] if len(values) > 8 else {}
            perform_fx_events = values[9] if len(values) > 9 else []
        self.loop_events = list(events)
        self.loop_source_events = list(source) if source is not None else None
        self.loop_event_meta = sanitize_event_meta(event_meta)
        self.perform_fx_events = self.sanitize_perform_fx_events(perform_fx_events, self.loop_bars)
        self.loop_recording = False
        self.loop_overdub = False
        self.loop_playing = bool(self.loop_events)
        self.loop_start_ns = now_ns if self.loop_playing else None
        self.loop_cycle_index = 0
        self.loop_schedule_bpm = self.bpm
        self.rebuild_loop_pending_locked(now_ns)

    def apply_current_feel_locked(self, now_ns):
        if not self.loop_events:
            return False
        if self.loop_source_events is None:
            self.loop_source_events = list(self.loop_events)
        grid = 1.0 / REPEAT_RATES[self.repeat_rate]
        previous_events = list(self.loop_events)
        self.loop_events = apply_loop_feel(
            self.loop_source_events,
            self.loop_bars,
            grid,
            self.feel_strength,
            self.feel_swing,
            self.feel_nudge_ms,
            self.feel_humanize_ms,
            self.bpm,
        )
        self.remap_event_meta_locked(previous_events, self.loop_events)
        if self.loop_playing:
            self.rebuild_loop_pending_locked(now_ns)
        return True

    def remap_event_meta_locked(self, previous_events, current_events):
        previous = list(enumerate(previous_events))
        used = set()
        remapped = {}
        for beat, pad, _velocity in current_events:
            candidates = [
                (abs(old_beat - beat), index, old_beat)
                for index, (old_beat, old_pad, _old_velocity) in previous
                if old_pad == pad and index not in used
            ]
            if not candidates:
                continue
            _distance, index, old_beat = min(candidates)
            used.add(index)
            meta = self.loop_event_meta.get(event_meta_key(pad, old_beat))
            if meta:
                remapped[event_meta_key(pad, beat)] = dict(meta)
        self.loop_event_meta = remapped

    def set_feel_preset(self, preset, now_ns=None):
        if preset not in FEEL_PRESETS:
            return False
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            if not self.loop_events:
                return False
            self.push_loop_history_locked()
            values = FEEL_PRESETS[preset]
            self.feel_preset = preset
            self.feel_strength = values["strength"]
            self.feel_swing = values["swing"]
            self.feel_nudge_ms = values["nudge_ms"]
            self.feel_humanize_ms = values["humanize_ms"]
            self.apply_current_feel_locked(now_ns)
        self.persist_settings_async()
        self.set_surface_notice(f"Feel: {preset}")
        return True

    def adjust_feel(self, field, amount, now_ns=None):
        limits = {
            "strength": (0, 100),
            "swing": (50, 75),
            "nudge_ms": (-50, 50),
            "humanize_ms": (0, 20),
        }
        if field not in limits or not self.loop_events:
            return False
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        attribute = f"feel_{field}"
        with self.loop_lock:
            self.push_loop_history_locked()
            low, high = limits[field]
            setattr(self, attribute, max(low, min(high, getattr(self, attribute) + amount)))
            self.feel_preset = "Custom"
            self.apply_current_feel_locked(now_ns)
        self.persist_settings_async()
        return True

    def reset_loop_feel(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            if self.loop_source_events is None:
                return False
            self.push_loop_history_locked()
            previous_events = list(self.loop_events)
            self.loop_events = list(self.loop_source_events)
            self.remap_event_meta_locked(previous_events, self.loop_events)
            self.loop_source_events = None
            self.feel_preset = "Natural"
            self.feel_strength = 50
            self.feel_swing = 50
            self.feel_nudge_ms = 0
            self.feel_humanize_ms = 0
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        self.set_surface_notice("Feel reset")
        return True

    def cycle_feel_grid(self):
        with self.loop_lock:
            if not self.loop_events:
                return False
            self.push_loop_history_locked()
            rates = tuple(REPEAT_RATES)
            current = rates.index(self.repeat_rate)
            self.repeat_rate = rates[(current + 1) % len(rates)]
            self.apply_current_feel_locked(time.perf_counter_ns())
        self.persist_settings_async()
        return True

    def cycle_pattern_launch_mode(self):
        current = PATTERN_LAUNCH_MODES.index(self.pattern_launch_mode)
        self.pattern_launch_mode = PATTERN_LAUNCH_MODES[(current + 1) % len(PATTERN_LAUNCH_MODES)]
        self.persist_settings()

    def switch_pattern_locked(self, index, start_ns, keep_playing):
        self.save_active_pattern_locked()
        self.active_pattern = int(index)
        self.apply_pattern_data_locked(self.patterns[self.active_pattern])
        self.pending_pattern = None
        self.pattern_switch_deadline_ns = None
        self.loop_recording = False
        self.loop_overdub = False
        self.loop_record_pending = False
        self.loop_history.clear()
        self.loop_redo.clear()
        self.loop_cycle_index = 0
        if keep_playing and self.loop_events:
            self.loop_playing = True
            self.loop_start_ns = int(start_ns)
            self.loop_schedule_bpm = self.bpm
            self.rebuild_loop_pending_locked(int(start_ns))
        else:
            self.loop_playing = False
            self.loop_start_ns = None
            self.loop_pending.clear()

    def request_pattern(self, index, now_ns=None):
        if not 0 <= int(index) < PATTERN_COUNT:
            return False
        index = int(index)
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            if index == self.active_pattern:
                if self.pending_pattern is not None:
                    self.pending_pattern = None
                    self.pattern_switch_deadline_ns = None
                    self.set_surface_notice("Pattern queue cancelled")
                    return True
                return False
            if not self.loop_playing or self.loop_start_ns is None:
                self.switch_pattern_locked(index, now_ns, False)
                message = f"Pattern {index + 1}"
            else:
                quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
                phase = max(0.0, (now_ns - self.loop_start_ns) / quarter_ns)
                quantum = 1.0 if self.pattern_launch_mode == "Next beat" else 4.0 if self.pattern_launch_mode == "Next bar" else self.loop_bars * 4.0
                remaining = quantum - (phase % quantum)
                if remaining < 0.001:
                    remaining = quantum
                self.pending_pattern = index
                self.pattern_switch_deadline_ns = now_ns + max(1, round(remaining * quarter_ns))
                message = f"Pattern {index + 1} queued"
        self.persist_settings_async()
        self.set_surface_notice(message)
        return True

    def process_pattern_switch(self, now_ns):
        with self.loop_lock:
            if self.pending_pattern is None or self.pattern_switch_deadline_ns is None:
                return
            if now_ns < self.pattern_switch_deadline_ns:
                return
            target = self.pending_pattern
            deadline = self.pattern_switch_deadline_ns
            self.switch_pattern_locked(target, deadline, True)
        self.persist_settings_async()

    def duplicate_pattern(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        self.push_project_history()
        with self.loop_lock:
            self.save_active_pattern_locked()
            candidates = [index for index, value in enumerate(self.patterns) if value is None]
            target = candidates[0] if candidates else (self.active_pattern + 1) % PATTERN_COUNT
            self.patterns[target] = json.loads(json.dumps(self.patterns[self.active_pattern]))
        self.request_pattern(target, now_ns)
        self.set_surface_notice(f"Duplicated to {target + 1}")
        return target

    def double_pattern(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            if self.loop_bars >= max(LOOP_BAR_OPTIONS):
                return False
            self.push_loop_history_locked()
            previous_events = list(self.loop_events)
            previous_source = list(self.loop_source_events) if self.loop_source_events is not None else None
            old_beats = self.loop_bars * 4.0
            self.loop_bars *= 2
            copies = [(beat + old_beats, pad, velocity) for beat, pad, velocity in previous_events]
            for beat, pad, _velocity in previous_events:
                meta = self.loop_event_meta.get(event_meta_key(pad, beat))
                if meta:
                    self.loop_event_meta[event_meta_key(pad, beat + old_beats)] = dict(meta)
            self.loop_events = sorted(previous_events + copies)
            if previous_source is not None:
                self.loop_source_events = sorted(previous_source + [(beat + old_beats, pad, velocity) for beat, pad, velocity in previous_source])
            self.save_active_pattern_locked()
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        self.set_surface_notice(f"Pattern doubled to {self.loop_bars} bars")
        return True

    def add_scene_pattern(self, index=None):
        index = self.active_pattern if index is None else int(index)
        if not 0 <= index < PATTERN_COUNT or self.patterns[index] is None:
            return False
        self.scene_order.append(index)
        del self.scene_order[64:]
        self.persist_settings_async()
        return True

    def remove_scene_step(self):
        if not self.scene_order:
            return False
        self.scene_order.pop()
        self.scene_position = min(self.scene_position, max(0, len(self.scene_order) - 1))
        self.persist_settings_async()
        return True

    def toggle_song_playback(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            if self.song_playing:
                self.song_playing = False
                return False
            if not self.scene_order:
                return False
            self.song_playing = True
            self.scene_position = 0
            target = self.scene_order[0]
            self.switch_pattern_locked(target, now_ns, True)
            if not self.loop_events:
                self.song_playing = False
                return False
        return True

    @staticmethod
    def sequence_cell_beat(bar_page, step):
        return int(bar_page) * 4.0 + int(step) * 0.25

    def sequence_cell_events_locked(self, pad_index, bar_page, step):
        center = self.sequence_cell_beat(bar_page, step)
        start = center - 0.125
        end = center + 0.125
        return [
            index
            for index, (beat, pad, _velocity) in enumerate(self.loop_events)
            if pad == pad_index and start <= beat < end
        ]

    def toggle_sequence_step(self, pad_index, bar_page, step, now_ns=None):
        if not 0 <= pad_index < len(PADS) or not 0 <= bar_page < self.loop_bars or not 0 <= step < 16:
            return False
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            self.push_loop_history_locked()
            indices = self.sequence_cell_events_locked(pad_index, bar_page, step)
            if indices:
                for index in reversed(indices):
                    beat, pad, _velocity = self.loop_events.pop(index)
                    self.loop_event_meta.pop(event_meta_key(pad, beat), None)
                self.sequence_selected = None
                self.sequence_selection.clear()
            else:
                beat = self.sequence_cell_beat(bar_page, step)
                self.loop_events.append((beat, pad_index, self.sequence_velocity))
                self.loop_events.sort()
                self.sequence_selected = (pad_index, beat)
                self.sequence_selection = {(pad_index, round(beat, 6))}
            self.loop_source_events = None
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def add_sequence_step_from_pad(self, pad_index, velocity, now_ns=None):
        if not self.sequence_step_input or self.view_mode != "Sequence":
            return False
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            self.push_loop_history_locked()
            indices = self.sequence_cell_events_locked(
                pad_index, self.sequence_bar_page, self.sequence_step_cursor
            )
            beat = self.sequence_cell_beat(self.sequence_bar_page, self.sequence_step_cursor)
            if indices:
                old_beat, _pad, _old_velocity = self.loop_events[indices[0]]
                self.loop_events[indices[0]] = (old_beat, pad_index, max(1, min(127, int(velocity))))
            else:
                self.loop_events.append((beat, pad_index, max(1, min(127, int(velocity)))))
                self.loop_events.sort()
            self.sequence_selected = (pad_index, beat)
            self.sequence_selection = {(pad_index, round(beat, 6))}
            self.sequence_velocity = max(1, min(127, int(velocity)))
            self.sequence_step_cursor = (self.sequence_step_cursor + 1) % 16
            self.loop_source_events = None
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def select_sequence_step(self, pad_index, bar_page, step):
        with self.loop_lock:
            indices = self.sequence_cell_events_locked(pad_index, bar_page, step)
            if not indices:
                self.sequence_selected = None
                self.sequence_selection.clear()
                return False
            beat, pad, velocity = self.loop_events[indices[0]]
            self.sequence_selected = (pad, beat)
            self.sequence_selection = {(pad, round(beat, 6))}
            self.sequence_velocity = velocity
            return True

    def select_sequence_range(self, pad_index, bar_page, step):
        target_beat = self.sequence_cell_beat(bar_page, step)
        with self.loop_lock:
            if self.sequence_selected is None:
                return self.select_sequence_step(pad_index, bar_page, step)
            start_pad, start_beat = self.sequence_selected
            low_pad, high_pad = sorted((start_pad, pad_index))
            low_beat, high_beat = sorted((start_beat, target_beat))
            selected = {
                (pad, round(beat, 6))
                for beat, pad, _velocity in self.loop_events
                if low_pad <= pad <= high_pad and low_beat - 0.125 <= beat <= high_beat + 0.125
            }
            if not selected:
                return False
            self.sequence_selection = selected
            return True

    def selected_sequence_index_locked(self):
        if self.sequence_selected is None:
            return None
        selected_pad, selected_beat = self.sequence_selected
        matches = [
            (abs(beat - selected_beat), index)
            for index, (beat, pad, _velocity) in enumerate(self.loop_events)
            if pad == selected_pad and abs(beat - selected_beat) <= 0.13
        ]
        return min(matches)[1] if matches else None

    def selected_sequence_indices_locked(self):
        if self.sequence_selection:
            indices = [
                index
                for index, (beat, pad, _velocity) in enumerate(self.loop_events)
                if (pad, round(beat, 6)) in self.sequence_selection
            ]
            if indices:
                return indices
        index = self.selected_sequence_index_locked()
        return [index] if index is not None else []

    def adjust_sequence_velocity(self, amount, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            indices = self.selected_sequence_indices_locked()
            if not indices:
                return False
            self.push_loop_history_locked()
            velocities = []
            for index in indices:
                beat, pad, velocity = self.loop_events[index]
                velocity = max(1, min(127, velocity + int(amount)))
                self.loop_events[index] = (beat, pad, velocity)
                velocities.append(velocity)
            self.sequence_velocity = round(sum(velocities) / len(velocities))
            self.loop_source_events = None
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def nudge_sequence_event(self, milliseconds, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            indices = self.selected_sequence_indices_locked()
            if not indices:
                return False
            self.push_loop_history_locked()
            delta = float(milliseconds) * self.bpm / 60000.0
            previous_events = list(self.loop_events)
            new_selection = set()
            for index in indices:
                beat, pad, velocity = self.loop_events[index]
                beat = (beat + delta) % (self.loop_bars * 4.0)
                self.loop_events[index] = (beat, pad, velocity)
                new_selection.add((pad, round(beat, 6)))
            self.loop_events.sort()
            self.remap_event_meta_locked(previous_events, self.loop_events)
            self.sequence_selection = new_selection
            selected_pad, selected_beat = next(iter(new_selection))
            self.sequence_selected = (selected_pad, selected_beat)
            self.loop_source_events = None
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def copy_sequence_event(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            indices = self.selected_sequence_indices_locked()
            if not indices:
                return False
            self.push_loop_history_locked()
            copies = []
            new_selection = set()
            for index in indices:
                beat, pad, velocity = self.loop_events[index]
                copied_beat = (beat + 0.25) % (self.loop_bars * 4.0)
                copies.append((copied_beat, pad, velocity))
                meta = self.loop_event_meta.get(event_meta_key(pad, beat))
                if meta:
                    self.loop_event_meta[event_meta_key(pad, copied_beat)] = dict(meta)
                new_selection.add((pad, round(copied_beat, 6)))
            self.loop_events.extend(copies)
            self.loop_events.sort()
            self.sequence_selection = new_selection
            selected_pad, selected_beat = next(iter(new_selection))
            self.sequence_selected = (selected_pad, selected_beat)
            self.loop_source_events = None
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def adjust_sequence_meta(self, field, amount, now_ns=None):
        if field not in ("chance", "ratchet"):
            return False
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self.loop_lock:
            indices = self.selected_sequence_indices_locked()
            if not indices:
                return False
            self.push_loop_history_locked()
            for index in indices:
                beat, pad, _velocity = self.loop_events[index]
                key = event_meta_key(pad, beat)
                meta = dict(self.loop_event_meta.get(key, {"chance": 100, "ratchet": 1}))
                if field == "chance":
                    meta[field] = max(0, min(100, meta[field] + int(amount)))
                else:
                    meta[field] = max(1, min(4, meta[field] + int(amount)))
                if meta["chance"] == 100 and meta["ratchet"] == 1:
                    self.loop_event_meta.pop(key, None)
                else:
                    self.loop_event_meta[key] = meta
            if self.loop_playing:
                self.rebuild_loop_pending_locked(now_ns)
        self.persist_settings_async()
        return True

    def selected_sequence_meta(self):
        with self.loop_lock:
            indices = self.selected_sequence_indices_locked()
            if not indices:
                return 100, 1
            values = []
            for index in indices:
                beat, pad, _velocity = self.loop_events[index]
                values.append(self.loop_event_meta.get(event_meta_key(pad, beat), {"chance": 100, "ratchet": 1}))
            return (
                round(sum(value["chance"] for value in values) / len(values)),
                round(sum(value["ratchet"] for value in values) / len(values)),
            )

    def record_performance_hit(self, pad_index, velocity, event_ns=None):
        event_ns = time.perf_counter_ns() if event_ns is None else int(event_ns)
        cutoff_ns = event_ns - int(PERFORMANCE_BUFFER_SECONDS * 1_000_000_000)
        event = (event_ns, pad_index, max(1, min(127, int(velocity))))
        with self.performance_lock:
            self.performance_events.append(event)
            while self.performance_events and self.performance_events[0][0] < cutoff_ns:
                self.performance_events.popleft()

    def performance_capture_available(self, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        cutoff_ns = now_ns - int(PERFORMANCE_BUFFER_SECONDS * 1_000_000_000)
        with self.performance_lock:
            return bool(self.performance_events and self.performance_events[-1][0] >= cutoff_ns)

    def capture_last_performance_locked(self, now_ns):
        cutoff_ns = now_ns - int(PERFORMANCE_BUFFER_SECONDS * 1_000_000_000)
        with self.performance_lock:
            events = [event for event in self.performance_events if event[0] >= cutoff_ns]
        if not events:
            return False

        gap_ns = int(PERFORMANCE_PHRASE_GAP_SECONDS * 1_000_000_000)
        phrase_start = 0
        for index in range(1, len(events)):
            if events[index][0] - events[index - 1][0] > gap_ns:
                phrase_start = index
        events = events[phrase_start:]

        quarter_ns = int((60.0 / self.bpm) * 1_000_000_000)
        max_span_ns = int(15.75 * quarter_ns)
        while len(events) > 1 and events[-1][0] - events[0][0] > max_span_ns:
            events.pop(0)

        origin_ns = events[0][0]
        span_beats = (events[-1][0] - origin_ns) / quarter_ns
        required_beats = span_beats + 0.25
        capture_bars = next(
            (bars for bars in LOOP_BAR_OPTIONS if required_beats <= bars * 4),
            LOOP_BAR_OPTIONS[-1],
        )
        captured = sorted(
            (
                min(capture_bars * 4.0 - 0.001, (event_ns - origin_ns) / quarter_ns),
                pad_index,
                velocity,
            )
            for event_ns, pad_index, velocity in events
        )

        self.push_loop_history_locked()
        self.loop_bars = capture_bars
        self.loop_events = captured
        self.loop_recording = False
        self.loop_overdub = False
        self.loop_playing = True
        self.loop_start_ns = now_ns
        self.loop_cycle_index = 0
        self.loop_schedule_bpm = self.bpm
        self.rebuild_loop_pending_locked(now_ns)
        return True

    def rebuild_loop_pending_locked(self, now_ns):
        self.loop_pending.clear()
        self.perform_fx_pending.clear()
        if not self.loop_playing or not self.loop_events or self.loop_start_ns is None:
            return
        quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
        for sequence, (beat, field, value) in enumerate(self.perform_fx_events):
            deadline = self.loop_start_ns + int(beat * quarter_ns)
            if deadline >= now_ns:
                heapq.heappush(self.perform_fx_pending, (deadline, sequence, field, value))
        for sequence, (beat, pad_index, velocity) in enumerate(self.loop_events):
            meta = self.loop_event_meta.get(event_meta_key(pad_index, beat), {"chance": 100, "ratchet": 1})
            if deterministic_event_roll(self.loop_cycle_index, pad_index, beat) > meta["chance"]:
                continue
            ratchet = meta["ratchet"]
            for repeat_index in range(ratchet):
                repeat_beat = beat + repeat_index * (0.25 / ratchet)
                deadline = self.loop_start_ns + int(repeat_beat * quarter_ns)
                if deadline < now_ns:
                    continue
                heapq.heappush(
                    self.loop_pending,
                    (deadline, sequence * 4 + repeat_index, pad_index, velocity),
                )

    def finish_recording_locked(self, start_playback_ns):
        self.loop_recording = False
        self.loop_overdub = False
        if self.loop_events:
            self.loop_playing = True
            self.loop_start_ns = start_playback_ns
            self.loop_cycle_index = 0
            self.loop_schedule_bpm = self.bpm
            self.rebuild_loop_pending_locked(start_playback_ns)
        else:
            self.loop_playing = False
            self.loop_start_ns = None
            self.loop_pending.clear()

    def process_loop_scheduler(self, now_ns):
        due_events = []
        due_fx = []
        should_persist = False
        with self.loop_lock:
            loop_length_ns = self.loop_length_ns_locked()
            if (
                self.loop_recording
                and not self.loop_overdub
                and self.loop_start_ns is not None
                and now_ns >= self.loop_start_ns + loop_length_ns
            ):
                boundary = self.loop_start_ns + loop_length_ns
                self.finish_recording_locked(boundary)
                should_persist = True

            if self.loop_playing:
                if not self.loop_events:
                    self.loop_playing = False
                    self.loop_pending.clear()
                else:
                    if self.loop_start_ns is None:
                        self.loop_start_ns = now_ns
                        self.loop_cycle_index = 0
                        self.loop_schedule_bpm = self.bpm
                        self.rebuild_loop_pending_locked(now_ns)
                    loop_length_ns = self.loop_length_ns_locked()
                    if now_ns >= self.loop_start_ns + loop_length_ns:
                        boundary = self.loop_start_ns + loop_length_ns
                        if self.song_playing and self.scene_order:
                            self.scene_position = (self.scene_position + 1) % len(self.scene_order)
                            self.switch_pattern_locked(
                                self.scene_order[self.scene_position], boundary, True
                            )
                            should_persist = True
                        else:
                            elapsed = now_ns - self.loop_start_ns
                            completed_cycles = max(1, elapsed // loop_length_ns)
                            self.loop_start_ns += completed_cycles * loop_length_ns
                            self.loop_cycle_index += completed_cycles
                            self.rebuild_loop_pending_locked(now_ns)

                    while self.loop_pending and self.loop_pending[0][0] <= now_ns:
                        _deadline, _sequence, pad_index, velocity = heapq.heappop(self.loop_pending)
                        due_events.append((pad_index, velocity))
                    while self.perform_fx_pending and self.perform_fx_pending[0][0] <= now_ns:
                        _deadline, _sequence, field, value = heapq.heappop(self.perform_fx_pending)
                        due_fx.append((field, value))

        for field, value in due_fx:
            self.perform_fx[field] = value
            self.processed_sound_cache.clear()
        for pad_index, velocity in due_events:
            self.play_pad(pad_index, velocity, "Loop")
        if should_persist:
            self.persist_settings_async()

    def record_loop_hit(self, pad_index, velocity, event_ns=None):
        with self.loop_lock:
            if not self.loop_recording or self.loop_start_ns is None:
                return
            event_ns = event_ns or time.perf_counter_ns()
            elapsed_ns = event_ns - self.loop_start_ns
            if elapsed_ns < 0:
                return
            if not self.loop_overdub and elapsed_ns >= self.loop_length_ns_locked():
                return
            quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
            total_beats = self.loop_bars * 4.0
            beat = (elapsed_ns / quarter_ns) % total_beats
            self.loop_events.append((beat, pad_index, max(1, min(127, velocity))))
            self.loop_events.sort()

    def handle_loop_command(self, command, now_ns=None):
        now_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        was_playing = self.loop_playing
        should_persist = False
        export_kind = None
        message = None
        with self.loop_lock:
            if command == "RECORD":
                if self.loop_record_pending:
                    self.clear_record_pending_locked()
                    message = "Record cancelled"
                elif self.loop_recording and not self.loop_overdub:
                    self.finish_recording_locked(now_ns)
                    message = "Recording stopped"
                else:
                    message = self.schedule_loop_recording_locked(now_ns)
                should_persist = True
            elif command == "PLAY":
                self.clear_record_pending_locked()
                self.loop_recording = False
                self.loop_overdub = False
                self.loop_playing = bool(self.loop_events) and not self.loop_playing
                if self.loop_playing:
                    self.loop_start_ns = now_ns
                    self.loop_cycle_index = 0
                    self.loop_schedule_bpm = self.bpm
                    self.rebuild_loop_pending_locked(now_ns)
                    message = "Loop playing"
                else:
                    self.loop_start_ns = None
                    self.loop_pending.clear()
                    message = "Loop stopped"
            elif command == "OVERDUB":
                self.clear_record_pending_locked()
                if self.loop_overdub:
                    self.loop_overdub = False
                    self.loop_recording = False
                    message = "Overdub stopped"
                    should_persist = True
                elif self.loop_events:
                    self.push_loop_history_locked()
                    self.loop_source_events = None
                    self.loop_overdub = True
                    self.loop_recording = True
                    if not self.loop_playing:
                        self.loop_playing = True
                        self.loop_start_ns = now_ns
                        self.loop_cycle_index = 0
                        self.loop_schedule_bpm = self.bpm
                        self.rebuild_loop_pending_locked(now_ns)
                    message = "Overdub"
                else:
                    message = "Record a loop first"
            elif command == "UNDO":
                self.clear_record_pending_locked()
                if self.loop_history:
                    self.loop_redo.append(self.loop_history_snapshot_locked())
                    self.restore_loop_snapshot_locked(self.loop_history.pop(), now_ns)
                    message = "Loop undo"
                    should_persist = True
            elif command == "REDO":
                self.clear_record_pending_locked()
                if self.loop_redo:
                    self.push_loop_history_locked(clear_redo=False)
                    self.restore_loop_snapshot_locked(self.loop_redo.pop(), now_ns)
                    message = "Loop redo"
                    should_persist = True
            elif command == "CAPTURE":
                self.clear_record_pending_locked()
                if self.capture_last_performance_locked(now_ns):
                    self.loop_source_events = None
                    self.loop_event_meta = {}
                    self.perform_fx_events = []
                    message = f"Captured {self.loop_bars} bar"
                    should_persist = True
                else:
                    message = "Play something first"
            elif command == "CLEAR":
                self.clear_record_pending_locked()
                if self.loop_events:
                    self.push_loop_history_locked()
                self.loop_events.clear()
                self.loop_source_events = None
                self.loop_event_meta = {}
                self.perform_fx_events.clear()
                self.loop_pending.clear()
                self.perform_fx_pending.clear()
                self.loop_playing = False
                self.loop_recording = False
                self.loop_overdub = False
                self.loop_start_ns = None
                message = "Loop cleared"
                should_persist = True
            elif command == "QUANTIZE" and self.loop_events:
                self.clear_record_pending_locked()
                self.push_loop_history_locked()
                values = FEEL_PRESETS["Tight"]
                self.feel_preset = "Tight"
                self.feel_strength = values["strength"]
                self.feel_swing = values["swing"]
                self.feel_nudge_ms = values["nudge_ms"]
                self.feel_humanize_ms = values["humanize_ms"]
                self.apply_current_feel_locked(now_ns)
                message = f"Tight {self.repeat_rate}"
                should_persist = True
            elif command == "BARS":
                self.clear_record_pending_locked()
                self.push_loop_history_locked()
                current = LOOP_BAR_OPTIONS.index(self.loop_bars)
                previous_events = list(self.loop_events)
                self.loop_bars = LOOP_BAR_OPTIONS[(current + 1) % len(LOOP_BAR_OPTIONS)]
                total_beats = self.loop_bars * 4.0
                self.loop_events = sorted(
                    (beat % total_beats, pad, velocity)
                    for beat, pad, velocity in self.loop_events
                )
                if self.loop_source_events is not None:
                    self.loop_source_events = sorted(
                        (beat % total_beats, pad, velocity)
                        for beat, pad, velocity in self.loop_source_events
                    )
                self.remap_event_meta_locked(previous_events, self.loop_events)
                if self.loop_playing or self.loop_recording:
                    self.loop_start_ns = now_ns
                    self.loop_cycle_index = 0
                    self.loop_schedule_bpm = self.bpm
                    self.rebuild_loop_pending_locked(now_ns)
                message = f"Loop bars: {self.loop_bars}"
                should_persist = True
            elif command == "EXPORT_WAV":
                export_kind = "WAV"
            elif command == "EXPORT_MIDI":
                export_kind = "MIDI"

        if message:
            self.log(message)
            self.set_surface_notice(message)
        if self.clock_output_enabled and self.midi_output and self.clock_active_source == "Internal":
            if self.loop_playing and not was_playing:
                self.midi_output.send(0xFA)
                self.next_midi_clock_out_ns = now_ns
            elif was_playing and not self.loop_playing:
                self.midi_output.send(0xFC)
        if should_persist:
            self.persist_settings_async()
        if export_kind:
            self.start_loop_export(export_kind)

    def request_loop_command(self, command):
        self.audio_events.put(("LOOP_CMD", command))

    def loop_snapshot(self):
        now_ns = time.perf_counter_ns()
        with self.loop_lock:
            total_beats = self.loop_bars * 4.0
            phase = 0.0
            if self.loop_start_ns is not None and (self.loop_playing or self.loop_recording):
                quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
                phase = ((now_ns - self.loop_start_ns) / quarter_ns) % total_beats
            return {
                "events": list(self.loop_events),
                "bars": self.loop_bars,
                "playing": self.loop_playing,
                "recording": self.loop_recording,
                "overdub": self.loop_overdub,
                "record_pending": self.loop_record_pending,
                "count_remaining": self.loop_count_remaining,
                "phase": phase,
                "exporting": self.loop_exporting,
                "last_export": self.last_export,
                "can_undo": bool(self.loop_history),
                "can_redo": bool(self.loop_redo),
                "can_capture": self.performance_capture_available(now_ns),
            }

    def loop_render_snapshot(self):
        return {
            "events": list(self.loop_events), "bars": self.loop_bars, "bpm": self.bpm,
            "pad_synths": list(self.pad_synths), "pad_sensitivity": list(self.pad_sensitivity),
            "custom_samples": list(self.custom_sample_files),
            "sample_edits": json.loads(json.dumps(self.sample_edits)), "volume": self.volume,
            "event_meta": json.loads(json.dumps(self.loop_event_meta)), "pad_volume": list(self.pad_volume),
            "pad_pan": list(self.pad_pan), "pad_tune": list(self.pad_tune), "pad_mute": list(self.pad_mute),
            "pad_punch": list(self.pad_punch), "pad_air": list(self.pad_air),
            "pad_space": list(self.pad_space), "pad_bus": list(self.pad_bus),
            "solo_pads": list(self.solo_pads), "mixer_bypass": self.mixer_bypass,
            "perform_fx": dict(self.perform_fx), "perform_fx_bypass": self.perform_fx_bypass,
            "perform_fx_events": [list(event) for event in self.perform_fx_events],
            "project": self.project_payload(), "project_name": self.project_name,
        }

    def adjust_perform_fx(self, field, amount):
        if field not in self.perform_fx:
            return False
        self.perform_fx[field] = max(0, min(100, self.perform_fx[field] + int(amount)))
        self.processed_sound_cache.clear()
        with self.loop_lock:
            if self.loop_recording and self.loop_start_ns is not None:
                quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
                beat = ((time.perf_counter_ns() - self.loop_start_ns) / quarter_ns) % (self.loop_bars * 4.0)
                self.perform_fx_events.append((beat, field, self.perform_fx[field]))
                self.perform_fx_events.sort()
        return True

    def set_perform_fx_from_position(self, field, x, rect):
        if field not in self.perform_fx:
            return False
        target = round(max(0.0, min(1.0, (x - rect.left) / max(1, rect.width))) * 100)
        return self.adjust_perform_fx(field, target - self.perform_fx[field])

    def handle_perform_fx_drag(self, pos):
        for name, rect in self.perform_fx_buttons.items():
            if name.startswith("perform_fx_track_") and rect.inflate(0, 22).collidepoint(pos):
                return self.set_perform_fx_from_position(name.removeprefix("perform_fx_track_"), pos[0], rect)
        return False

    def reset_perform_fx(self):
        for field in self.perform_fx:
            if self.perform_fx[field]:
                self.adjust_perform_fx(field, -self.perform_fx[field])
        self.perform_fx_bypass = False
        self.processed_sound_cache.clear()

    def start_loop_bounce(self):
        with self.loop_lock:
            if self.bounce_processing or not self.loop_events:
                return False
            target = next(
                (index for index in range(self.selected_pad + 1, len(PADS)) if not self.custom_sample_files[index]),
                next((index for index in range(self.selected_pad) if not self.custom_sample_files[index]), None),
            )
            if target is None:
                self.set_surface_notice("No empty pad for Bounce")
                return False
            snapshot = self.loop_render_snapshot()
            self.bounce_processing = True
        self.bounce_thread = threading.Thread(
            target=self.bounce_loop_worker, args=(target, snapshot), name="DrumLoopBounce", daemon=True
        )
        self.bounce_thread.start()
        self.set_surface_notice("Bouncing loop")
        return True

    def bounce_loop_worker(self, target, snapshot):
        try:
            USER_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"bounce-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}.wav"
            self.render_loop_wav(USER_SAMPLE_DIR / filename, snapshot)
            self.bounce_results.put(("OK", target, filename))
        except Exception as exc:
            self.bounce_results.put(("ERROR", target, str(exc)))

    def poll_bounce_results(self):
        while True:
            try:
                result, target, value = self.bounce_results.get_nowait()
            except queue.Empty:
                return
            self.bounce_processing = False
            if result == "ERROR":
                self.set_surface_notice("Bounce failed")
                self.log(f"Bounce failed: {value}")
                continue
            try:
                sound = pygame.mixer.Sound(str(USER_SAMPLE_DIR / value))
                self.push_project_history()
                self.custom_sample_files[target] = value
                self.custom_sound_cache[value] = sound
                self.sample_edits[target] = default_sample_edit()
                self.selected_pad = target
                self.pad_selection = {target}
                self.persist_settings()
                self.set_surface_notice(f"Bounced to Pad {target + 1}")
            except pygame.error as exc:
                self.set_surface_notice("Bounce load failed")
                self.log(f"Bounce load failed: {exc}")

    def start_loop_export(self, export_kind):
        with self.loop_lock:
            if self.loop_exporting:
                self.log("Export already running")
                return
            if not self.loop_events:
                self.log("Nothing to export")
                return
            snapshot = self.loop_render_snapshot()
            self.loop_exporting = True

        self.export_thread = threading.Thread(
            target=self.export_loop_worker,
            args=(export_kind, snapshot),
            name=f"DrumExport{export_kind}",
            daemon=True,
        )
        self.export_thread.start()

    def wait_for_export(self):
        export_thread = self.export_thread
        if export_thread and export_thread.is_alive():
            export_thread.join(timeout=10.0)

    def export_loop_worker(self, export_kind, snapshot):
        try:
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
            if export_kind == "MIDI":
                path = EXPORT_DIR / f"drum-loop-{timestamp}.mid"
                path.write_bytes(self.export_midi_bytes(snapshot))
            elif export_kind == "STEMS":
                path = EXPORT_DIR / f"drum-stems-{timestamp}"
                self.export_stems(path, snapshot)
            elif export_kind == "BUNDLE":
                path = EXPORT_DIR / f"{snapshot['project_name']}-{timestamp}.zip"
                self.export_project_bundle(path, snapshot)
            else:
                path = EXPORT_DIR / f"drum-loop-{timestamp}.wav"
                self.render_loop_wav(path, snapshot)

            with self.loop_lock:
                self.last_export = path.name
            self.log(f"Exported {path.name}")
        except Exception as exc:
            self.status = f"Export failed: {exc}"
            self.log(self.status)
        finally:
            with self.loop_lock:
                self.loop_exporting = False

    def export_midi_bytes(self, snapshot):
        adjusted_events = [
            (
                beat,
                pad_index,
                max(1, min(127, round(velocity * snapshot["pad_sensitivity"][pad_index]))),
            )
            for beat, pad_index, velocity in realize_loop_events(
                snapshot["events"], snapshot.get("event_meta", {}), snapshot["bars"]
            )
        ]
        return build_midi_file(
            adjusted_events,
            snapshot["bars"],
            snapshot["bpm"],
            snapshot["pad_synths"],
        )

    def export_stems(self, directory, snapshot):
        directory.mkdir(parents=True, exist_ok=True)
        self.render_loop_wav(directory / "00-Master.wav", snapshot)
        for pad_index, pad in enumerate(PADS):
            safe_name = "".join(character if character.isalnum() else "-" for character in pad["name"]).strip("-")
            self.render_loop_wav(
                directory / f"{pad_index + 1:02d}-{safe_name}.wav",
                snapshot,
                pad_filter={pad_index},
            )
        (directory / "loop.mid").write_bytes(self.export_midi_bytes(snapshot))
        (directory / "tempo.txt").write_text(
            f"BPM={snapshot['bpm']}\nBARS={snapshot['bars']}\nRATE={MIXER_FREQUENCY}\n",
            encoding="ascii",
        )

    def export_project_bundle(self, path, snapshot):
        with tempfile.TemporaryDirectory(prefix="starrypad-") as temporary:
            root = Path(temporary)
            stems = root / "Stems"
            self.export_stems(stems, snapshot)
            project_name = f"{snapshot['project_name']}{PROJECT_EXTENSION}"
            (root / project_name).write_text(
                json.dumps(snapshot["project"], indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            sample_names = {
                name
                for profile in snapshot["project"].get("kits", {}).values()
                for name in profile.get("custom_samples", [])
                if name
            }
            for name in sample_names:
                source = self.custom_sample_path(name)
                if source.exists():
                    destination = root / "Samples" / Path(name).name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in sorted(root.rglob("*")):
                    if item.is_file():
                        archive.write(item, item.relative_to(root))

    def render_loop_wav(self, path, snapshot, pad_filter=None):
        import numpy

        total_seconds = snapshot["bars"] * 4 * 60.0 / snapshot["bpm"]
        total_frames = max(1, round(total_seconds * MIXER_FREQUENCY))
        mix = numpy.zeros((total_frames, 2), dtype=numpy.float32)
        quarter_frames = MIXER_FREQUENCY * 60.0 / snapshot["bpm"]
        events = realize_loop_events(
            snapshot["events"], snapshot.get("event_meta", {}), snapshot["bars"]
        )
        sample_edits = snapshot.get("sample_edits", [default_sample_edit() for _ in PADS])

        def event_choke(pad_index):
            custom_name = snapshot.get("custom_samples", [None] * len(PADS))[pad_index]
            if custom_name:
                custom_group = sanitize_sample_edit(sample_edits[pad_index]).get("choke_group")
                if custom_group:
                    return custom_group
            synth_name = snapshot["pad_synths"][pad_index]
            return next((layer.get("choke") for layer in KIT.get(synth_name, []) if layer.get("choke")), None)

        for event_index, (beat, pad_index, raw_velocity) in enumerate(events):
            if pad_filter is not None and pad_index not in pad_filter:
                continue
            if snapshot.get("pad_mute", [False] * len(PADS))[pad_index]:
                continue
            solo_pads = set(snapshot.get("solo_pads", []))
            if solo_pads and pad_index not in solo_pads:
                continue
            synth = snapshot["pad_synths"][pad_index]
            adjusted_velocity = max(
                1,
                min(127, round(raw_velocity * snapshot["pad_sensitivity"][pad_index])),
            )
            tier = velocity_tier(adjusted_velocity)
            curved = velocity_gain(adjusted_velocity)
            start_frame = round(beat * quarter_frames)
            custom_file = snapshot.get("custom_samples", [None] * len(PADS))[pad_index]
            custom_sound = self.custom_sound_cache.get(custom_file) if custom_file else None
            bypass = snapshot.get("mixer_bypass", False)
            pad_volume = 1.0 if bypass else snapshot.get("pad_volume", [1.0] * len(PADS))[pad_index]
            pan = 0.0 if bypass else snapshot.get("pad_pan", [0.0] * len(PADS))[pad_index]
            tune = 0 if bypass else snapshot.get("pad_tune", [0] * len(PADS))[pad_index]
            left, right = stereo_pan_gains(pan)
            punch = 0 if bypass else snapshot.get("pad_punch", [0] * len(PADS))[pad_index]
            air = 0 if bypass else snapshot.get("pad_air", [0] * len(PADS))[pad_index]
            space = 0 if bypass else snapshot.get("pad_space", [0] * len(PADS))[pad_index]
            if custom_sound is not None:
                samples = pygame.sndarray.array(custom_sound).astype(numpy.float32)
                if samples.ndim == 1:
                    samples = numpy.repeat(samples[:, None], 2, axis=1)
                edit = sample_edits[pad_index]
                if not bypass:
                    samples = apply_sample_edits(samples, edit).astype(numpy.float32)
                    samples = apply_sample_tempo(samples, edit, snapshot["bpm"]).astype(numpy.float32)
                    tune += sanitize_sample_edit(edit)["tune"]
                samples = apply_sound_macros(samples, punch, air, space).astype(numpy.float32)
                samples = pitch_shift_array(samples, tune).astype(numpy.float32)
                frame_count = min(len(samples), total_frames - start_frame)
                choke = event_choke(pad_index)
                if choke:
                    for next_beat, next_pad, _next_velocity in events[event_index + 1:]:
                        if event_choke(next_pad) == choke:
                            frame_count = min(frame_count, max(0, round(next_beat * quarter_frames) - start_frame))
                            break
                if frame_count > 0:
                    gain = min(1.0, snapshot["volume"] * curved * 1.12 * pad_volume)
                    stereo_gain = numpy.array((left, right), dtype=numpy.float32)
                    mix[start_frame:start_frame + frame_count] += samples[:frame_count, :2] * gain * stereo_gain
                continue
            for layer_index, layer in enumerate(KIT.get(synth, [])):
                files = layer_files_for_tier(layer, tier)
                file = files[(event_index + layer_index) % len(files)]
                sound = self.samples.get(file)
                if sound is None:
                    continue
                samples = pygame.sndarray.array(sound).astype(numpy.float32)
                if samples.ndim == 1:
                    samples = numpy.repeat(samples[:, None], 2, axis=1)
                variation_index = ((event_index + 1) * 7 + (layer_index + 1) * 3) % 3
                variation_tune = 0.0
                variation_gain = (0.985, 1.0, 1.015)[variation_index]
                samples = apply_sound_macros(samples, punch, air, space).astype(numpy.float32)
                samples = pitch_shift_array(
                    samples,
                    tune + int(layer.get("tune", 0)) + variation_tune,
                ).astype(numpy.float32)

                available = total_frames - start_frame
                if available <= 0:
                    continue
                frame_count = min(len(samples), available)
                duration_ms = int(layer.get("duration_ms", 0))
                if duration_ms > 0:
                    frame_count = min(frame_count, round(duration_ms * MIXER_FREQUENCY / 1000.0))

                choke = layer.get("choke")
                if choke:
                    for next_beat, next_pad, _next_velocity in events[event_index + 1:]:
                        if event_choke(next_pad) == choke:
                            choke_frame = round(next_beat * quarter_frames) - start_frame
                            frame_count = min(frame_count, max(0, choke_frame))
                            break
                if frame_count <= 0:
                    continue

                gain = min(
                    1.0,
                    snapshot["volume"]
                    * layer.get("gain", 1.0)
                    * curved
                    * VELOCITY_TIER_GAIN[tier]
                    * variation_gain
                    * pad_volume,
                )
                stereo_gain = numpy.array((left, right), dtype=numpy.float32)
                mix[start_frame:start_frame + frame_count] += samples[:frame_count, :2] * gain * stereo_gain

        perform_fx = snapshot.get("perform_fx", {}) if not snapshot.get("perform_fx_bypass", False) else {}
        automation = snapshot.get("perform_fx_events", []) if perform_fx else []
        mix = apply_perform_fx_automation(
            mix, automation, snapshot["bpm"], perform_fx
        ).astype(numpy.float32)
        output = apply_master_limiter(mix).astype(numpy.int16)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(MIXER_FREQUENCY)
            wav_file.writeframes(output.tobytes())

    def queue_pad(self, index, velocity):
        self.audio_events.put(("PAD", index, velocity, time.perf_counter_ns()))

    def toggle_repeat(self):
        self.repeat_enabled = not self.repeat_enabled
        self.audio_events.put(("CONFIG",))
        self.persist_settings()
        self.log(f"Repeat: {'On' if self.repeat_enabled else 'Off'}")

    def cycle_repeat_rate(self):
        rates = tuple(REPEAT_RATES)
        current = rates.index(self.repeat_rate)
        self.repeat_rate = rates[(current + 1) % len(rates)]
        self.audio_events.put(("TIMING",))
        self.persist_settings()
        self.log(f"Repeat rate: {self.repeat_rate}")

    def cycle_record_start_mode(self):
        current = RECORD_START_MODES.index(self.record_start_mode)
        self.record_start_mode = RECORD_START_MODES[(current + 1) % len(RECORD_START_MODES)]
        self.persist_settings()
        self.log(f"Record start: {self.record_start_mode}")

    def toggle_metronome(self):
        self.metronome_enabled = not self.metronome_enabled
        self.next_metronome_ns = None
        self.audio_events.put(("CONFIG",))
        self.persist_settings()
        self.log(f"Metronome: {'On' if self.metronome_enabled else 'Off'}")

    def adjust_bpm(self, amount):
        if self.clock_active_source == "External":
            return
        self.push_project_history()
        self.bpm = max(BPM_MIN, min(BPM_MAX, self.bpm + amount))
        self.audio_events.put(("TIMING",))
        self.persist_settings()

    def tap_tempo(self):
        if self.clock_active_source == "External":
            return
        now = time.perf_counter()
        if self.tap_times and now - self.tap_times[-1] > 2.0:
            self.tap_times.clear()
        self.tap_times.append(now)
        if len(self.tap_times) >= 2:
            intervals = [
                later - earlier
                for earlier, later in zip(self.tap_times, list(self.tap_times)[1:])
            ]
            average = sum(intervals) / len(intervals)
            self.push_project_history()
            self.bpm = max(BPM_MIN, min(BPM_MAX, round(60.0 / average)))
            self.audio_events.put(("TIMING",))
            self.persist_settings()

    def handle_midi_trigger(self, kind, number, velocity, received_ns=None):
        # Recorded before any mapping decision: this asks whether the port is
        # alive, not whether the message landed on a pad.
        self.last_midi_event_ns = received_ns or time.perf_counter_ns()
        if kind == "CC" and number == 4 and MAPPING_MODES[self.mapping_mode] == "GM Drums":
            self.hat_openness = max(0.0, min(1.0, velocity / 127.0))
            return
        if kind == "N" and self.chop_lazy_active:
            self.add_lazy_chop_marker()
            return
        key = (kind, number)
        with self.mapping_lock:
            index = self.resolve_preset_pad(kind, number)
            if index is None and MAPPING_MODES[self.mapping_mode] == "Learn":
                index = self.assign_trigger(key)

        if index is None:
            with self.metrics_lock:
                self.ignored_event_count += 1
            return

        now_ns = received_ns or time.perf_counter_ns()
        if self.calibration_active and index == self.calibration_pad:
            if self.calibration_last_raw_ns is not None:
                raw_interval_ms = (now_ns - self.calibration_last_raw_ns) / 1_000_000.0
                if 0 < raw_interval_ms < 35:
                    self.calibration_duplicate_ms.append(raw_interval_ms)
                    self.calibration_last_raw_ns = now_ns
                    with self.metrics_lock:
                        self.ignored_event_count += 1
                    return
            self.calibration_last_raw_ns = now_ns
        dead_time_ns = self.pad_calibrations[index]["dead_time_ms"] * 1_000_000
        if dead_time_ns and now_ns - self.last_pad_trigger_ns[index] < dead_time_ns:
            with self.metrics_lock:
                self.ignored_event_count += 1
            return
        self.last_pad_trigger_ns[index] = now_ns
        self.collect_calibration_hit(index, velocity)
        self.add_sequence_step_from_pad(index, velocity, now_ns)

        self.record_performance_hit(index, velocity, received_ns)
        self.record_loop_hit(index, velocity, received_ns)
        self.play_pad(index, velocity, f"{kind}{number}")
        self.record_trigger_latency(received_ns)
        if kind == "N" and self.repeat_enabled:
            interval_ns = int(repeat_interval_seconds(self.repeat_rate, self.bpm) * 1_000_000_000)
            self.held_triggers[key] = {
                "index": index,
                "velocity": velocity,
                "label": f"{kind}{number} Repeat",
                "next_ns": time.perf_counter_ns() + interval_ns,
            }

    def handle_midi_clock(self, status, received_ns):
        if self.clock_source == "Internal":
            return
        received_ns = int(received_ns) + self.clock_correction_ms * 1_000_000
        self.clock_active_source = "External"
        if status == 0xFA:  # Start
            self.external_clock_ticks = 0
            self.external_transport_running = True
            self.last_midi_clock_ns = None
            with self.loop_lock:
                self.loop_playing = bool(self.loop_events)
                self.loop_recording = False
                self.loop_overdub = False
                self.loop_start_ns = received_ns if self.loop_playing else None
                self.loop_cycle_index = 0
                self.loop_schedule_bpm = self.bpm
                if self.loop_playing:
                    self.rebuild_loop_pending_locked(received_ns)
            return
        if status == 0xFB:  # Continue
            self.external_transport_running = True
            if self.loop_events and not self.loop_playing:
                with self.loop_lock:
                    self.loop_playing = True
                    self.loop_start_ns = received_ns
                    self.rebuild_loop_pending_locked(received_ns)
            return
        if status == 0xFC:  # Stop
            self.external_transport_running = False
            with self.loop_lock:
                self.loop_playing = False
                self.loop_recording = False
                self.loop_overdub = False
                self.loop_start_ns = None
                self.loop_pending.clear()
            return
        if status != 0xF8:
            return
        if self.last_midi_clock_ns is not None:
            interval = received_ns - self.last_midi_clock_ns
            if 1_000_000 <= interval <= 100_000_000:
                self.clock_intervals_ns.append(interval)
        self.last_midi_clock_ns = received_ns
        if len(self.clock_intervals_ns) >= 6:
            detected = midi_clock_bpm(self.clock_intervals_ns)
            if detected is not None and abs(detected - self.bpm) >= 0.25:
                self.bpm = round(detected)
                self.loop_schedule_bpm = self.bpm
        if not self.external_transport_running:
            return
        self.external_clock_ticks += 1
        if self.loop_playing:
            self.process_pending_record(received_ns)
            self.process_pattern_switch(received_ns)
            self.process_loop_scheduler(received_ns)
            if self.external_clock_ticks % 24 == 0:
                with self.loop_lock:
                    quarter_ns = round(60_000_000_000 / self.bpm)
                    elapsed_beats = self.external_clock_ticks / 24.0
                    self.loop_start_ns = received_ns - round(elapsed_beats * quarter_ns)

    def handle_midi_release(self, kind, number):
        self.held_triggers.pop((kind, number), None)
        index = self.resolve_preset_pad(kind, number)
        if index is None:
            return
        edit = self.sample_edits[index]
        if edit["mode"] not in ("Gate", "Loop"):
            return
        channel = self.sample_channels.pop(index, None)
        if channel is not None:
            channel.fadeout(edit["release_ms"])

    def resolve_preset_pad(self, kind, number):
        mode = MAPPING_MODES[self.mapping_mode]
        if mode == "Learn":
            return self.assignments.get((kind, number))

        if kind == "N":
            if mode == "GM Drums":
                return GM_NOTE_TO_PAD.get(number)
            if 20 <= number <= 35:
                return number - 20
            if 36 <= number <= 51:
                return number - 36
            if 48 <= number <= 63 and mode == "DONNER Mini":
                return number - 48
            return None

        if kind == "CC" and mode == "DONNER Mini":
            if 20 <= number <= 35:
                return number - 20
            if 36 <= number <= 51:
                return number - 36
            return None

        return None

    def assign_trigger(self, key):
        if key in self.assignments:
            return self.assignments[key]
        try:
            index = self.pad_notes.index(None)
        except ValueError:
            index = key[1] % len(PADS)
            old_key = self.pad_notes[index]
            if old_key in self.assignments:
                del self.assignments[old_key]
        self.pad_notes[index] = key
        self.assignments[key] = index
        self.log(f"Map {key[0]}{key[1]} -> {PADS[index]['name']}")
        return index

    def record_trigger_latency(self, received_ns):
        if received_ns is None:
            return
        latency_ms = max(0.0, (time.perf_counter_ns() - received_ns) / 1_000_000.0)
        with self.metrics_lock:
            self.trigger_latencies.append(latency_ms)
            self.trigger_count += 1

    def diagnostic_snapshot(self):
        with self.metrics_lock:
            samples = sorted(self.trigger_latencies)
            hit_count = self.trigger_count
            ignored = self.ignored_event_count
            errors = self.audio_error_count
            queue_depth = self.max_queue_depth

        if not samples:
            return 0.0, 0.0, hit_count, ignored, errors, queue_depth
        p95 = samples[round((len(samples) - 1) * 0.95)]
        p99 = samples[round((len(samples) - 1) * 0.99)]
        return p95, p99, hit_count, ignored, errors, queue_depth

    def play_pad(self, index, velocity, midi_note):
        if index < 0 or index >= len(PADS):
            return

        synth = self.pad_synths[index]
        custom_file = self.custom_sample_files[index]
        custom_source = self.custom_sound_cache.get(custom_file) if custom_file else None
        custom_sound = self.edited_custom_sound(index, self.sample_edit_bypass) if custom_source else None
        if not custom_sound and index == PAD_NAME_TO_INDEX["Closed Hat"] and str(midi_note).startswith("N42"):
            if self.hat_openness >= 0.67:
                synth = HAT_OPEN_PAIRS.get(synth, "open_hat")
            elif self.hat_openness >= 0.25:
                synth = "hat_semi"
        display_name = "Sample" if custom_sound else SYNTH_LABELS[synth]
        mapped_velocity = calibrated_velocity(velocity, self.pad_calibrations[index])
        adjusted_velocity = max(
            1,
            min(127, round(mapped_velocity * self.pad_sensitivity[index])),
        )
        curved = velocity_gain(adjusted_velocity)
        audible = not self.pad_mute[index] and (not self.solo_pads or index in self.solo_pads)
        if audible:
            pad_volume = 1.0 if self.mixer_bypass else self.pad_volume[index]
            pan = 0.0 if self.mixer_bypass else self.pad_pan[index]
            tune = 0 if self.mixer_bypass else self.pad_tune[index]
            if custom_sound:
                custom_sound = self.processed_sound(custom_sound, index, self.mixer_bypass)
                edit = default_sample_edit() if self.sample_edit_bypass else self.sample_edits[index]
                self.play_custom_sample(
                    custom_sound, synth, curved, pad_volume, pan,
                    tune + edit["tune"], edit["mode"], edit["release_ms"], index,
                    edit.get("choke_group"),
                )
            else:
                for layer in KIT.get(synth, []):
                    self.play_layer(layer, curved, adjusted_velocity, pad_volume, pan, tune, index)

        now = time.perf_counter()
        with self.state_lock:
            # A harder hit holds longer and glows brighter, so the panel
            # carries the dynamics the pad LEDs cannot.
            energy = max(0.0, min(1.0, adjusted_velocity / 127.0))
            self.hit_energy[index] = energy
            self.hit_until[index] = now + HIT_FLASH_MIN + HIT_FLASH_RANGE * energy
            self.last_hit = display_name
            self.last_velocity_value = adjusted_velocity
            if midi_note is None:
                self.last_velocity = f"vel {velocity} -> {adjusted_velocity}"
            else:
                self.last_velocity = f"note {midi_note} vel {velocity} -> {adjusted_velocity}"

    def play_custom_sample(
        self, sound, synth, velocity, pad_volume=1.0, pan=0.0, tune=0,
        mode="One-shot", release_ms=8, pad_index=None, custom_choke=None,
    ):
        choke = custom_choke or next(
            (layer.get("choke") for layer in KIT.get(synth, []) if layer.get("choke")),
            None,
        )
        if choke:
            self.stop_choke(choke)
        existing = self.sample_channels.get(pad_index) if pad_index is not None else None
        if mode == "Toggle" and existing is not None and existing.get_busy():
            existing.fadeout(release_ms)
            self.sample_channels.pop(pad_index, None)
            return
        channel = pygame.mixer.find_channel(True)
        if channel is None:
            return
        left, right = stereo_pan_gains(pan)
        volume = min(1.0, self.volume * velocity * 1.12 * pad_volume)
        self.track_master_peak(volume)
        channel.set_volume(volume * left, volume * right)
        channel.play(self.tuned_sound(sound, tune), loops=-1 if mode in ("Toggle", "Loop") else 0)
        if pad_index is not None and mode in ("Gate", "Toggle", "Loop"):
            self.sample_channels[pad_index] = channel
        if choke:
            self.chokes.setdefault(choke, []).append(channel)

    def play_layer(self, layer, velocity, raw_velocity, pad_volume=1.0, pan=0.0, tune=0, pad_index=None):
        choke = layer.get("choke")
        if choke:
            self.stop_choke(choke)

        for tier, weight in velocity_layer_mix(raw_velocity):
            if weight <= 0:
                continue
            files = layer_files_for_tier(layer, tier)
            history_key = (id(layer), tier)
            file = choose_nonrepeating_sample(files, self.sample_choice_history.get(history_key))
            self.sample_choice_history[history_key] = file
            sound = self.samples.get(file)
            if not sound:
                continue
            if pad_index is not None:
                sound = self.processed_sound(sound, pad_index, self.mixer_bypass)
            channel = pygame.mixer.find_channel(True)
            if channel is None:
                continue
            volume = min(
                1.0,
                self.volume
                * layer.get("gain", 1.0)
                * velocity
                * VELOCITY_TIER_GAIN[tier]
                * random.choice((0.985, 1.0, 1.015))
                * (weight ** 0.5)
                * pad_volume,
            )
            left, right = stereo_pan_gains(pan)
            channel.set_volume(volume * left, volume * right)
            self.track_master_peak(volume)
            maxtime = int(layer.get("duration_ms", 0))
            layer_tune = tune + int(layer.get("tune", 0))
            if maxtime > 0:
                channel.play(self.tuned_sound(sound, layer_tune), maxtime=maxtime)
            else:
                channel.play(self.tuned_sound(sound, layer_tune))
            if choke:
                self.chokes.setdefault(choke, []).append(channel)

    def stop_choke(self, group):
        channels = self.chokes.get(group)
        if not channels:
            return
        for channel in channels:
            channel.stop()
        channels.clear()

    def stop_all_sounds(self):
        self.held_triggers.clear()
        self.chokes.clear()
        try:
            pygame.mixer.stop()
        except pygame.error:
            pass

    def pad_rects(self):
        rects = {}
        left = 24
        top = 74
        width = 154
        height = 142
        gap = 12
        for index in range(len(PADS)):
            row = 3 - (index // 4)
            col = index % 4
            rects[index] = pygame.Rect(left + col * (width + gap), top + row * (height + gap), width, height)
        return rects
    def log(self, text):
        with self.log_lock:
            self.logs.insert(0, f"{time.strftime('%H:%M:%S')}  {text}")
            del self.logs[12:]

    def set_surface_notice(self, text, duration=2.4):
        with self.state_lock:
            self.surface_notice = str(text)
            self.surface_notice_until = time.perf_counter() + duration

    def trigger_label(self, value):
        if value is None:
            return "--"
        if isinstance(value, tuple):
            return f"{value[0]}{value[1]}"
        return str(value)

    def draw(self):
        self.buttons = {}
        self.settings_buttons = {}
        self.project_buttons = {}
        self.feel_buttons = {}
        self.scene_buttons = {}
        self.mixer_buttons = {}
        self.share_buttons = {}
        self.perform_fx_buttons = {}
        self.sample_editor_buttons = {}
        self.chop_buttons = {}
        self.browser_buttons = {}
        self.clip_prompt_buttons = {}
        self.screen.fill(theme.GROUND)
        if self.grain:
            self.screen.blit(self.grain, (0, 0))
        self.draw_header()
        if self.view_mode == "Sequence":
            self.draw_sequence_view()
        else:
            self.draw_pads()
            self.draw_loop_timeline()
            self.draw_side_panel()
            self.draw_pattern_strip(compact=True)
        if self.browser_open:
            self.draw_sample_browser()
        if self.settings_open:
            self.draw_settings_overlay()
        if self.project_menu_open:
            self.draw_project_overlay()
        if self.feel_open:
            self.draw_feel_overlay()
        if self.scene_open:
            self.draw_scene_overlay()
        if self.mixer_open:
            self.draw_mixer_overlay()
        if self.perform_fx_open:
            self.draw_perform_fx_overlay()
        if self.share_open:
            self.draw_share_overlay()
        if self.sample_editor_open:
            self.draw_sample_editor_overlay()
        if self.chop_open:
            self.draw_chop_overlay()
        if self.clip_prompt_open:
            self.draw_clip_prompt()
        self.draw_keyboard_focus()
        self.draw_tooltip()
        self.present_screen()

    def draw_keyboard_focus(self):
        if not self.keyboard_focus_name:
            return
        rect = dict(self.focusable_controls()).get(self.keyboard_focus_name)
        if rect is None:
            self.keyboard_focus_name = None
            return
        pygame.draw.rect(self.screen, theme.ACCENT, rect.inflate(6, 6), width=2, border_radius=10)

    def draw_tooltip(self):
        labels = {
            "vol_down": "Lower master volume", "vol_up": "Raise master volume",
            "sound_prev": "Previous sound", "sound_next": "Next sound",
            "sens_down": "Lower sensitivity", "sens_up": "Raise sensitivity",
            "bpm_down": "Lower tempo", "bpm_up": "Raise tempo",
            "sequence_page_prev": "Previous bar", "sequence_page_next": "Next bar",
            "sequence_nudge_left": "Move hit earlier", "sequence_nudge_right": "Move hit later",
            "mixer_close": "Close mixer", "perform_fx_close": "Close effects",
            # The shortcuts were documented only in the README until now.
            "loop_record": "Record loop  (L)", "loop_play": "Play or stop loop  (Space)",
            "loop_overdub": "Overdub  (O)", "loop_capture": "Capture last performance  (C)",
            "loop_undo": "Undo  (U)", "loop_redo": "Redo  (Y)",
            "loop_quantize": "Feel and quantize  (Q)", "loop_clear": "Clear the loop",
            "loop_bars": "Loop length", "repeat": "Note repeat  (N)",
            "repeat_rate": "Repeat subdivision", "metro": "Metronome  (M)",
            "tap": "Tap tempo", "kit": "Switch kit", "settings": "Settings",
            "project": "Projects: new, open, save as", "browser": "Browse sounds",
            "mixer": "Pad mixer", "perform_fx": "Perform FX", "share": "Share and export",
            "sample_edit": "Edit the sample", "sample_clear": "Back to the kit sound",
            "view_perform": "Play the pads", "view_sequence": "Step sequencer",
            "reconnect": "Reconnect MIDI and audio",
        }
        if not self.audio_inputs_available:
            labels["sample"] = "No audio input connected"
        else:
            labels["sample"] = "Record a sample  (S)"
        collections_to_scan = (self.buttons, self.mixer_buttons, self.perform_fx_buttons)
        hovered = next(
            ((name, rect) for collection in collections_to_scan for name, rect in collection.items()
             if name in labels and rect.collidepoint(self.mouse_logical)),
            None,
        )
        key = hovered[0] if hovered else None
        now = time.perf_counter()
        if key != self.tooltip_key:
            self.tooltip_key = key
            self.tooltip_since = now
            return
        if not hovered or now - self.tooltip_since < 0.65:
            return
        text_surface = self.small_font.render(labels[key], True, theme.INK)
        rect = text_surface.get_rect()
        rect.inflate_ip(16, 10)
        rect.topleft = (min(WINDOW_SIZE[0] - rect.width - 8, self.mouse_logical[0] + 12),
                        min(WINDOW_SIZE[1] - rect.height - 8, self.mouse_logical[1] + 14))
        pygame.draw.rect(self.screen, theme.PANEL_2, rect, border_radius=5)
        pygame.draw.rect(self.screen, theme.RULE, rect, width=1, border_radius=5)
        self.screen.blit(text_surface, text_surface.get_rect(center=rect.center))

    def draw_header(self):
        """A 52px status strip: what is connected, what is loaded, how loud."""
        bar = pygame.Rect(0, 0, WINDOW_SIZE[0], 52)
        pygame.draw.rect(self.screen, theme.PANEL, bar)
        pygame.draw.line(self.screen, theme.RULE, (0, 52), (WINDOW_SIZE[0], 52))

        mark = self.label_font.render("Starrypad", True, theme.INK)
        self.screen.blit(mark, (24, 26 - mark.get_height() // 2))

        connected = self.midi_input is not None
        audio_ready = bool(pygame.mixer.get_init())
        if connected and audio_ready:
            name, dot, _hint = self.midi_activity()
            self.draw_chip(pygame.Rect(132, 13, 186, 26), name, dot)
        else:
            self.buttons["reconnect"] = pygame.Rect(132, 12, 118, 28)
            self.draw_button(self.buttons["reconnect"], "Reconnect", danger=True)

        self.buttons["project"] = pygame.Rect(330, 12, 170, 28)
        self.draw_button(self.buttons["project"], self.project_name[:22])

        self.buttons["view_perform"] = pygame.Rect(512, 12, 78, 28)
        self.buttons["view_sequence"] = pygame.Rect(594, 12, 84, 28)
        self.draw_button(self.buttons["view_perform"], "Perform", active=self.view_mode == "Perform")
        self.draw_button(self.buttons["view_sequence"], "Sequence", active=self.view_mode == "Sequence")

        peaking = time.perf_counter() < self.master_peak_warning_until
        if peaking:
            peak = self.data_font_sm.render("PEAK", True, theme.DANGER)
            self.screen.blit(peak, (694, 26 - peak.get_height() // 2))

        self.buttons["vol_down"] = pygame.Rect(748, 12, 26, 28)
        self.buttons["vol_up"] = pygame.Rect(824, 12, 26, 28)
        self.draw_button(self.buttons["vol_down"], "-")
        self.draw_button(self.buttons["vol_up"], "+")
        volume_track = pygame.Rect(780, 23, 38, 6)
        pygame.draw.rect(self.screen, theme.RULE, volume_track, border_radius=3)
        pygame.draw.rect(
            self.screen,
            theme.DANGER if peaking else theme.SIGNAL,
            pygame.Rect(volume_track.x, volume_track.y, round(volume_track.width * self.volume), volume_track.height),
            border_radius=3,
        )

        self.buttons["kit"] = pygame.Rect(858, 12, 62, 28)
        self.buttons["settings"] = pygame.Rect(928, 12, 88, 28)
        self.draw_button(self.buttons["kit"], f"Kit {self.active_kit}")
        self.draw_button(self.buttons["settings"], "Settings", icon="gear")
    def midi_activity(self):
        """Chip text and dot colour for the open MIDI port.

        A device that splits into several endpoints will happily open a port
        that never sends a note, which otherwise looks identical to a working
        one. Silence for a few seconds after opening says so out loud.
        """
        name = self.midi_device_name or "MIDI in"
        if not self.last_midi_event_ns:
            silent_for = time.perf_counter() - self.midi_opened_at
            if self.midi_opened_at and silent_for > MIDI_SILENCE_HINT_SECONDS:
                return name, theme.DANGER, "No MIDI data on this port - switch Device"
            return name, theme.INK_3, ""
        since_ms = (time.perf_counter_ns() - self.last_midi_event_ns) / 1_000_000.0
        return name, theme.ACCENT if since_ms < 180 else theme.SIGNAL, ""

    def draw_project_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 150, 500, 520)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.font.render(self.project_name[:32], True, theme.INK), (294, 178))
        self.project_buttons["project_close"] = pygame.Rect(718, 164, 30, 30)
        self.draw_button(self.project_buttons["project_close"], "", icon="close")

        actions = (
            ("project_new", "New", 294),
            ("project_open", "Open", 404),
            ("project_save_as", "Save As", 514),
            ("project_collect", "Collect", 624),
        )
        for name, label, x in actions:
            self.project_buttons[name] = pygame.Rect(x, 232, 100, 38)
            self.draw_button(self.project_buttons[name], label)

        self.project_buttons["project_undo"] = pygame.Rect(294, 286, 100, 36)
        self.project_buttons["project_redo"] = pygame.Rect(404, 286, 100, 36)
        self.draw_button(self.project_buttons["project_undo"], "Undo", enabled=bool(self.project_history))
        self.draw_button(self.project_buttons["project_redo"], "Redo", enabled=bool(self.project_redo))

        self.screen.blit(self.small_font.render("Recent", True, theme.INK_2), (294, 346))
        for index, value in enumerate(self.recent_projects[:5]):
            path = Path(value)
            y = 374 + index * 52
            self.project_buttons[f"recent_{index}"] = pygame.Rect(294, y, 430, 40)
            self.draw_button(self.project_buttons[f"recent_{index}"], path.name.removesuffix(PROJECT_EXTENSION)[:38])

    def draw_sequence_view(self):
        self.sequence_bar_page = max(0, min(self.loop_bars - 1, self.sequence_bar_page))
        loop = self.loop_snapshot()
        self.buttons["sequence_play"] = pygame.Rect(34, 92, 68, 34)
        self.buttons["sequence_undo"] = pygame.Rect(108, 92, 62, 34)
        self.buttons["sequence_redo"] = pygame.Rect(176, 92, 62, 34)
        self.buttons["sequence_clear"] = pygame.Rect(244, 92, 62, 34)
        self.buttons["sequence_bars"] = pygame.Rect(312, 92, 54, 34)
        self.buttons["sequence_page_prev"] = pygame.Rect(382, 92, 34, 34)
        self.buttons["sequence_page_next"] = pygame.Rect(512, 92, 34, 34)
        self.draw_button(self.buttons["sequence_play"], "Stop" if loop["playing"] else "Play", active=loop["playing"])
        self.draw_button(self.buttons["sequence_undo"], "Undo", enabled=loop["can_undo"])
        self.draw_button(self.buttons["sequence_redo"], "Redo", enabled=loop["can_redo"])
        self.draw_button(self.buttons["sequence_clear"], "Clear", enabled=bool(loop["events"]))
        self.draw_button(self.buttons["sequence_bars"], f"{self.loop_bars}B")
        self.draw_button(self.buttons["sequence_page_prev"], "<")
        self.draw_button(self.buttons["sequence_page_next"], ">")
        page_text = self.small_font.render(f"Bar {self.sequence_bar_page + 1}/{self.loop_bars}", True, theme.INK)
        self.screen.blit(page_text, page_text.get_rect(center=(464, 109)))

        selection_count = len(self.sequence_selection) or (1 if self.sequence_selected else 0)
        velocity_label = "Velocity" if selection_count <= 1 else f"Velocity ({selection_count})"
        self.screen.blit(self.small_font.render(velocity_label, True, theme.INK_2), (570, 101))
        self.buttons["sequence_velocity_down"] = pygame.Rect(644, 92, 34, 34)
        self.buttons["sequence_velocity_up"] = pygame.Rect(744, 92, 34, 34)
        self.draw_button(self.buttons["sequence_velocity_down"], "-", enabled=self.sequence_selected is not None)
        self.draw_button(self.buttons["sequence_velocity_up"], "+", enabled=self.sequence_selected is not None)
        velocity_text = self.small_font.render(str(self.sequence_velocity), True, theme.INK)
        self.screen.blit(velocity_text, velocity_text.get_rect(center=(711, 109)))
        self.buttons["sequence_nudge_left"] = pygame.Rect(794, 92, 34, 34)
        self.buttons["sequence_nudge_right"] = pygame.Rect(834, 92, 34, 34)
        self.buttons["sequence_copy"] = pygame.Rect(884, 92, 76, 34)
        self.draw_button(self.buttons["sequence_nudge_left"], "<", enabled=self.sequence_selected is not None)
        self.draw_button(self.buttons["sequence_nudge_right"], ">", enabled=self.sequence_selected is not None)
        self.draw_button(self.buttons["sequence_copy"], "Copy", enabled=self.sequence_selected is not None)

        self.buttons["sequence_step_input"] = pygame.Rect(34, 132, 104, 34)
        self.buttons["sequence_cursor_prev"] = pygame.Rect(146, 132, 34, 34)
        self.buttons["sequence_cursor_next"] = pygame.Rect(276, 132, 34, 34)
        self.draw_button(self.buttons["sequence_step_input"], "Step Input", active=self.sequence_step_input)
        self.draw_button(self.buttons["sequence_cursor_prev"], "<")
        self.draw_button(self.buttons["sequence_cursor_next"], ">")
        cursor_text = self.small_font.render(f"Step {self.sequence_step_cursor + 1}", True, theme.INK)
        self.screen.blit(cursor_text, cursor_text.get_rect(center=(228, 149)))

        chance, ratchet = self.selected_sequence_meta()
        self.screen.blit(self.small_font.render("Chance", True, theme.INK_2), (570, 141))
        self.buttons["sequence_chance_down"] = pygame.Rect(632, 132, 34, 34)
        self.buttons["sequence_chance_up"] = pygame.Rect(724, 132, 34, 34)
        self.draw_button(self.buttons["sequence_chance_down"], "-", enabled=self.sequence_selected is not None)
        self.draw_button(self.buttons["sequence_chance_up"], "+", enabled=self.sequence_selected is not None)
        chance_text = self.small_font.render(f"{chance}%", True, theme.INK)
        self.screen.blit(chance_text, chance_text.get_rect(center=(695, 149)))
        self.screen.blit(self.small_font.render("Ratchet", True, theme.INK_2), (780, 141))
        self.buttons["sequence_ratchet_down"] = pygame.Rect(842, 132, 34, 34)
        self.buttons["sequence_ratchet_up"] = pygame.Rect(934, 132, 34, 34)
        self.draw_button(self.buttons["sequence_ratchet_down"], "-", enabled=self.sequence_selected is not None)
        self.draw_button(self.buttons["sequence_ratchet_up"], "+", enabled=self.sequence_selected is not None)
        ratchet_text = self.small_font.render(f"x{ratchet}", True, theme.INK)
        self.screen.blit(ratchet_text, ratchet_text.get_rect(center=(905, 149)))

        grid_left = 160
        grid_top = 178
        grid_width = 846
        row_height = 36
        cell_width = grid_width / 16.0
        self.sequence_cells = {}
        with self.loop_lock:
            events = list(self.loop_events)
            selection = set(self.sequence_selection)
        page_start = self.sequence_bar_page * 4.0
        for pad_index in range(len(PADS)):
            y = grid_top + pad_index * row_height
            label = self.small_font.render(PADS[pad_index]["name"], True, theme.INK_2)
            self.screen.blit(label, (34, y + 8))
            for step in range(16):
                x0 = round(grid_left + step * cell_width)
                x1 = round(grid_left + (step + 1) * cell_width)
                rect = pygame.Rect(x0, y, max(1, x1 - x0 - 2), row_height - 3)
                self.sequence_cells[(pad_index, step)] = rect
                beat_start = page_start + step * 0.25 - 0.125
                beat_end = beat_start + 0.25
                cell_events = [event for event in events if event[1] == pad_index and beat_start <= event[0] < beat_end]
                if cell_events:
                    velocity = max(event[2] for event in cell_events)
                    base = PADS[pad_index]["color"]
                    ratio = 0.4 + 0.6 * velocity / 127.0
                    fill = tuple(max(0, min(255, round(component * ratio))) for component in base)
                else:
                    fill = theme.PANEL_2 if step % 4 else theme.PANEL
                pygame.draw.rect(self.screen, fill, rect, border_radius=3)
                pygame.draw.rect(self.screen, theme.RULE, rect, width=1, border_radius=3)
                if cell_events and any((pad_index, round(event[0], 6)) in selection for event in cell_events):
                    pygame.draw.rect(self.screen, theme.ACCENT, rect, width=3, border_radius=3)

        if self.sequence_step_input:
            cursor_x0 = round(grid_left + self.sequence_step_cursor * cell_width)
            cursor_x1 = round(grid_left + (self.sequence_step_cursor + 1) * cell_width)
            pygame.draw.rect(
                self.screen,
                theme.SIGNAL,
                pygame.Rect(cursor_x0, grid_top, cursor_x1 - cursor_x0 - 2, 16 * row_height - 3),
                width=2,
                border_radius=3,
            )

        if loop["playing"] and self.loop_start_ns is not None:
            quarter_ns = int((60.0 / self.loop_schedule_bpm) * 1_000_000_000)
            phase = ((time.perf_counter_ns() - self.loop_start_ns) / quarter_ns) % (self.loop_bars * 4.0)
            if page_start <= phase < page_start + 4.0:
                x = round(grid_left + ((phase - page_start) / 4.0) * grid_width)
                pygame.draw.line(self.screen, theme.DANGER, (x, grid_top), (x, grid_top + 16 * row_height - 3), 2)
        self.draw_pattern_strip(compact=False)

    def draw_pattern_strip(self, compact=False):
        y = 778 if compact else 774
        start_x = 24 if compact else 160
        width = 48
        gap = 4
        for index in range(PATTERN_COUNT):
            rect = pygame.Rect(start_x + index * (width + gap), y, width, 32)
            self.buttons[f"pattern_{index}"] = rect
            self.draw_button(
                rect,
                f"P{index + 1}",
                active=index == self.active_pattern,
                danger=index == self.pending_pattern,
            )
            if self.patterns[index] is not None and index not in (self.active_pattern, self.pending_pattern):
                pygame.draw.circle(self.screen, theme.SIGNAL, (rect.right - 7, rect.top + 7), 3)
        if compact:
            self.buttons["pattern_launch"] = pygame.Rect(458, y, 146, 32)
            self.buttons["pattern_scenes"] = pygame.Rect(610, y, 74, 32)
            self.draw_button(self.buttons["pattern_launch"], self.pattern_launch_mode)
            self.draw_button(self.buttons["pattern_scenes"], "Scenes", active=self.song_playing)
        else:
            self.buttons["pattern_duplicate"] = pygame.Rect(584, y, 78, 32)
            self.buttons["pattern_double"] = pygame.Rect(668, y, 72, 32)
            self.buttons["pattern_launch"] = pygame.Rect(746, y, 126, 32)
            self.buttons["pattern_scenes"] = pygame.Rect(878, y, 90, 32)
            self.draw_button(self.buttons["pattern_duplicate"], "Duplicate")
            self.draw_button(self.buttons["pattern_double"], "Double", enabled=self.loop_bars < 4)
            self.draw_button(self.buttons["pattern_launch"], self.pattern_launch_mode)
            self.draw_button(self.buttons["pattern_scenes"], "Scenes", active=self.song_playing)

    def draw_scene_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 120, 500, 580)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Scenes", True, theme.INK), (294, 154))
        self.scene_buttons["scene_close"] = pygame.Rect(718, 134, 30, 30)
        self.draw_button(self.scene_buttons["scene_close"], "", icon="close")
        self.screen.blit(self.small_font.render("Add pattern", True, theme.INK_2), (294, 222))
        for index in range(PATTERN_COUNT):
            rect = pygame.Rect(294 + (index % 4) * 108, 248 + (index // 4) * 48, 98, 36)
            self.scene_buttons[f"scene_add_{index}"] = rect
            self.draw_button(rect, f"Pattern {index + 1}", enabled=self.patterns[index] is not None)

        self.screen.blit(self.small_font.render("Song order", True, theme.INK_2), (294, 370))
        order = self.scene_order[:24]
        for index, pattern_index in enumerate(order):
            x = 294 + (index % 8) * 54
            y = 398 + (index // 8) * 42
            active = self.song_playing and index == self.scene_position
            rect = pygame.Rect(x, y, 48, 32)
            self.draw_button(rect, str(pattern_index + 1), active=active)
        if not order:
            self.screen.blit(self.small_font.render("--", True, theme.INK_2), (294, 406))

        self.scene_buttons["scene_play"] = pygame.Rect(294, 562, 120, 38)
        self.scene_buttons["scene_remove"] = pygame.Rect(424, 562, 120, 38)
        self.draw_button(self.scene_buttons["scene_play"], "Stop Song" if self.song_playing else "Play Song", active=self.song_playing, enabled=bool(self.scene_order))
        self.draw_button(self.scene_buttons["scene_remove"], "Remove Last", enabled=bool(self.scene_order))

    def draw_perform_fx_overlay(self):
        self.perform_fx_buttons.clear()
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 120, 500, 580)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Perform FX", True, theme.INK), (294, 154))
        self.perform_fx_buttons["perform_fx_close"] = pygame.Rect(718, 134, 30, 30)
        self.draw_button(self.perform_fx_buttons["perform_fx_close"], "", icon="close")

        for field, label, y in (
            ("filter", "Filter", 258), ("delay", "Delay", 326),
            ("stutter", "Stutter", 394), ("crush", "Crush", 462),
        ):
            value = self.perform_fx[field]
            self.screen.blit(self.small_font.render(label, True, theme.INK_2), (294, y + 9))
            self.perform_fx_buttons[f"perform_fx_{field}_down"] = pygame.Rect(582, y, 36, 36)
            self.perform_fx_buttons[f"perform_fx_{field}_up"] = pygame.Rect(712, y, 36, 36)
            self.draw_button(self.perform_fx_buttons[f"perform_fx_{field}_down"], "-")
            self.draw_button(self.perform_fx_buttons[f"perform_fx_{field}_up"], "+")
            track = pygame.Rect(630, y + 15, 70, 6)
            self.perform_fx_buttons[f"perform_fx_track_{field}"] = track.inflate(0, 22)
            pygame.draw.rect(self.screen, theme.RULE, track, border_radius=3)
            if value:
                pygame.draw.rect(
                    self.screen, theme.SIGNAL,
                    pygame.Rect(track.x, track.y, round(track.width * value / 100), track.height), border_radius=3,
                )
            marker_x = track.x + round(track.width * value / 100)
            pygame.draw.circle(self.screen, theme.INK, (marker_x, track.centery), 5)

        self.perform_fx_buttons["perform_fx_bypass"] = pygame.Rect(294, 540, 92, 38)
        self.perform_fx_buttons["perform_fx_reset"] = pygame.Rect(396, 540, 92, 38)
        self.perform_fx_buttons["perform_fx_bounce"] = pygame.Rect(294, 596, 194, 42)
        self.draw_button(self.perform_fx_buttons["perform_fx_bypass"], "A/B", active=self.perform_fx_bypass)
        self.draw_button(self.perform_fx_buttons["perform_fx_reset"], "Reset")
        self.draw_button(
            self.perform_fx_buttons["perform_fx_bounce"],
            "Bouncing..." if self.bounce_processing else "Bounce Loop",
            enabled=bool(self.loop_events) and not self.bounce_processing,
        )

    def draw_mixer_overlay(self):
        self.mixer_buttons.clear()
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 120, 500, 580)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        targets = self.mixer_targets()
        heading = PADS[targets[0]]["name"] if len(targets) == 1 else f"{len(targets)} Pads"
        self.screen.blit(self.big_font.render("Mixer", True, theme.INK), (294, 154))
        self.screen.blit(self.font.render(heading, True, theme.INK), (294, 210))
        self.mixer_buttons["mixer_close"] = pygame.Rect(718, 134, 30, 30)
        self.draw_button(self.mixer_buttons["mixer_close"], "", icon="close")
        self.mixer_buttons["mixer_all"] = pygame.Rect(626, 204, 122, 34)
        self.draw_button(self.mixer_buttons["mixer_all"], "Select All")

        self.mixer_buttons["mixer_mix_tab"] = pygame.Rect(294, 246, 92, 34)
        self.mixer_buttons["mixer_fx_tab"] = pygame.Rect(392, 246, 92, 34)
        self.draw_button(self.mixer_buttons["mixer_mix_tab"], "Mix", active=not self.mixer_fx_view)
        self.draw_button(self.mixer_buttons["mixer_fx_tab"], "FX", active=self.mixer_fx_view)

        rows = (("punch", "Punch", self.pad_punch, "", 300),
                ("air", "Air", self.pad_air, "", 368),
                ("space", "Space", self.pad_space, "", 436)) if self.mixer_fx_view else (
            ("volume", "Volume", self.pad_volume, "%", 300),
            ("pan", "Pan", self.pad_pan, "", 368),
            ("tune", "Tune", self.pad_tune, " st", 436),
        )
        for field, label, values, suffix, y in rows:
            selected_values = [values[index] for index in targets]
            mixed = any(value != selected_values[0] for value in selected_values[1:])
            if mixed:
                value_text = "Mixed"
            elif field in ("punch", "air", "space"):
                value = selected_values[0]
                value_text = "Off" if value == 0 else "Low" if value <= 30 else "Med" if value <= 70 else "High"
            elif field == "volume":
                value_text = f"{round(selected_values[0] * 100)}{suffix}"
            elif field == "pan":
                value = selected_values[0]
                value_text = "Center" if value == 0 else f"L{round(abs(value) * 100)}" if value < 0 else f"R{round(value * 100)}"
            else:
                value_text = f"{selected_values[0]:+d}{suffix}"
            self.screen.blit(self.small_font.render(label, True, theme.INK_2), (294, y + 9))
            self.mixer_buttons[f"mixer_{field}_down"] = pygame.Rect(582, y, 36, 36)
            self.mixer_buttons[f"mixer_{field}_up"] = pygame.Rect(712, y, 36, 36)
            self.draw_button(self.mixer_buttons[f"mixer_{field}_down"], "-")
            self.draw_button(self.mixer_buttons[f"mixer_{field}_up"], "+")
            value_surface = self.small_font.render(value_text, True, theme.INK)
            self.screen.blit(value_surface, value_surface.get_rect(center=(665, y + 18)))

        if self.mixer_fx_view:
            buses = [self.pad_bus[index] for index in targets]
            bus_label = "Mixed" if any(value != buses[0] for value in buses[1:]) else f"Bus {'ABCD'[buses[0]]}"
            self.mixer_buttons["mixer_bus"] = pygame.Rect(294, 516, 122, 38)
            self.mixer_buttons["mixer_bypass"] = pygame.Rect(426, 516, 92, 38)
            self.draw_button(self.mixer_buttons["mixer_bus"], bus_label)
            self.draw_button(self.mixer_buttons["mixer_bypass"], "A/B", active=self.mixer_bypass)
        else:
            all_muted = all(self.pad_mute[index] for index in targets)
            all_solo = bool(targets) and set(targets).issubset(self.solo_pads)
            self.mixer_buttons["mixer_mute"] = pygame.Rect(294, 516, 92, 38)
            self.mixer_buttons["mixer_solo"] = pygame.Rect(396, 516, 92, 38)
            self.mixer_buttons["mixer_bypass"] = pygame.Rect(498, 516, 92, 38)
            self.draw_button(self.mixer_buttons["mixer_mute"], "Mute", active=all_muted)
            self.draw_button(self.mixer_buttons["mixer_solo"], "Solo", active=all_solo)
            self.draw_button(self.mixer_buttons["mixer_bypass"], "A/B", active=self.mixer_bypass)
        self.mixer_buttons["mixer_undo"] = pygame.Rect(294, 572, 92, 38)
        self.mixer_buttons["mixer_reset"] = pygame.Rect(396, 572, 92, 38)
        self.draw_button(self.mixer_buttons["mixer_undo"], "Undo", enabled=bool(self.project_history))
        self.draw_button(self.mixer_buttons["mixer_reset"], "Reset")

    def draw_button(self, rect, text, active=False, danger=False, enabled=True, icon=None):
        """Buttons carry weight by frequency of use.

        A ghost button is the resting state, `active` fills with the accent, and
        `danger` is reserved for Record and destructive actions.
        """
        if not enabled:
            fill, border, text_color = theme.PANEL, theme.RULE_SOFT, theme.INK_3
        elif danger:
            fill, border, text_color = theme.DANGER, theme.DANGER, theme.ON_ACCENT
        elif active:
            fill, border, text_color = theme.ACCENT, theme.ACCENT, theme.ON_ACCENT
        else:
            fill, border, text_color = theme.PANEL_2, theme.RULE, theme.INK
        radius = theme.RADIUS["button"]
        pygame.draw.rect(self.screen, fill, rect, border_radius=radius)
        pygame.draw.rect(self.screen, border, rect, width=1, border_radius=radius)

        label = self.small_font.render(text, True, text_color) if text else None
        if icon is None:
            if label:
                self.screen.blit(label, label.get_rect(center=rect.center))
            return
        glyph_width = 16
        gap = 7 if label else 0
        total = glyph_width + gap + (label.get_width() if label else 0)
        x = rect.centerx - total // 2
        icons.draw(self.screen, icon, pygame.Rect(x, rect.centery - 8, glyph_width, 16), text_color)
        if label:
            self.screen.blit(label, (x + glyph_width + gap, rect.centery - label.get_height() // 2))

    def draw_chip(self, rect, text, dot_color=None):
        """A read-only status pill: connection, clock source, mapping."""
        pygame.draw.rect(self.screen, theme.PANEL_2, rect, border_radius=theme.RADIUS["field"])
        pygame.draw.rect(self.screen, theme.RULE, rect, width=1, border_radius=theme.RADIUS["field"])
        x = rect.x + 10
        if dot_color:
            pygame.draw.circle(self.screen, dot_color, (x + 3, rect.centery), 3)
            x += 13
        available = rect.right - 10 - x
        label = self.fit_text(self.small_font, text, available, theme.INK_2)
        self.screen.blit(label, (x, rect.centery - label.get_height() // 2))

    def fit_text(self, face, text, available, color):
        """Render `text`, trimming with an ellipsis until it fits `available`."""
        text = str(text)
        if face.measure(text)[0] <= available:
            return face.render(text, True, color)
        while len(text) > 1 and face.measure(text + "\u2026")[0] > available:
            text = text[:-1]
        return face.render(text + "\u2026", True, color)
    def draw_pads(self):
        """Pads are graphite. Colour on a pad means state, never identity.

        One name per pad, and it is the sound: a pad can be holding anything
        after the kit is rearranged, so the fixed position name would only
        contradict it. The 2px stripe carries the kit hue and travels with the
        sound, which keeps the layout learnable without spending real colour.
        """
        now = time.perf_counter()
        rects = self.pad_rects()
        for index, rect in rects.items():
            if now < self.hit_until[index] and not self.pad_mute[index]:
                self.draw_pad_glow(rect.move(0, 2), self.hit_energy[index])

        for index, rect in rects.items():
            muted = self.pad_mute[index]
            soloed = index in self.solo_pads
            selected = index in self.pad_selection
            sounding = now < self.hit_until[index]
            dragging_from = index == self.pad_drag_from and self.pad_drag_active
            drop_target = index == self.pad_drag_over
            flash = max(0.0, (self.pad_swap_flash.get(index, 0.0) - now) / PAD_SWAP_FLASH_SECONDS)

            energy = self.hit_energy[index] if sounding else 0.0
            face = theme.PAD_HIT if sounding or drop_target else theme.PAD
            if sounding:
                face = theme.mix(face, theme.ACCENT_SOFT, 0.35 + 0.55 * energy)
            border = theme.ACCENT if sounding or selected or drop_target else theme.RULE
            if flash:
                face = theme.mix(face, theme.ACCENT_SOFT, flash)
                border = theme.mix(border, theme.ACCENT, flash)
            if muted:
                face, border = theme.dim(face, 0.4), theme.dim(border, 0.6)
            if dragging_from:
                face, border = theme.mix(theme.PAD, theme.GROUND, 0.5), theme.INK_3
            if sounding:
                rect = rect.move(0, 2)

            radius = theme.RADIUS["pad"]
            pygame.draw.rect(self.screen, face, rect, border_radius=radius)

            custom_file = self.custom_sample_files[index]
            has_sample = bool(custom_file and custom_file in self.custom_sound_cache)
            hue = theme.SIGNAL if has_sample else synth_color(self.pad_synths[index], PADS[index]["color"])
            hue = theme.hue_hint(hue)
            if muted or dragging_from:
                hue = theme.dim(hue, 0.6)
            pygame.draw.rect(self.screen, hue, pygame.Rect(rect.x + 1, rect.y + 1, rect.width - 2, 2))

            edge = 2 if (sounding or selected or drop_target or flash) else 1
            pygame.draw.rect(self.screen, border, rect, width=edge, border_radius=radius)

            if drop_target:
                # The two-way arrow says swap, not move.
                icons.draw(self.screen, "swap", pygame.Rect(rect.centerx - 12, rect.top + 18, 24, 24), theme.ACCENT)

            name_color = theme.ACCENT if sounding or drop_target else theme.INK if selected else theme.INK_2
            if muted or dragging_from:
                name_color = theme.INK_3
            label = "Sample" if has_sample else SYNTH_LABELS[self.pad_synths[index]]
            surface = self.fit_text(self.label_font, label, rect.width - 20, name_color)
            self.screen.blit(surface, surface.get_rect(center=rect.center))

            note = PAD_TO_GM_NOTE.get(index)
            if note is not None:
                number = self.data_font_sm.render(f"{note}", True, theme.INK_3)
                self.screen.blit(number, (rect.right - 12 - number.get_width(), rect.bottom - 14 - number.get_height()))
            if muted or soloed:
                marker = self.data_font_sm.render("M" if muted else "S", True, theme.INK_2)
                self.screen.blit(marker, (rect.left + 12, rect.bottom - 14 - marker.get_height()))

        self.draw_pad_ghost()

    def draw_pad_glow(self, rect, energy):
        """Three soft rings outside a struck pad, sized and lit by velocity."""
        radius = theme.RADIUS["pad"]
        for step in range(3, 0, -1):
            spread = round(step * (3 + 5 * energy))
            alpha = round((30 + 60 * energy) / step)
            if alpha <= 0:
                continue
            halo = rect.inflate(spread * 2, spread * 2)
            layer = pygame.Surface(halo.size, pygame.SRCALPHA)
            pygame.draw.rect(
                layer, (*theme.ACCENT, alpha), layer.get_rect(),
                border_radius=radius + spread,
            )
            self.screen.blit(layer, halo.topleft)

    def draw_pad_ghost(self):
        """The dragged pad rides the cursor, so the gesture has something to follow."""
        if not self.pad_drag_active or self.pad_drag_from is None:
            return
        source = self.pad_drag_from
        template = self.pad_rects()[source]
        size = (round(template.width * PAD_GHOST_SCALE), round(template.height * PAD_GHOST_SCALE))
        ghost = pygame.Surface(size, pygame.SRCALPHA)
        body = pygame.Rect(0, 0, *size)
        radius = theme.RADIUS["pad"]
        pygame.draw.rect(ghost, (*theme.PAD_HIT, 235), body, border_radius=radius)

        custom_file = self.custom_sample_files[source]
        has_sample = bool(custom_file and custom_file in self.custom_sound_cache)
        hue = theme.SIGNAL if has_sample else synth_color(self.pad_synths[source], PADS[source]["color"])
        pygame.draw.rect(ghost, theme.hue_hint(hue), pygame.Rect(1, 1, size[0] - 2, 2))
        pygame.draw.rect(ghost, theme.ACCENT, body, width=2, border_radius=radius)

        label = "Sample" if has_sample else SYNTH_LABELS[self.pad_synths[source]]
        surface = self.fit_text(self.label_font, label, size[0] - 12, theme.ACCENT)
        ghost.blit(surface, surface.get_rect(center=body.center))

        # Offset from the cursor like a drag cursor, so the pad underneath
        # keeps showing its own name and drop arrow.
        x = self.mouse_logical[0] + PAD_GHOST_OFFSET[0]
        y = self.mouse_logical[1] + PAD_GHOST_OFFSET[1]
        x = min(x, WINDOW_SIZE[0] - size[0] - 4)
        y = min(y, WINDOW_SIZE[1] - size[1] - 4)
        self.screen.blit(ghost, (x, y))

    def draw_side_panel(self):
        """One panel, four labelled sections. Rules and labels replace nested cards."""
        panel = pygame.Rect(700, 74, 316, 604)
        pygame.draw.rect(self.screen, theme.PANEL, panel, border_radius=theme.RADIUS["panel"])
        pygame.draw.rect(self.screen, theme.RULE, panel, width=1, border_radius=theme.RADIUS["panel"])
        x0, x1 = 716, 1000

        def section(label, y, trailing=None, trailing_color=theme.INK_3):
            self.screen.blit(self.label_font.render(label, True, theme.INK_3), (x0, y))
            if trailing:
                badge = self.data_font_sm.render(trailing, True, trailing_color)
                self.screen.blit(badge, (x1 - badge.get_width(), y + 2))

        def divider(y):
            pygame.draw.line(self.screen, theme.RULE_SOFT, (x0, y), (x1, y))

        def track(rect, ratio, color=theme.SIGNAL):
            pygame.draw.rect(self.screen, theme.RULE, rect, border_radius=3)
            filled = round(rect.width * max(0.0, min(1.0, ratio)))
            if filled:
                pygame.draw.rect(self.screen, color, pygame.Rect(rect.x, rect.y, filled, rect.height), border_radius=3)

        # --- now playing ---------------------------------------------------
        with self.state_lock:
            last_hit = self.last_hit
            last_velocity_value = self.last_velocity_value
        section("Now playing", 94)
        hit = self.fit_text(self.head_font, last_hit, 190, theme.INK)
        self.screen.blit(hit, (x0, 116))
        velocity = self.data_font_lg.render(f"{last_velocity_value:>3}", True, theme.ACCENT)
        self.screen.blit(velocity, (x1 - velocity.get_width(), 114))
        track(pygame.Rect(x0, 158, x1 - x0, 4), last_velocity_value / 127.0, theme.ACCENT)
        trigger_p95 = self.diagnostic_snapshot()[0]
        detail = self.data_font_sm.render(f"trig {trigger_p95:.2f} ms", True, theme.INK_3)
        self.screen.blit(detail, (x0, 170))

        divider(196)

        # --- selected pad ---------------------------------------------------
        synth = self.pad_synths[self.selected_pad]
        custom_file = self.custom_sample_files[self.selected_pad]
        selected_name = "Sample" if custom_file else SYNTH_LABELS[synth]
        section(f"Pad \u00b7 {PADS[self.selected_pad]['name']}", 214)
        self.buttons["mixer"] = pygame.Rect(x1 - 62, 208, 62, 26)
        self.draw_button(self.buttons["mixer"], "Mixer", icon="sliders")

        self.screen.blit(self.fit_text(self.font, selected_name, 176, theme.INK), (x0, 240))
        self.buttons["browser"] = pygame.Rect(x1 - 138, 238, 66, 26)
        self.buttons["sound_prev"] = pygame.Rect(x1 - 66, 238, 30, 26)
        self.buttons["sound_next"] = pygame.Rect(x1 - 32, 238, 32, 26)
        self.draw_button(self.buttons["browser"], "Browse")
        self.draw_button(self.buttons["sound_prev"], "", icon="chevron_left")
        self.draw_button(self.buttons["sound_next"], "", icon="chevron_right")

        self.screen.blit(self.label_font.render("Sensitivity", True, theme.INK_3), (x0, 278))
        sensitivity_ratio = (self.pad_sensitivity[self.selected_pad] - 0.6) / 1.0
        track(pygame.Rect(x0 + 92, 284, 96, 4), sensitivity_ratio)
        self.buttons["sens_down"] = pygame.Rect(x1 - 66, 274, 30, 26)
        self.buttons["sens_up"] = pygame.Rect(x1 - 32, 274, 32, 26)
        self.draw_button(self.buttons["sens_down"], "-")
        self.draw_button(self.buttons["sens_up"], "+")

        sample_detail = self.sampler.detail_snapshot()
        recording = sample_detail["active"]
        input_level = sample_detail["level"]
        self.buttons["sample"] = pygame.Rect(x0, 314, 128, 30)
        self.buttons["sample_edit"] = pygame.Rect(x0 + 134, 314, 68, 30)
        self.buttons["sample_clear"] = pygame.Rect(x0 + 208, 314, 76, 30)
        sample_label = (
            "Cancel"
            if recording and sample_detail["auto_start"] and not sample_detail["triggered"]
            else "Stop"
            if recording
            else "Sample"
        )
        self.draw_button(
            self.buttons["sample"], sample_label, danger=recording,
            enabled=recording or self.audio_inputs_available, icon="microphone",
        )
        self.draw_button(self.buttons["sample_edit"], "Edit", enabled=bool(custom_file), icon="waveform")
        self.draw_button(self.buttons["sample_clear"], "Use Kit", enabled=bool(custom_file))
        sample_track = pygame.Rect(x0, 356, x1 - x0, 4)
        if recording:
            track(sample_track, min(1.0, input_level * 3.0), theme.DANGER)
        elif custom_file:
            track(sample_track, 1.0)
        elif self.sample_processing:
            track(sample_track, 0.2 + (time.perf_counter() % 1.0) * 0.35)
        else:
            pygame.draw.rect(self.screen, theme.RULE, sample_track, border_radius=3)

        divider(380)

        # --- tempo -----------------------------------------------------------
        external_clock = self.clock_active_source == "External"
        section("Tempo", 398, "EXT" if external_clock else None, theme.SIGNAL)
        bpm_value = self.data_font_lg.render(f"{self.bpm}", True, theme.INK)
        self.screen.blit(bpm_value, (x0, 416))
        self.screen.blit(
            self.label_font.render("bpm", True, theme.INK_3),
            (x0 + bpm_value.get_width() + 8, 430),
        )
        self.buttons["bpm_down"] = pygame.Rect(x1 - 170, 420, 30, 28)
        self.buttons["bpm_up"] = pygame.Rect(x1 - 136, 420, 30, 28)
        self.buttons["tap"] = pygame.Rect(x1 - 98, 420, 98, 28)
        self.draw_button(self.buttons["bpm_down"], "-", enabled=not external_clock)
        self.draw_button(self.buttons["bpm_up"], "+", enabled=not external_clock)
        self.draw_button(self.buttons["tap"], "Tap Tempo", enabled=not external_clock)

        self.buttons["repeat"] = pygame.Rect(x0, 462, 96, 28)
        self.buttons["repeat_rate"] = pygame.Rect(x0 + 102, 462, 60, 28)
        self.buttons["metro"] = pygame.Rect(x0 + 168, 462, 116, 28)
        self.draw_button(self.buttons["repeat"], "Repeat", active=self.repeat_enabled, icon="repeat")
        self.draw_button(self.buttons["repeat_rate"], self.repeat_rate)
        self.draw_button(self.buttons["metro"], "Metronome", active=self.metronome_enabled, icon="metronome")

        divider(502)

        # --- loop -------------------------------------------------------------
        loop = self.loop_snapshot()
        state = (
            f"COUNT {loop['count_remaining']}"
            if loop["record_pending"] and loop["count_remaining"]
            else "WAIT"
            if loop["record_pending"]
            else "REC"
            if loop["recording"] and not loop["overdub"]
            else "OVERDUB"
            if loop["overdub"]
            else "PLAY"
            if loop["playing"]
            else "STOP"
        )
        state_color = (
            theme.DANGER if loop["recording"] or loop["record_pending"]
            else theme.ACCENT if loop["playing"]
            else theme.INK_3
        )
        section("Loop", 520, state, state_color)

        self.buttons["loop_record"] = pygame.Rect(x0, 540, 100, 30)
        self.buttons["loop_play"] = pygame.Rect(x0 + 106, 540, 34, 30)
        self.buttons["loop_overdub"] = pygame.Rect(x0 + 146, 540, 34, 30)
        self.buttons["loop_capture"] = pygame.Rect(x0 + 186, 540, 98, 30)
        record_label = "Cancel" if loop["record_pending"] else "Record"
        self.draw_button(
            self.buttons["loop_record"],
            record_label,
            danger=loop["record_pending"] or (loop["recording"] and not loop["overdub"]),
            icon="record",
        )
        self.draw_button(self.buttons["loop_play"], "", active=loop["playing"], icon="play")
        self.draw_button(self.buttons["loop_overdub"], "", active=loop["overdub"], icon="overdub")
        self.draw_button(self.buttons["loop_capture"], "Capture", enabled=loop["can_capture"])

        self.buttons["loop_undo"] = pygame.Rect(x0, 580, 34, 30)
        self.buttons["loop_redo"] = pygame.Rect(x0 + 40, 580, 34, 30)
        self.buttons["loop_clear"] = pygame.Rect(x0 + 80, 580, 56, 30)
        self.buttons["loop_quantize"] = pygame.Rect(x0 + 142, 580, 56, 30)
        self.buttons["loop_bars"] = pygame.Rect(x0 + 204, 580, 40, 30)
        self.buttons["perform_fx"] = pygame.Rect(x0 + 250, 580, 34, 30)
        self.draw_button(self.buttons["loop_undo"], "", enabled=loop["can_undo"], icon="undo")
        self.draw_button(self.buttons["loop_redo"], "", enabled=loop["can_redo"], icon="redo")
        self.draw_button(self.buttons["loop_clear"], "Clear")
        self.draw_button(self.buttons["loop_quantize"], "Feel")
        self.draw_button(self.buttons["loop_bars"], f"{loop['bars']}B")
        self.draw_button(
            self.buttons["perform_fx"], "", icon="zap",
            active=any(self.perform_fx.values()) and not self.perform_fx_bypass,
        )

        self.buttons["share"] = pygame.Rect(x0, 620, x1 - x0, 30)
        self.draw_button(
            self.buttons["share"],
            "Exporting..." if loop["exporting"] else "Share",
            enabled=not loop["exporting"] and bool(loop["events"]),
            icon="share",
        )

        with self.state_lock:
            notice = self.surface_notice if time.perf_counter() < self.surface_notice_until else ""
        midi_hint = self.midi_activity()[2] if self.midi_input is not None else ""
        surface_status = notice or midi_hint or self.sample_status or self.status
        if not surface_status and loop["last_export"] != "--":
            surface_status = "Export complete"
        if surface_status:
            failed = any(word in surface_status.casefold() for word in ("failed", "unavailable", "no ", "disconnect"))
            color = theme.DANGER if failed else theme.INK_2
            self.screen.blit(self.fit_text(self.small_font, surface_status, 316, color), (700, 786))
    def sample_waveform_peaks(self, filename, bins=512):
        key = (filename, bins)
        if key in self.waveform_cache:
            return self.waveform_cache[key]
        sound = self.custom_sound_cache.get(filename)
        if sound is None:
            return []
        import numpy
        values = pygame.sndarray.array(sound).astype(numpy.float32)
        if values.ndim > 1:
            values = numpy.max(numpy.abs(values), axis=1)
        else:
            values = numpy.abs(values)
        chunk = max(1, len(values) // bins)
        peaks = [float(numpy.max(values[index:index + chunk])) for index in range(0, len(values), chunk)][:bins]
        maximum = max(peaks, default=1.0) or 1.0
        peaks = [value / maximum for value in peaks]
        self.waveform_cache[key] = peaks
        return peaks

    def draw_clip_prompt(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 140))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(340, 270, 360, 250)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.font.render("Input clipped", True, theme.DANGER), (366, 306))
        self.screen.blit(self.small_font.render("Lower the input level and record again.", True, theme.INK_2), (366, 350))
        self.clip_prompt_buttons["clip_retry"] = pygame.Rect(366, 420, 146, 44)
        self.clip_prompt_buttons["clip_keep"] = pygame.Rect(528, 420, 146, 44)
        self.draw_button(self.clip_prompt_buttons["clip_retry"], "Record Again", danger=True)
        self.draw_button(self.clip_prompt_buttons["clip_keep"], "Keep Anyway")

    def draw_sample_browser(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 70))
        self.screen.blit(shade, (0, 0))
        drawer = pygame.Rect(620, 76, 404, 728)
        pygame.draw.rect(self.screen, theme.PANEL, drawer, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, drawer, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Sounds", True, theme.INK), (644, 104))
        self.browser_buttons["browser_close"] = pygame.Rect(972, 92, 30, 30)
        self.draw_button(self.browser_buttons["browser_close"], "", icon="close")

        search = pygame.Rect(644, 160, 356, 38)
        pygame.draw.rect(self.screen, theme.PANEL_2, search, border_radius=6)
        pygame.draw.rect(self.screen, theme.RULE, search, width=1, border_radius=6)
        query = self.browser_query or "Search"
        color = theme.INK if self.browser_query else theme.INK_3
        self.screen.blit(self.small_font.render(query[-34:], True, color), (656, 171))

        filters = (
            ("browser_type", self.browser_type, 644, 83),
            ("browser_source", self.browser_source, 735, 83),
            ("browser_kit", self.browser_kit, 826, 83),
            ("browser_view", self.browser_view, 917, 83),
        )
        for name, label, x, width in filters:
            rect = pygame.Rect(x, 210, width, 34)
            self.browser_buttons[name] = rect
            self.draw_button(rect, label, active=label not in ("All", "All Kits"))

        candidates = self.sample_browser_candidates()
        page_size = 6
        max_page = max(0, (len(candidates) - 1) // page_size)
        self.browser_page = min(self.browser_page, max_page)
        page = candidates[self.browser_page * page_size:(self.browser_page + 1) * page_size]
        self.browser_row_ids = [candidate["id"] for candidate in page]
        for index, candidate in enumerate(page):
            y = 260 + index * 68
            row = pygame.Rect(644, y, 356, 58)
            active = candidate["id"] == self.browser_selected
            fill = theme.SIGNAL_SOFT if active else theme.PANEL_2
            pygame.draw.rect(self.screen, fill, row, border_radius=6)
            pygame.draw.rect(self.screen, theme.RULE, row, width=1, border_radius=6)
            label = candidate["label"] + ("  Missing" if candidate["missing"] else "")
            label_color = theme.DANGER if candidate["missing"] else theme.INK
            self.screen.blit(self.small_font.render(label[:31], True, label_color), (656, y + 10))
            detail = f"{candidate['type']}  {candidate['source']}"
            self.screen.blit(self.small_font.render(detail, True, theme.INK_2), (656, y + 32))
            self.browser_buttons[f"browser_preview_{index}"] = pygame.Rect(644, y, 300, 58)
            favorite_rect = pygame.Rect(950, y + 10, 42, 36)
            self.browser_buttons[f"browser_favorite_{index}"] = favorite_rect
            self.draw_button(favorite_rect, "Fav", active=candidate["id"] in self.sample_favorites)
        if not page:
            self.screen.blit(self.small_font.render("No matching sounds", True, theme.INK_2), (644, 282))

        missing_count = sum(1 for candidate in candidates if candidate["missing"])
        self.browser_buttons["browser_prev"] = pygame.Rect(644, 678, 42, 34)
        self.browser_buttons["browser_next"] = pygame.Rect(692, 678, 42, 34)
        self.browser_buttons["browser_relink"] = pygame.Rect(744, 678, 118, 34)
        self.draw_button(self.browser_buttons["browser_prev"], "<", enabled=self.browser_page > 0)
        self.draw_button(self.browser_buttons["browser_next"], ">", enabled=self.browser_page < max_page)
        self.draw_button(self.browser_buttons["browser_relink"], "Relink", enabled=missing_count > 0)
        self.browser_buttons["browser_use"] = pygame.Rect(644, 734, 356, 46)
        selected = next((candidate for candidate in candidates if candidate["id"] == self.browser_selected), None)
        self.draw_button(self.browser_buttons["browser_use"], "Use Sound", enabled=bool(selected and not selected["missing"]))

    def draw_chop_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(250, 88, 540, 642)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Chop", True, theme.INK), (274, 116))
        self.chop_buttons["chop_close"] = pygame.Rect(738, 106, 30, 30)
        self.draw_button(self.chop_buttons["chop_close"], "", icon="close")

        filename = self.custom_sample_files[self.selected_pad]
        peaks = self.sample_waveform_peaks(filename)
        self.chop_wave_rect = pygame.Rect(274, 176, 492, 174)
        pygame.draw.rect(self.screen, theme.PANEL_2, self.chop_wave_rect, border_radius=6)
        if peaks:
            center = self.chop_wave_rect.centery
            for index, peak in enumerate(peaks):
                x = self.chop_wave_rect.left + round(index * (self.chop_wave_rect.width - 1) / max(1, len(peaks) - 1))
                height = round(peak * (self.chop_wave_rect.height / 2 - 8))
                pygame.draw.line(self.screen, theme.SIGNAL, (x, center - height), (x, center + height))
        samples = self.current_sample_array()
        marker_ratios = []
        if samples is not None and len(samples):
            marker_ratios = [value / len(samples) for value in self.current_chop_markers(samples)[1:-1]]
        for ratio in marker_ratios:
            x = self.chop_wave_rect.left + round(ratio * self.chop_wave_rect.width)
            pygame.draw.line(self.screen, theme.PANEL, (x, self.chop_wave_rect.top + 4), (x, self.chop_wave_rect.bottom - 4), 2)

        self.chop_buttons["chop_mode"] = pygame.Rect(274, 378, 160, 40)
        self.chop_buttons["chop_count"] = pygame.Rect(446, 378, 112, 40)
        self.draw_button(self.chop_buttons["chop_mode"], self.chop_mode, active=self.chop_mode in ("Manual", "Lazy"))
        self.draw_button(self.chop_buttons["chop_count"], f"{self.chop_count} Slices", enabled=self.chop_mode in ("Transient", "Equal"))

        options = (
            ("chop_keep", "Keep Original", self.chop_keep_original),
            ("chop_through", "Play Through", self.chop_play_through),
            ("chop_choke", "Choke", self.chop_choke),
        )
        for index, (name, label, active) in enumerate(options):
            rect = pygame.Rect(274 + index * 164, 442, 152, 40)
            self.chop_buttons[name] = rect
            self.draw_button(rect, label, active=active)
        self.chop_buttons["chop_lazy"] = pygame.Rect(274, 506, 152, 40)
        self.chop_buttons["chop_clear"] = pygame.Rect(438, 506, 120, 40)
        self.draw_button(self.chop_buttons["chop_lazy"], "Stop Tapping" if self.chop_lazy_active else "Live Chop", active=self.chop_lazy_active)
        self.draw_button(self.chop_buttons["chop_clear"], "Clear Marks")

        slice_count = len(marker_ratios) + 1
        if self.chop_mode in ("Transient", "Equal"):
            slice_count = self.chop_count
        start = self.selected_pad + (1 if self.chop_keep_original else 0)
        available = (0 if self.chop_keep_original else 1) + sum(
            1 for index in range(start, len(PADS))
            if index != self.selected_pad and not self.custom_sample_files[index]
        )
        status = f"{slice_count} slices  /  {available} pads available"
        status_color = theme.DANGER if slice_count > available else theme.INK_2
        self.screen.blit(self.small_font.render(status, True, status_color), (274, 574))
        self.chop_buttons["chop_apply"] = pygame.Rect(274, 624, 492, 46)
        self.draw_button(self.chop_buttons["chop_apply"], "Chop to Pads", enabled=slice_count <= available and slice_count > 0)

    def draw_sample_editor_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(250, 62, 540, 696)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Sample", True, theme.INK), (274, 92))
        self.sample_editor_buttons["sample_editor_close"] = pygame.Rect(738, 80, 30, 30)
        self.sample_editor_buttons["sample_preview"] = pygame.Rect(650, 86, 78, 34)
        self.draw_button(self.sample_editor_buttons["sample_editor_close"], "", icon="close")
        self.draw_button(self.sample_editor_buttons["sample_preview"], "Play")

        filename = self.custom_sample_files[self.selected_pad]
        edit = self.sample_edits[self.selected_pad]
        wave_rect = pygame.Rect(274, 148, 492, 150)
        pygame.draw.rect(self.screen, theme.PANEL_2, wave_rect, border_radius=6)
        peaks = self.sample_waveform_peaks(filename)
        if peaks:
            first = round(len(peaks) * edit["start"]) if self.sample_wave_zoom else 0
            last = round(len(peaks) * edit["end"]) if self.sample_wave_zoom else len(peaks)
            visible = peaks[first:max(first + 1, last)]
            center = wave_rect.centery
            points_top, points_bottom = [], []
            for index, peak in enumerate(visible):
                x = wave_rect.left + round(index * (wave_rect.width - 1) / max(1, len(visible) - 1))
                height = round(peak * (wave_rect.height / 2 - 8))
                points_top.append((x, center - height))
                points_bottom.append((x, center + height))
            pygame.draw.lines(self.screen, theme.SIGNAL, False, points_top, 2)
            pygame.draw.lines(self.screen, theme.SIGNAL, False, points_bottom, 2)
            if not self.sample_wave_zoom:
                for ratio in (edit["start"], edit["end"]):
                    x = wave_rect.left + round(ratio * wave_rect.width)
                    pygame.draw.line(self.screen, theme.PANEL, (x, wave_rect.top + 4), (x, wave_rect.bottom - 4), 2)

        rows = (
            ("start", "Crop Start", f"{round(edit['start'] * 100)}%", 322, 0.01),
            ("end", "Crop End", f"{round(edit['end'] * 100)}%", 370, 0.01),
            ("tune", "Tune", f"{edit['tune']:+d} st", 418, 1),
            ("attack", "Attack", f"{edit['attack_ms']} ms", 466, 5),
            ("release", "Release", f"{edit['release_ms']} ms", 514, 5),
        )
        for field, label, value, y, _step in rows:
            self.screen.blit(self.small_font.render(label, True, theme.INK_2), (274, y + 9))
            down = pygame.Rect(600, y, 36, 36)
            up = pygame.Rect(730, y, 36, 36)
            self.sample_editor_buttons[f"sample_{field}_down"] = down
            self.sample_editor_buttons[f"sample_{field}_up"] = up
            self.draw_button(down, "-")
            self.draw_button(up, "+")
            surface = self.small_font.render(value, True, theme.INK)
            self.screen.blit(surface, surface.get_rect(center=(683, y + 18)))

        option_data = (
            ("sample_normalize", "Normalize", edit["normalize"]),
            ("sample_reverse", "Reverse", edit["reverse"]),
            ("sample_mode", edit["mode"], edit["mode"] != "One-shot"),
            ("sample_ab", "A/B", self.sample_edit_bypass),
        )
        for index, (name, label, active) in enumerate(option_data):
            rect = pygame.Rect(274 + index * 123, 578, 112, 38)
            self.sample_editor_buttons[name] = rect
            self.draw_button(rect, label, active=active)
        bottom = (("sample_zoom", "Zoom", self.sample_wave_zoom), ("sample_undo", "Undo", False), ("sample_reset", "Reset", False), ("sample_chop", "Chop", False))
        for index, (name, label, active) in enumerate(bottom):
            rect = pygame.Rect(274 + index * 123, 638, 112, 38)
            self.sample_editor_buttons[name] = rect
            self.draw_button(rect, label, active=active, enabled=name != "sample_undo" or bool(self.project_history))

        bpm = edit.get("source_bpm")
        tempo_label = f"Fits {bpm:g} BPM" if bpm else "Detect Tempo"
        tempo_controls = (
            ("sample_detect", tempo_label, bool(bpm), 274, 176),
            ("sample_half", "1/2", False, 462, 62),
            ("sample_double", "x2", False, 536, 62),
            ("sample_stretch", edit.get("stretch_mode", "Off"), edit.get("stretch_mode") != "Off", 610, 156),
        )
        for name, label, active, x, width in tempo_controls:
            rect = pygame.Rect(x, 696, width, 38)
            self.sample_editor_buttons[name] = rect
            enabled = name == "sample_detect" or bool(bpm)
            self.draw_button(rect, label, active=active, enabled=enabled)

    def draw_share_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(330, 180, 380, 430)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        self.screen.blit(self.big_font.render("Share", True, theme.INK), (354, 214))
        self.share_buttons["share_close"] = pygame.Rect(658, 198, 30, 30)
        self.draw_button(self.share_buttons["share_close"], "", icon="close")

        options = (
            ("share_wav", "Master WAV"),
            ("share_midi", "MIDI"),
            ("share_stems", "Pad Stems"),
            ("share_bundle", "Project Bundle"),
        )
        for offset, (name, label) in enumerate(options):
            rect = pygame.Rect(354, 278 + offset * 66, 332, 46)
            self.share_buttons[name] = rect
            self.draw_button(rect, label)

    def draw_settings_overlay(self):
        self.settings_buttons = {}
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 82, 500, 692)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        title = self.font.render("Settings", True, theme.INK)
        self.screen.blit(title, (294, 108))
        self.settings_buttons["settings_close"] = pygame.Rect(718, 96, 30, 30)
        self.draw_button(self.settings_buttons["settings_close"], "", icon="close")

        if self.calibration_active:
            self.draw_calibration_setup()
            return
        if self.audio_setup_open:
            self.draw_audio_setup()
            return
        if self.sync_setup_open:
            self.draw_sync_setup()
            return

        label_color = theme.INK_2
        value_color = theme.INK
        midi_label = self.small_font.render("MIDI input", True, label_color)
        self.screen.blit(midi_label, (294, 164))
        midi_value = self.small_font.render(self.midi_device_name[:34], True, value_color)
        self.screen.blit(midi_value, (294, 187))
        self.settings_buttons["device"] = pygame.Rect(646, 168, 102, 34)
        self.draw_button(self.settings_buttons["device"], "Next")

        mapping_label = self.small_font.render("Mapping", True, label_color)
        self.screen.blit(mapping_label, (294, 226))
        mapping_value = self.small_font.render(MAPPING_MODES[self.mapping_mode], True, value_color)
        self.screen.blit(mapping_value, (294, 249))
        self.settings_buttons["preset"] = pygame.Rect(560, 230, 88, 34)
        self.settings_buttons["reset"] = pygame.Rect(656, 230, 92, 34)
        self.draw_button(self.settings_buttons["preset"], "Change")
        self.draw_button(self.settings_buttons["reset"], "Reset")

        input_label = self.small_font.render("Sample input", True, label_color)
        self.screen.blit(input_label, (294, 288))
        input_name = self.sample_input_name or "Default input"
        input_value = self.small_font.render(input_name[:38], True, value_color)
        self.screen.blit(input_value, (294, 311))
        self.settings_buttons["input_prev"] = pygame.Rect(680, 292, 30, 34)
        self.settings_buttons["input_next"] = pygame.Rect(718, 292, 30, 34)
        self.draw_button(self.settings_buttons["input_prev"], "<")
        self.draw_button(self.settings_buttons["input_next"], ">")

        sample_start_label = self.small_font.render("Sample start", True, label_color)
        self.screen.blit(sample_start_label, (294, 350))
        sample_start_value = "Waits for sound" if self.sample_start_mode == "Auto" else "Starts immediately"
        self.screen.blit(self.small_font.render(sample_start_value, True, value_color), (294, 373))
        self.settings_buttons["sample_monitor"] = pygame.Rect(506, 354, 74, 34)
        self.settings_buttons["sample_continuous"] = pygame.Rect(588, 354, 74, 34)
        self.settings_buttons["sample_start"] = pygame.Rect(670, 354, 78, 34)
        self.draw_button(self.settings_buttons["sample_monitor"], "Monitor", active=self.sample_monitor_enabled)
        self.draw_button(self.settings_buttons["sample_continuous"], "Multi", active=self.sample_continuous_enabled)
        self.draw_button(self.settings_buttons["sample_start"], self.sample_start_mode)

        record_start_label = self.small_font.render("Loop recording", True, label_color)
        self.screen.blit(record_start_label, (294, 412))
        self.screen.blit(self.small_font.render(self.record_start_mode, True, value_color), (294, 435))
        self.settings_buttons["record_start"] = pygame.Rect(646, 416, 102, 34)
        self.draw_button(self.settings_buttons["record_start"], "Change")

        profile = self.pad_calibrations[self.selected_pad]
        setup_label = self.small_font.render(f"Pad {self.selected_pad + 1} setup", True, label_color)
        self.screen.blit(setup_label, (294, 474))
        setup_state = "Calibrated" if profile["enabled"] else "Default response"
        self.screen.blit(self.small_font.render(setup_state, True, value_color), (294, 497))
        self.settings_buttons["calibrate"] = pygame.Rect(626, 478, 122, 34)
        self.draw_button(self.settings_buttons["calibrate"], "Calibrate")

        audio_label = self.small_font.render("Audio output", True, label_color)
        self.screen.blit(audio_label, (294, 536))
        audio_value = self.small_font.render(self.audio_mode, True, value_color)
        self.screen.blit(audio_value, (294, 559))
        self.settings_buttons["audio_setup"] = pygame.Rect(646, 540, 102, 34)
        self.draw_button(self.settings_buttons["audio_setup"], "Setup")

        self.screen.blit(self.small_font.render("MIDI sync", True, label_color), (294, 598))
        sync_value = f"{self.clock_active_source}  {self.bpm} BPM"
        self.screen.blit(self.small_font.render(sync_value, True, value_color), (294, 621))
        self.settings_buttons["sync_setup"] = pygame.Rect(646, 602, 102, 34)
        self.draw_button(self.settings_buttons["sync_setup"], "Setup")

        self.screen.blit(self.small_font.render("Display size", True, label_color), (294, 664))
        self.settings_buttons["ui_scale"] = pygame.Rect(646, 654, 102, 34)
        self.draw_button(self.settings_buttons["ui_scale"], f"{round(self.ui_scale * 100)}%")

        self.screen.blit(self.small_font.render("Pad light", True, label_color), (294, 712))
        self.screen.blit(self.small_font.render(self.accent_name, True, value_color), (294, 735))
        for index, (name, color) in enumerate(theme.ACCENT_CHOICES):
            swatch = pygame.Rect(560 + index * 32, 716, 28, 28)
            self.settings_buttons[f"accent_{name}"] = swatch
            pygame.draw.rect(self.screen, color, swatch, border_radius=theme.RADIUS["field"])
            if name == self.accent_name:
                pygame.draw.rect(self.screen, theme.INK, swatch.inflate(6, 6), width=2,
                                 border_radius=theme.RADIUS["field"] + 2)

    def draw_sync_setup(self):
        label_color = theme.INK_2
        value_color = theme.INK
        self.screen.blit(self.big_font.render("MIDI Sync", True, value_color), (294, 164))
        self.settings_buttons["sync_back"] = pygame.Rect(294, 214, 80, 34)
        self.draw_button(self.settings_buttons["sync_back"], "Back")
        self.screen.blit(self.small_font.render("Clock source", True, label_color), (294, 286))
        for index, source in enumerate(("Auto", "Internal", "External")):
            name = f"sync_source_{source.lower()}"
            rect = pygame.Rect(294 + index * 126, 312, 116, 40)
            self.settings_buttons[name] = rect
            self.draw_button(rect, source, active=self.clock_source == source)
        active = self.clock_active_source if self.clock_source == "Auto" else self.clock_source
        self.screen.blit(self.small_font.render(f"Active: {active}", True, value_color), (294, 372))

        self.screen.blit(self.small_font.render("Clock output", True, label_color), (294, 422))
        self.settings_buttons["sync_output"] = pygame.Rect(600, 408, 148, 40)
        self.draw_button(self.settings_buttons["sync_output"], "On" if self.clock_output_enabled else "Off", active=self.clock_output_enabled)
        output_name = self.midi_output_name or "No output selected"
        self.screen.blit(self.small_font.render(output_name[:34], True, value_color), (294, 466))
        self.settings_buttons["sync_port"] = pygame.Rect(648, 458, 100, 34)
        self.draw_button(self.settings_buttons["sync_port"], "Next port", enabled=bool(MidiOutput.devices()))

        self.screen.blit(self.small_font.render("Clock offset", True, label_color), (294, 536))
        self.settings_buttons["sync_correction_down"] = pygame.Rect(620, 522, 36, 36)
        self.settings_buttons["sync_correction_up"] = pygame.Rect(712, 522, 36, 36)
        self.draw_button(self.settings_buttons["sync_correction_down"], "-")
        self.draw_button(self.settings_buttons["sync_correction_up"], "+")
        offset = self.small_font.render(f"{self.clock_correction_ms:+d} ms", True, value_color)
        self.screen.blit(offset, offset.get_rect(center=(684, 540)))

    def draw_audio_setup(self):
        label_color = theme.INK_2
        value_color = theme.INK
        self.screen.blit(self.big_font.render("Audio", True, value_color), (294, 164))

        self.screen.blit(self.small_font.render("Output", True, label_color), (294, 232))
        output_name = self.audio_output_name or "System default"
        self.screen.blit(self.small_font.render(output_name[:36], True, value_color), (294, 255))
        self.settings_buttons["audio_output_prev"] = pygame.Rect(680, 236, 30, 34)
        self.settings_buttons["audio_output_next"] = pygame.Rect(718, 236, 30, 34)
        self.draw_button(self.settings_buttons["audio_output_prev"], "<")
        self.draw_button(self.settings_buttons["audio_output_next"], ">")

        self.screen.blit(self.small_font.render("Response", True, label_color), (294, 308))
        self.settings_buttons["audio_low"] = pygame.Rect(294, 334, 150, 40)
        self.settings_buttons["audio_stable"] = pygame.Rect(454, 334, 150, 40)
        self.draw_button(self.settings_buttons["audio_low"], "Low latency", active=self.audio_mode == "Low latency")
        self.draw_button(self.settings_buttons["audio_stable"], "Stable", active=self.audio_mode == "Stable")

        self.settings_buttons["audio_advanced"] = pygame.Rect(294, 408, 120, 34)
        self.draw_button(self.settings_buttons["audio_advanced"], "Advanced")
        if self.audio_advanced:
            self.screen.blit(self.small_font.render("Sample rate", True, label_color), (294, 470))
            self.settings_buttons["audio_rate"] = pygame.Rect(620, 454, 128, 34)
            self.draw_button(self.settings_buttons["audio_rate"], f"{self.audio_rate / 1000:g} kHz")
            self.screen.blit(self.small_font.render("Buffer", True, label_color), (294, 522))
            self.settings_buttons["audio_buffer"] = pygame.Rect(620, 506, 128, 34)
            self.draw_button(self.settings_buttons["audio_buffer"], f"{self.audio_buffer} samples")
            latency_ms = self.audio_buffer * 1000.0 / self.audio_rate
            detail = self.small_font.render(f"Buffer duration {latency_ms:.2f} ms", True, value_color)
            self.screen.blit(detail, (294, 558))

        self.settings_buttons["audio_test"] = pygame.Rect(294, 594, 180, 38)
        self.draw_button(
            self.settings_buttons["audio_test"],
            "Testing..." if self.audio_test_active else "Test 10 sec",
            active=self.audio_test_active, enabled=not self.audio_test_active,
        )
        if self.audio_test_result:
            self.screen.blit(self.small_font.render(self.audio_test_result, True, value_color), (490, 604))

        self.settings_buttons["audio_back"] = pygame.Rect(294, 660, 100, 36)
        self.draw_button(self.settings_buttons["audio_back"], "Back")

    def draw_feel_overlay(self):
        shade = pygame.Surface(WINDOW_SIZE, pygame.SRCALPHA)
        shade.fill((20, 24, 26, 120))
        self.screen.blit(shade, (0, 0))
        modal = pygame.Rect(270, 90, 500, 640)
        pygame.draw.rect(self.screen, theme.PANEL, modal, border_radius=8)
        pygame.draw.rect(self.screen, theme.RULE, modal, width=1, border_radius=8)
        value_color = theme.INK
        label_color = theme.INK_2
        self.screen.blit(self.big_font.render("Feel", True, value_color), (294, 132))
        self.feel_buttons["feel_close"] = pygame.Rect(718, 104, 30, 30)
        self.draw_button(self.feel_buttons["feel_close"], "", icon="close")

        for index, preset in enumerate(FEEL_PRESETS):
            rect = pygame.Rect(294 + index * 145, 214, 135, 42)
            self.feel_buttons[f"feel_preset_{preset.lower()}"] = rect
            self.draw_button(rect, preset, active=self.feel_preset == preset, enabled=bool(self.loop_events))

        self.feel_buttons["feel_advanced"] = pygame.Rect(294, 292, 120, 34)
        self.feel_buttons["feel_reset"] = pygame.Rect(424, 292, 100, 34)
        self.draw_button(self.feel_buttons["feel_advanced"], "Advanced")
        self.draw_button(self.feel_buttons["feel_reset"], "Reset", enabled=self.loop_source_events is not None)

        if self.feel_advanced:
            rows = (
                ("grid", "Grid", self.repeat_rate, 360),
                ("strength", "Strength", f"{self.feel_strength}%", 412),
                ("swing", "Swing", f"{self.feel_swing}%", 464),
                ("nudge_ms", "Nudge", f"{self.feel_nudge_ms:+d} ms", 516),
                ("humanize_ms", "Humanize", f"{self.feel_humanize_ms} ms", 568),
            )
            for field, label, value, y in rows:
                self.screen.blit(self.small_font.render(label, True, label_color), (294, y + 7))
                if field == "grid":
                    self.feel_buttons["feel_grid"] = pygame.Rect(620, y, 128, 34)
                    self.draw_button(self.feel_buttons["feel_grid"], value, enabled=bool(self.loop_events))
                    continue
                self.feel_buttons[f"feel_{field}_down"] = pygame.Rect(610, y, 34, 34)
                self.feel_buttons[f"feel_{field}_up"] = pygame.Rect(714, y, 34, 34)
                self.draw_button(self.feel_buttons[f"feel_{field}_down"], "-", enabled=bool(self.loop_events))
                self.draw_button(self.feel_buttons[f"feel_{field}_up"], "+", enabled=bool(self.loop_events))
                text_surface = self.small_font.render(value, True, value_color)
                self.screen.blit(text_surface, text_surface.get_rect(center=(679, y + 17)))

        source_label = "Original timing kept" if self.loop_source_events is not None else "No timing changes"
        self.screen.blit(self.small_font.render(source_label, True, label_color), (294, 676))

    def draw_calibration_setup(self):
        label_color = theme.INK_2
        value_color = theme.INK
        labels = ("Soft", "Natural", "Hard")
        pad_name = PADS[self.calibration_pad]["name"]
        heading = self.big_font.render(f"Pad {self.calibration_pad + 1}: {pad_name}", True, value_color)
        self.screen.blit(heading, (294, 176))
        instruction = self.font.render(f"Play 3 {labels[self.calibration_stage].lower()} hits", True, value_color)
        self.screen.blit(instruction, (294, 226))
        progress = len(self.calibration_hits)
        for index, label in enumerate(labels):
            y = 292 + index * 62
            complete = index < self.calibration_stage
            active = index == self.calibration_stage
            color = theme.SIGNAL if complete else theme.DANGER if active else label_color
            marker = "Done" if complete else f"{progress}/3" if active else "Waiting"
            self.screen.blit(self.font.render(label, True, color), (294, y))
            self.screen.blit(self.small_font.render(marker, True, color), (620, y + 4))
        hint = self.small_font.render("Only the selected physical pad is measured.", True, label_color)
        self.screen.blit(hint, (294, 500))
        self.settings_buttons["calibration_cancel"] = pygame.Rect(294, 554, 112, 36)
        self.settings_buttons["calibration_reset"] = pygame.Rect(416, 554, 132, 36)
        self.draw_button(self.settings_buttons["calibration_cancel"], "Cancel")
        self.draw_button(self.settings_buttons["calibration_reset"], "Use Default")

    def draw_loop_timeline(self):
        """The loop as a bar with real beat divisions, not a hairline."""
        loop = self.loop_snapshot()
        left = 24
        width = 992
        top = 700
        total_beats = loop["bars"] * 4.0

        state = "REC" if loop["recording"] else "PLAY" if loop["playing"] else "STOP"
        title = self.label_font.render(f"Loop \u00b7 {loop['bars']} bar", True, theme.INK_3)
        self.screen.blit(title, (left, top))
        state_color = theme.DANGER if loop["recording"] else theme.ACCENT if loop["playing"] else theme.INK_3
        badge = self.data_font_sm.render(state, True, state_color)
        self.screen.blit(badge, (left + width - badge.get_width(), top + 2))

        track = pygame.Rect(left, top + 22, width, 42)
        pygame.draw.rect(self.screen, theme.PANEL, track, border_radius=theme.RADIUS["field"])
        pygame.draw.rect(self.screen, theme.RULE, track, width=1, border_radius=theme.RADIUS["field"])

        for beat in range(1, int(total_beats)):
            x = left + round((beat / total_beats) * width)
            downbeat = beat % 4 == 0
            pygame.draw.line(
                self.screen,
                theme.RULE if downbeat else theme.RULE_SOFT,
                (x, track.top + (3 if downbeat else 12)),
                (x, track.bottom - (3 if downbeat else 12)),
                1,
            )

        for beat, pad_index, velocity in loop["events"]:
            x = left + round((beat / total_beats) * width)
            height = 6 + round((velocity / 127.0) * 22)
            marker = pygame.Rect(x - 1, track.centery - height // 2, 3, height)
            pygame.draw.rect(self.screen, theme.hue_hint(PADS[pad_index]["color"]), marker, border_radius=1)

        if loop["playing"] or loop["recording"]:
            playhead_x = left + round((loop["phase"] / total_beats) * width)
            played = pygame.Rect(track.x + 1, track.y + 1, max(0, playhead_x - track.x - 1), track.height - 2)
            pygame.draw.rect(self.screen, theme.ACCENT_SOFT, played, border_radius=theme.RADIUS["field"])
            pygame.draw.line(self.screen, theme.ACCENT, (playhead_x, track.top + 1), (playhead_x, track.bottom - 1), 2)

def main():
    mutex = acquire_single_instance()
    if mutex is None:
        print("STARRYPAD is already running. Switch to the open window.", file=sys.stderr)
        return
    try:
        app = DrumPadNative()
        app.run()
    except Exception as exc:
        try:
            pygame.quit()
        except Exception:
            pass
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise
    finally:
        release_single_instance(mutex)


if __name__ == "__main__":
    main()
