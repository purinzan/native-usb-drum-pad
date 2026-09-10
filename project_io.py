"""Project filesystem operations; no pygame, MIDI, or application-state access."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import queue
import shutil
import tempfile
import threading
import zipfile

PROJECT_EXTENSION = ".starrypad.json"


def write_text_atomic(path: Path, text: str) -> None:
    """Flush a unique sibling temporary file, then replace the destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_verified(source: Path, destination: Path) -> bool:
    """Copy without replacing different existing data. Return whether copied.

    Link publication is atomic. On filesystems without hard links, an exclusive
    copy still refuses overwrites; the caller commits the referencing JSON last.
    A killed fallback copy can leave an unreferenced file, never a new project
    pointing at it. A subsequent conflict is reported, not silently overwritten.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.is_symlink():
        raise FileNotFoundError(f"Sample unavailable or not a regular file: {source}")
    expected = digest(source)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file() or digest(destination) != expected:
            raise FileExistsError(f"Different file already exists: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        if digest(Path(temporary)) != expected:
            raise OSError(f"Source changed while copying: {source}")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or not destination.is_file() or digest(destination) != expected:
                raise FileExistsError(f"Different file already exists: {destination}")
            return False
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV) and getattr(exc, "winerror", None) != 1:
                raise
            # FAT/exFAT have no hard links. "xb" prevents a concurrent overwrite.
            owned = False
            try:
                with destination.open("xb") as output, Path(temporary).open("rb") as incoming:
                    owned = True
                    shutil.copyfileobj(incoming, output)
                    output.flush()
                    os.fsync(output.fileno())
                if digest(destination) != expected:
                    raise OSError(f"Copy verification failed: {destination}")
            except BaseException:
                if owned:
                    destination.unlink(missing_ok=True)
                raise
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def sample_name(value: str) -> str:
    """Reject traversal and Windows path/stream syntax on every host OS."""
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(char in value for char in '/\\:\x00')
            or PureWindowsPath(value).name != value
            or Path(value).suffix.lower() != ".wav"):
        raise ValueError(f"Invalid sample filename: {value!r}")
    return value


def referenced_samples(project: dict) -> tuple[str, ...]:
    """All kits, all pad layers, and legacy first-layer sample references."""
    names = set()
    for profile in project.get("kits", {}).values():
        if not isinstance(profile, dict):
            continue
        for value in profile.get("custom_samples", []) or []:
            if value:
                names.add(sample_name(value))
        for layers in profile.get("pad_layers", []) or []:
            for layer in layers or []:
                if not isinstance(layer, dict):
                    continue
                for field in ("file", "source_file"):
                    if layer.get(field):
                        names.add(sample_name(layer[field]))
    folded = {}
    for name in sorted(names):
        previous = folded.setdefault(name.casefold(), name)
        if previous != name:
            raise ValueError(f"Sample names collide on Windows/macOS: {previous}, {name}")
    return tuple(sorted(names))


def project_sample_dir(project_path: Path) -> Path:
    target = Path(project_path)
    return target.parent / f"{target.name.removesuffix(PROJECT_EXTENSION)}.samples"


def resolve_sample(name: str, project_path: Path | None, user_samples: Path) -> Path:
    name = sample_name(name)
    roots = ([project_sample_dir(project_path)] if project_path else []) + [Path(user_samples)]
    for root in roots:
        if root.is_symlink():
            raise ValueError(f"Sample directories cannot be symlinks: {root}")
        candidate = root / name
        # Do not follow a shared project's symlink outside its sample folder.
        if candidate.is_symlink():
            raise ValueError(f"Sample symlinks are not supported: {candidate}")
        if candidate.is_file():
            return candidate
    return roots[-1] / name  # Existing browser uses a missing path to show/relink it.


def collect_samples(project: dict, target_dir: Path, sources: dict[str, Path]) -> int:
    names = referenced_samples(project)
    # Validate every source before publishing any collected file.
    for name in names:
        source = Path(sources[name])
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Missing sample: {name}")
    return sum(copy_verified(Path(sources[name]), Path(target_dir) / name) for name in names)


def safe_project_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in " -_" else "_" for char in name).strip(" .")[:48] or "Project"


def bundle_project(path: Path, project: dict, name: str, sources: dict[str, Path],
                   write_stems, license_path: Path | None = None) -> None:
    """Publish a portable ZIP only after its samples, stems and JSON are ready."""
    safe_name = safe_project_name(name)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".starrypad-bundle-", dir=path.parent) as temporary:
        root = Path(temporary) / "content"
        root.mkdir()
        project_file = root / f"{safe_name}{PROJECT_EXTENSION}"
        collect_samples(project, project_sample_dir(project_file), sources)
        write_stems(root / "Stems")
        if license_path is not None:
            copy_verified(Path(license_path), root / "LICENSE-SAMPLES")
        write_text_atomic(project_file, json.dumps(project, indent=2, ensure_ascii=True) + "\n")
        archive_path = Path(temporary) / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(root))
        with archive_path.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(archive_path, path)


def migrate_legacy_data(legacy_root: Path, paths) -> bool:
    """Copy a known old layout, never delete originals or overwrite conflicts.

    Import is attempted once per source directory. Interrupted attempts can be
    retried because published copies are content-verified and idempotent.
    """
    legacy_root = Path(legacy_root).resolve()
    marker_id = hashlib.sha256(str(legacy_root).encode()).hexdigest()[:16]
    marker = paths.config_dir / f"legacy-import-{marker_id}.json"
    if marker.exists():
        return False
    old_settings = legacy_root / "drum_pad_settings.json"
    directories = ((legacy_root / "user-samples", paths.samples),
                   (legacy_root / "projects", paths.projects),
                   (legacy_root / "exports", paths.exports))
    if not old_settings.exists() and not Path(str(old_settings) + ".bak").exists() and not any(source.exists() for source, _ in directories):
        return False

    # Samples/projects first: settings are the last references to be committed.
    for source_dir, destination_dir in directories:
        if source_dir.is_symlink():
            raise ValueError(f"Legacy import does not follow symlinks: {source_dir}")
        if source_dir.resolve() == destination_dir.resolve():
            continue
        for source in sorted(source_dir.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"Legacy import does not follow symlinks: {source}")
            if source.is_file():
                copy_verified(source, destination_dir / source.relative_to(source_dir))

    settings_data = None
    errors = []
    for source in (old_settings, Path(str(old_settings) + ".bak")):
        if not source.exists():
            continue
        try:
            settings_data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(settings_data, dict):
                raise ValueError("Legacy settings must be an object")
            break
        except (OSError, ValueError) as exc:
            settings_data = None
            errors.append(str(exc))
    if settings_data is None and errors:
        raise ValueError("Legacy settings and backup could not be read: " + "; ".join(errors))
    for suffix in ("", ".bak") if settings_data is not None else ():
        target = Path(str(paths.settings_file) + suffix)
        data = deepcopy(settings_data)

        def relocated(value):
            if not isinstance(value, str):
                return value
            old = Path(value)
            if not old.is_absolute():
                old = legacy_root / old
            try:
                relative = old.resolve().relative_to((legacy_root / "projects").resolve())
            except ValueError:
                return value  # User-chosen external project: retain its location.
            return str(paths.projects / relative)

        if "current_project" in data:
            data["current_project"] = relocated(data["current_project"])
        if isinstance(data.get("recent_projects"), list):
            data["recent_projects"] = [relocated(value) for value in data["recent_projects"]]
        if target.exists():
            if json.loads(target.read_text(encoding="utf-8")) != data:
                raise FileExistsError(f"Existing settings were not overwritten: {target}")
        else:
            write_text_atomic(target, json.dumps(data, indent=2, ensure_ascii=True) + "\n")
        if json.loads(target.read_text(encoding="utf-8")) != data:
            raise OSError(f"Legacy settings verification failed: {target}")
    write_text_atomic(marker, json.dumps({"source": str(legacy_root), "version": 1}) + "\n")
    return True


@dataclass(frozen=True)
class SaveRequest:
    key: Path
    revision: int
    files: tuple  # Ordered (Path, detached JSON payload) pairs; project before settings.


@dataclass(frozen=True)
class SaveResult:
    key: Path
    revision: int
    error: str | None = None


def write_save_request(request: SaveRequest) -> None:
    # Serialize outside the application's locks, before changing any file.
    encoded = [(path, json.dumps(data, indent=2, ensure_ascii=True) + "\n")
               for path, data in request.files]
    for path, text in encoded:
        write_text_atomic(path, text)
        write_text_atomic(path.with_suffix(path.suffix + ".bak"), text)


class SaveWorker:
    """One serial writer. Queued saves coalesce only for the same destination.

    Application state is copied in submit(), on the owning thread; the worker
    never reads the application or calls back into its locks. Failures carry a
    destination and revision, and a later successful retry clears that error.
    """
    def __init__(self, writer=write_save_request):
        self._writer = writer
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._outcomes = {}
        self._revision = 0
        self._active = False
        self._closed = False
        self.results = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="DrumProjectSave", daemon=True)
        self._thread.start()

    def submit(self, key: Path, files) -> SaveRequest:
        key = Path(key).resolve()
        detached = tuple((Path(path).resolve(), deepcopy(payload)) for path, payload in files)
        with self._condition:
            if self._closed:
                raise RuntimeError("Save worker is closed")
            self._revision += 1
            request = SaveRequest(key, self._revision, detached)
            self._pending[key] = request
            # A newer A must not be written before an older B's app settings.
            self._pending.move_to_end(key)
            self._condition.notify_all()
            return request

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if not self._pending:
                    return
                _, request = self._pending.popitem(last=False)
                self._active = True
            error = None
            try:
                self._writer(request)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            result = SaveResult(request.key, request.revision, error)
            with self._condition:
                self._outcomes[request.key] = result
                self._active = False
                self.results.put(result)
                self._condition.notify_all()

    def wait(self, request: SaveRequest, timeout: float = 10.0) -> SaveResult:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: (request.key in self._outcomes
                         and self._outcomes[request.key].revision >= request.revision), timeout)
            if not ready:
                raise TimeoutError(f"Save still pending: {request.key}")
            return self._outcomes[request.key]

    def flush(self, timeout: float = 10.0) -> None:
        with self._condition:
            if not self._condition.wait_for(lambda: not self._pending and not self._active, timeout):
                raise TimeoutError("Save worker has pending writes")

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                raise TimeoutError("Save worker did not finish before shutdown")
