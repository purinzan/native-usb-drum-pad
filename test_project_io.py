"""Storage tests run without pygame, an audio device, or real user directories."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from app_paths import AppPaths
import project_io as io


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="starrypad-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = AppPaths.under(self.root / "new 日本語 space", self.root / "resources")

    def test_path_discovery_does_not_create_directories(self):
        self.assertFalse(self.paths.data_dir.exists())
        self.assertNotEqual(self.paths.resource_root, self.paths.data_dir)
        self.assertEqual(self.paths.settings_file.parent, self.paths.config_dir)
        self.assertEqual(self.paths.samples.parent, self.paths.data_dir)

    def test_atomic_save_replaces_and_leaves_no_temporary_file(self):
        path = self.root / "日本語 space" / "song.json"
        io.write_text_atomic(path, '{"value":1}\n')
        io.write_text_atomic(path, '{"value":2}\n')
        self.assertEqual(json.loads(path.read_text())["value"], 2)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_replace_preserves_previous_file_and_cleans_up(self):
        path = self.root / "song.json"
        path.write_text("old")
        with mock.patch.object(io.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                io.write_text_atomic(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_copy_is_idempotent_and_conflicts_never_overwrite(self):
        source, target = self.root / "old.wav", self.root / "new.wav"
        source.write_bytes(b"one")
        self.assertTrue(io.copy_verified(source, target))
        self.assertFalse(io.copy_verified(source, target))
        source.write_bytes(b"two")
        with self.assertRaises(FileExistsError):
            io.copy_verified(source, target)
        self.assertEqual(target.read_bytes(), b"one")
        self.assertEqual(source.read_bytes(), b"two")

    def test_copy_on_filesystem_without_hard_links(self):
        source, target = self.root / "old.wav", self.root / "new.wav"
        source.write_bytes(b"works on removable media")
        with mock.patch.object(io.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            self.assertTrue(io.copy_verified(source, target))
        self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_failed_copy_leaves_no_partial_destination(self):
        source, target = self.root / "old.wav", self.root / "new.wav"
        source.write_bytes(b"one")
        with mock.patch.object(io.shutil, "copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                io.copy_verified(source, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_all_kits_layers_and_optional_originals_are_collected(self):
        project = {"kits": {"A": {"custom_samples": ["first.wav", None],
                                   "pad_layers": [[{"file": "first.wav"},
                                                   {"file": "second.wav", "source_file": "raw.wav"}]]},
                             "D": {"pad_layers": [[{"file": "last.wav"}]]}}}
        self.assertEqual(io.referenced_samples(project), ("first.wav", "last.wav", "raw.wav", "second.wav"))

    def test_cross_platform_path_traversal_is_rejected(self):
        for name in ("../bad.wav", "..\\bad.wav", "C:bad.wav", "C:\\bad.wav", "/bad.wav",
                     "\\\\server\\bad.wav", "normal.wav:stream", "bad\x00.wav", "plain.mp3"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                io.sample_name(name)
        self.assertEqual(io.sample_name("自分の音 1.wav"), "自分の音 1.wav")

    def test_case_collisions_are_rejected_on_all_hosts(self):
        with self.assertRaises(ValueError):
            io.referenced_samples({"kits": {"A": {"custom_samples": ["Kick.wav", "kick.wav"]}}})

    def test_collected_sample_takes_priority_over_global_sample(self):
        project = self.root / "Music.starrypad.json"
        local = io.project_sample_dir(project) / "sound.wav"
        global_sample = self.paths.samples / "sound.wav"
        local.parent.mkdir()
        global_sample.parent.mkdir(parents=True)
        local.write_bytes(b"local")
        global_sample.write_bytes(b"global")
        self.assertEqual(io.resolve_sample(local.name, project, self.paths.samples), local)

    def test_missing_sample_aborts_before_copying_anything(self):
        available = self.root / "present.wav"
        available.write_bytes(b"yes")
        project = {"kits": {"A": {"custom_samples": ["present.wav", "missing.wav"]}}}
        target = self.root / "collected"
        with self.assertRaises(FileNotFoundError):
            io.collect_samples(project, target, {"present.wav": available, "missing.wav": self.root / "missing.wav"})
        self.assertFalse(target.exists())

    def test_bundle_round_trip_uses_no_original_sample_library(self):
        old = self.root / "old-library"
        old.mkdir()
        project = {"version": 1, "kits": {"A": {"custom_samples": ["soft.wav"],
                           "pad_layers": [[{"file": "soft.wav"}, {"file": "hard.wav"}]]},
                         "D": {"pad_layers": [[{"file": "other.wav", "source_file": "raw.wav"}]]}}}
        sources = {}
        for name in io.referenced_samples(project):
            sources[name] = old / name
            sources[name].write_bytes(name.encode())
        license_path = self.root / "license"
        license_path.write_text("sample attribution")
        output = self.root / "project.zip"
        def stems(folder):
            folder.mkdir()
            (folder / "loop.mid").write_bytes(b"midi")
        io.bundle_project(output, project, "自分の音", sources, stems, license_path)
        for source in sources.values():
            source.unlink()
        old.rmdir()
        extracted = self.root / "other computer"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        project_file = extracted / "自分の音.starrypad.json"
        restored = json.loads(project_file.read_text())
        for name in io.referenced_samples(restored):
            resolved = io.resolve_sample(name, project_file, self.root / "empty-cache")
            self.assertEqual(resolved.read_bytes(), name.encode())
        self.assertTrue((extracted / "LICENSE-SAMPLES").exists())
        self.assertFalse((self.root / "empty-cache").exists())

    def test_bundle_failure_preserves_existing_export(self):
        output = self.root / "project.zip"
        output.write_bytes(b"last good export")
        project = {"kits": {"A": {"custom_samples": ["missing.wav"]}}}
        with self.assertRaises(FileNotFoundError):
            io.bundle_project(output, project, "Name", {"missing.wav": self.root / "gone.wav"}, lambda _: None)
        self.assertEqual(output.read_bytes(), b"last good export")
        self.assertEqual(list(self.root.iterdir()), [output])

    def test_stem_failure_does_not_publish_bundle(self):
        output = self.root / "project.zip"
        with self.assertRaises(OSError):
            io.bundle_project(output, {"kits": {}}, "Name", {}, mock.Mock(side_effect=OSError("render failed")))
        self.assertFalse(output.exists())

    def test_bundle_project_title_cannot_escape_archive_root(self):
        output = self.root / "project.zip"
        io.bundle_project(output, {"kits": {}}, "../../escape", {}, lambda _: None)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(len(archive.namelist()), 1)
            self.assertNotIn("/", archive.namelist()[0])

    def legacy(self):
        root = self.paths.resource_root
        (root / "projects").mkdir(parents=True)
        (root / "user-samples").mkdir()
        project = root / "projects" / "Song.starrypad.json"
        project.write_text('{"version":1}')
        (root / "user-samples" / "mine.wav").write_bytes(b"original")
        data = {"current_project": str(project), "recent_projects": [str(project), str(self.root / "external.json")]}
        settings = root / "drum_pad_settings.json"
        settings.write_text(json.dumps(data))
        return root, settings, data

    def test_copy_migration_remaps_only_known_paths_and_keeps_originals(self):
        root, settings, data = self.legacy()
        original = settings.read_bytes()
        self.assertTrue(io.migrate_legacy_data(root, self.paths))
        self.assertFalse(io.migrate_legacy_data(root, self.paths))
        new = json.loads(self.paths.settings_file.read_text())
        self.assertEqual(new["current_project"], str(self.paths.projects / "Song.starrypad.json"))
        self.assertEqual(new["recent_projects"][1], data["recent_projects"][1])
        self.assertEqual(settings.read_bytes(), original)
        self.assertEqual((root / "user-samples" / "mine.wav").read_bytes(), b"original")
        self.assertEqual((self.paths.samples / "mine.wav").read_bytes(), b"original")

    def test_interrupted_migration_can_retry_without_overwriting_data(self):
        root, _, _ = self.legacy()
        writer = io.write_text_atomic
        def fail_marker(path, text):
            if path.name.startswith("legacy-import-"):
                raise OSError("power lost before marker")
            writer(path, text)
        with mock.patch.object(io, "write_text_atomic", side_effect=fail_marker), self.assertRaises(OSError):
            io.migrate_legacy_data(root, self.paths)
        self.assertTrue(io.migrate_legacy_data(root, self.paths))
        self.assertFalse(io.migrate_legacy_data(root, self.paths))

    def test_migration_conflict_preserves_existing_settings(self):
        root, _, _ = self.legacy()
        self.paths.config_dir.mkdir(parents=True)
        self.paths.settings_file.write_text('{"current_project":"newer"}')
        with self.assertRaises(FileExistsError):
            io.migrate_legacy_data(root, self.paths)
        self.assertEqual(json.loads(self.paths.settings_file.read_text())["current_project"], "newer")
        self.assertEqual(list(self.paths.config_dir.glob("legacy-import-*")), [])

    def test_legacy_corrupt_primary_recovers_from_backup_without_changing_source(self):
        root, settings, data = self.legacy()
        Path(str(settings) + ".bak").write_text(json.dumps(data))
        settings.write_text("{corrupt")
        self.assertTrue(io.migrate_legacy_data(root, self.paths))
        migrated = json.loads(self.paths.settings_file.read_text())
        self.assertEqual(migrated["current_project"], str(self.paths.projects / "Song.starrypad.json"))
        self.assertEqual(settings.read_text(), "{corrupt")

    def test_empty_legacy_layout_does_not_write_marker(self):
        self.assertFalse(io.migrate_legacy_data(self.paths.resource_root, self.paths))
        self.assertFalse(self.paths.config_dir.exists())

    @unittest.skipIf(os.name == "nt", "Creating symlinks requires Windows privileges")
    def test_sample_symlink_cannot_escape_bundle(self):
        self.paths.samples.mkdir(parents=True)
        outside = self.root / "private.wav"
        outside.write_bytes(b"not a project sample")
        (self.paths.samples / "link.wav").symlink_to(outside)
        with self.assertRaises(ValueError):
            io.resolve_sample("link.wav", None, self.paths.samples)


class SaveWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def worker(self, writer=io.write_save_request):
        worker = io.SaveWorker(writer)
        self.addCleanup(worker.close)
        return worker

    def test_requests_detach_nested_payloads_and_destinations(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(request):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release writer")
            io.write_save_request(request)
        worker = self.worker(delayed)
        self.addCleanup(release.set)
        path = self.root / "old.json"
        payload = {"nested": {"notes": [1]}}
        request = worker.submit(path, [(path, payload)])
        self.assertTrue(entered.wait(3))
        payload["nested"]["notes"].append(2)
        path = self.root / "new.json"
        release.set()
        self.assertIsNone(worker.wait(request).error)
        self.assertEqual(json.loads((self.root / "old.json").read_text()), {"nested": {"notes": [1]}})
        self.assertFalse(path.exists())

    def test_coalesces_same_destination_without_losing_other_projects(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        def delayed(request):
            if not seen:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release writer")
            seen.append((request.key.name, request.revision))
            io.write_save_request(request)
        worker = self.worker(delayed)
        self.addCleanup(release.set)
        busy, a, b, settings = [self.root / name for name in ("busy.json", "a.json", "b.json", "settings.json")]
        worker.submit(busy, [(busy, {})])
        self.assertTrue(entered.wait(3))
        old_a = worker.submit(a, [(a, {"value": 1}), (settings, {"project": "old A"})])
        req_b = worker.submit(b, [(b, {"value": 2}), (settings, {"project": "B"})])
        new_a = worker.submit(a, [(a, {"value": 3}), (settings, {"project": "new A"})])
        release.set()
        self.assertIsNone(worker.wait(old_a).error)
        worker.flush()
        self.assertEqual([item[0] for item in seen], ["busy.json", "b.json", "a.json"])
        self.assertEqual(json.loads(a.read_text())["value"], 3)
        self.assertEqual(json.loads(b.read_text())["value"], 2)
        self.assertEqual(json.loads(settings.read_text())["project"], "new A")
        self.assertEqual(worker.wait(new_a).revision, new_a.revision)
        self.assertEqual(worker.wait(req_b).revision, req_b.revision)

    def test_failure_reports_target_revision_and_retry_clears_it(self):
        path = self.root / "song.json"
        writer = mock.Mock(side_effect=[OSError("disk full"), None])
        worker = self.worker(writer)
        first = worker.submit(path, [(path, {"a": 1})])
        result = worker.wait(first)
        self.assertEqual(result.key, path.resolve())
        self.assertEqual(result.revision, first.revision)
        self.assertIn("disk full", result.error)
        second = worker.submit(path, [(path, {"a": 2})])
        self.assertIsNone(worker.wait(second).error)

    def test_close_drains_pending_writes_and_rejects_new_saves(self):
        worker = self.worker()
        path = self.root / "song.json"
        worker.submit(path, [(path, {"a": 1})])
        worker.close()
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertFalse(worker._thread.is_alive())
        with self.assertRaises(RuntimeError):
            worker.submit(path, [(path, {})])

    def test_primary_and_backup_are_identical(self):
        worker = self.worker()
        path = self.root / "song.json"
        request = worker.submit(path, [(path, {"a": 1})])
        self.assertIsNone(worker.wait(request).error)
        self.assertEqual(path.read_bytes(), path.with_suffix(".json.bak").read_bytes())

    def test_serialization_error_does_not_change_any_destination(self):
        worker = self.worker()
        first, second = self.root / "project.json", self.root / "settings.json"
        first.write_text("unchanged")
        request = worker.submit(first, [(first, {"a": 1}), (second, {"bad": object()})])
        self.assertIn("TypeError", worker.wait(request).error)
        self.assertEqual(first.read_text(), "unchanged")
        self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
