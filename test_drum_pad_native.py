import json
import queue
import struct
import sys
import tempfile
import time
import unittest
import wave
import zipfile
from pathlib import Path
from unittest import mock

import numpy

import drum_pad_native as drum


def wav_attack_ms(path):
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        raw = source.readframes(source.getnframes())

    if sample_width != 3:
        raise AssertionError(f"Expected 24-bit PCM: {path}")

    packed = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, 3)
    samples = (
        packed[:, 0].astype(numpy.int32)
        | (packed[:, 1].astype(numpy.int32) << 8)
        | (packed[:, 2].astype(numpy.int32) << 16)
    )
    samples = numpy.where(samples & 0x800000, samples - 0x1000000, samples)
    frames = numpy.max(numpy.abs(samples.reshape(-1, channels)), axis=1)
    window_frames = max(1, round(sample_rate * 0.001))
    envelope = numpy.convolve(
        frames,
        numpy.ones(window_frames, dtype=numpy.float64) / window_frames,
        mode="valid",
    )
    threshold = float(numpy.max(frames)) * 0.05
    crossings = numpy.flatnonzero(envelope >= threshold)
    if not len(crossings):
        raise AssertionError(f"No attack detected: {path}")
    return float(crossings[0]) * 1000.0 / sample_rate


class VelocityTests(unittest.TestCase):
    def test_velocity_curve_is_audible_and_monotonic(self):
        gains = [drum.velocity_gain(value) for value in range(1, 128)]
        self.assertGreaterEqual(gains[0], 0.12)
        self.assertGreater(drum.velocity_gain(49), 0.25)
        self.assertLess(drum.velocity_gain(70), 0.5)
        self.assertGreaterEqual(drum.velocity_gain(100), 0.88)
        self.assertEqual(gains, sorted(gains))
        self.assertLessEqual(gains[-1], 1.0)

    def test_velocity_layers_match_playing_targets(self):
        self.assertEqual(drum.velocity_tier(30), "ghost")
        self.assertEqual(drum.velocity_tier(50), "soft")
        self.assertEqual(drum.velocity_tier(70), "soft")
        self.assertEqual(drum.velocity_tier(99), "mid")
        self.assertEqual(drum.velocity_tier(100), "hard")
        self.assertEqual(drum.velocity_tier(116), "accent")

    def test_velocity_layers_crossfade_at_boundaries(self):
        self.assertEqual(drum.velocity_layer_mix(70), (("soft", 1.0),))
        self.assertEqual(drum.velocity_layer_mix(78), (("soft", 0.5), ("mid", 0.5)))
        self.assertEqual(drum.velocity_layer_mix(100), (("mid", 0.5), ("hard", 0.5)))
        self.assertEqual(drum.velocity_layer_mix(116), (("hard", 0.5), ("accent", 0.5)))
        for velocity in range(1, 128):
            self.assertAlmostEqual(sum(weight for _tier, weight in drum.velocity_layer_mix(velocity)), 1.0)

    def test_five_zones_reuse_source_layers_without_empty_choices(self):
        for layers in drum.KIT.values():
            for layer in layers:
                for tier in ("ghost", "soft", "mid", "hard", "accent"):
                    self.assertTrue(drum.layer_files_for_tier(layer, tier))

    def test_sample_choice_never_repeats_immediately(self):
        files = ("one.wav", "two.wav", "three.wav")
        previous = None
        choices = []
        for _ in range(100):
            current = drum.choose_nonrepeating_sample(files, previous)
            self.assertNotEqual(current, previous)
            choices.append(current)
            previous = current
        self.assertGreater(len(set(choices)), 1)

    def test_calibrated_velocity_is_monotonic_and_reaches_hard_target(self):
        profile = {"enabled": True, "soft": 44, "natural": 72, "hard": 101, "dead_time_ms": 10}
        values = [drum.calibrated_velocity(value, profile) for value in range(1, 128)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(drum.calibrated_velocity(44, profile), 42)
        self.assertEqual(drum.calibrated_velocity(72, profile), 76)
        self.assertGreaterEqual(drum.calibrated_velocity(101, profile), 115)

    def test_disabled_calibration_preserves_raw_velocity(self):
        profile = drum.default_pad_calibration()
        self.assertEqual([drum.calibrated_velocity(value, profile) for value in (1, 50, 100, 127)], [1, 50, 100, 127])


class AudioSetupTests(unittest.TestCase):
    def test_system_resume_panics_and_checks_both_connections_immediately(self):
        app = drum.DrumPadNative(settings_path=None)
        app.next_midi_health_check_at = 999.0
        app.next_audio_health_check_at = 999.0
        app.next_metronome_ns = 123
        app.next_midi_clock_out_ns = 456
        app.maintain_midi_connection = mock.Mock()
        app.maintain_audio_connection = mock.Mock()
        app.handle_system_resume(100.0)
        self.assertEqual(app.audio_events.get_nowait(), ("PANIC",))
        app.maintain_midi_connection.assert_called_once_with(100.0)
        app.maintain_audio_connection.assert_called_once_with(100.0)
        self.assertIsNone(app.next_metronome_ns)
        self.assertIsNone(app.next_midi_clock_out_ns)

    def test_ten_second_audio_test_recommends_low_latency_when_stable(self):
        app = drum.DrumPadNative(settings_path=None)
        app.queue_pad = mock.Mock()
        app.diagnostic_snapshot = mock.Mock(return_value=(1.0, 2.0, 100, 0, 0, 2))
        with mock.patch.object(drum.pygame.mixer, "get_init", return_value=(48000, -16, 2)):
            self.assertTrue(app.start_audio_test(10.0))
            self.assertIsNone(app.update_audio_test(19.9))
            self.assertTrue(app.update_audio_test(20.0))
        self.assertEqual(app.audio_test_result, "Low latency passed")
        self.assertGreaterEqual(app.queue_pad.call_count, 80)

    def test_audio_modes_use_supported_low_latency_configs(self):
        self.assertEqual(drum.audio_mode_config("Low latency"), (48000, 128))
        self.assertEqual(drum.audio_mode_config("Stable"), (48000, 256))

    def test_audio_setup_applies_and_reloads_sounds(self):
        app = drum.DrumPadNative(settings_path=None)
        calls = []
        app.stop_audio_worker = lambda: calls.append("stop")
        app.start_audio_worker = lambda: calls.append("start")
        app.reload_mixer_sounds = lambda: calls.append("reload")
        app.initialize_mixer = lambda device, rate, buffer: calls.append((device, rate, buffer))
        with mock.patch.object(drum.pygame.mixer, "stop"), mock.patch.object(drum.pygame.mixer, "quit"):
            result = app.apply_audio_setup("Headphones", "Stable", 44100, 256)
        self.assertTrue(result)
        self.assertEqual((app.audio_output_name, app.audio_rate, app.audio_buffer), ("Headphones", 44100, 256))
        self.assertEqual(calls, ["stop", ("Headphones", 44100, 256), "reload", "start"])

    def test_failed_audio_setup_restores_previous_config(self):
        app = drum.DrumPadNative(settings_path=None)
        app.audio_output_name = "Working output"
        calls = []
        app.stop_audio_worker = lambda: None
        app.start_audio_worker = lambda: None
        app.reload_mixer_sounds = lambda: calls.append("reload")

        def initialize(device, rate, buffer):
            calls.append((device, rate, buffer))
            if device == "Missing output":
                raise drum.pygame.error("missing")

        app.initialize_mixer = initialize
        with mock.patch.object(drum.pygame.mixer, "stop"), mock.patch.object(drum.pygame.mixer, "quit"):
            result = app.apply_audio_setup("Missing output", "Stable", 48000, 256)
        self.assertFalse(result)
        self.assertEqual(app.audio_output_name, "Working output")
        self.assertIn(("Working output", 48000, 128), calls)
        self.assertIn("previous setup restored", app.status)


class MixerTests(unittest.TestCase):
    def test_perform_fx_each_changes_audio_without_changing_frame_count(self):
        signal = numpy.zeros((12000, 2), dtype=numpy.int16)
        signal[::97] = 14000
        for field in ("filter_amount", "delay", "stutter", "crush"):
            processed = drum.apply_perform_fx(signal, **{field: 80})
            self.assertEqual(processed.shape, signal.shape)
            self.assertFalse(numpy.array_equal(processed, signal), field)

    def test_perform_fx_changes_record_at_loop_beat_and_round_trip_pattern(self):
        app = drum.DrumPadNative(settings_path=None)
        app.loop_recording = True
        app.loop_start_ns = 1_000_000_000
        app.loop_schedule_bpm = 120
        with mock.patch.object(drum.time, "perf_counter_ns", return_value=1_500_000_000):
            app.adjust_perform_fx("delay", 40)
        beat, field, value = app.perform_fx_events[-1]
        self.assertAlmostEqual(beat, 1.0)
        self.assertEqual((field, value), ("delay", 40))
        pattern = app.current_pattern_data_locked()
        app.perform_fx_events = []
        app.apply_pattern_data_locked(pattern)
        self.assertEqual(app.perform_fx_events, [(1.0, "delay", 40)])

    def test_perform_fx_slider_maps_position_without_exposing_raw_value(self):
        app = drum.DrumPadNative(settings_path=None)
        track = drum.pygame.Rect(100, 20, 200, 20)
        self.assertTrue(app.set_perform_fx_from_position("filter", 250, track))
        self.assertEqual(app.perform_fx["filter"], 75)
        self.assertTrue(app.set_perform_fx_from_position("filter", 400, track))
        self.assertEqual(app.perform_fx["filter"], 100)

    def test_perform_fx_automation_changes_only_after_event(self):
        signal = numpy.tile(numpy.array([[1234, -1234], [7654, -7654]], dtype=numpy.int16), (4000, 1))
        processed = drum.apply_perform_fx_automation(
            signal, [(0.05, "crush", 100)], bpm=120, sample_rate=48000
        )
        boundary = round(0.05 * 48000 * 60 / 120)
        numpy.testing.assert_array_equal(processed[:boundary], signal[:boundary])
        self.assertFalse(numpy.array_equal(processed[boundary:], signal[boundary:]))

    def test_loop_bounce_assigns_next_empty_pad_and_supports_undo(self):
        app = drum.DrumPadNative(settings_path=None)
        app.loop_bars = 2
        app.loop_events = [(0.0, 0, 100), (4.0, 1, 90)]
        app.loop_playing = True
        app.loop_start_ns = 123456
        app.custom_sample_files[1] = "occupied.wav"

        class ImmediateThread:
            def __init__(self, target, args, **_kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

            def is_alive(self):
                return False

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(drum, "USER_SAMPLE_DIR", Path(directory)), \
             mock.patch.object(drum.threading, "Thread", ImmediateThread), \
             mock.patch.object(app, "render_loop_wav", side_effect=lambda path, _snapshot: path.write_bytes(b"wav")), \
             mock.patch.object(drum.pygame.mixer, "Sound", return_value=object()):
            self.assertTrue(app.start_loop_bounce())
            self.assertTrue(app.loop_playing)
            self.assertEqual(app.loop_start_ns, 123456)
            app.poll_bounce_results()
        self.assertEqual(app.selected_pad, 2)
        self.assertTrue(app.custom_sample_files[2].startswith("bounce-"))
        self.assertTrue(app.undo_project_edit())
        self.assertIsNone(app.custom_sample_files[2])

    def test_sound_macros_shape_audio_and_limiter_prevents_clipping(self):
        impulse = numpy.zeros((480, 2), dtype=numpy.int16)
        impulse[0] = 12000
        punch = drum.apply_sound_macros(impulse, punch=80)
        air = drum.apply_sound_macros(impulse, air=80)
        space = drum.apply_sound_macros(impulse, space=80)

        self.assertEqual(punch.shape, impulse.shape)
        self.assertFalse(numpy.array_equal(punch, impulse))
        self.assertFalse(numpy.array_equal(air, impulse))
        self.assertGreater(len(space), len(impulse))
        limited = drum.apply_master_limiter(numpy.array([[-90000, 90000]], dtype=numpy.float32))
        self.assertLessEqual(float(numpy.max(numpy.abs(limited))), 32767 * 0.98 + 1)

    def test_multi_pad_fx_bus_and_undo(self):
        app = drum.DrumPadNative(settings_path=None)
        app.pad_selection = {0, 1, 2}
        app.adjust_pad_fx("punch", 30)
        app.adjust_pad_fx("space", 20)
        app.cycle_pad_bus()
        self.assertEqual(app.pad_punch[:3], [30, 30, 30])
        self.assertEqual(app.pad_space[:3], [20, 20, 20])
        self.assertEqual(app.pad_bus[:3], [1, 1, 1])
        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.pad_bus[:3], [0, 0, 0])

    def test_peak_warning_only_appears_after_overlapping_gain(self):
        app = drum.DrumPadNative(settings_path=None)
        with mock.patch.object(drum.time, "perf_counter_ns", side_effect=[1_000_000_000, 1_001_000_000]), \
             mock.patch.object(drum.time, "perf_counter", return_value=10.0):
            app.track_master_peak(0.6)
            self.assertEqual(app.master_peak_warning_until, 0.0)
            app.track_master_peak(0.6)
        self.assertEqual(app.master_peak_warning_until, 11.2)

    def test_pan_gains_and_pitch_shift_are_stable(self):
        self.assertEqual(drum.stereo_pan_gains(-1), (1.0, 0.0))
        self.assertEqual(drum.stereo_pan_gains(0), (1.0, 1.0))
        self.assertEqual(drum.stereo_pan_gains(1), (0.0, 1.0))
        samples = numpy.arange(200, dtype=numpy.int16).reshape(100, 2)
        octave_up = drum.pitch_shift_array(samples, 12)
        octave_down = drum.pitch_shift_array(samples, -12)
        self.assertEqual(octave_up.shape, (50, 2))
        self.assertEqual(octave_down.shape, (200, 2))

    def test_multi_pad_mix_edit_supports_undo(self):
        app = drum.DrumPadNative(settings_path=None)
        app.pad_selection = {0, 1, 2}
        app.adjust_pad_mix("volume", -0.2)
        app.adjust_pad_mix("pan", 0.3)
        app.adjust_pad_mix("tune", 2)
        self.assertEqual(app.pad_volume[:3], [0.8, 0.8, 0.8])
        self.assertEqual(app.pad_pan[:3], [0.3, 0.3, 0.3])
        self.assertEqual(app.pad_tune[:3], [2, 2, 2])
        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.pad_tune[:3], [0, 0, 0])

    def test_mute_solo_and_bypass_control_audible_pad(self):
        app = drum.DrumPadNative(settings_path=None)
        calls = []
        app.play_layer = lambda _layer, _gain, _velocity, volume, pan, tune, *_rest: calls.append((volume, pan, tune))
        app.pad_volume[0] = 0.7
        app.pad_pan[0] = -0.4
        app.pad_tune[0] = 3
        app.play_pad(0, 80, "N20")
        self.assertTrue(calls)
        self.assertEqual(set(calls), {(0.7, -0.4, 3)})

        calls.clear()
        app.pad_mute[0] = True
        app.play_pad(0, 80, "N20")
        self.assertEqual(calls, [])
        app.pad_mute[0] = False
        app.solo_pads = {1}
        app.play_pad(0, 80, "N20")
        self.assertEqual(calls, [])

        app.solo_pads.clear()
        app.mixer_bypass = True
        app.play_pad(0, 80, "N20")
        self.assertEqual(set(calls), {(1.0, 0.0, 0)})

    def test_transient_mixer_state_does_not_leak_between_kits(self):
        app = drum.DrumPadNative(settings_path=None)
        app.selected_pad = 3
        app.pad_selection = {2, 3}
        app.solo_pads = {2, 3}
        app.mixer_bypass = True

        app.switch_kit()

        self.assertEqual(app.solo_pads, set())
        self.assertFalse(app.mixer_bypass)
        self.assertEqual(app.pad_selection, {3})


class AccessibilityTests(unittest.TestCase):
    def test_resized_letterbox_coordinates_map_to_logical_canvas(self):
        app = drum.DrumPadNative(settings_path=None)
        app.update_display_viewport((1600, 900))
        viewport = app.display_viewport
        self.assertEqual(app.window_to_logical(viewport.center), (520, 410))
        self.assertEqual(app.window_to_logical(viewport.topleft), (0, 0))

    def test_ctrl_shortcuts_use_project_undo_and_redo(self):
        app = drum.DrumPadNative(settings_path=None)
        app.undo_project_edit = mock.Mock()
        app.redo_project_edit = mock.Mock()
        with mock.patch.object(drum.pygame.key, "get_mods", return_value=drum.pygame.KMOD_CTRL):
            app.handle_key(drum.pygame.K_z)
            app.handle_key(drum.pygame.K_y)
        app.undo_project_edit.assert_called_once_with()
        app.redo_project_edit.assert_called_once_with()

    def test_tab_focus_and_arrow_keys_are_keyboard_operable(self):
        app = drum.DrumPadNative(settings_path=None)
        app.buttons = {"first": drum.pygame.Rect(10, 10, 40, 30), "second": drum.pygame.Rect(60, 10, 40, 30)}
        app.handle_mouse = mock.Mock()
        app.queue_pad = mock.Mock()
        with mock.patch.object(drum.pygame.key, "get_mods", return_value=0):
            app.handle_key(drum.pygame.K_TAB)
            self.assertEqual(app.keyboard_focus_name, "first")
            app.handle_key(drum.pygame.K_RETURN)
            app.keyboard_focus_name = None
            # Pad 0 is bottom left, so Up moves to the row above it.
            app.handle_key(drum.pygame.K_UP)
            self.assertEqual(app.selected_pad, 4)
            app.handle_key(drum.pygame.K_DOWN)
            self.assertEqual(app.selected_pad, 0)
            app.handle_key(drum.pygame.K_UP)
            app.handle_key(drum.pygame.K_RETURN)
        app.handle_mouse.assert_called_once_with((30, 25), 0)
        app.queue_pad.assert_called_once_with(4, 112)


class ProjectTests(unittest.TestCase):
    def test_legacy_combined_settings_migrate_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(json.dumps({
                "version": 1,
                "volume": 0.7,
                "bpm": 141,
                "loop_bars": 1,
                "loop_events": [[0.5, 0, 77]],
                "kits": {slot: drum.DrumPadNative.default_kit_profile() for slot in drum.KIT_SLOTS},
            }), encoding="utf-8")
            app = drum.DrumPadNative(settings_path=settings_path)
            migrated = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["version"], 2)
            self.assertNotIn("kits", migrated)
            self.assertEqual(app.bpm, 141)
            self.assertEqual(app.loop_events, [(0.5, 0, 77)])
            self.assertTrue(app.project_path.exists())

    def test_project_state_is_stored_separately_from_app_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            app.bpm = 146
            app.loop_events = [(0.25, 1, 88)]
            app.loop_source_events = [(0.19, 1, 88)]
            app.loop_event_meta = {drum.event_meta_key(1, 0.25): {"chance": 75, "ratchet": 3}}
            app.feel_preset = "Natural"
            app.feel_strength = 50
            app.patterns[1] = {
                "bars": 1, "events": [[0.0, 2, 99]], "source_events": None,
                "event_meta": {}, "feel_preset": "Natural", "feel_strength": 50,
                "feel_swing": 50, "feel_nudge_ms": 0, "feel_humanize_ms": 0,
            }
            app.scene_order = [0, 1, 0]
            app.pad_synths[0] = "ride"
            app.persist_settings()

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            project = json.loads(app.project_path.read_text(encoding="utf-8"))
            self.assertNotIn("kits", settings)
            self.assertNotIn("loop_events", settings)
            self.assertEqual(project["bpm"], 146)
            self.assertEqual(project["loop_events"], [[0.25, 1, 88]])
            self.assertEqual(project["loop_source_events"], [[0.19, 1, 88]])
            self.assertEqual(project["loop_event_meta"][drum.event_meta_key(1, 0.25)]["ratchet"], 3)
            self.assertEqual(project["patterns"][1]["events"], [[0.0, 2, 99]])
            self.assertEqual(project["scene_order"], [0, 1, 0])
            self.assertEqual(project["kits"]["A"]["pad_synths"][0], "ride")

            restored = drum.DrumPadNative(settings_path=settings_path)
            self.assertEqual(restored.bpm, 146)
            self.assertEqual(restored.loop_events, [(0.25, 1, 88)])
            self.assertEqual(restored.loop_source_events, [(0.19, 1, 88)])
            self.assertEqual(restored.loop_event_meta[drum.event_meta_key(1, 0.25)]["chance"], 75)
            self.assertEqual(restored.feel_strength, 50)
            self.assertEqual(restored.patterns[1]["events"], [(0.0, 2, 99)])
            self.assertEqual(restored.scene_order, [0, 1, 0])
            self.assertEqual(restored.pad_synths[0], "ride")

    def test_project_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            app.bpm = 133
            app.persist_settings()
            app.project_path.write_text("{broken", encoding="utf-8")

            restored = drum.DrumPadNative(settings_path=settings_path)
            self.assertEqual(restored.bpm, 133)
            self.assertEqual(restored.status, "Recovered project autosave")
            self.assertIsInstance(json.loads(restored.project_path.read_text(encoding="utf-8")), dict)

    def test_save_as_and_new_project_do_not_change_app_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            app.volume = 0.44
            app.bpm = 138
            saved = Path(directory) / "My Song.starrypad.json"
            self.assertTrue(app.save_project_as(saved))
            self.assertEqual(app.project_name, "My Song")
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["bpm"], 138)

            app.new_project()
            self.assertEqual(app.bpm, 120)
            self.assertEqual(app.volume, 0.44)
            self.assertEqual(app.loop_events, [])

    def test_collect_samples_makes_project_self_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            source = drum.USER_SAMPLE_DIR / "project-test-sample.wav"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"sample")
            try:
                app.custom_sample_files[0] = source.name
                app.save_current_kit()
                app.save_project_as(Path(directory) / "Portable.starrypad.json")
                self.assertTrue(app.collect_project_samples())
                collected = Path(directory) / "Portable.samples" / source.name
                self.assertEqual(collected.read_bytes(), b"sample")
            finally:
                source.unlink(missing_ok=True)

    def test_pad_edits_support_twenty_step_undo_and_redo(self):
        app = drum.DrumPadNative(settings_path=None)
        original_sound = app.pad_synths[0]
        app.cycle_pad_sound(1)
        changed_sound = app.pad_synths[0]
        app.adjust_pad_sensitivity(0.1)
        self.assertEqual(len(app.project_history), 2)

        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.pad_sensitivity[0], 1.0)
        self.assertEqual(app.pad_synths[0], changed_sound)
        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.pad_synths[0], original_sound)
        self.assertTrue(app.redo_project_edit())
        self.assertEqual(app.pad_synths[0], changed_sound)

        for _ in range(25):
            app.adjust_pad_sensitivity(0.05)
        self.assertEqual(len(app.project_history), 20)

    def test_project_undo_keeps_loop_playing(self):
        app = drum.DrumPadNative(settings_path=None)
        app.loop_events = [(0.0, 0, 80)]
        app.loop_playing = True
        app.loop_start_ns = time.perf_counter_ns() - 100_000_000
        app.loop_schedule_bpm = app.bpm
        app.cycle_pad_sound(1)
        self.assertTrue(app.undo_project_edit())
        self.assertTrue(app.loop_playing)
        self.assertIsNotNone(app.loop_start_ns)


class KitTests(unittest.TestCase):
    def test_only_hihats_use_choke_groups(self):
        choked_synths = {
            synth
            for synth, layers in drum.KIT.items()
            if any(layer.get("choke") for layer in layers)
        }
        expected = set(drum.TIMBRE_FAMILIES[1] + drum.TIMBRE_FAMILIES[2] + ("hat_semi",))
        self.assertEqual(choked_synths, expected)
        self.assertEqual(
            {layer.get("choke") for synth in expected for layer in drum.KIT[synth]},
            {"hat"},
        )

    def test_requested_timbre_families_have_four_layered_choices(self):
        self.assertEqual([len(family) for family in drum.TIMBRE_FAMILIES], [4, 4, 4, 4])
        for family in drum.TIMBRE_FAMILIES:
            for synth in family:
                for layer in drum.KIT[synth]:
                    zones = layer.get("velocity_files", {})
                    self.assertEqual(set(zones), {"soft", "mid", "hard"})
                    self.assertTrue(all(len(files) >= 2 for files in zones.values()), synth)

    def test_sound_buttons_cycle_inside_major_instrument_family(self):
        app = drum.DrumPadNative(settings_path=None)
        for pad_index, family in ((1, drum.TIMBRE_FAMILIES[0]), (2, drum.TIMBRE_FAMILIES[1]), (12, drum.TIMBRE_FAMILIES[3])):
            app.selected_pad = pad_index
            app.pad_synths[pad_index] = family[0]
            visited = []
            for _ in range(4):
                visited.append(app.pad_synths[pad_index])
                app.cycle_pad_sound(1)
            self.assertEqual(tuple(visited), family)

    def test_timbre_choice_round_trips_through_kit_profile(self):
        app = drum.DrumPadNative(settings_path=None)
        app.pad_synths[1] = "snare_deep"
        app.pad_synths[2] = "hat_dark"
        app.pad_synths[3] = "open_hat_dark"
        app.pad_synths[12] = "ride_washy"
        app.save_current_kit()

        restored = drum.DrumPadNative(settings_path=None)
        restored.apply_kit_profile(app.kit_slots["A"])
        self.assertEqual(
            [restored.pad_synths[index] for index in (1, 2, 3, 12)],
            ["snare_deep", "hat_dark", "open_hat_dark", "ride_washy"],
        )

    def test_gm_hat_pedal_selects_closed_semi_and_matching_open_articulation(self):
        app = drum.DrumPadNative(settings_path=None)
        app.mapping_mode = drum.MAPPING_MODES.index("GM Drums")
        layers = []
        app.play_layer = lambda layer, *_args: layers.append(layer)
        hat_index = drum.PAD_NAME_TO_INDEX["Closed Hat"]
        app.pad_synths[hat_index] = "hat_dark"

        app.handle_midi_trigger("CC", 4, 0)
        app.play_pad(hat_index, 80, "N42")
        self.assertIs(layers[-1], drum.KIT["hat_dark"][0])
        app.handle_midi_trigger("CC", 4, 64)
        app.play_pad(hat_index, 80, "N42")
        self.assertIs(layers[-1], drum.KIT["hat_semi"][0])
        app.handle_midi_trigger("CC", 4, 127)
        app.play_pad(hat_index, 80, "N42")
        self.assertIs(layers[-1], drum.KIT["open_hat_dark"][0])

    def test_all_referenced_samples_exist(self):
        missing = [
            name
            for name in drum.all_sample_files()
            if not (drum.SAMPLE_DIR / name).exists()
        ]
        self.assertEqual(missing, [])

    def test_soft_snare_attacks_are_timing_aligned(self):
        attacks = {
            name: wav_attack_ms(drum.SAMPLE_DIR / name)
            for name in drum.S["snare_soft"]
        }
        self.assertLessEqual(
            max(attacks.values()) - min(attacks.values()),
            3.0,
            attacks,
        )


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.app = drum.DrumPadNative(settings_path=None)

    def test_donner_mapping_is_strict(self):
        self.assertEqual(self.app.resolve_preset_pad("N", 20), 0)
        self.assertEqual(self.app.resolve_preset_pad("N", 35), 15)
        self.assertEqual(self.app.resolve_preset_pad("N", 36), 0)
        self.assertIsNone(self.app.resolve_preset_pad("N", 90))
        self.assertIsNone(self.app.resolve_preset_pad("CC", 2))
        self.assertIsNone(self.app.resolve_preset_pad("PC", 2))

    def test_gm_mapping_does_not_fall_back_to_modulo(self):
        self.app.mapping_mode = drum.MAPPING_MODES.index("GM Drums")
        self.assertEqual(
            self.app.resolve_preset_pad("N", 38),
            drum.PAD_NAME_TO_INDEX["Snare"],
        )
        self.assertIsNone(self.app.resolve_preset_pad("N", 20))
        self.assertIsNone(self.app.resolve_preset_pad("CC", 20))


class PadSwapTests(unittest.TestCase):
    """Rearranging the layout moves the sound, not the physical pad's response."""

    def setUp(self):
        self.app = drum.DrumPadNative(settings_path=None)
        self.kick = drum.PAD_NAME_TO_INDEX["Kick"]
        self.snare = drum.PAD_NAME_TO_INDEX["Snare"]

    def test_sound_travels_and_calibration_stays_put(self):
        app = self.app
        app.pad_synths[self.kick] = "kick"
        app.pad_synths[self.snare] = "snare"
        app.pad_volume[self.kick] = 0.4
        app.custom_sample_files[self.snare] = "take.wav"
        app.pad_sensitivity[self.kick] = 1.4
        app.pad_calibrations[self.kick]["enabled"] = True

        self.assertTrue(app.swap_pads(self.kick, self.snare))

        self.assertEqual(app.pad_synths[self.kick], "snare")
        self.assertEqual(app.pad_synths[self.snare], "kick")
        self.assertEqual(app.pad_volume[self.snare], 0.4)
        self.assertEqual(app.custom_sample_files[self.kick], "take.wav")
        # Sensitivity and calibration describe the rubber, so they do not move.
        self.assertEqual(app.pad_sensitivity[self.kick], 1.4)
        self.assertTrue(app.pad_calibrations[self.kick]["enabled"])

    def test_recorded_hits_follow_the_sound_so_the_take_is_unchanged(self):
        app = self.app
        app.loop_events = [(0.0, self.kick, 100), (1.0, self.snare, 90), (2.0, 7, 80)]
        app.loop_event_meta = {drum.event_meta_key(self.kick, 0.0): {"chance": 50, "ratchet": 2}}

        app.swap_pads(self.kick, self.snare)

        self.assertEqual(
            app.loop_events,
            sorted([(0.0, self.snare, 100), (1.0, self.kick, 90), (2.0, 7, 80)]),
        )
        self.assertEqual(
            app.loop_event_meta[drum.event_meta_key(self.snare, 0.0)],
            {"chance": 50, "ratchet": 2},
        )

    def test_stored_patterns_are_remapped_too(self):
        app = self.app
        # Slot 0 is the active one and is synced from the live loop, so a
        # stored slot is what proves the remap reaches saved patterns.
        app.patterns[1] = app.sanitize_pattern_data(
            {"bars": 1, "events": [[0.5, self.kick, 110]], "event_meta": {}}
        )
        app.swap_pads(self.kick, self.snare)
        self.assertEqual(app.patterns[1]["events"], [(0.5, self.snare, 110)])

    def test_one_undo_restores_the_layout(self):
        app = self.app
        app.pad_synths[self.kick] = "kick"
        app.swap_pads(self.kick, self.snare)
        self.assertNotEqual(app.pad_synths[self.kick], "kick")
        app.undo_project_edit()
        self.assertEqual(app.pad_synths[self.kick], "kick")

    def test_a_pad_cannot_swap_with_itself(self):
        self.assertFalse(self.app.swap_pads(self.kick, self.kick))

    def test_drag_below_the_threshold_does_not_swap(self):
        app = self.app
        rects = app.pad_rects()
        app.begin_pad_drag(self.kick, rects[self.kick].center)
        app.update_pad_drag((rects[self.kick].centerx + 3, rects[self.kick].centery))
        self.assertIsNone(app.pad_drag_over)
        self.assertFalse(app.finish_pad_drag(rects[self.snare].center))

    def test_dragging_onto_another_pad_swaps_and_selects_it(self):
        app = self.app
        app.pad_synths[self.kick] = "kick"
        rects = app.pad_rects()
        app.begin_pad_drag(self.kick, rects[self.kick].center)
        app.update_pad_drag(rects[self.snare].center)
        self.assertEqual(app.pad_drag_over, self.snare)
        self.assertTrue(app.finish_pad_drag(rects[self.snare].center))
        self.assertEqual(app.pad_synths[self.snare], "kick")
        self.assertEqual(app.selected_pad, self.snare)
        self.assertIsNone(app.pad_drag_from)

    def test_command_arrow_moves_the_sound_and_follows_it(self):
        import pygame

        app = self.app
        app.selected_pad = self.kick
        app.pad_synths[self.kick] = "kick"
        with mock.patch.object(pygame.key, "get_mods", return_value=drum.COMMAND_MODIFIER):
            self.assertTrue(app.handle_key(pygame.K_UP))
        self.assertEqual(app.selected_pad, self.kick + 4)
        self.assertEqual(app.pad_synths[self.kick + 4], "kick")


class QuitShortcutTests(unittest.TestCase):
    """Esc closes panels; quitting takes the platform chord."""

    def setUp(self):
        import pygame

        self.pygame = pygame
        self.app = drum.DrumPadNative(settings_path=None)

    def press(self, key, mods=0):
        with mock.patch.object(self.pygame.key, "get_mods", return_value=mods):
            return self.app.handle_key(key)

    def test_escape_with_nothing_open_names_the_quit_chord_instead_of_quitting(self):
        self.assertTrue(self.press(self.pygame.K_ESCAPE))
        self.assertIn(drum.QUIT_SHORTCUT.casefold(), self.app.status.casefold())

    def test_escape_closes_the_open_panel_first(self):
        self.app.settings_open = True
        self.assertTrue(self.press(self.pygame.K_ESCAPE))
        self.assertFalse(self.app.settings_open)

    def test_command_q_quits(self):
        self.assertFalse(self.press(self.pygame.K_q, drum.COMMAND_MODIFIER))

    def test_plain_q_still_quantizes_rather_than_quitting(self):
        self.assertTrue(self.press(self.pygame.K_q))


class MidiCallbackTests(unittest.TestCase):
    """Decoding is shared by every backend, so this runs on Windows and macOS."""

    def setUp(self):
        self.input = drum.MidiInput.__new__(drum.MidiInput)
        self.input.event_queue = queue.SimpleQueue()

    def emit(self, status, data1, data2=0):
        self.input._emit(status, data1, data2, time.perf_counter_ns())

    def test_note_on_is_timestamped(self):
        self.emit(0x99, 20, 47)
        event = self.input.event_queue.get_nowait()
        self.assertEqual(event[:4], ("MIDI", "N", 20, 47))
        self.assertGreater(event[4], 0)

    def test_note_off_is_reported_and_zero_cc_does_not_retrigger(self):
        self.emit(0x99, 20, 0)
        self.emit(0xB9, 20, 0)
        event = self.input.event_queue.get_nowait()
        self.assertEqual(event[:4], ("MIDI_OFF", "N", 20, 0))
        with self.assertRaises(queue.Empty):
            self.input.event_queue.get_nowait()

    def test_realtime_clock_and_transport_are_forwarded(self):
        for status in (0xF8, 0xFA, 0xFB, 0xFC):
            self.emit(status, 0, 0)
            event = self.input.event_queue.get_nowait()
            self.assertEqual(event[:2], ("MIDI_CLOCK", status))
            self.assertGreater(event[2], 0)


@unittest.skipUnless(sys.platform == "darwin", "CoreMIDI backend is macOS only")
class CoreMidiPacketTests(unittest.TestCase):
    """Push real packet lists through CoreMIDI into the macOS input backend.

    A virtual source is the only way to cover packet walking, running status,
    and the 4-byte MIDIPacketNext rounding that Apple Silicon requires.
    """

    def setUp(self):
        import ctypes
        import platform_backend

        self.ctypes = ctypes
        self.backend = platform_backend
        coremidi = platform_backend.coremidi
        coremidi.MIDISourceCreate.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        coremidi.MIDISourceCreate.restype = ctypes.c_int32
        coremidi.MIDIReceived.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        coremidi.MIDIReceived.restype = ctypes.c_int32
        coremidi.MIDIEndpointDispose.argtypes = [ctypes.c_uint32]

        name = platform_backend._cfstring("DrumPadPacketTest")
        self.source = ctypes.c_uint32()
        try:
            status = coremidi.MIDISourceCreate(
                platform_backend._shared_client(), name, ctypes.byref(self.source)
            )
        finally:
            platform_backend.corefoundation.CFRelease(name)
        self.assertEqual(status, 0, "MIDISourceCreate failed")

        index = self.wait_for(
            lambda: next(
                (i for i, n in platform_backend.MidiInput.devices() if n == "DrumPadPacketTest"),
                None,
            )
        )
        self.assertIsNotNone(index, "virtual source never appeared")
        self.queue = queue.SimpleQueue()
        self.input = platform_backend.MidiInput(index, self.queue)
        self.addCleanup(coremidi.MIDIEndpointDispose, self.source)
        self.addCleanup(self.input.close)

    @staticmethod
    def wait_for(probe, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = probe()
            if result is not None:
                return result
            time.sleep(0.02)
        return None

    def send(self, *payloads):
        blob = struct.pack("<I", len(payloads))
        for payload in payloads:
            blob += struct.pack("<QH", 0, len(payload)) + payload
            blob += b"\x00" * (-len(blob) % 4)
        buffer = self.ctypes.create_string_buffer(blob, len(blob))
        self.assertEqual(self.backend.coremidi.MIDIReceived(self.source, buffer), 0)

    def drain(self, count):
        events = []
        deadline = time.monotonic() + 3.0
        while len(events) < count and time.monotonic() < deadline:
            try:
                events.append(self.queue.get(timeout=0.05)[:-1])
            except queue.Empty:
                continue
        return events

    def test_packet_list_decodes_every_message_shape(self):
        self.send(bytes([0x99, 38, 100]))
        self.send(bytes([0x99, 38, 0]))
        self.send(bytes([0x89, 38, 64]))
        self.send(bytes([0xB9, 20, 90]), bytes([0xF8]))          # two packets in one list
        self.send(bytes([0xC9, 5]))
        self.send(bytes([0x99, 40, 70, 42, 80]))                 # running status
        self.send(bytes([0xF0, 0x7E, 0x01, 0xF7, 0x99, 44, 55]))  # sysex is skipped
        self.send(bytes([0xFA]), bytes([0xFC]))

        self.assertEqual(
            self.drain(11),
            [
                ("MIDI", "N", 38, 100),
                ("MIDI_OFF", "N", 38, 0),
                ("MIDI_OFF", "N", 38, 64),
                ("MIDI", "CC", 20, 90),
                ("MIDI_CLOCK", 0xF8),
                ("MIDI", "PC", 5, 127),
                ("MIDI", "N", 40, 70),
                ("MIDI", "N", 42, 80),
                ("MIDI", "N", 44, 55),
                ("MIDI_CLOCK", 0xFA),
                ("MIDI_CLOCK", 0xFC),
            ],
        )


class MidiSyncTests(unittest.TestCase):
    def test_clock_interval_converts_to_bpm(self):
        interval = round(60_000_000_000 / (123 * 24))
        self.assertAlmostEqual(drum.midi_clock_bpm([interval] * 96), 123, places=2)

    def test_external_transport_locks_phase_for_ten_minutes(self):
        app = drum.DrumPadNative(settings_path=None)
        app.clock_source = "External"
        app.loop_events = [(0.0, 0, 90)]
        app.play_pad = lambda *_args: None
        start = 1_000_000_000
        interval = round(60_000_000_000 / (120 * 24))
        app.handle_midi_clock(0xFA, start)
        ticks = 120 * 24 * 10
        for tick in range(1, ticks + 1):
            app.handle_midi_clock(0xF8, start + tick * interval)
        self.assertEqual(app.clock_active_source, "External")
        self.assertEqual(app.bpm, 120)
        self.assertLess(abs(app.loop_start_ns - start), interval)
        sixteenth_ns = 60_000_000_000 / 120 / 4
        phase_error = abs((start + ticks * interval - app.loop_start_ns) % (4 * 60_000_000_000 / 120))
        self.assertTrue(phase_error < sixteenth_ns or phase_error > 4 * 60_000_000_000 / 120 - sixteenth_ns)
        app.handle_midi_clock(0xFC, start + ticks * interval)
        self.assertFalse(app.loop_playing)

    def test_internal_clock_outputs_start_ticks_and_stop(self):
        class Output:
            def __init__(self): self.messages = []
            def send(self, status): self.messages.append(status)
        app = drum.DrumPadNative(settings_path=None)
        app.clock_output_enabled = True
        app.clock_active_source = "Internal"
        app.midi_output = Output()
        app.loop_events = [(0.0, 0, 90)]
        start = 10_000_000_000
        app.handle_loop_command("PLAY", now_ns=start)
        interval = round(60_000_000_000 / (app.bpm * 24))
        app.process_midi_clock_output(start + interval * 3)
        app.handle_loop_command("PLAY", now_ns=start + interval * 4)
        self.assertEqual(app.midi_output.messages[0], 0xFA)
        self.assertEqual(app.midi_output.messages[-1], 0xFC)
        self.assertGreaterEqual(app.midi_output.messages.count(0xF8), 4)

    def test_auto_clock_falls_back_after_timeout(self):
        app = drum.DrumPadNative(settings_path=None)
        app.clock_source = "Auto"
        app.clock_active_source = "External"
        app.external_transport_running = True
        app.last_midi_clock_ns = 1_000_000_000
        with mock.patch.object(drum.time, "perf_counter_ns", return_value=2_100_000_001):
            app.process_scheduled_events()
        self.assertEqual(app.clock_active_source, "Internal")
        self.assertFalse(app.external_transport_running)


class MidiConnectionTests(unittest.TestCase):
    def test_first_connection_suggests_calibration_only_once(self):
        app = drum.DrumPadNative(settings_path=None)
        fake = mock.Mock()
        with mock.patch.object(drum, "MidiInput") as midi_class:
            midi_class.return_value = fake
            midi_class.devices.return_value = [(0, "STARRYPAD MINI")]
            self.assertTrue(app.open_midi(0))
            self.assertTrue(app.calibration_prompted)
            self.assertIn("calibrate", app.status.casefold())
            app.status = "unchanged"
            self.assertTrue(app.open_midi(0))
            self.assertNotIn("calibrate", app.status.casefold())

    def test_removed_device_requests_panic_and_marks_disconnected(self):
        app = drum.DrumPadNative(settings_path=None)

        class FakeInput:
            closed = False

            def close(self):
                self.closed = True

        midi_input = FakeInput()
        app.midi_input = midi_input
        app.midi_device_id = 4
        app.midi_device_name = "STARRYPAD MINI"
        app.preferred_midi_name = "STARRYPAD MINI"
        app.midi_devices = lambda: []

        app.maintain_midi_connection(now=10.0)

        self.assertTrue(midi_input.closed)
        self.assertIsNone(app.midi_input)
        self.assertEqual(app.status, "MIDI disconnected")
        self.assertEqual(app.audio_events.get_nowait(), ("PANIC",))

    def test_same_device_is_reopened_when_it_returns(self):
        app = drum.DrumPadNative(settings_path=None)
        app.preferred_midi_name = "STARRYPAD MINI"
        app.midi_disconnect_notified = True
        app.midi_devices = lambda: [(9, "STARRYPAD MINI", 0)]
        opened = []

        def open_midi(device_id):
            opened.append(device_id)
            return True

        app.open_midi = open_midi
        app.maintain_midi_connection(now=10.0)
        self.assertEqual(opened, [9])

    def test_reconnect_does_not_switch_to_unrelated_device(self):
        app = drum.DrumPadNative(settings_path=None)
        app.preferred_midi_name = "STARRYPAD MINI"
        app.midi_devices = lambda: [(2, "Other Controller", 0)]
        opened = []
        app.open_midi = lambda device_id: opened.append(device_id) or True

        app.maintain_midi_connection(now=10.0)
        self.assertEqual(opened, [])


class KitAndSettingsTests(unittest.TestCase):
    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            app.pad_synths[0] = "ride"
            app.pad_sensitivity[0] = 1.25
            app.pad_volume[0] = 0.75
            app.pad_pan[0] = -0.3
            app.pad_tune[0] = 4
            app.pad_mute[0] = True
            app.bpm = 138
            app.repeat_rate = "1/16T"
            app.loop_bars = 2
            app.loop_events = [(0.5, 0, 70), (2.25, 1, 100)]
            app.custom_sample_files[0] = "pad-01-test.wav"
            app.sample_input_name = "Test microphone"
            app.sample_start_mode = "Manual"
            app.record_start_mode = "Next bar"
            app.pad_calibrations[0] = {
                "enabled": True, "soft": 42, "natural": 73, "hard": 105, "dead_time_ms": 12
            }
            app.audio_output_name = "Test output"
            app.audio_mode = "Stable"
            app.audio_rate = 44100
            app.audio_buffer = 256
            app.ui_scale = 1.5
            app.perform_fx_events = [(1.0, "delay", 60)]
            app.persist_settings()

            restored = drum.DrumPadNative(settings_path=settings_path)
            self.assertEqual(restored.pad_synths[0], "ride")
            self.assertEqual(restored.pad_sensitivity[0], 1.25)
            self.assertEqual(restored.pad_volume[0], 0.75)
            self.assertEqual(restored.pad_pan[0], -0.3)
            self.assertEqual(restored.pad_tune[0], 4)
            self.assertTrue(restored.pad_mute[0])
            self.assertEqual(restored.bpm, 138)
            self.assertEqual(restored.repeat_rate, "1/16T")
            self.assertEqual(restored.loop_bars, 2)
            self.assertEqual(restored.loop_events, [(0.5, 0, 70), (2.25, 1, 100)])
            self.assertEqual(restored.custom_sample_files[0], "pad-01-test.wav")
            self.assertEqual(restored.sample_input_name, "Test microphone")
            self.assertEqual(restored.sample_start_mode, "Manual")
            self.assertEqual(restored.record_start_mode, "Next bar")
            self.assertEqual(restored.pad_calibrations[0]["soft"], 42)
            self.assertTrue(restored.pad_calibrations[0]["enabled"])
            self.assertEqual(restored.audio_output_name, "Test output")
            self.assertEqual((restored.audio_mode, restored.audio_rate, restored.audio_buffer), ("Stable", 44100, 256))
            self.assertEqual(restored.ui_scale, 1.5)
            self.assertEqual(restored.perform_fx_events, [(1.0, "delay", 60)])
            self.assertTrue(settings_path.with_suffix(".json.bak").exists())

    def test_corrupt_settings_are_recovered_from_latest_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            app = drum.DrumPadNative(settings_path=settings_path)
            app.volume = 0.44
            app.bpm = 132
            app.loop_events = [(0.5, 1, 88)]
            app.persist_settings()
            settings_path.write_text("{broken", encoding="utf-8")

            restored = drum.DrumPadNative(settings_path=settings_path)

            self.assertEqual(restored.volume, 0.44)
            self.assertEqual(restored.bpm, 132)
            self.assertEqual(restored.loop_events, [(0.5, 1, 88)])
            self.assertEqual(restored.status, "Recovered saved session")
            self.assertIsInstance(json.loads(settings_path.read_text(encoding="utf-8")), dict)

    def test_kit_slots_preserve_independent_profiles(self):
        app = drum.DrumPadNative(settings_path=None)
        app.pad_sensitivity[0] = 1.3
        app.switch_kit()
        self.assertEqual(app.active_kit, "B")
        self.assertEqual(app.pad_sensitivity[0], 1.0)
        app.pad_synths[0] = "ride"
        app.switch_kit()
        app.switch_kit()
        app.switch_kit()
        self.assertEqual(app.active_kit, "A")
        self.assertEqual(app.pad_sensitivity[0], 1.3)
        self.assertEqual(app.pad_synths[0], "kick")

    def test_pad_sensitivity_changes_effective_velocity(self):
        app = drum.DrumPadNative(settings_path=None)
        app.pad_sensitivity[0] = 1.2
        played_velocities = []
        app.play_layer = lambda _layer, _gain, raw_velocity, *_mix: played_velocities.append(raw_velocity)
        app.play_pad(0, 50, "N20")
        self.assertTrue(played_velocities)
        self.assertEqual(set(played_velocities), {60})

    def test_calibration_collects_three_strengths_and_rejects_flat_hits(self):
        app = drum.DrumPadNative(settings_path=None)
        app.start_pad_calibration()
        for velocity in (30, 32, 31, 70, 72, 71, 105, 108, 106):
            app.collect_calibration_hit(0, velocity)
        self.assertFalse(app.calibration_active)
        self.assertEqual(app.pad_calibrations[0]["soft"], 31)
        self.assertEqual(app.pad_calibrations[0]["natural"], 71)
        self.assertEqual(app.pad_calibrations[0]["hard"], 106)

        app.start_pad_calibration()
        for velocity in (60,) * 9:
            app.collect_calibration_hit(0, velocity)
        self.assertTrue(app.calibration_active)
        self.assertFalse(app.pad_calibrations[0]["enabled"] is False)
        self.assertIn("too similar", app.status)

    def test_calibration_learns_dead_time_from_duplicate_pulses(self):
        app = drum.DrumPadNative(settings_path=None)
        app.play_pad = lambda *_args: None
        app.start_pad_calibration()
        valid = (30, 32, 31, 70, 72, 71, 105, 107, 106)
        start = 1_000_000_000
        for index, velocity in enumerate(valid):
            timestamp = start + index * 200_000_000
            app.handle_midi_trigger("N", 20, velocity, timestamp)
            app.handle_midi_trigger("N", 20, velocity, timestamp + 12_000_000)
        self.assertFalse(app.calibration_active)
        self.assertEqual(app.pad_calibrations[0]["dead_time_ms"], 14)
        self.assertTrue(app.pad_calibrations[0]["enabled"])

    def test_dead_time_suppresses_only_near_duplicate_hardware_hits(self):
        app = drum.DrumPadNative(settings_path=None)
        played = []
        app.play_pad = lambda index, velocity, label: played.append((index, velocity, label))
        first = 1_000_000_000
        app.handle_midi_trigger("N", 20, 70, first)
        app.handle_midi_trigger("N", 20, 71, first + 5_000_000)
        app.handle_midi_trigger("N", 20, 72, first + 15_000_000)
        self.assertEqual([item[1] for item in played], [70, 72])


class TimingFeatureTests(unittest.TestCase):
    def test_repeat_intervals_follow_bpm(self):
        self.assertAlmostEqual(drum.repeat_interval_seconds("1/8", 120), 0.25)
        self.assertAlmostEqual(drum.repeat_interval_seconds("1/16", 120), 0.125)
        self.assertAlmostEqual(drum.repeat_interval_seconds("1/16T", 120), 1 / 12)
        self.assertAlmostEqual(drum.repeat_interval_seconds("1/32", 120), 0.0625)

    def test_repeat_stops_on_note_off(self):
        app = drum.DrumPadNative(settings_path=None)
        app.repeat_enabled = True
        played = []
        app.play_pad = lambda index, velocity, label: played.append((index, velocity, label))
        app.handle_midi_trigger("N", 20, 70, time.perf_counter_ns())
        self.assertEqual(len(played), 1)
        self.assertIn(("N", 20), app.held_triggers)

        app.held_triggers[("N", 20)]["next_ns"] = time.perf_counter_ns() - 1
        app.process_scheduled_events()
        self.assertEqual(len(played), 2)

        app.handle_midi_release("N", 20)
        self.assertNotIn(("N", 20), app.held_triggers)

    def test_partial_quantize_keeps_half_the_original_offset(self):
        result = drum.apply_loop_feel([(0.13, 0, 70)], 1, 0.25, 50, 50, 0, 0, 120)
        self.assertAlmostEqual(result[0][0], 0.19)

    def test_swing_moves_only_every_second_grid_step(self):
        result = drum.apply_loop_feel(
            [(0.25, 0, 70), (0.5, 1, 80)], 1, 0.25, 100, 75, 0, 0, 120
        )
        self.assertAlmostEqual(result[0][0], 0.375)
        self.assertAlmostEqual(result[1][0], 0.5)

    def test_humanize_is_deterministic_and_nudge_uses_milliseconds(self):
        events = [(0.0, 0, 70), (0.5, 1, 80)]
        first = drum.apply_loop_feel(events, 1, 0.25, 0, 50, 10, 5, 120)
        second = drum.apply_loop_feel(events, 1, 0.25, 0, 50, 10, 5, 120)
        self.assertEqual(first, second)
        without_nudge = drum.apply_loop_feel(events, 1, 0.25, 0, 50, 0, 5, 120)
        self.assertAlmostEqual(first[0][0] - without_nudge[0][0], 0.02)


class LooperTests(unittest.TestCase):
    def setUp(self):
        self.app = drum.DrumPadNative(settings_path=None)
        self.app.record_start_mode = "Instant"

    def test_recorded_hit_uses_musical_beat_position(self):
        self.app.handle_loop_command("RECORD")
        start_ns = self.app.loop_start_ns
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)
        self.app.record_loop_hit(0, 72, start_ns + quarter_ns)
        self.assertEqual(len(self.app.loop_events), 1)
        beat, pad, velocity = self.app.loop_events[0]
        self.assertAlmostEqual(beat, 1.0, places=3)
        self.assertEqual((pad, velocity), (0, 72))

    def test_hit_after_fixed_recording_boundary_is_not_wrapped(self):
        self.app.handle_loop_command("RECORD")
        start_ns = self.app.loop_start_ns
        self.app.record_loop_hit(0, 72, start_ns + self.app.loop_length_ns_locked() + 1)
        self.assertEqual(self.app.loop_events, [])

    def test_fixed_recording_length_transitions_to_playback(self):
        self.app.handle_loop_command("RECORD")
        start_ns = self.app.loop_start_ns
        self.app.record_loop_hit(0, 70, start_ns)
        self.app.play_pad = lambda _pad, _velocity, _label: None
        boundary = start_ns + self.app.loop_length_ns_locked()
        self.app.process_loop_scheduler(boundary)
        self.assertFalse(self.app.loop_recording)
        self.assertTrue(self.app.loop_playing)

    def test_overdub_can_be_undone_as_one_operation(self):
        self.app.loop_events = [(0.0, 0, 70)]
        self.app.handle_loop_command("OVERDUB")
        start_ns = self.app.loop_start_ns
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)
        self.app.record_loop_hit(1, 90, start_ns + quarter_ns)
        self.app.handle_loop_command("OVERDUB")
        self.assertEqual(len(self.app.loop_events), 2)
        self.app.handle_loop_command("UNDO")
        self.assertEqual(self.app.loop_events, [(0.0, 0, 70)])

    def test_loop_undo_can_be_redone(self):
        self.app.loop_events = [(0.0, 0, 70)]
        self.app.handle_loop_command("CLEAR", now_ns=1_000_000_000)
        self.app.handle_loop_command("UNDO", now_ns=2_000_000_000)
        self.assertEqual(self.app.loop_events, [(0.0, 0, 70)])
        self.app.handle_loop_command("REDO", now_ns=3_000_000_000)
        self.assertEqual(self.app.loop_events, [])

    def test_capture_recovers_recent_performance_and_supports_undo(self):
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)
        start_ns = 10_000_000_000
        self.app.loop_events = [(0.0, 4, 80)]
        self.app.record_performance_hit(0, 72, start_ns)
        self.app.record_performance_hit(1, 96, start_ns + quarter_ns)

        self.app.handle_loop_command("CAPTURE", now_ns=start_ns + 2 * quarter_ns)

        self.assertEqual(self.app.loop_bars, 1)
        self.assertEqual(self.app.loop_events, [(0.0, 0, 72), (1.0, 1, 96)])
        self.assertTrue(self.app.loop_playing)
        self.app.handle_loop_command("UNDO", now_ns=start_ns + 3 * quarter_ns)
        self.assertEqual(self.app.loop_events, [(0.0, 4, 80)])

    def test_capture_uses_hits_after_the_last_long_pause(self):
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)
        start_ns = 10_000_000_000
        self.app.record_performance_hit(5, 60, start_ns)
        later_ns = start_ns + int((drum.PERFORMANCE_PHRASE_GAP_SECONDS + 0.5) * 1_000_000_000)
        self.app.record_performance_hit(0, 70, later_ns)
        self.app.record_performance_hit(1, 90, later_ns + quarter_ns)

        self.app.handle_loop_command("CAPTURE", now_ns=later_ns + 2 * quarter_ns)
        self.assertEqual(self.app.loop_events, [(0.0, 0, 70), (1.0, 1, 90)])

    def test_count_in_preserves_old_loop_until_recording_starts(self):
        self.app.record_start_mode = "Count 1 bar"
        self.app.loop_events = [(0.0, 4, 80)]
        start_ns = 10_000_000_000
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)

        self.app.handle_loop_command("RECORD", now_ns=start_ns)

        self.assertTrue(self.app.loop_record_pending)
        self.assertFalse(self.app.loop_recording)
        self.assertEqual(self.app.loop_events, [(0.0, 4, 80)])
        self.app.process_pending_record(start_ns + 4 * quarter_ns)
        self.assertFalse(self.app.loop_record_pending)
        self.assertTrue(self.app.loop_recording)
        self.assertEqual(self.app.loop_events, [])
        self.app.handle_loop_command("UNDO", now_ns=start_ns + 5 * quarter_ns)
        self.assertEqual(self.app.loop_events, [(0.0, 4, 80)])

    def test_second_record_press_cancels_count_in_without_data_loss(self):
        self.app.record_start_mode = "Count 1 bar"
        self.app.loop_events = [(0.0, 2, 75)]
        start_ns = 10_000_000_000
        self.app.handle_loop_command("RECORD", now_ns=start_ns)
        self.app.handle_loop_command("RECORD", now_ns=start_ns + 100_000_000)
        self.assertFalse(self.app.loop_record_pending)
        self.assertFalse(self.app.loop_recording)
        self.assertEqual(self.app.loop_events, [(0.0, 2, 75)])

    def test_quantize_uses_current_repeat_grid(self):
        self.app.repeat_rate = "1/16"
        self.app.loop_events = [(0.13, 0, 70), (0.39, 1, 80)]
        self.app.handle_loop_command("QUANTIZE")
        self.assertEqual(self.app.loop_events, [(0.25, 0, 70), (0.5, 1, 80)])

    def test_natural_feel_is_non_destructive_and_reset_restores_exact_timing(self):
        original = [(0.13, 0, 70), (0.39, 1, 80)]
        self.app.loop_events = list(original)
        self.assertTrue(self.app.set_feel_preset("Natural", now_ns=1_000_000_000))
        self.assertEqual(self.app.loop_source_events, original)
        self.assertAlmostEqual(self.app.loop_events[0][0], 0.19)
        self.assertTrue(self.app.reset_loop_feel(now_ns=2_000_000_000))
        self.assertEqual(self.app.loop_events, original)
        self.assertIsNone(self.app.loop_source_events)

    def test_feel_changes_support_undo_and_redo(self):
        original = [(0.13, 0, 70)]
        self.app.loop_events = list(original)
        self.app.set_feel_preset("Tight", now_ns=1_000_000_000)
        self.assertEqual(self.app.loop_events, [(0.25, 0, 70)])
        self.app.handle_loop_command("UNDO", now_ns=2_000_000_000)
        self.assertEqual(self.app.loop_events, original)
        self.assertIsNone(self.app.loop_source_events)
        self.app.handle_loop_command("REDO", now_ns=3_000_000_000)
        self.assertEqual(self.app.loop_events, [(0.25, 0, 70)])
        self.assertEqual(self.app.loop_source_events, original)

    def test_sequence_grid_adds_removes_and_undoes_steps(self):
        self.assertTrue(self.app.toggle_sequence_step(0, 0, 4, now_ns=1_000_000_000))
        self.assertEqual(self.app.loop_events, [(1.0, 0, 100)])
        self.app.handle_loop_command("UNDO", now_ns=2_000_000_000)
        self.assertEqual(self.app.loop_events, [])
        self.app.handle_loop_command("REDO", now_ns=3_000_000_000)
        self.assertEqual(self.app.loop_events, [(1.0, 0, 100)])
        self.assertTrue(self.app.toggle_sequence_step(0, 0, 4, now_ns=4_000_000_000))
        self.assertEqual(self.app.loop_events, [])

    def test_sequence_velocity_nudge_and_copy_edit_selected_event(self):
        self.app.loop_events = [(0.5, 1, 80)]
        self.assertTrue(self.app.select_sequence_step(1, 0, 2))
        self.assertTrue(self.app.adjust_sequence_velocity(5, now_ns=1_000_000_000))
        self.assertEqual(self.app.loop_events, [(0.5, 1, 85)])
        self.assertTrue(self.app.nudge_sequence_event(5, now_ns=2_000_000_000))
        nudged = self.app.loop_events[0][0]
        self.assertAlmostEqual(nudged, 0.51)
        self.assertTrue(self.app.copy_sequence_event(now_ns=3_000_000_000))
        self.assertEqual(len(self.app.loop_events), 2)
        self.assertAlmostEqual(self.app.loop_events[1][0], nudged + 0.25)

    def test_sequence_edit_keeps_playback_running_and_supports_bar_pages(self):
        self.app.loop_bars = 4
        self.app.loop_events = [(0.0, 0, 80)]
        self.app.loop_playing = True
        self.app.loop_start_ns = 1_000_000_000
        self.assertTrue(self.app.toggle_sequence_step(2, 3, 15, now_ns=1_100_000_000))
        self.assertIn((15.75, 2, 100), self.app.loop_events)
        self.assertTrue(self.app.loop_playing)

    def test_sequence_range_edits_probability_and_ratchet_with_undo(self):
        self.app.loop_events = [(0.0, 0, 80), (0.25, 0, 90), (0.25, 1, 100), (1.0, 3, 70)]
        self.assertTrue(self.app.select_sequence_step(0, 0, 0))
        self.assertTrue(self.app.select_sequence_range(1, 0, 1))
        self.assertEqual(len(self.app.sequence_selection), 3)
        self.assertTrue(self.app.adjust_sequence_meta("chance", -25, now_ns=1_000_000_000))
        self.assertTrue(self.app.adjust_sequence_meta("ratchet", 2, now_ns=2_000_000_000))
        for beat, pad, _velocity in self.app.loop_events[:3]:
            self.assertEqual(self.app.loop_event_meta[drum.event_meta_key(pad, beat)], {"chance": 75, "ratchet": 3})
        self.app.handle_loop_command("UNDO", now_ns=3_000_000_000)
        for beat, pad, _velocity in self.app.loop_events[:3]:
            self.assertEqual(self.app.loop_event_meta[drum.event_meta_key(pad, beat)]["ratchet"], 1)

    def test_nudge_and_copy_keep_event_metadata(self):
        self.app.loop_events = [(0.5, 1, 80)]
        self.app.loop_event_meta = {drum.event_meta_key(1, 0.5): {"chance": 60, "ratchet": 2}}
        self.app.select_sequence_step(1, 0, 2)
        self.app.nudge_sequence_event(5, now_ns=1_000_000_000)
        nudged = self.app.loop_events[0][0]
        self.assertEqual(self.app.loop_event_meta[drum.event_meta_key(1, nudged)], {"chance": 60, "ratchet": 2})
        self.app.copy_sequence_event(now_ns=2_000_000_000)
        copied = max(event[0] for event in self.app.loop_events)
        self.assertEqual(self.app.loop_event_meta[drum.event_meta_key(1, copied)], {"chance": 60, "ratchet": 2})

    def test_ratchet_schedules_repeated_hits_and_zero_chance_skips_event(self):
        played = []
        self.app.play_pad = lambda pad, velocity, label: played.append((pad, velocity, label))
        self.app.loop_events = [(0.0, 0, 90)]
        self.app.loop_event_meta = {drum.event_meta_key(0, 0.0): {"chance": 100, "ratchet": 4}}
        start = 10_000_000_000
        self.app.handle_loop_command("PLAY", now_ns=start)
        quarter_ns = int((60.0 / self.app.bpm) * 1_000_000_000)
        self.app.process_loop_scheduler(start + quarter_ns // 4)
        self.assertEqual(len(played), 4)

        self.app.handle_loop_command("PLAY", now_ns=start + quarter_ns)
        self.app.loop_event_meta = {drum.event_meta_key(0, 0.0): {"chance": 0, "ratchet": 4}}
        self.app.handle_loop_command("PLAY", now_ns=start + 2 * quarter_ns)
        self.app.process_loop_scheduler(start + 3 * quarter_ns)
        self.assertEqual(len(played), 4)

    def test_probability_roll_is_reproducible(self):
        rolls = [drum.deterministic_event_roll(cycle, 2, 0.75) for cycle in range(8)]
        self.assertEqual(rolls, [drum.deterministic_event_roll(cycle, 2, 0.75) for cycle in range(8)])
        self.assertGreater(len(set(rolls)), 1)

    def test_probability_and_ratchet_are_realized_for_export(self):
        events = [(0.0, 0, 90), (1.0, 1, 80)]
        metadata = {
            drum.event_meta_key(0, 0.0): {"chance": 100, "ratchet": 3},
            drum.event_meta_key(1, 1.0): {"chance": 0, "ratchet": 1},
        }
        realized = drum.realize_loop_events(events, metadata, bars=1)
        self.assertEqual(len(realized), 3)
        self.assertEqual({event[1] for event in realized}, {0})
        self.assertAlmostEqual(realized[-1][0], 1 / 6)

    def test_physical_pad_step_input_uses_velocity_and_advances_cursor(self):
        self.app.view_mode = "Sequence"
        self.app.sequence_step_input = True
        self.app.sequence_step_cursor = 3
        self.assertTrue(self.app.add_sequence_step_from_pad(2, 73, now_ns=1_000_000_000))
        self.assertEqual(self.app.loop_events, [(0.75, 2, 73)])
        self.assertEqual(self.app.sequence_step_cursor, 4)
        self.assertEqual(self.app.sequence_selection, {(2, 0.75)})

        self.app.sequence_step_cursor = 3
        self.app.add_sequence_step_from_pad(2, 91, now_ns=2_000_000_000)
        self.assertEqual(self.app.loop_events, [(0.75, 2, 91)])

    def pattern_data(self, events, bars=1):
        return {
            "bars": bars,
            "events": events,
            "source_events": None,
            "event_meta": {},
            "feel_preset": "Natural",
            "feel_strength": 50,
            "feel_swing": 50,
            "feel_nudge_ms": 0,
            "feel_humanize_ms": 0,
        }

    def test_pattern_switch_is_queued_to_next_bar_without_stopping_audio(self):
        start = 10_000_000_000
        quarter = int((60 / self.app.bpm) * 1_000_000_000)
        self.app.loop_events = [(0.0, 0, 80)]
        self.app.patterns[0] = self.pattern_data([(0.0, 0, 80)])
        self.app.patterns[1] = self.pattern_data([(0.0, 1, 90)])
        self.app.loop_playing = True
        self.app.loop_start_ns = start
        self.app.loop_schedule_bpm = self.app.bpm
        self.app.pattern_launch_mode = "Next bar"

        self.assertTrue(self.app.request_pattern(1, now_ns=start + quarter // 2))
        self.assertEqual(self.app.active_pattern, 0)
        self.assertEqual(self.app.pattern_switch_deadline_ns, start + 4 * quarter)
        self.app.process_pattern_switch(start + 4 * quarter - 1)
        self.assertEqual(self.app.active_pattern, 0)
        self.app.process_pattern_switch(start + 4 * quarter)
        self.assertEqual(self.app.active_pattern, 1)
        self.assertEqual(self.app.loop_events, [(0.0, 1, 90)])
        self.assertTrue(self.app.loop_playing)

    def test_pattern_launch_modes_use_expected_boundaries(self):
        start = 10_000_000_000
        quarter = int((60 / self.app.bpm) * 1_000_000_000)
        self.app.loop_bars = 2
        self.app.loop_events = [(0.0, 0, 80)]
        self.app.patterns[1] = self.pattern_data([(0.0, 1, 90)])
        self.app.loop_playing = True
        self.app.loop_start_ns = start
        self.app.loop_schedule_bpm = self.app.bpm
        for mode, expected_beats in (("Next beat", 1), ("Next bar", 4), ("Pattern end", 8)):
            self.app.pattern_launch_mode = mode
            self.app.pending_pattern = None
            self.app.request_pattern(1, now_ns=start)
            self.assertEqual(self.app.pattern_switch_deadline_ns, start + expected_beats * quarter)

    def test_duplicate_and_double_preserve_events_and_metadata(self):
        self.app.loop_events = [(0.0, 0, 90)]
        self.app.loop_event_meta = {drum.event_meta_key(0, 0.0): {"chance": 75, "ratchet": 2}}
        target = self.app.duplicate_pattern(now_ns=1_000_000_000)
        self.assertEqual(target, 1)
        self.assertEqual(self.app.active_pattern, 1)
        self.assertEqual(self.app.loop_events, [(0.0, 0, 90)])
        self.assertTrue(self.app.double_pattern(now_ns=2_000_000_000))
        self.assertEqual(self.app.loop_bars, 2)
        self.assertEqual(self.app.loop_events, [(0.0, 0, 90), (4.0, 0, 90)])
        self.assertEqual(self.app.loop_event_meta[drum.event_meta_key(0, 4.0)]["ratchet"], 2)

    def test_song_view_advances_through_three_scenes_at_pattern_ends(self):
        self.app.patterns[0] = self.pattern_data([(0.0, 0, 80)])
        self.app.patterns[1] = self.pattern_data([(0.0, 1, 90)])
        self.app.patterns[2] = self.pattern_data([(0.0, 2, 100)])
        self.app.loop_events = [(0.0, 0, 80)]
        self.app.scene_order = [0, 1, 2]
        start = 10_000_000_000
        quarter = int((60 / self.app.bpm) * 1_000_000_000)
        self.assertTrue(self.app.toggle_song_playback(now_ns=start))
        self.assertEqual(self.app.active_pattern, 0)
        self.app.process_loop_scheduler(start + 4 * quarter)
        self.assertEqual(self.app.active_pattern, 1)
        self.app.process_loop_scheduler(start + 8 * quarter)
        self.assertEqual(self.app.active_pattern, 2)
        self.assertTrue(self.app.song_playing)

    def test_loop_playback_fires_due_events(self):
        played = []
        self.app.play_pad = lambda pad, velocity, label: played.append((pad, velocity, label))
        self.app.loop_events = [(0.0, 0, 70), (1.0, 1, 90)]
        self.app.handle_loop_command("PLAY")
        self.app.process_loop_scheduler(self.app.loop_start_ns)
        self.assertEqual(played, [(0, 70, "Loop")])


class MidiExportTests(unittest.TestCase):
    def test_variable_length_encoding(self):
        self.assertEqual(drum.encode_midi_varlen(0), b"\x00")
        self.assertEqual(drum.encode_midi_varlen(127), b"\x7f")
        self.assertEqual(drum.encode_midi_varlen(128), b"\x81\x00")
        self.assertEqual(drum.encode_midi_varlen(16384), b"\x81\x80\x00")

    def test_midi_export_has_valid_header_track_and_drum_note(self):
        data = drum.build_midi_file(
            [(0.0, 0, 90), (1.0, 1, 100)],
            bars=1,
            bpm=120,
            pad_synths=list(drum.DEFAULT_PAD_SYNTHS),
        )
        self.assertEqual(data[:4], b"MThd")
        self.assertEqual(data[14:18], b"MTrk")
        self.assertIn(bytes((0x99, 36, 90)), data)
        self.assertIn(bytes((0x99, 38, 100)), data)
        declared_track_size = int.from_bytes(data[18:22], "big")
        self.assertEqual(declared_track_size, len(data) - 22)


class AudioExportTests(unittest.TestCase):
    @staticmethod
    def snapshot():
        return {
            "events": [(0.0, 0, 80), (0.0, 1, 80)],
            "event_meta": {}, "bars": 1, "bpm": 120,
            "pad_synths": list(drum.DEFAULT_PAD_SYNTHS),
            "pad_sensitivity": [1.0] * 16,
            "custom_samples": [None] * 16,
            "volume": 0.5, "pad_volume": [1.0] * 16,
            "pad_pan": [0.0] * 16, "pad_tune": [0] * 16,
            "pad_mute": [False] * 16, "solo_pads": [],
            "mixer_bypass": False,
        }

    @staticmethod
    def wav_samples(path):
        with wave.open(str(path), "rb") as source:
            shape = (source.getnframes(), source.getnchannels())
            return numpy.frombuffer(source.readframes(source.getnframes()), dtype=numpy.int16).reshape(shape)

    def test_pad_stems_align_and_sum_to_master(self):
        app = drum.DrumPadNative(settings_path=None)
        token_values = {}
        for synth, value in (("kick", 120.0), ("snare", 220.0)):
            for layer in drum.KIT[synth]:
                for filename in set(layer["files"] + sum(layer.get("velocity_files", {}).values(), [])):
                    token = object()
                    app.samples[filename] = token
                    token_values[token] = value

        def sample_array(sound):
            return numpy.full((240, 2), token_values[sound], dtype=numpy.float32)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            drum.pygame.sndarray, "array", side_effect=sample_array
        ):
            root = Path(directory)
            snapshot = self.snapshot()
            master_path = root / "master.wav"
            kick_path = root / "kick.wav"
            snare_path = root / "snare.wav"
            app.render_loop_wav(master_path, snapshot)
            app.render_loop_wav(kick_path, snapshot, pad_filter={0})
            app.render_loop_wav(snare_path, snapshot, pad_filter={1})
            master = self.wav_samples(master_path)
            kick = self.wav_samples(kick_path)
            snare = self.wav_samples(snare_path)
            self.assertEqual(master.shape, kick.shape)
            self.assertEqual(master.shape, snare.shape)
            numpy.testing.assert_array_equal(master, kick + snare)

    def test_master_export_limits_heavy_overlapping_hits(self):
        app = drum.DrumPadNative(settings_path=None)
        for layers in drum.KIT.values():
            for layer in layers:
                for filename in set(layer["files"] + sum(layer.get("velocity_files", {}).values(), [])):
                    app.samples[filename] = object()
        snapshot = self.snapshot()
        snapshot["events"] = [(0.0, index, 127) for index in range(16)]
        snapshot["volume"] = 1.0
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            drum.pygame.sndarray, "array",
            return_value=numpy.full((240, 2), 30000, dtype=numpy.int16),
        ):
            output = Path(directory) / "limited.wav"
            app.render_loop_wav(output, snapshot)
            rendered = self.wav_samples(output)
        self.assertLessEqual(int(numpy.max(numpy.abs(rendered.astype(numpy.int32)))), round(32767 * 0.98))

    def test_project_bundle_contains_project_midi_stems_and_samples(self):
        app = drum.DrumPadNative(settings_path=None)
        snapshot = self.snapshot()
        profile = app.default_kit_profile()
        profile["custom_samples"][0] = "owned.wav"
        snapshot.update({
            "project_name": "Bundle Test",
            "project": {"kits": {"A": profile}},
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "owned.wav"
            sample.write_bytes(b"sample")
            output = root / "bundle.zip"

            def fake_stems(target, _snapshot):
                target.mkdir(parents=True)
                (target / "00-Master.wav").write_bytes(b"wav")
                (target / "loop.mid").write_bytes(b"midi")

            with mock.patch.object(app, "export_stems", side_effect=fake_stems), mock.patch.object(
                app, "custom_sample_path", return_value=sample
            ):
                app.export_project_bundle(output, snapshot)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertIn("Bundle Test.starrypad.json", names)
            self.assertIn("Stems/00-Master.wav", names)
            self.assertIn("Stems/loop.mid", names)
            self.assertIn("Samples/owned.wav", names)


class SamplingTests(unittest.TestCase):
    def test_monitor_duplex_callback_is_attenuated_and_still_records(self):
        sampler = drum.AudioSampler(max_seconds=5)
        sampler._reset_capture_state(1000, auto_start=False)
        input_data = numpy.full((100, 1), 0.5, dtype=numpy.float32)
        output_data = numpy.zeros((100, 2), dtype=numpy.float32)
        sampler._duplex_callback(input_data, output_data, 100, None, None)
        numpy.testing.assert_allclose(output_data, 0.35)
        self.assertEqual(sampler.total_frames, 100)

    def test_monitor_setup_falls_back_to_input_only_when_duplex_fails(self):
        sampler = drum.AudioSampler()
        stream = mock.Mock()
        fake_sounddevice = mock.Mock()
        fake_sounddevice.query_devices.return_value = {"default_samplerate": 48000}
        fake_sounddevice.Stream.side_effect = RuntimeError("no duplex")
        fake_sounddevice.InputStream.return_value = stream
        with mock.patch.dict("sys.modules", {"sounddevice": fake_sounddevice}):
            sampler.start(device=2, monitor=True)
        fake_sounddevice.InputStream.assert_called_once()
        stream.start.assert_called_once()

    def test_continuous_sampling_advances_and_stops_when_full(self):
        app = drum.DrumPadNative(settings_path=None)
        app.sample_continuous_active = True
        app.sample_results.put(("OK", 0, "first.wav"))
        started = []
        app.start_sampling = lambda: started.append(app.selected_pad)
        with mock.patch.object(drum.pygame.mixer, "Sound", return_value=object()):
            app.poll_sample_results()
        self.assertEqual(app.selected_pad, 1)
        self.assertEqual(started, [1])

        app.custom_sample_files[2:] = [f"used-{index}.wav" for index in range(2, 16)]
        app.sample_results.put(("OK", 1, "last.wav"))
        with mock.patch.object(drum.pygame.mixer, "Sound", return_value=object()):
            app.poll_sample_results()
        self.assertFalse(app.sample_continuous_active)
        self.assertIn("filled", app.sample_status)

    def test_clipped_recording_waits_for_keep_or_retry_before_assignment(self):
        app = drum.DrumPadNative(settings_path=None)
        app.sample_was_clipped = True
        app.sample_results.put(("OK", 0, "clipped.wav"))
        sound = object()
        with mock.patch.object(drum.pygame.mixer, "Sound", return_value=sound):
            app.poll_sample_results()
        self.assertTrue(app.clip_prompt_open)
        self.assertIsNone(app.custom_sample_files[0])
        self.assertTrue(app.resolve_clipped_sample(keep=True))
        self.assertEqual(app.custom_sample_files[0], "clipped.wav")
        self.assertFalse(app.clip_prompt_open)

    def test_ten_auto_started_hits_keep_preroll_attack(self):
        for _ in range(10):
            sampler = drum.AudioSampler(max_seconds=2, pre_roll_seconds=0.2)
            sampler._reset_capture_state(1000, auto_start=True)
            sampler._callback(numpy.zeros((100, 1), numpy.float32), 100, None, None)
            attack = numpy.zeros((100, 1), numpy.float32)
            attack[0, 0] = 0.5
            sampler._callback(attack, 100, None, None)
            self.assertTrue(sampler.triggered)
            self.assertEqual(sampler.total_frames, 200)
            self.assertEqual(float(sampler.chunks[1][0]), 0.5)

    def test_browser_filters_favorites_recent_and_search(self):
        app = drum.DrumPadNative(settings_path=None)
        app.browser_type = "Snare"
        snares = app.sample_browser_candidates()
        self.assertTrue(snares)
        self.assertTrue(all(candidate["type"] == "Snare" for candidate in snares))
        chosen = snares[0]["id"]
        app.toggle_sample_favorite(chosen)
        app.browser_view = "Favorites"
        self.assertEqual([candidate["id"] for candidate in app.sample_browser_candidates()], [chosen])
        app.browser_view = "Recent"
        app.remember_sample_candidate(chosen)
        self.assertEqual([candidate["id"] for candidate in app.sample_browser_candidates()], [chosen])
        app.browser_view = "All"
        app.browser_query = "deep"
        self.assertTrue(all("deep" in candidate["label"].casefold() for candidate in app.sample_browser_candidates()))
        app.browser_query = ""
        app.browser_type = "All"
        app.browser_kit = "Kit A"
        kit_ids = {candidate["id"] for candidate in app.sample_browser_candidates()}
        self.assertIn(f"synth:{app.kit_slots['A']['pad_synths'][0]}", kit_ids)
        self.assertNotIn("synth:snare_deep", kit_ids)

    def test_browser_preview_is_non_destructive_and_assignment_undoes(self):
        app = drum.DrumPadNative(settings_path=None)
        app.selected_pad = 1
        original = app.pad_synths[1]
        played = []
        app.play_pad = lambda index, velocity, label: played.append((index, velocity, label, app.pad_synths[index]))
        candidate_id = "synth:snare_deep"
        self.assertTrue(app.preview_sample_candidate(candidate_id))
        self.assertEqual(app.pad_synths[1], original)
        self.assertEqual(played[-1][3], "snare_deep")
        self.assertTrue(app.assign_sample_candidate(candidate_id))
        self.assertEqual(app.pad_synths[1], "snare_deep")
        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.pad_synths[1], original)

    def test_relink_finds_multiple_missing_samples_from_one_folder(self):
        app = drum.DrumPadNative(settings_path=None)
        app.kit_slots["A"]["custom_samples"][:2] = ["missing-one.wav", "missing-two.wav"]
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as user_dir:
            source = Path(source_dir)
            nested = source / "nested"
            nested.mkdir()
            (source / "missing-one.wav").write_bytes(b"one")
            (nested / "missing-two.wav").write_bytes(b"two")
            with mock.patch.object(drum, "USER_SAMPLE_DIR", Path(user_dir)), mock.patch.object(
                app, "load_custom_samples"
            ):
                self.assertEqual(app.relink_missing_samples(source), 2)
            self.assertEqual((Path(user_dir) / "missing-one.wav").read_bytes(), b"one")
            self.assertEqual((Path(user_dir) / "missing-two.wav").read_bytes(), b"two")

    def test_auto_start_keeps_preroll_and_waits_for_signal(self):
        sampler = drum.AudioSampler(max_seconds=5, pre_roll_seconds=0.2)
        sampler._reset_capture_state(1000, auto_start=True)
        silence = numpy.zeros((100, 1), dtype=numpy.float32)
        signal = numpy.full((100, 1), 0.1, dtype=numpy.float32)

        sampler._callback(silence, 100, None, None)
        self.assertFalse(sampler.detail_snapshot()["triggered"])
        sampler._callback(signal, 100, None, None)

        detail = sampler.detail_snapshot()
        self.assertTrue(detail["triggered"])
        self.assertEqual(sampler.total_frames, 200)
        self.assertEqual(len(sampler.chunks), 2)

    def test_auto_start_stops_after_sustained_silence(self):
        sampler = drum.AudioSampler(max_seconds=5, silence_stop_seconds=0.3)
        sampler._reset_capture_state(1000, auto_start=True)
        signal = numpy.full((100, 1), 0.1, dtype=numpy.float32)
        silence = numpy.zeros((100, 1), dtype=numpy.float32)
        sampler._callback(signal, 100, None, None)
        for _ in range(3):
            sampler._callback(silence, 100, None, None)
        detail = sampler.detail_snapshot()
        self.assertTrue(detail["auto_stop"])
        self.assertEqual(detail["stop_reason"], "silence")

    def test_sampler_reports_clipping(self):
        sampler = drum.AudioSampler(max_seconds=5)
        sampler._reset_capture_state(1000, auto_start=False)
        clipped = numpy.full((100, 1), 1.0, dtype=numpy.float32)
        sampler._callback(clipped, 100, None, None)
        self.assertTrue(sampler.detail_snapshot()["clipped"])

    def test_sample_processing_trims_normalizes_and_resamples(self):
        source_rate = 44100
        silence = numpy.zeros(round(source_rate * 0.1), dtype=numpy.float32)
        timeline = numpy.arange(round(source_rate * 0.2), dtype=numpy.float32) / source_rate
        tone = numpy.sin(2 * numpy.pi * 440 * timeline).astype(numpy.float32) * 0.2
        source = numpy.concatenate((silence, tone, silence))

        prepared = drum.prepare_sample_audio(source, source_rate)
        self.assertEqual(prepared.dtype, numpy.int16)
        self.assertEqual(prepared.ndim, 2)
        self.assertEqual(prepared.shape[1], 2)
        self.assertGreater(len(prepared), round(48000 * 0.2))
        self.assertLess(len(prepared), round(48000 * 0.4))
        self.assertGreater(int(numpy.max(numpy.abs(prepared.astype(numpy.int32)))), 29000)
        self.assertEqual(int(prepared[0, 0]), 0)
        self.assertEqual(int(prepared[-1, 0]), 0)

    def test_silent_recording_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "No usable audio"):
            drum.prepare_sample_audio(numpy.zeros(4410, dtype=numpy.float32), 44100)

    def test_custom_sample_overrides_kit_sound(self):
        app = drum.DrumPadNative(settings_path=None)
        marker = object()
        app.custom_sample_files[0] = "pad-01-test.wav"
        app.custom_sound_cache["pad-01-test.wav"] = marker
        played = []
        app.play_custom_sample = lambda sound, synth, gain, *_mix: played.append((sound, synth, gain))
        app.play_pad(0, 70, "N20")
        self.assertEqual(len(played), 1)
        self.assertIs(played[0][0], marker)
        self.assertEqual(played[0][1], "kick")

    def test_unsafe_custom_sample_paths_are_rejected(self):
        profile = drum.DrumPadNative.default_kit_profile()
        profile["custom_samples"][0] = "../outside.wav"
        sanitized = drum.DrumPadNative.sanitize_kit_profile(profile)
        self.assertIsNone(sanitized["custom_samples"][0])

    def test_sample_edits_are_non_destructive_and_loop_edges_are_silent(self):
        source = numpy.arange(2000, dtype=numpy.int16).reshape(1000, 2)
        original = source.copy()
        edit = {
            "start": 0.2, "end": 0.8, "normalize": True, "reverse": True,
            "attack_ms": 0, "release_ms": 0, "tune": 3, "mode": "Loop",
        }
        edited = drum.apply_sample_edits(source, edit, sample_rate=1000)
        numpy.testing.assert_array_equal(source, original)
        self.assertEqual(edited.shape, (600, 2))
        self.assertGreaterEqual(int(numpy.max(numpy.abs(edited.astype(numpy.int32)))), 29500)
        self.assertEqual(edited[0].tolist(), [0, 0])
        self.assertEqual(edited[-1].tolist(), [0, 0])

    def test_sample_edit_supports_undo_and_kit_round_trip(self):
        app = drum.DrumPadNative(settings_path=None)
        app.custom_sample_files[0] = "pad.wav"
        self.assertTrue(app.adjust_sample_edit("start", 0.15))
        self.assertTrue(app.adjust_sample_edit("reverse"))
        self.assertTrue(app.adjust_sample_edit("mode", 1))
        self.assertEqual(app.sample_edits[0]["mode"], "Gate")
        self.assertTrue(app.undo_project_edit())
        self.assertEqual(app.sample_edits[0]["mode"], "One-shot")
        app.save_current_kit()
        restored = drum.DrumPadNative(settings_path=None)
        restored.apply_kit_profile(app.kit_slots["A"])
        self.assertEqual(restored.sample_edits[0], app.sample_edits[0])

    def test_custom_sample_modes_loop_toggle_and_release(self):
        class Channel:
            def __init__(self):
                self.busy = True
                self.play_calls = []
                self.fadeouts = []
            def set_volume(self, *_args): pass
            def play(self, _sound, **kwargs): self.play_calls.append(kwargs)
            def get_busy(self): return self.busy
            def fadeout(self, value): self.fadeouts.append(value); self.busy = False

        app = drum.DrumPadNative(settings_path=None)
        app.tuned_sound = lambda sound, _tune: sound
        channel = Channel()
        with mock.patch.object(drum.pygame.mixer, "find_channel", return_value=channel):
            app.play_custom_sample(object(), "kick", 0.5, mode="Loop", release_ms=25, pad_index=0)
        self.assertEqual(channel.play_calls, [{"loops": -1}])
        self.assertIs(app.sample_channels[0], channel)

        app.sample_edits[0]["mode"] = "Loop"
        app.sample_edits[0]["release_ms"] = 25
        app.handle_midi_release("N", 20)
        self.assertEqual(channel.fadeouts, [25])
        self.assertNotIn(0, app.sample_channels)

        toggle = Channel()
        app.sample_channels[0] = toggle
        app.play_custom_sample(object(), "kick", 0.5, mode="Toggle", release_ms=12, pad_index=0)
        self.assertEqual(toggle.fadeouts, [12])

    def test_equal_transient_and_play_through_slice_boundaries(self):
        samples = numpy.zeros((48000, 2), dtype=numpy.int16)
        for frame in (6000, 18000, 30000):
            samples[frame:frame + 100] = 20000
        equal = drum.equal_slice_markers(len(samples), 4)
        self.assertEqual(equal, [0, 12000, 24000, 36000, 48000])
        transient = drum.transient_slice_markers(samples, 4)
        self.assertEqual(len(transient), 5)
        for expected in (6000, 18000, 30000):
            self.assertTrue(any(abs(marker - expected) <= 480 for marker in transient), transient)
        regular = drum.slice_sample_audio(samples, equal)
        through = drum.slice_sample_audio(samples, equal, play_through=True)
        self.assertEqual([len(value) for value in regular], [12000] * 4)
        self.assertEqual([len(value) for value in through], [48000, 36000, 24000, 12000])
        self.assertTrue(all(numpy.all(value[0] == 0) and numpy.all(value[-1] == 0) for value in regular))

    def test_chop_expands_to_pads_and_one_undo_restores_assignment(self):
        app = drum.DrumPadNative(settings_path=None)
        app.selected_pad = 0
        app.custom_sample_files[0] = "source.wav"
        app.custom_sound_cache["source.wav"] = object()
        app.chop_mode = "Equal"
        app.chop_count = 4
        app.chop_keep_original = True
        app.chop_choke = True
        source = numpy.arange(3200, dtype=numpy.int16).reshape(1600, 2)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            drum, "USER_SAMPLE_DIR", Path(directory)
        ), mock.patch.object(
            drum.pygame.sndarray, "array", return_value=source
        ), mock.patch.object(
            drum.pygame.mixer, "Sound", side_effect=lambda path: Path(path).name
        ):
            self.assertTrue(app.execute_chop())
            assigned = app.custom_sample_files[1:5]
            self.assertTrue(all(assigned))
            groups = {app.sample_edits[index]["choke_group"] for index in range(1, 5)}
            self.assertEqual(len(groups), 1)
            self.assertNotIn(None, groups)
            self.assertEqual(len(list(Path(directory).glob("chop-*.wav"))), 4)
            self.assertTrue(app.undo_project_edit())
            self.assertEqual(app.custom_sample_files[1:5], [None] * 4)

    def test_live_chop_records_pad_taps_as_manual_markers(self):
        class Sound:
            def get_length(self): return 4.0
        app = drum.DrumPadNative(settings_path=None)
        app.chop_lazy_active = True
        app.chop_lazy_started_at = 10.0
        app.edited_custom_sound = lambda _index: Sound()
        with mock.patch.object(drum.time, "perf_counter", return_value=11.0):
            self.assertTrue(app.add_lazy_chop_marker())
        with mock.patch.object(drum.time, "perf_counter", return_value=12.0):
            self.assertTrue(app.add_lazy_chop_marker())
        self.assertEqual(app.chop_markers, [0.25, 0.5])

    def test_tempo_detect_handles_known_loops_and_half_double_candidates(self):
        sample_rate = 48000
        for expected in (90, 120, 140):
            audio = numpy.zeros((round(sample_rate * 16 * 60 / expected), 2), dtype=numpy.float32)
            for beat in range(16):
                frame = round(beat * 60 / expected * sample_rate)
                audio[frame:frame + 240] = 1.0
            bpm, bars, confidence = drum.detect_sample_tempo(audio, sample_rate)
            self.assertAlmostEqual(bpm, expected, delta=1.0)
            self.assertEqual(bars, 4)
            self.assertGreater(confidence, 0.5)

    def test_wsola_stretch_preserves_pitch_and_repitch_changes_it(self):
        sample_rate = 48000
        timeline = numpy.arange(sample_rate * 2) / sample_rate
        mono = numpy.sin(2 * numpy.pi * 440 * timeline).astype(numpy.float32)
        audio = numpy.column_stack((mono, mono))
        stretched = drum.apply_sample_tempo(
            audio, {"source_bpm": 100, "stretch_mode": "Stretch"}, 125
        )
        repitched = drum.apply_sample_tempo(
            audio, {"source_bpm": 100, "stretch_mode": "Repitch"}, 125
        )
        self.assertAlmostEqual(len(stretched), len(audio) / 1.25, delta=2500)
        self.assertEqual(len(repitched), round(len(audio) / 1.25))

        def dominant_frequency(values):
            values = values[5000:, 0]
            return numpy.argmax(numpy.abs(numpy.fft.rfft(values))) * sample_rate / len(values)
        self.assertAlmostEqual(dominant_frequency(stretched), 440, delta=3)
        self.assertAlmostEqual(dominant_frequency(repitched), 550, delta=3)

    def test_detected_tempo_and_stretch_mode_support_undo(self):
        app = drum.DrumPadNative(settings_path=None)
        app.custom_sample_files[0] = "loop.wav"
        app.custom_sound_cache["loop.wav"] = object()
        clicks = numpy.zeros((96000, 2), dtype=numpy.float32)
        clicks[::24000] = 1
        with mock.patch.object(drum.pygame.sndarray, "array", return_value=clicks):
            self.assertTrue(app.detect_selected_sample_tempo())
        self.assertIsNotNone(app.sample_edits[0]["source_bpm"])
        self.assertEqual(app.sample_edits[0]["stretch_mode"], "Stretch")
        self.assertTrue(app.undo_project_edit())
        self.assertIsNone(app.sample_edits[0]["source_bpm"])


if __name__ == "__main__":
    unittest.main()
