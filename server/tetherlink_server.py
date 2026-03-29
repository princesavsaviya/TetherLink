"""
TetherLink Server - Milestone 3: Wayland + PipeWire
Uses the XDG ScreenCast portal to capture a monitor via PipeWire,
then streams JPEG frames to the Android tablet over TCP.

Usage:
    python server/tetherlink_server.py
    python server/tetherlink_server.py --fps 60 --quality 70

Protocol:
    Handshake : [4B width][4B height]   (sent once on connect)
    Stream    : [4B size][JPEG data]    (repeated per frame)
"""

import argparse
import logging
import os
import random
import socket
import string
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

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="TetherLink PipeWire Server")
parser.add_argument("--fps",     type=int, default=30)
parser.add_argument("--quality", type=int, default=80)
parser.add_argument("--port",    type=int, default=8080)
args = parser.parse_args()

FPS            = args.fps
JPEG_QUALITY   = args.quality
PORT           = args.port
FRAME_INTERVAL = 1.0 / FPS
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TetherLink")


# ── XDG ScreenCast portal ─────────────────────────────────────────────────────

class ScreenCastPortal:
    """
    Async GLib-based portal flow matching the working test script exactly.
    Uses bus.add_signal_receiver() and GLib.idle_add() for sequencing.
    """

    PORTAL_BUS  = "org.freedesktop.portal.Desktop"
    PORTAL_PATH = "/org/freedesktop/portal/desktop"
    PORTAL_IF   = "org.freedesktop.portal.ScreenCast"
    REQUEST_IF  = "org.freedesktop.portal.Request"
    REQUEST_BASE = "/org/freedesktop/portal/desktop/request"

    def __init__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus   = dbus.SessionBus()
        self._loop  = GLib.MainLoop()
        self._state = {
            "session_handle": None,
            "node_id":        None,
            "width":          1920,
            "height":         1080,
            "error":          None,
        }

        # Stable token prefix for this run
        suffix = "".join(random.choices(string.ascii_lowercase, k=6))
        self._prefix      = f"tl{suffix}"
        self._sender_name = (
            self._bus.get_unique_name().replace(".", "_").replace(":", "")
        )

        desktop = self._bus.get_object(self.PORTAL_BUS, self.PORTAL_PATH)
        self._sc = dbus.Interface(desktop, self.PORTAL_IF)

    def _req_path(self, token: str) -> str:
        return f"{self.REQUEST_BASE}/{self._sender_name}/{token}"

    def _subscribe(self, token: str, callback):
        self._bus.add_signal_receiver(
            callback,
            signal_name="Response",
            dbus_interface=self.REQUEST_IF,
            path=self._req_path(token),
        )

    # ── Step 1 ────────────────────────────────────────────────────────────────
    def _create_session(self):
        log.info("Portal: CreateSession...")
        token = f"{self._prefix}_create"

        def on_response(code, results):
            if code != 0:
                self._state["error"] = f"CreateSession denied (code={code})"
                self._loop.quit()
                return
            self._state["session_handle"] = str(results["session_handle"])
            log.info("Session: %s", self._state["session_handle"])
            GLib.idle_add(self._select_sources)

        self._subscribe(token, on_response)
        self._sc.CreateSession(dbus.Dictionary({
            "handle_token":        dbus.String(token),
            "session_handle_token": dbus.String(f"{self._prefix}_session"),
        }, signature="sv"))

    # ── Step 2 ────────────────────────────────────────────────────────────────
    def _select_sources(self):
        log.info("Portal: SelectSources...")
        token = f"{self._prefix}_select"

        def on_response(code, results):
            if code != 0:
                self._state["error"] = f"SelectSources denied (code={code})"
                self._loop.quit()
                return
            GLib.idle_add(self._start)

        self._subscribe(token, on_response)
        self._sc.SelectSources(
            dbus.ObjectPath(self._state["session_handle"]),
            dbus.Dictionary({
                "handle_token": dbus.String(token),
                "types":        dbus.UInt32(1),      # monitor
                "multiple":     dbus.Boolean(False),
                "cursor_mode":  dbus.UInt32(2),      # embedded
            }, signature="sv")
        )

    # ── Step 3 ────────────────────────────────────────────────────────────────
    def _start(self):
        log.info("Portal: Start — select your monitor and click Share...")
        token = f"{self._prefix}_start"

        def on_response(code, results):
            if code != 0:
                self._state["error"] = f"Start denied (code={code})"
                self._loop.quit()
                return
            streams = results.get("streams", [])
            if not streams:
                self._state["error"] = "No streams returned"
                self._loop.quit()
                return
            node_id, props = streams[0]
            self._state["node_id"] = int(node_id)
            size = props.get("size", None)
            if size:
                self._state["width"]  = int(size[0])
                self._state["height"] = int(size[1])
            log.info("PipeWire node: %d  (%dx%d)",
                     self._state["node_id"],
                     self._state["width"],
                     self._state["height"])
            self._loop.quit()

        self._subscribe(token, on_response)
        self._sc.Start(
            dbus.ObjectPath(self._state["session_handle"]),
            dbus.String(""),
            dbus.Dictionary({
                "handle_token": dbus.String(token),
            }, signature="sv")
        )

    def acquire(self) -> tuple[int, int, int]:
        GLib.timeout_add(120_000, lambda: (self._loop.quit(), False)[1])
        GLib.idle_add(self._create_session)
        self._loop.run()

        if self._state["error"]:
            raise RuntimeError(self._state["error"])
        if self._state["node_id"] is None:
            raise RuntimeError("Portal flow completed but no node_id obtained")

        return (
            self._state["node_id"],
            self._state["width"],
            self._state["height"],
        )

    def close(self):
        h = self._state.get("session_handle")
        if h:
            try:
                obj = self._bus.get_object(self.PORTAL_BUS, h)
                dbus.Interface(obj, "org.freedesktop.portal.Session").Close()
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
            f"! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
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

        while True:
            start = time.monotonic()
            r = capture.get_frame()
            if r:
                raw, fw, fh = r
                jpeg = to_jpeg(raw, fw, fh)
                conn.sendall(struct.pack(">I", len(jpeg)) + jpeg)
            elapsed = time.monotonic() - start
            sleep = FRAME_INTERVAL - elapsed
            if sleep > 0:
                time.sleep(sleep)

    except (BrokenPipeError, ConnectionResetError):
        log.info("Client disconnected: %s:%d", *addr)
    except Exception as e:
        log.error("Stream error %s:%d — %s", *addr, e)
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server():
    log.info("TetherLink M3 — Wayland + PipeWire")
    log.info("Select your monitor in the GNOME dialog and click Share")

    portal = ScreenCastPortal()
    try:
        node_id, w, h = portal.acquire()
    except Exception as e:
        log.error("Portal error: %s", e)
        portal.close()
        raise SystemExit(1)

    capture = PipeWireCapture(node_id, w, h)
    time.sleep(0.5)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PORT))
        srv.listen(5)
        log.info("Server ready on port %d — waiting for tablet...", PORT)
        try:
            while True:
                conn, addr = srv.accept()
                threading.Thread(
                    target=stream_to_client,
                    args=(conn, addr, capture),
                    daemon=True,
                ).start()
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            capture.close()
            portal.close()


if __name__ == "__main__":
    run_server()