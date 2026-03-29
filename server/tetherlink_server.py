"""
TetherLink Server - Linux (Wayland + X11 compatible)
Auto-detects display server and uses the best capture method available.

Wayland: uses grim (wayland screenshooter) or scrot fallback
X11:     uses mss directly

Protocol: [4-byte big-endian size][JPEG data] repeated per frame
"""

import socket
import struct
import threading
import logging
import time
import subprocess
import shutil
import os
from io import BytesIO
from PIL import Image

# ── Configuration ────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 8080
FPS = 30
JPEG_QUALITY = 80
FRAME_INTERVAL = 1.0 / FPS
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TetherLink")


def detect_capture_method() -> str:
    """Detect display server and return best capture method."""
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    xdg_session = os.environ.get("XDG_SESSION_TYPE", "").lower()

    is_wayland = bool(wayland_display) or xdg_session == "wayland"

    if is_wayland:
        if shutil.which("grim"):
            log.info("Display: Wayland — using grim for capture")
            return "grim"
        elif shutil.which("scrot"):
            log.info("Display: Wayland (XWayland) — using scrot for capture")
            return "scrot"
        else:
            log.warning("Wayland detected but neither 'grim' nor 'scrot' found.")
            log.warning("Install with: sudo apt install grim   (recommended)")
            log.warning("           or: sudo apt install scrot")
            raise RuntimeError("No screen capture tool found. Run: sudo apt install grim")
    else:
        log.info("Display: X11 — using mss for capture")
        return "mss"


def capture_grim() -> bytes:
    """Capture screen using grim (native Wayland tool). Outputs PNG to stdout."""
    result = subprocess.run(
        ["grim", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"grim failed: {result.stderr.decode()}")
    img = Image.open(BytesIO(result.stdout)).convert("RGB")
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def capture_scrot() -> bytes:
    """Capture screen using scrot (XWayland fallback). Outputs to temp file."""
    tmp = "/tmp/tetherlink_frame.png"
    result = subprocess.run(
        ["scrot", "--overwrite", tmp],
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"scrot failed: {result.stderr.decode()}")
    img = Image.open(tmp).convert("RGB")
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def capture_mss() -> bytes:
    """Capture screen using mss (X11)."""
    import mss
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()


def stream_to_client(conn: socket.socket, addr: tuple, method: str) -> None:
    """Stream frames to a single connected client."""
    log.info("Client connected: %s:%d", *addr)

    # For mss, keep one instance open per thread for efficiency
    mss_instance = None
    mss_monitor = None
    if method == "mss":
        import mss as mss_lib
        mss_instance = mss_lib.mss()
        mss_monitor = mss_instance.monitors[1]

    try:
        while True:
            start = time.monotonic()

            if method == "grim":
                jpeg_data = capture_grim()
            elif method == "scrot":
                jpeg_data = capture_scrot()
            else:
                raw = mss_instance.grab(mss_monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                jpeg_data = buf.getvalue()

            size_header = struct.pack(">I", len(jpeg_data))
            conn.sendall(size_header + jpeg_data)

            elapsed = time.monotonic() - start
            sleep_time = FRAME_INTERVAL - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except (BrokenPipeError, ConnectionResetError):
        log.info("Client disconnected: %s:%d", *addr)
    except Exception as exc:
        log.error("Error streaming to %s:%d — %s", *addr, exc)
    finally:
        if mss_instance:
            mss_instance.close()
        conn.close()


def run_server() -> None:
    method = detect_capture_method()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(5)
        log.info("TetherLink server listening on %s:%d", HOST, PORT)
        log.info("Capture method: %s", method)

        while True:
            conn, addr = server.accept()
            thread = threading.Thread(
                target=stream_to_client,
                args=(conn, addr, method),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    run_server()