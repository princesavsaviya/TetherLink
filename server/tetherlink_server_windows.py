"""
TetherLink Server - Windows
Captures the screen using mss (faster than PIL.ImageGrab on Windows)
and streams JPEG frames to connected Android clients over TCP on port 8080.

Protocol: [4-byte big-endian size][JPEG data] repeated per frame
"""

import socket
import struct
import threading
import logging
import time
from io import BytesIO
import mss
from PIL import Image

# ── Configuration ────────────────────────────────────────────────────────────
HOST = "0.0.0.0"       # Listen on all interfaces
PORT = 8080            # TCP port
FPS = 30               # Target frames per second
JPEG_QUALITY = 80      # JPEG encode quality (1-95)
FRAME_INTERVAL = 1.0 / FPS
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TetherLink")


def capture_frame(sct: mss.mss, monitor: dict) -> bytes:
    """
    Capture the primary screen with mss and return JPEG bytes.
    mss returns BGRA data; we convert to RGB before encoding.
    """
    raw = sct.grab(monitor)
    # Convert BGRA (mss default) → RGB for Pillow
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def stream_to_client(conn: socket.socket, addr: tuple) -> None:
    """
    Continuously capture frames and send them to a single connected client.
    Each frame is prefixed with a 4-byte big-endian integer indicating the
    length of the JPEG payload that follows.
    """
    log.info("Client connected: %s:%d", *addr)
    try:
        with mss.mss() as sct:
            # Monitor 1 is the primary screen (index 0 is the virtual full desktop)
            monitor = sct.monitors[1]
            while True:
                start = time.monotonic()

                jpeg_data = capture_frame(sct, monitor)
                size_header = struct.pack(">I", len(jpeg_data))

                conn.sendall(size_header + jpeg_data)

                # Throttle to target FPS
                elapsed = time.monotonic() - start
                sleep_time = FRAME_INTERVAL - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    except (BrokenPipeError, ConnectionResetError, OSError):
        log.info("Client disconnected: %s:%d", *addr)
    except Exception as exc:
        log.error("Error streaming to %s:%d — %s", *addr, exc)
    finally:
        conn.close()


def run_server() -> None:
    """Accept incoming connections and spawn a streaming thread per client."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(5)
        log.info("TetherLink server listening on %s:%d", HOST, PORT)
        log.info("Connect your Android tablet via USB Tethering, then open the app.")

        while True:
            conn, addr = server.accept()
            thread = threading.Thread(
                target=stream_to_client,
                args=(conn, addr),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    run_server()
