"""Windowed desktop entry point; source development still uses drum_pad_native.py."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import traceback


def run_application():
    # Import after windowed stdout/stderr have been made usable.
    from drum_pad_native import main
    main()


def show_startup_error(message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        try:
            root.withdraw()
            messagebox.showerror("STARRYPAD could not start", message, parent=root)
        finally:
            root.destroy()
    except Exception:
        # A missing/broken Tk runtime must not hide the original logged error.
        pass


def run_windowed():
    from app_paths import AppPaths
    paths = AppPaths.discover(Path(__file__).resolve().parent)
    log_path = paths.log_dir / "desktop.log"
    old_out, old_err = sys.stdout, sys.stderr
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            log = stack.enter_context(log_path.open("a", encoding="utf-8", buffering=1))
            if sys.stdout is None:
                sys.stdout = log
            if sys.stderr is None:
                sys.stderr = log
            try:
                run_application()
                return 0
            except Exception:
                detail = traceback.format_exc()
                log.write(detail)
                log.flush()
                show_startup_error(f"Startup failed. Diagnostic log:\n{log_path}\n\n{detail.splitlines()[-1]}")
                return 1
    except OSError as exc:
        show_startup_error(f"Cannot open the diagnostic log:\n{log_path}\n\n{exc}")
        return 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def main(argv=None):
    parser = argparse.ArgumentParser(description="STARRYPAD desktop preview")
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT_JSON",
                        help="Run isolated dummy-device packaging checks; never use real user projects")
    args = parser.parse_args(argv)
    if args.smoke_test is not None:
        # This path must not initialize normal storage, MIDI or microphone streams.
        from desktop_smoke import run_smoke_test
        return run_smoke_test(args.smoke_test)
    return run_windowed()


if __name__ == "__main__":
    raise SystemExit(main())
