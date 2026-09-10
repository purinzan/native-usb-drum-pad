# PR1: project storage and portable bundles

This change does not alter the playing surface, MIDI routing, loop timing or
project schema. The four-sound workflow, application packaging and iOS work
are not part of PR1.

## Paths and import

`app_paths.AppPaths.discover()` resolves version-independent STARRYPAD user
folders with platformdirs. Bundled resources remain under the source/bundle
root; app settings use the config folder; projects, user-samples and exports
use the data folder. No directories are created at import time.

Known legacy folders alongside the program are copied on normal first startup.
Settings references within the old projects folder are relocated; user-chosen
external paths remain unchanged. Originals are never removed. Every copied
file is checked by SHA-256, existing differing content is not replaced, and an
import marker is written only after the import has been verified. An interrupted
import can be retried. A corrupt old settings file can fall back to its backup.
Unknown old development folders are not searched automatically.

An injected `AppPaths` isolates tests. An explicit non-default settings path
uses sibling projects/user-samples/exports, unless explicit AppPaths were also
provided. `settings_path=None` disables persistence and starts no save worker.
Tests that exercise sample-file operations must also inject paths or mock the
module sample directory; disabling settings alone is not a sample sandbox.

## Saves

The UI/state owner copies the project and settings under the existing loop
RLock. The audio worker only sets a dirty event. A single writer serializes
those detached snapshots; it never reads current application state. Queued
requests coalesce only by destination, and replacement requests move to the
back of the queue so shared settings cannot be written out of request order.

Files are serialized before any destination changes, then written through
unique sibling temporary files, flush/fsync and replacement. Project files
are written before app settings. Each JSON has a `.bak` copy. A save is not a
multi-file filesystem transaction; primary and backup updates are separate.
Destination/revision-tagged results prevent an old project's notification from
changing the current project's status. Explicit saves wait for completion and
report failure; graceful shutdown drains pending writes. Force-killed work that
has not reached a snapshot is not guaranteed to survive.

## Sharing

A bundle contains a project file, the matching `<name>.samples` directory,
Stems (including MIDI), and LICENSE-SAMPLES. Collection walks all layers in all
kits as well as legacy first-layer references. Optional `source_file` references
are collected when present; creating/persisting those new metadata fields is
reserved for the later four-sound change.

Sample names reject path separators, traversal, drive/alternate-stream syntax,
NULs and cross-platform case collisions. Sample symlinks are not followed.
Missing sources fail before collection. Copying refuses different destination
files; a temporary ZIP is published only after its content is complete. On
filesystems without hard links, an exclusive-copy fallback avoids overwriting
existing data; a force-killed fallback can leave an unreferenced incomplete
sample, which is reported as a conflict on retry rather than overwritten.

Save As collects while the old project still owns the sample references, then
switches paths. Export snapshots retain the old sound objects and sample paths;
a later project/cache change cannot substitute another project's sounds. A
missing custom sound is an export error, not a silent built-in substitution.
The existing renderer's sample-rate conversion behavior is not redesigned here.

## Validation

- `python -m unittest -v test_project_io`: device-independent filesystem,
  migration, collision, worker-ordering/failure and ZIP round-trip tests.
- `python -m unittest discover -v`: those tests plus application integration and
  the existing complete regression suite. Use dummy SDL for CI.
- Integration tests reopen a bundle in an empty user-data environment after the
  source library has been deleted; the ZIP-presence check alone is insufficient.

CI is not a substitute for physical MIDI, microphone, sleep/wake, audio-latency
or unsigned/signed application-package testing. No desktop installer or release
is published by this change.
