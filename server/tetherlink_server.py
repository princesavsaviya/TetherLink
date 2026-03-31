"""
TetherLink Server - v0.8.0
Wayland + PipeWire virtual display server with secure HMAC authentication.

Security flow (3-way handshake):
  1. Client → Server: [HELLO][16B device_id]
  2. Server → Client: [CHALLENGE][16B nonce]
  3. Client → Server: [RESPONSE][32B HMAC-SHA256(nonce, secret)]
  4. Server → Client: [OK][4B width][4B height]  or  [REJECT]

Pairing:
  First run: server generates secret key, displays QR code in terminal
  Android scans QR once → key stored in Android KeyStore
  Subsequent connections use stored key

Usage:
    python server/tetherlink_server.py
    python server/tetherlink_server.py --fps 60 --quality 90
    python server/tetherlink_server.py --pair   # show QR code again
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import struct
import threading
import time
from io import BytesIO
from pathlib import Path

import dbus
import dbus.mainloop.glib
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
from PIL import Image
from tray import TrayState, start_tray
from discovery import DiscoveryBroadcaster

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="TetherLink Server")
parser.add_argument("--fps",     type=int,  default=60)
parser.add_argument("--quality", type=int,  default=90)
parser.add_argument("--port",    type=int,  default=8080)
parser.add_argument("--pair",    action="store_true", help="Show pairing QR code")
parser.add_argument("--reset",   action="store_true", help="Reset pairing (new secret key)")
args = parser.parse_args()

WIDTH          = 2960
HEIGHT         = 1848
FPS            = args.fps
JPEG_QUALITY   = args.quality
PORT           = args.port
FRAME_INTERVAL = 1.0 / FPS
CONFIG_DIR     = Path.home() / ".config" / "tetherlink"
SECRET_FILE    = CONFIG_DIR / "secret.key"
DEVICES_FILE   = CONFIG_DIR / "paired_devices.json"
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TetherLink")

# ── Secret key management ─────────────────────────────────────────────────────

def load_or_create_secret() -> bytes:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if args.reset and SECRET_FILE.exists():
        SECRET_FILE.unlink()
        DEVICES_FILE.unlink(missing_ok=True)
        log.info("Secret reset — all paired devices removed")

    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()

    secret = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(secret)
    SECRET_FILE.chmod(0o600)
    log.info("New secret key generated: %s", SECRET_FILE)
    return secret


def load_paired_devices() -> dict:
    if DEVICES_FILE.exists():
        return json.loads(DEVICES_FILE.read_text())
    return {}


def save_paired_device(device_id: str, name: str):
    devices = load_paired_devices()
    devices[device_id] = {"name": name, "paired_at": time.time()}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(devices, indent=2))
    log.info("Paired device saved: %s (%s)", name, device_id)


def show_qr_code(secret: bytes):
    """Display pairing QR code in terminal."""
    try:
        import qrcode
        import base64
        payload = base64.b64encode(secret).decode()
        qr = qrcode.QRCode(border=1)
        qr.add_data(f"tetherlink://pair?key={payload}")
        qr.make(fit=True)
        print("\n" + "="*50)
        print("  TetherLink — Scan to pair your Android tablet")
        print("="*50)
        qr.print_ascii(invert=True)
        print("="*50 + "\n")
    except ImportError:
        # Fallback: show key as hex if qrcode not installed
        import base64
        payload = base64.b64encode(secret).decode()
        print("\n" + "="*50)
        print("  TetherLink Pairing Key (install qrcode for QR):")
        print(f"  tetherlink://pair?key={payload}")
        print("="*50 + "\n")
        log.info("Install qrcode for QR display: pip install qrcode")


# ── 3-way HMAC handshake ──────────────────────────────────────────────────────

MAGIC_HELLO     = b"TLHELO"
MAGIC_CHALLENGE = b"TLCHAL"
MAGIC_RESPONSE  = b"TLRESP"
MAGIC_OK        = b"TLOK__"
MAGIC_REJECT    = b"TLREJ_"

# Global: only one client at a time
_active_client_lock = threading.Lock()
_active_client_ip   = None


def authenticate_client(conn: socket.socket, addr: tuple, secret: bytes) -> tuple[bool, str, str]:
    """
    Run 3-way HMAC handshake.
    Returns (success, device_id, device_name).
    """
    global _active_client_ip

    conn.settimeout(10.0)

    # ── Step 1: Receive HELLO ─────────────────────────────────────────────────
    try:
        hello = conn.recv(6 + 16 + 64)  # magic + device_id + device_name
    except socket.timeout:
        log.warning("Auth timeout from %s:%d", *addr)
        return False, "", ""

    if len(hello) < 22 or hello[:6] != MAGIC_HELLO:
        log.warning("Bad HELLO from %s:%d", *addr)
        conn.sendall(MAGIC_REJECT)
        return False, "", ""

    device_id   = hello[6:22].hex()
    device_name = hello[22:].decode("utf-8", errors="replace").strip("\x00")
    if not device_name:
        device_name = f"Android-{device_id[:8]}"

    # ── One connection limit ───────────────────────────────────────────────────
    if not _active_client_lock.acquire(blocking=False):
        log.warning("Rejecting %s — another client already connected", addr[0])
        conn.sendall(MAGIC_REJECT + b"BUSY")
        return False, "", ""

    _active_client_ip = addr[0]

    # ── Step 2: Send CHALLENGE ────────────────────────────────────────────────
    nonce = secrets.token_bytes(16)
    conn.sendall(MAGIC_CHALLENGE + nonce)

    # ── Step 3: Receive RESPONSE ──────────────────────────────────────────────
    try:
        response = conn.recv(6 + 32)
    except socket.timeout:
        log.warning("No HMAC response from %s:%d", *addr)
        _active_client_lock.release()
        return False, "", ""

    if len(response) < 38 or response[:6] != MAGIC_RESPONSE:
        log.warning("Bad RESPONSE from %s:%d", *addr)
        conn.sendall(MAGIC_REJECT)
        _active_client_lock.release()
        return False, "", ""

    client_hmac   = response[6:38]
    expected_hmac = hmac.new(secret, nonce, hashlib.sha256).digest()

    if not hmac.compare_digest(client_hmac, expected_hmac):
        log.warning("HMAC mismatch from %s:%d — rejected", *addr)
        conn.sendall(MAGIC_REJECT)
        _active_client_lock.release()
        return False, "", ""

    conn.settimeout(None)
    log.info("Authenticated: %s (%s)", device_name, device_id)

    # Save device if new
    devices = load_paired_devices()
    if device_id not in devices:
        save_paired_device(device_id, device_name)

    return True, device_id, device_name


# ── Mutter ScreenCast ─────────────────────────────────────────────────────────

MUTTER_BUS    = "org.gnome.Mutter.ScreenCast"
MUTTER_PATH   = "/org/gnome/Mutter/ScreenCast"
MUTTER_SC_IF  = "org.gnome.Mutter.ScreenCast"
MUTTER_SES_IF = "org.gnome.Mutter.ScreenCast.Session"
MUTTER_STR_IF = "org.gnome.Mutter.ScreenCast.Stream"


def cleanup_orphaned_sessions(bus: dbus.SessionBus):
    """Clean up any orphaned Mutter ScreenCast sessions from previous crashes."""
    try:
        sc_obj = bus.get_object(MUTTER_BUS, MUTTER_PATH)
        sc     = dbus.Interface(sc_obj, MUTTER_SC_IF)

        # Introspect to find existing sessions
        intro = dbus.Interface(sc_obj, "org.freedesktop.DBus.Introspectable")
        xml   = intro.Introspect()

        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        for node in root.findall("node"):
            name = node.get("name", "")
            if name:
                path = f"{MUTTER_PATH}/{name}"
                try:
                    obj = bus.get_object(MUTTER_BUS, path)
                    ses = dbus.Interface(obj, MUTTER_SES_IF)
                    ses.Stop()
                    log.info("Cleaned up orphaned session: %s", path)
                except Exception:
                    pass
    except Exception as e:
        log.debug("Session cleanup: %s", e)


class MutterVirtualDisplay:

    def __init__(self, width: int, height: int):
        self.width    = width
        self.height   = height
        self._node_id = None
        self._error   = None

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus  = dbus.SessionBus()
        self._loop = GLib.MainLoop()

        cleanup_orphaned_sessions(self._bus)

        sc_obj    = self._bus.get_object(MUTTER_BUS, MUTTER_PATH)
        self._sc  = dbus.Interface(sc_obj, MUTTER_SC_IF)
        self._session_path = None

    def _on_stream_added(self, node_id):
        self._node_id = int(node_id)
        log.info("PipeWire stream ready — node_id: %d", self._node_id)
        self._loop.quit()

    def _on_session_closed(self):
        self._error = "Session closed unexpectedly"
        self._loop.quit()

    def setup(self) -> int:
        log.info("Creating Mutter ScreenCast session...")
        self._session_path = str(self._sc.CreateSession(
            dbus.Dictionary({}, signature="sv")
        ))
        log.info("Session: %s", self._session_path)

        session_obj = self._bus.get_object(MUTTER_BUS, self._session_path)
        session     = dbus.Interface(session_obj, MUTTER_SES_IF)
        session_obj.connect_to_signal("Closed", self._on_session_closed,
                                      dbus_interface=MUTTER_SES_IF)

        log.info("Creating virtual monitor %dx%d...", self.width, self.height)
        stream_path = str(session.RecordVirtual(
            dbus.Dictionary({
                "cursor-mode": dbus.UInt32(1),
            }, signature="sv")
        ))
        log.info("Stream: %s", stream_path)

        stream_obj = self._bus.get_object(MUTTER_BUS, stream_path)
        stream_obj.connect_to_signal("PipeWireStreamAdded", self._on_stream_added,
                                     dbus_interface=MUTTER_STR_IF)

        log.info("Starting session...")
        session.Start()

        GLib.timeout_add(10_000, lambda: (
            setattr(self, "_error", "Timeout"), self._loop.quit()
        ))
        self._loop.run()

        if self._error:
            raise RuntimeError(self._error)
        return self._node_id

    def close(self):
        if self._session_path:
            try:
                obj = self._bus.get_object(MUTTER_BUS, self._session_path)
                dbus.Interface(obj, MUTTER_SES_IF).Stop()
            except Exception:
                pass


# ── GStreamer capture ─────────────────────────────────────────────────────────

class PipeWireCapture:

    def __init__(self, node_id: int, width: int, height: int):
        self.width  = width
        self.height = height
        self._frame = None
        self._fw    = width
        self._fh    = height
        self._lock  = threading.Lock()

        Gst.init(None)
        self._loop = GLib.MainLoop()

        pipeline_str = (
            f"pipewiresrc path={node_id} always-copy=true "
            f"! videoconvert "
            f"! video/x-raw,format=BGR "
            f"! appsink name=sink emit-signals=true "
            f"max-buffers=2 drop=true sync=false"
        )
        log.info("GStreamer: %s", pipeline_str)
        self._pipeline = Gst.parse_launch(pipeline_str)

        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer pipeline failed")
        log.info("GStreamer pipeline playing")

        threading.Thread(target=self._loop.run, daemon=True).start()

    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR
        buf  = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w    = caps.get_value("width")
        h    = caps.get_value("height")
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            with self._lock:
                self._frame = bytes(mi.data)
                self._fw    = w
                self._fh    = h
            buf.unmap(mi)
        return Gst.FlowReturn.OK

    def get_frame(self):
        with self._lock:
            return (self._frame, self._fw, self._fh) if self._frame else None

    def close(self):
        self._pipeline.set_state(Gst.State.NULL)
        self._loop.quit()


# ── JPEG encode ───────────────────────────────────────────────────────────────

def to_jpeg(raw: bytes, w: int, h: int) -> bytes:
    img = Image.frombytes("RGB", (w, h), raw, "raw", "BGR")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ── TCP streaming ─────────────────────────────────────────────────────────────

def stream_to_client(conn: socket.socket, addr: tuple,
                     capture: PipeWireCapture, secret: bytes,
                     tray: TrayState) -> None:
    global _active_client_ip

    log.info("Client connected: %s:%d", *addr)

    ok, device_id, device_name = authenticate_client(conn, addr, secret)
    if not ok:
        conn.close()
        return

    try:
        # Wait for first frame
        for _ in range(100):
            r = capture.get_frame()
            if r:
                _, w, h = r
                break
            time.sleep(0.05)
        else:
            w, h = capture.width, capture.height

        # Send OK + resolution
        conn.sendall(MAGIC_OK + struct.pack(">II", w, h))
        log.info("Streaming %dx%d @ %d FPS → %s (%s)", w, h, FPS, device_name, addr[0])
        tray.update(connected=True, client_ip=f"{device_name} ({addr[0]})")

        frame_count  = 0
        fps_deadline = time.monotonic() + 1.0

        while True:
            start = time.monotonic()
            r = capture.get_frame()
            if r:
                raw, fw, fh = r
                jpeg = to_jpeg(raw, fw, fh)
                conn.sendall(struct.pack(">I", len(jpeg)) + jpeg)
                frame_count += 1

            if time.monotonic() >= fps_deadline:
                tray.update(fps=frame_count)
                frame_count  = 0
                fps_deadline = time.monotonic() + 1.0

            elapsed = time.monotonic() - start
            sleep   = FRAME_INTERVAL - elapsed
            if sleep > 0:
                time.sleep(sleep)

    except (BrokenPipeError, ConnectionResetError):
        log.info("Client disconnected: %s", device_name)
    except Exception as e:
        log.error("Stream error for %s — %s", device_name, e)
    finally:
        tray.update(connected=False, client_ip=None, fps=0)
        _active_client_ip = None
        _active_client_lock.release()
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server():
    secret = load_or_create_secret()

    if args.pair or not DEVICES_FILE.exists():
        show_qr_code(secret)

    log.info("TetherLink v0.8.0 — Secure Virtual Display")
    log.info("Paired devices: %d", len(load_paired_devices()))

    display = MutterVirtualDisplay(WIDTH, HEIGHT)
    try:
        node_id = display.setup()
    except Exception as e:
        log.error("Virtual display failed: %s", e)
        display.close()
        raise SystemExit(1)

    log.info("Virtual display ready — drag windows onto it!")
    capture = PipeWireCapture(node_id, WIDTH, HEIGHT)
    time.sleep(0.5)

    tray_state  = TrayState()
    tray_state.update(resolution=f"{WIDTH}×{HEIGHT}")
    shutdown_event = threading.Event()

    def on_quit():
        log.info("Quit from tray")
        shutdown_event.set()

    broadcaster = DiscoveryBroadcaster(PORT, WIDTH, HEIGHT)
    broadcaster.start()

    tray = start_tray(tray_state, on_quit=on_quit)
    log.info("Tray icon started")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PORT))
        srv.listen(5)
        srv.settimeout(1.0)
        log.info("Server ready on port %d", PORT)

        try:
            while not shutdown_event.is_set():
                try:
                    conn, addr = srv.accept()
                    threading.Thread(
                        target=stream_to_client,
                        args=(conn, addr, capture, secret, tray_state),
                        daemon=True,
                    ).start()
                except socket.timeout:
                    continue
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            tray.quit()
            broadcaster.stop()
            capture.close()
            display.close()
            import os, signal
            os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    run_server()