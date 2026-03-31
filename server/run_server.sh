#!/bin/bash
# TetherLink Server Launcher
# Ensures correct system GLib is loaded (fixes GStreamer plugin issues with venv)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Activate venv
source "$PROJECT_DIR/venv/bin/activate"

# Force system GLib to avoid Anaconda version conflicts
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libglib-2.0.so.0
export GST_PLUGIN_SYSTEM_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0

# Pass all args to server
python "$SCRIPT_DIR/tetherlink_server.py" "$@"