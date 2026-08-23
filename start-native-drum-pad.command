#!/bin/sh
# macOS launcher: double-click in Finder, or run from a terminal.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python drum_pad_native.py
