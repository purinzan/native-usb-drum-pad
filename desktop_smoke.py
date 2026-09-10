"""Check the *frozen* program, using dummy SDL and temporary user data only."""
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import traceback
import wave
from unittest import mock


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run_smoke_test(report_path):
    report_path = Path(report_path).resolve()
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)),
              "platform": platform.platform(), "architecture": platform.machine(),
              "python": platform.python_version(), "checks": {},
              "physical_devices_tested": False}
    apps = []
    pygame = None
    try:
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
        import numpy as np
        import pygame
        import sounddevice
        from audiotsm import wsola
        from audiotsm.io.array import ArrayReader, ArrayWriter
        import tkinter as tk
        from tkinter import filedialog  # noqa: F401: verify the packaged dependency
        import drum_pad_native as drum
        from app_paths import AppPaths

        info = drum.ROOT / "BUILD-INFO.json"
        if report["frozen"]:
            require(info.is_file(), "Packaged build provenance is missing")
            report["build"] = json.loads(info.read_text(encoding="utf-8"))
            for module in (drum, pygame, np, sounddevice):
                require(Path(module.__file__).resolve().is_relative_to(drum.ROOT.resolve()),
                        f"Module was loaded outside the bundle: {module.__name__}")
            report["checks"]["bundled_module_paths"] = True
        for name in ("LICENSE", "LICENSE-SAMPLES", "assets/tex/grain-256.png",
                     "assets/brand/icon-64.png", "assets/fonts/OFL-Barlow.txt",
                     "assets/fonts/OFL-BarlowCondensed.txt", "assets/fonts/OFL-IBMPlexMono.txt"):
            require((drum.ROOT / name).is_file(), f"Missing packaged resource: {name}")
        if report["frozen"]:
            require((drum.ROOT / "THIRD-PARTY" / "Python-LICENSE.txt").is_file(),
                    "Python license is missing")
        report["checks"]["portaudio"] = str(sounddevice.get_portaudio_version())
        reader = ArrayReader(np.ones((2, 8192), dtype=np.float32) * 0.1)
        writer = ArrayWriter(2)
        wsola(2, speed=0.8).run(reader, writer)
        require(writer.data.size > 0, "WSOLA produced no audio")
        report["checks"]["numpy_wsola"] = True

        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
            require(root.tk.call("info", "commands", "tk_getOpenFile"), "Tk file dialog command missing")
            report["checks"]["tcl_tk"] = root.tk.call("info", "patchlevel")
        finally:
            root.destroy()

        with tempfile.TemporaryDirectory(prefix="starrypad-package-") as temporary:
            paths = AppPaths.under(Path(temporary) / "user", drum.ROOT)
            # A non-default settings filename skips legacy import entirely.
            settings = paths.config_dir / "smoke-settings.json"
            app = drum.DrumPadNative(settings_path=settings, app_paths=paths)
            apps.append(app)
            # Do not query microphones or modify CoreAudio hardware buffers.
            with mock.patch.object(drum, "audio_input_devices", return_value=[]), \
                 mock.patch.object(app, "apply_hardware_buffer", return_value=None):
                app.init_pygame()
            app.load_samples()
            expected = set(drum.all_sample_files())
            require(expected and set(app.samples) == expected, "One or more built-in samples did not load")
            require(all(sound.get_length() > 0 for sound in app.samples.values()), "Empty built-in sound")
            report["checks"]["builtin_samples"] = len(expected)
            app.draw()
            pygame.event.pump()
            require(app.buttons, "The playing surface did not draw its controls")
            report["checks"]["playing_surface"] = True
            app.start_audio_worker()
            for pad in range(len(drum.PADS)):
                app.queue_pad(pad, 100)
            deadline = time.monotonic() + 5
            while app.diagnostic_snapshot()[2] < len(drum.PADS) and time.monotonic() < deadline:
                pygame.event.pump()
                time.sleep(0.01)
            app.stop_audio_worker()
            metrics = app.diagnostic_snapshot()
            require(metrics[2] == len(drum.PADS) and metrics[4] == 0,
                    f"Dummy audio dispatch failed: {metrics}")
            report["checks"]["dummy_pad_hits"] = metrics[2]

            paths.samples.mkdir(parents=True, exist_ok=True)
            custom = paths.samples / "smoke-tone.wav"
            tone = (np.sin(np.arange(4800) * 2 * np.pi * 220 / 48000) * 3000).astype("<i2")
            with wave.open(str(custom), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(np.column_stack((tone, tone)).tobytes())
            app.custom_sample_files[0] = custom.name
            app.load_custom_samples()
            require(custom.name in app.custom_sound_cache, "Custom sample failed to load")
            app.bpm = 120
            app.loop_bars = 2
            app.loop_events = [(0.0, 0, 100), (4.0, 1, 90)]
            require(app.persist_settings(), "Project save failed")
            exported = Path(temporary) / "master.wav"
            app.render_loop_wav(exported, app.loop_render_snapshot())
            with wave.open(str(exported), "rb") as wav:
                require((wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (48000, 2, 192000),
                        "Export rate/channels/duration are incorrect")
                require(np.any(np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")), "Export is silent")
            report["checks"]["wav_export"] = True
            app.close_storage()
            restored = drum.DrumPadNative(settings_path=settings, app_paths=paths)
            apps.append(restored)
            require(restored.loop_bars == 2 and len(restored.loop_events) == 2, "Loop did not survive restart")
            require(restored.custom_sample_files[0] == custom.name, "Custom sample reference did not survive restart")
            restored.load_custom_samples()
            require(custom.name in restored.custom_sound_cache, "Restored custom sound did not load")
            report["checks"]["save_reopen"] = True
            # Close file-owning workers before TemporaryDirectory is cleaned up on Windows.
            restored.close_storage()
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        for app in apps:
            try:
                app.stop_audio_worker()
                app.close_storage()
            except Exception:
                report["ok"] = False
                report["cleanup_error"] = traceback.format_exc()
        if pygame is not None:
            pygame.quit()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1
