#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# TetherLink — Setup Script (Milestone 3: Wayland + PipeWire)
# Verifies all dependencies and prepares the environment.
#
# Usage: ./server/setup_virtual_display.sh
# ─────────────────────────────────────────────────────────────────────────────

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS="${GREEN}[PASS]${NC}"; FAIL="${RED}[FAIL]${NC}"; INFO="${BOLD}[INFO]${NC}"

echo ""
echo -e "${BOLD}TetherLink M3 — Environment Setup${NC}"
echo "────────────────────────────────────"

ERRORS=0

# ── Session type ──────────────────────────────────────────────────────────────
SESSION=$(echo $XDG_SESSION_TYPE)
if [ "$SESSION" = "wayland" ]; then
    echo -e "$PASS  Wayland session detected"
else
    echo -e "$FAIL  Not running Wayland (detected: $SESSION)"
    echo "       Log out and select 'Ubuntu' (not 'Ubuntu on Xorg') at login"
    ERRORS=$((ERRORS+1))
fi

# ── PipeWire ──────────────────────────────────────────────────────────────────
if systemctl --user is-active pipewire &>/dev/null; then
    echo -e "$PASS  PipeWire is running"
else
    echo -e "$FAIL  PipeWire is not running"
    echo "       Run: systemctl --user start pipewire"
    ERRORS=$((ERRORS+1))
fi

# ── XDG ScreenCast portal ─────────────────────────────────────────────────────
if gdbus introspect --session \
    --dest org.freedesktop.portal.Desktop \
    --object-path /org/freedesktop/portal/desktop \
    --xml 2>/dev/null | grep -q "ScreenCast"; then
    echo -e "$PASS  XDG ScreenCast portal available"
else
    echo -e "$FAIL  XDG ScreenCast portal not found"
    ERRORS=$((ERRORS+1))
fi

# ── GStreamer PipeWire plugin ──────────────────────────────────────────────────
if GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0 \
   gst-inspect-1.0 pipewiresrc &>/dev/null || \
   find /usr/lib -name "libgstpipewire.so" 2>/dev/null | grep -q .; then
    echo -e "$PASS  GStreamer pipewiresrc plugin found"
else
    echo -e "$FAIL  GStreamer pipewiresrc not found"
    echo "       Run: sudo apt install gstreamer1.0-pipewire"
    ERRORS=$((ERRORS+1))
fi

# ── Python deps (check active python, fallback to system) ────────────────────
PYTHON=$(which python3)
$PYTHON -c "from gi.repository import Gst, GLib; import dbus" 2>/dev/null && \
    echo -e "$PASS  Python gi + dbus available" || {
    echo -e "$FAIL  Python gi or dbus missing"
    echo "       Run: sudo apt install python3-gi python3-dbus"
    ERRORS=$((ERRORS+1))
}

$PYTHON -c "import mss, PIL" 2>/dev/null && \
    echo -e "$PASS  mss + Pillow available" || {
    echo -e "$FAIL  mss or Pillow missing"
    echo "       Run: pip install mss Pillow"
    ERRORS=$((ERRORS+1))
}

# ── venv check ────────────────────────────────────────────────────────────────
if [ -f "venv/bin/python3" ]; then
    VENV_PYTHON=$(venv/bin/python3 -c "import sys; print(sys.executable)")
    if echo "$VENV_PYTHON" | grep -q "anaconda\|conda"; then
        echo -e "${YELLOW}[WARN]${NC}  venv uses Anaconda Python — system packages may not be visible"
        echo "       Rebuild with: /usr/bin/python3 -m venv venv --system-site-packages"
    else
        venv/bin/python3 -c "from gi.repository import Gst; import dbus" 2>/dev/null && \
            echo -e "$PASS  venv can access gi + dbus" || {
            echo -e "${YELLOW}[WARN]${NC}  venv cannot see gi/dbus — rebuild with:"
            echo "       /usr/bin/python3 -m venv venv --system-site-packages"
        }
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅ All checks passed!${NC}"
    echo ""
    echo "  Start server:"
    echo "  ${BOLD}source venv/bin/activate && python server/tetherlink_server.py${NC}"
    echo ""
    echo "  A permission dialog will appear on first run."
    echo "  Select the monitor you want to stream to your tablet."
else
    echo -e "${RED}${BOLD}❌ $ERRORS check(s) failed — fix the above before running the server.${NC}"
fi
echo ""