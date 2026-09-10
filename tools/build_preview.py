"""Build, relocate, smoke-test, then publish an unsigned desktop preview ZIP."""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def target_label(system=None, machine=None):
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if system == "darwin" and machine in ("arm64", "x86_64"):
        return f"macos-{machine}"
    raise RuntimeError(f"Unsupported build target: {system}/{machine}")


def isolated_environment(source=None):
    env = dict(os.environ if source is None else source)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("PYTHON", "CONDA", "VIRTUAL_ENV", "DYLD_", "LD_LIBRARY_")) or upper in ("TCL_LIBRARY", "TK_LIBRARY"):
            env.pop(key, None)
        if upper == "PATH":
            env.pop(key, None)
    if sys.platform == "win32":
        windows = Path(env.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    else:
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["SDL_AUDIODRIVER"] = "dummy"
    env["SDL_VIDEODRIVER"] = "dummy"
    return env


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            result[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[relative] = digest(path)
    return result


def prepare_metadata(root):
    destination = root / "build" / "preview-metadata"
    destination.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    distributions = {dist.metadata["Name"]: dist.version for dist in metadata.distributions()}
    info = {"commit": commit, "target": target_label(), "python": platform.python_version(),
            "build_platform": platform.platform(), "pyinstaller": metadata.version("pyinstaller"),
            "requirements_sha256": digest(root / "requirements.txt"),
            "build_requirements_sha256": digest(root / "requirements-build.txt"),
            "installed_distributions": distributions,
            "distribution": "preview", "developer_signed": False, "notarized": False}
    (destination / "BUILD-INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    notices = destination / "THIRD-PARTY"
    notices.mkdir(exist_ok=True)
    # Build-time only: copy the complete license matching the embedded interpreter.
    url = f"https://raw.githubusercontent.com/python/cpython/v{platform.python_version()}/LICENSE"
    with urlopen(url, timeout=30) as response:
        license_text = response.read().decode("utf-8")
    if "PYTHON SOFTWARE FOUNDATION LICENSE" not in license_text:
        raise RuntimeError("Unexpected CPython license response")
    (notices / "Python-LICENSE.txt").write_text(license_text, encoding="utf-8")
    (notices / "README.txt").write_text(
        "Python license is in this directory. Python package metadata and license files\n"
        "ship in the adjacent *.dist-info directories. Bundled sample and font terms\n"
        "remain in LICENSE-SAMPLES, samples/, and assets/fonts/.\n", encoding="utf-8")
    return info


def verify_report(report, expected_commit):
    if not report.get("ok") or not report.get("frozen"):
        raise RuntimeError("The frozen application smoke test failed: " + json.dumps(report))
    if report.get("build", {}).get("commit") != expected_commit:
        raise RuntimeError("Smoke test exercised a different build")
    required = ("portaudio", "numpy_wsola", "tcl_tk", "builtin_samples", "playing_surface",
                "dummy_pad_hits", "wav_export", "save_reopen", "bundled_module_paths")
    if not all(report.get("checks", {}).get(key) for key in required):
        raise RuntimeError("Smoke test report lacks a required check")
    if report.get("physical_devices_tested") is not False:
        raise RuntimeError("Packaging checks must not claim physical device validation")


def build_preview(root=ROOT):
    root = Path(root).resolve()
    label = target_label()
    info = prepare_metadata(root)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(root / "Starrypad.spec")],
                   cwd=root, check=True)
    dist = root / "dist"
    package = dist / ("STARRYPAD.app" if sys.platform == "darwin" else "STARRYPAD")
    if sys.platform == "darwin":
        with (package / "Contents" / "Info.plist").open("rb") as stream:
            plist = plistlib.load(stream)
        if not plist.get("NSMicrophoneUsageDescription") or plist.get("CFBundleIdentifier") != "io.github.purinzan.starrypad":
            raise RuntimeError("macOS bundle identity or microphone usage description is missing")
        # Ad-hoc signing is expected. This is not Developer ID signing or notarization.
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(package)], check=True)
    name = f"STARRYPAD-{label}-{info['commit'][:7]}"
    reports = root / "build" / "preview-reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{name}-smoke.json"
    with tempfile.TemporaryDirectory(prefix="starrypad-distribution-") as temporary:
        temporary = Path(temporary)
        archive = temporary / f"{name}.zip"
        if sys.platform == "darwin":
            # Preserve bundle symlinks, executable permissions and ad-hoc signatures.
            subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(package), str(archive)], check=True)
        else:
            shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=dist, base_dir=package.name)
        relocated = temporary / "relocated 日本語"  # Also exercise spaces and non-ASCII paths.
        relocated.mkdir()
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(relocated)], check=True)
            executable = relocated / "STARRYPAD.app" / "Contents" / "MacOS" / "STARRYPAD"
        else:
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(relocated)
            executable = relocated / "STARRYPAD" / "STARRYPAD.exe"
        before = inventory(relocated)
        empty = temporary / "empty working directory"
        empty.mkdir()
        result = subprocess.run([str(executable), "--smoke-test", str(report_path)], cwd=empty,
                                env=isolated_environment(), timeout=120, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        (reports / f"{name}-process.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        if not report_path.exists():
            raise RuntimeError(f"Packaged program produced no report (exit {result.returncode}); see {reports}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        verify_report(report, info["commit"])
        if result.returncode != 0:
            raise RuntimeError(f"Packaged program exited {result.returncode}")
        if before != inventory(relocated):
            raise RuntimeError("Packaged program wrote into its installed files")
        report["relocated_package_unchanged"] = True
        report["external_python_removed_from_path"] = True
        report["empty_working_directory"] = True
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(relocated / "STARRYPAD.app")], check=True)
        # Do not publish a downloadable ZIP until the relocated executable passes.
        output = dist / "preview"
        output.mkdir(parents=True, exist_ok=True)
        published = output / archive.name
        shutil.copy2(archive, published)
        (output / f"{name}.sha256").write_text(f"{digest(published)}  {published.name}\n", encoding="utf-8")
        shutil.copy2(report_path, output / report_path.name)
        print(f"Verified preview: {published}")


if __name__ == "__main__":
    build_preview()
