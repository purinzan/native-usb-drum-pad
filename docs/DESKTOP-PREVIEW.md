# Desktop preview (PR2)

PR2 adds a PyInstaller onedir build around the existing playing surface. It does
not add the four-sound workflow, an installer, auto-update, an account, or a
published GitHub Release. It depends on the storage foundation in PR #35.

## Artifacts and startup

The Desktop preview GitHub Actions run uploads `STARRYPAD-preview-windows-x64`
and `STARRYPAD-preview-macos-arm64` only after their relocated executable passes
the packaging smoke checks. Each artifact contains a ZIP, a SHA-256 checksum and
a JSON smoke report. Artifacts expire after 14 days; they are not permanent
release download links. A failed target does not upload a preview package.

Windows: fully extract the inner ZIP and run `STARRYPAD/STARRYPAD.exe`. Keep the
entire folder, including `_internal`; copying only the EXE will not work.
macOS: extract the inner ZIP and open `STARRYPAD.app`. Move the whole app bundle,
not its inner executable. No external Python or ffmpeg is required for the
basic pad/WAV path. Optional formats that need ffmpeg remain optional.

Windows builds are unsigned. macOS binaries/bundles have PyInstaller's ad-hoc
signature, not a Developer ID signature, and are not notarized. OS security
warnings can therefore block launch. This is an internal testing artifact, not
a frictionless public distribution. Do not disable OS security protections.

The current CI targets are Windows x64 and Apple Silicon macOS, not Intel Mac
or Windows ARM. macOS metadata declares 14.0 as the build floor, consistent with
the pinned NumPy arm64 wheel. That is not evidence of testing on every macOS
version: the smoke report records the actual runner OS. Windows likewise needs
real Windows 10/11 client validation beyond the hosted Windows Server runner.

Settings and musical content remain in the PR1 OS user folders. In windowed
mode, startup exceptions are written to `desktop.log` under the platformdirs
STARRYPAD user log directory; a startup failure also attempts a Tk error dialog.
The app does not save projects beside the executable. Existing source launchers
remain unchanged. Open projects from the app's project menu; file associations
and opening project documents by double-click are not part of this preview.

## Build

On the matching OS with CPython 3.12.10 and Git:

```text
python -m venv .venv
# Activate this environment using the OS-appropriate command.
python -m pip install -r requirements-build.txt
python tools/build_preview.py
```

The script generates build provenance and copies the CPython version's complete
license from the official CPython tag at build time (network required). Existing
runtime dependency pins are unchanged; PyInstaller is a separate build dependency.
Package metadata/licenses are copied alongside the existing sample/font notices.
The spec explicitly includes only assets, samples, notices and generated build
metadata, never local projects, recordings, credentials or source test files.

Windows and macOS builds run on their own OS. Intel Mac can use the same script
on an x86_64 host but is not a current CI artifact or validated target. No
universal2 packaging is attempted. No signing keys or public-release permissions
are needed by this workflow; its GitHub token has contents:read only.

## What is checked automatically

The complete source regression suite runs before building. The build then creates
an archive, extracts it into a new path containing spaces and Japanese characters,
and starts its executable with an empty working directory and a restricted PATH.
PYTHONHOME, PYTHONPATH, virtual-environment, Tcl/Tk and dynamic-library search
overrides are removed. The child runs the embedded interpreter, not `python`.

`--smoke-test REPORT_JSON` selects a separate path before normal startup. It uses
a temporary user-data root, a non-default settings filename (no legacy import),
dummy SDL audio/video, no MIDI ports, no microphone streams and no hardware
buffer changes. It checks all referenced built-in sounds, bundled fonts through
actual UI drawing, NumPy/WSOLA, the PortAudio library, Tcl/Tk's file-dialog command,
16 queued pad triggers, a non-silent 48 kHz stereo WAV, and project/custom-sample
save/reopen. Package contents must remain byte-for-byte unchanged after the run.
The macOS signature is verified before packaging and after extraction.

A preview is published only after the report states frozen=true, ok=true, matches
the build commit and contains every required check. Failure reports remain in the
separate diagnostic artifact. The report never claims physical-device testing.

## Remaining manual release gates

- Start the downloaded artifact in Finder/Explorer on a clean client machine
  without Python. Hosted runners still have build tools installed, even though
  the smoke subprocess cannot find their Python/ffmpeg via PATH.
- Verify actual file Open/Save dialog selection and normal keyboard/mouse use.
- Test microphone allow/deny/retry and recording from an actual input. The stable
  macOS bundle ID is `io.github.purinzan.starrypad`; a microphone usage description
  is present. Presence of that key does not prove the permission flow works.
- Test physical MIDI, audible output, unplug/replug, sleep/wake and latency.
- Validate minimum supported client OS versions, then obtain appropriate signing
  and macOS notarization before public distribution. No formal release is created
  by this PR or its workflow.

References: PyInstaller spec-files, runtime-information, feature-notes and
common-issues-and-pitfalls in the official documentation; Apple's
NSMicrophoneUsageDescription documentation. See the implementation plan for the
later four-sound workflow, which is outside this packaging change.
