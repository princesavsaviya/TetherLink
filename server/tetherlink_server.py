"""
TetherLink Server - Milestone 4: True Virtual Display via Mutter ScreenCast
Uses org.gnome.Mutter.ScreenCast to create a real virtual monitor at the
target resolution, then captures it via PipeWire and streams to Android.

No user permission dialog. GNOME treats the virtual display as a real
second monitor — you can drag windows onto it, set wallpaper, etc.

Flow:
  Mutter.ScreenCast.CreateSession()
    → session.RecordVirtual(width, height)   ← creates real virtual monitor
    → stream.Start()
    → PipeWireStreamAdded(node_id)
    → GStreamer pipewiresrc path=node_id
    → BGR frames → JPEG → TCP → Android

Usage:
    python server/tetherlink_server.py
    python server/tetherlink_server.py --width 2960 --height 1848
    python server/tetherlink_server.py --fps 60 --quality 70

Protocol:
    Handshake : [4B width][4B height]   (sent once on connect)
    Stream    : [4B size][JPEG data]    (repeated per frame)
"""

import argparse
import logging
import socket
import struct
import threading
import time
from io import BytesIO

import dbus
import dbus.mainloop.glib
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
from PIL import Image
from tray import TrayState, start_tray
from discovery import DiscoveryBroadcaster

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="TetherLink Virtual Display Server")
parser.add_argument("--width",   type=int, default=2960,
                    help="Virtual display width  (default: 2960)")
parser.add_argument("--height",  type=int, default=1848,
                    help="Virtual display height (default: 1848)")
parser.add_argument("--fps",     type=int, default=30)
parser.add_argument("--quality", type=int, default=100)
parser.add_argument("--port",    type=int, default=8080)
args = parser.parse_args()

WIDTH          = args.width
HEIGHT         = args.height
FPS            = args.fps
JPEG_QUALITY   = args.quality
PORT           = args.port
FRAME_INTERVAL = 1.0 / FPS

# Shared tray state — updated by streaming threads
tray_state = TrayState()
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TetherLink")

MUTTER_BUS      = "org.gnome.Mutter.ScreenCast"
MUTTER_PATH     = "/org/gnome/Mutter/ScreenCast"
MUTTER_SC_IF    = "org.gnome.Mutter.ScreenCast"
MUTTER_SES_IF   = "org.gnome.Mutter.ScreenCast.Session"
MUTTER_STR_IF   = "org.gnome.Mutter.ScreenCast.Stream"


# ── Mutter ScreenCast virtual display ────────────────────────────────────────

class MutterVirtualDisplay:
    """
    Creates a real virtual monitor via org.gnome.Mutter.ScreenCast.
    GNOME treats it as a second physical display.

    Sequence:
      1. CreateSession
      2. session.RecordVirtual(width, height)
      3. stream.Start()
      4. Wait for PipeWireStreamAdded signal → get node_id
    """

    def __init__(self, width: int, height: int):
        self.width    = width
        self.height   = height
        self._node_id = None
        self._error   = None

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus  = dbus.SessionBus()
        self._loop = GLib.MainLoop()

        sc_obj     = self._bus.get_object(MUTTER_BUS, MUTTER_PATH)
        self._sc   = dbus.Interface(sc_obj, MUTTER_SC_IF)

        self._session_path = None
        self._stream_path  = None

    def _on_pipewire_stream_added(self, node_id):
        self._node_id = int(node_id)
        log.info("PipeWire stream ready — node_id: %d", self._node_id)
        self._loop.quit()

    def _on_session_closed(self):
        log.warning("Mutter session closed unexpectedly")
        self._error = "Session closed"
        self._loop.quit()

    def setup(self) -> int:
        """
        Run the full setup flow.
        Returns the PipeWire node_id for the virtual display.
        """
        # ── 1. CreateSession ──────────────────────────────────────────────────
        log.info("Creating Mutter ScreenCast session...")
        self._session_path = str(self._sc.CreateSession(
            dbus.Dictionary({}, signature="sv")
        ))
        log.info("Session: %s", self._session_path)

        session_obj = self._bus.get_object(MUTTER_BUS, self._session_path)
        session     = dbus.Interface(session_obj, MUTTER_SES_IF)

        # Subscribe to Closed signal
        session_obj.connect_to_signal(
            "Closed", self._on_session_closed,
            dbus_interface=MUTTER_SES_IF,
        )

        # ── 2. RecordVirtual ──────────────────────────────────────────────────
        log.info("Creating virtual monitor %dx%d...", self.width, self.height)
        self._stream_path = str(session.RecordVirtual(
            dbus.Dictionary({
                "size": dbus.Struct(
                    (dbus.Int32(self.width), dbus.Int32(self.height)),
                    signature="ii"
                ),
                "cursor-mode": dbus.UInt32(1),  # 1=embedded cursor in stream
            }, signature="sv")
        ))
        log.info("Stream: %s", self._stream_path)

        stream_obj = self._bus.get_object(MUTTER_BUS, self._stream_path)

        # Subscribe to PipeWireStreamAdded BEFORE calling Start
        stream_obj.connect_to_signal(
            "PipeWireStreamAdded", self._on_pipewire_stream_added,
            dbus_interface=MUTTER_STR_IF,
        )

        # ── 3. Start session — this starts all streams automatically ─────────
        log.info("Starting session (streams start automatically)...")
        stream = dbus.Interface(stream_obj, MUTTER_STR_IF)
        session.Start()

        # Wait for PipeWireStreamAdded (timeout 10s)
        GLib.timeout_add(10_000, lambda: (
            setattr(self, "_error", "Timeout waiting for PipeWire stream"),
            self._loop.quit()
        ))
        self._loop.run()

        if self._error:
            raise RuntimeError(self._error)
        if self._node_id is None:
            raise RuntimeError("No PipeWire node_id received")

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
            raise RuntimeError("GStreamer pipeline failed to start")
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
                     capture: PipeWireCapture) -> None:
    log.info("Client connected: %s:%d", *addr)
    tray_state.update(connected=True, client_ip=addr[0])
    try:
        # Wait up to 5s for first real frame
        for _ in range(100):
            r = capture.get_frame()
            if r:
                _, w, h = r
                break
            time.sleep(0.05)
        else:
            w, h = capture.width, capture.height

        conn.sendall(struct.pack(">II", w, h))
        log.info("Streaming %dx%d @ %d FPS → %s:%d", w, h, FPS, *addr)

        # FPS tracking
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

            # Update tray FPS every second
            if time.monotonic() >= fps_deadline:
                tray_state.update(fps=frame_count)
                frame_count  = 0
                fps_deadline = time.monotonic() + 1.0

            elapsed = time.monotonic() - start
            sleep = FRAME_INTERVAL - elapsed
            if sleep > 0:
                time.sleep(sleep)

    except (BrokenPipeError, ConnectionResetError):
        log.info("Client disconnected: %s:%d", *addr)
    except Exception as e:
        log.error("Stream error %s:%d — %s", *addr, e)
    finally:
        tray_state.update(connected=False, client_ip=None, fps=0)
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server():
    log.info("TetherLink M4 — Virtual Display via Mutter ScreenCast")
    log.info("Creating virtual monitor %dx%d...", WIDTH, HEIGHT)

    display = MutterVirtualDisplay(WIDTH, HEIGHT)
    try:
        node_id = display.setup()
    except Exception as e:
        log.error("Virtual display setup failed: %s", e)
        display.close()
        raise SystemExit(1)

    log.info("Virtual display ready! Check GNOME display settings — "
             "you should see a second monitor.")
    log.info("Drag windows onto it to see them on the tablet.")

    capture = PipeWireCapture(node_id, WIDTH, HEIGHT)
    time.sleep(0.5)

    # Start UDP discovery broadcaster
    broadcaster = DiscoveryBroadcaster(PORT, WIDTH, HEIGHT)
    broadcaster.start()

    # Start tray icon
    tray_state.update(resolution=f"{WIDTH}×{HEIGHT}")
    shutdown_event = threading.Event()

    def on_quit():
        log.info("Quit requested from tray")
        shutdown_event.set()

    tray = start_tray(tray_state, on_quit=on_quit)
    log.info("Tray icon started — right-click it to quit")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PORT))
        srv.listen(5)
        srv.settimeout(1.0)  # allow checking shutdown_event
        log.info("Server ready on port %d — waiting for tablet...", PORT)
        try:
            while not shutdown_event.is_set():
                try:
                    conn, addr = srv.accept()
                    threading.Thread(
                        target=stream_to_client,
                        args=(conn, addr, capture),
                        daemon=True,
                    ).start()
                except socket.timeout:
                    continue  # check shutdown_event again
        except KeyboardInterrupt:
            log.info("Shutting down via Ctrl+C...")
        finally:
            log.info("Cleaning up...")
            tray.quit()
            broadcaster.stop()
            capture.close()
            display.close()
            import os, signal
            os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    run_server()