"""Packaging checks that do not require a display, audio devices or PyInstaller."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from app_paths import AppPaths
import starrypad_desktop as launcher
from desktop_smoke import require
from tools.build_preview import isolated_environment, inventory, target_label, verify_report


class PackagingTests(unittest.TestCase):
    def test_target_names_are_architecture_specific(self):
        self.assertEqual(target_label("win32", "AMD64"), "windows-x64")
        self.assertEqual(target_label("darwin", "arm64"), "macos-arm64")
        self.assertEqual(target_label("darwin", "x86_64"), "macos-x86_64")
        with self.assertRaises(RuntimeError):
            target_label("linux", "x86_64")
        with self.assertRaises(RuntimeError):
            target_label("win32", "arm64")

    def test_smoke_environment_removes_developer_search_paths(self):
        original = {"PATH": "/developer/python:/developer/ffmpeg", "PYTHONPATH": "/source",
                    "PYTHONHOME": "/venv", "VIRTUAL_ENV": "/venv", "DYLD_LIBRARY_PATH": "/homebrew",
                    "TCL_LIBRARY": "/developer/tcl", "KEEP": "yes"}
        env = isolated_environment(original)
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "DYLD_LIBRARY_PATH", "TCL_LIBRARY"):
            self.assertNotIn(name, env)
        self.assertNotIn("developer", env["PATH"])
        self.assertEqual(env["KEEP"], "yes")
        self.assertEqual(env["SDL_AUDIODRIVER"], "dummy")
        self.assertIn("PYTHONHOME", original)

    def test_smoke_dispatch_does_not_start_normal_application(self):
        with mock.patch("desktop_smoke.run_smoke_test", return_value=7) as smoke, \
             mock.patch.object(launcher, "run_windowed") as normal:
            self.assertEqual(launcher.main(["--smoke-test", "report.json"]), 7)
            smoke.assert_called_once_with(Path("report.json"))
            normal.assert_not_called()

    def test_normal_dispatch_keeps_existing_application_entry(self):
        with mock.patch.object(launcher, "run_windowed", return_value=0) as normal:
            self.assertEqual(launcher.main([]), 0)
            normal.assert_called_once_with()

    def test_windowed_startup_provides_and_restores_missing_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.under(Path(directory) / "user", Path(directory) / "resources")
            def launch():
                self.assertIsNotNone(sys.stdout)
                self.assertIsNotNone(sys.stderr)
                print("startup reached")
            with mock.patch.object(AppPaths, "discover", return_value=paths), \
                 mock.patch.object(launcher, "run_application", side_effect=launch), \
                 mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
                self.assertEqual(launcher.run_windowed(), 0)
                self.assertIsNone(sys.stdout)
                self.assertIsNone(sys.stderr)
            self.assertIn("startup reached", (paths.log_dir / "desktop.log").read_text())
            self.assertFalse(paths.resource_root.exists())

    def test_startup_failure_is_logged_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.under(Path(directory) / "user", Path(directory) / "resources")
            with mock.patch.object(AppPaths, "discover", return_value=paths), \
                 mock.patch.object(launcher, "run_application", side_effect=RuntimeError("missing SDL")), \
                 mock.patch.object(launcher, "show_startup_error") as display:
                self.assertEqual(launcher.run_windowed(), 1)
                self.assertIn("missing SDL", display.call_args.args[0])
            self.assertIn("RuntimeError: missing SDL", (paths.log_dir / "desktop.log").read_text())

    def test_log_directory_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file"
            blocked.write_text("not a directory")
            paths = AppPaths(Path(directory), Path(directory), Path(directory), Path(directory), blocked / "logs")
            with mock.patch.object(AppPaths, "discover", return_value=paths), \
                 mock.patch.object(launcher, "run_application") as normal, \
                 mock.patch.object(launcher, "show_startup_error") as display:
                self.assertEqual(launcher.run_windowed(), 1)
                normal.assert_not_called()
                display.assert_called_once()

    def test_inventory_detects_changes_and_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "resource"
            file.write_bytes(b"before")
            before = inventory(root)
            file.write_bytes(b"after")
            self.assertNotEqual(before, inventory(root))
            file.write_bytes(b"before")
            self.assertEqual(before, inventory(root))
            (root / "unexpected").write_text("state")
            self.assertNotEqual(before, inventory(root))

    def report(self):
        return {"ok": True, "frozen": True, "build": {"commit": "abc"}, "physical_devices_tested": False,
                "checks": {key: True for key in ("portaudio", "numpy_wsola", "tcl_tk", "builtin_samples",
                          "playing_surface", "dummy_pad_hits", "wav_export", "save_reopen", "bundled_module_paths")}}

    def test_report_requires_a_frozen_successful_matching_build(self):
        verify_report(self.report(), "abc")
        for key in ("ok", "frozen"):
            data = self.report()
            data[key] = False
            with self.assertRaises(RuntimeError):
                verify_report(data, "abc")
        with self.assertRaises(RuntimeError):
            verify_report(self.report(), "different")

    def test_report_cannot_omit_checks_or_claim_real_hardware(self):
        data = self.report()
        data["checks"].pop("save_reopen")
        with self.assertRaises(RuntimeError):
            verify_report(data, "abc")
        data = self.report()
        data["physical_devices_tested"] = True
        with self.assertRaises(RuntimeError):
            verify_report(data, "abc")

    def test_smoke_requirements_are_not_optimized_out(self):
        with self.assertRaisesRegex(RuntimeError, "required"):
            require(False, "required")


if __name__ == "__main__":
    unittest.main()
