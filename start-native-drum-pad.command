#!/bin/sh
# macOS launcher: double-click in Finder, or run from a terminal.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "STARRYPAD: no virtual environment found in $(pwd)/.venv"
  echo
  echo "Set one up first:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  echo
  printf "Press return to close. "
  read -r _
  exit 1
fi

exec .venv/bin/python drum_pad_native.py
