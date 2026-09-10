"""Application-level storage regressions; dummy SDL, no real MIDI/microphone."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from app_paths import AppPaths
import drum_pad_native as drum
import project_io


class ApplicationStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="starrypad-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resources = self.root / "read only resources"
        self.resources.mkdir()
        (self.resources / "LICENSE-SAMPLES").write_text("test attribution")

    def app(self, name="user", persist=True):
        paths = AppPaths.under(self.root / name, self.resources)
        app = drum.DrumPadNative(app_paths=paths) if persist else drum.DrumPadNative(settings_path=None, app_paths=paths)
        self.addCleanup(app.close_storage)
        return app

    def sound(self, app, name, data=b"sample"):
        path = app.user_sample_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_paths_are_injected_and_resources_are_untouched(self):
        before = {p.name: p.read_bytes() for p in self.resources.iterdir()}
        app = self.app()
        self.assertEqual(app.settings_path, app.app_paths.settings_file)
        self.assertEqual(app.project_path.parent, app.app_paths.projects)
        self.assertEqual(app.user_sample_dir, app.app_paths.samples)
        self.assertEqual(app.export_dir, app.app_paths.exports)
        self.assertEqual({p.name: p.read_bytes() for p in self.resources.iterdir()}, before)

    def test_settings_none_creates_no_storage_and_no_save_thread(self):
        app = self.app(persist=False)
        app.persist_settings_async()
        self.assertTrue(app.persist_settings())
        self.assertIsNone(app._save_worker)
        self.assertFalse((self.root / "user").exists())

    def test_audio_side_saves_only_mark_dirty(self):
        app = self.app()
        with mock.patch.object(app, "_submit_storage", wraps=app._submit_storage) as submit:
            thread = threading.Thread(target=app.persist_settings_async)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            submit.assert_not_called()
            self.assertTrue(app._storage_dirty.is_set())
            app._last_save_request_at = 0
            app.poll_storage()
            submit.assert_called_once()
            app._save_worker.flush()

    def test_save_request_keeps_original_project_and_nested_data(self):
        app = self.app()
        old_path = app.project_path
        app.bpm = 137
        request = app._submit_storage()
        app.project_path = self.root / "Other.starrypad.json"
        app.bpm = 180
        self.assertIsNone(app._save_worker.wait(request).error)
        self.assertEqual(json.loads(old_path.read_text())["bpm"], 137)
        self.assertFalse(app.project_path.exists())
        self.assertTrue(app.persist_settings())
        self.assertEqual(json.loads(app.project_path.read_text())["bpm"], 180)
        self.assertEqual(json.loads(old_path.read_text())["bpm"], 137)

    def test_save_failure_does_not_report_saved_or_change_save_as_target(self):
        app = self.app()
        previous = app.project_path
        with mock.patch.object(project_io, "write_text_atomic", side_effect=OSError("disk full")):
            self.assertFalse(app.save_project_as(self.root / "failed.starrypad.json"))
        self.assertEqual(app.project_path, previous)
        self.assertIn("failed", app.status.lower())
        self.assertNotEqual(app.status, "Project saved")
        self.assertTrue(app.persist_settings())

    def test_failed_save_blocks_new_and_open_before_discarding_state(self):
        app = self.app()
        previous = app.project_path
        app.bpm = 151
        with mock.patch.object(app, "persist_settings", return_value=False):
            self.assertFalse(app.new_project())
            self.assertFalse(app.open_project(self.root / "other.json"))
        self.assertEqual(app.project_path, previous)
        self.assertEqual(app.bpm, 151)

    def test_project_switch_flushes_dirty_old_project(self):
        app = self.app()
        previous = app.project_path
        app.bpm = 151
        app.persist_settings_async()
        self.assertTrue(app.new_project())
        self.assertNotEqual(app.project_path, previous)
        self.assertEqual(json.loads(previous.read_text())["bpm"], 151)
        self.assertEqual(json.loads(app.project_path.read_text())["bpm"], 120)

    def test_collect_includes_nonfirst_layers_and_inactive_kits(self):
        app = self.app()
        for name in ("one.wav", "two.wav", "other.wav"):
            self.sound(app, name, name.encode())
        one, two = drum.default_pad_layer(), drum.default_pad_layer()
        one["file"], two["file"] = "one.wav", "two.wav"
        app.pad_layers[0] = [one, two]
        profile = app.default_kit_profile()
        profile["custom_samples"][3] = "other.wav"
        app.kit_slots["D"] = app.sanitize_kit_profile(profile)
        self.assertTrue(app.collect_project_samples())
        for name in ("one.wav", "two.wav", "other.wav"):
            self.assertEqual((app.project_sample_dir() / name).read_bytes(), name.encode())

    def test_collect_missing_layer_fails_visibly(self):
        app = self.app()
        app.pad_layers[0][0]["file"] = "missing.wav"
        self.assertFalse(app.collect_project_samples())
        self.assertIn("missing.wav", app.status)

    def test_save_as_carries_samples_owned_only_by_old_project(self):
        app = self.app()
        name = "only-in-bundle.wav"
        old_dir = app.project_sample_dir()
        old_dir.mkdir(parents=True)
        (old_dir / name).write_bytes(b"local original")
        app.custom_sample_files[0] = name
        target = self.root / "elsewhere" / "Copy.starrypad.json"
        self.assertTrue(app.save_project_as(target))
        self.assertEqual(app.custom_sample_path(name).read_bytes(), b"local original")
        self.assertEqual(app.custom_sample_path(name).parent, target.parent / "Copy.samples")
        self.assertFalse((app.user_sample_dir / name).exists())

    def test_bundle_reopens_all_layers_in_empty_user_environment(self):
        app = self.app()
        for name in ("soft.wav", "hard.wav"):
            self.sound(app, name, name.encode())
        layers = [drum.default_pad_layer(), drum.default_pad_layer()]
        for layer, name in zip(layers, ("soft.wav", "hard.wav")):
            layer["file"] = name
        app.pad_layers[0] = layers
        app.loop_events = [(0.0, 0, 100)]
        snapshot = app.loop_render_snapshot()
        output = self.root / "bundle.zip"
        def stems(directory, _snapshot):
            directory.mkdir()
            (directory / "loop.mid").write_bytes(b"midi")
        with mock.patch.object(app, "export_stems", side_effect=stems):
            app.export_project_bundle(output, snapshot)
        for name in ("soft.wav", "hard.wav"):
            (app.user_sample_dir / name).unlink()
        extracted = self.root / "other computer"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        restored = self.app(name="empty user")
        self.assertTrue(restored.open_project(extracted / "Current.starrypad.json"))
        self.assertEqual(len(restored.pad_layers[0]), 2)
        for layer in restored.pad_layers[0]:
            self.assertEqual(restored.custom_sample_path(layer["file"]).read_bytes(), layer["file"].encode())
        self.assertFalse(restored.user_sample_dir.exists())

    def test_export_snapshot_keeps_paths_and_sound_references_after_switch(self):
        app = self.app()
        source = self.sound(app, "owned.wav")
        app.custom_sample_files[0] = source.name
        sound = object()
        app.custom_sound_cache[source.name] = sound
        snapshot = app.loop_render_snapshot()
        app.custom_sound_cache = {}
        app.project_path = self.root / "other.starrypad.json"
        app.pad_layers[0][0]["file"] = None
        self.assertEqual(snapshot["sample_paths"][source.name], source)
        self.assertIs(snapshot["custom_sound_cache"][source.name], sound)
        self.assertEqual(snapshot["project"]["kits"]["A"]["custom_samples"][0], source.name)

    def test_stale_save_failure_does_not_replace_current_project_status(self):
        app = self.app()
        old = app.project_path
        self.assertTrue(app.new_project())
        app.status = "Current project"
        app._save_worker.results.put(project_io.SaveResult(old.resolve(), 1, "old failure"))
        app.poll_storage()
        self.assertEqual(app.status, "Current project")

    def test_successful_retry_is_not_overridden_by_older_failure_notification(self):
        app = self.app()
        with mock.patch.object(project_io, "write_text_atomic", side_effect=OSError("disk full")):
            self.assertFalse(app.persist_settings())
        self.assertTrue(app.persist_settings())
        app.status = "Project saved"
        app.poll_storage()
        self.assertEqual(app.status, "Project saved")

    def test_backup_recovery_with_injected_paths(self):
        app = self.app()
        app.bpm = 141
        self.assertTrue(app.persist_settings())
        app.project_path.write_text("{broken")
        restored = self.app()
        self.assertEqual(restored.bpm, 141)
        self.assertEqual(restored.status, "Recovered project autosave")


if __name__ == "__main__":
    unittest.main()
