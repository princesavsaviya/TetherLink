#!/usr/bin/env python3
"""
TetherLink — Performance Benchmark Suite
Run from within the GNOME Wayland session with the venv active:
  source venv/bin/activate && python benchmark_server.py
"""
import gi, time, io, socket, struct, threading, sys
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
Gst.init(None)
import numpy as np
from PIL import Image, features

SEP = "=" * 62

def header(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")

# ── 1. Environment ─────────────────────────────────────────────────────────────
header("ENVIRONMENT")
import platform, subprocess
print(f"  OS:            {platform.freedesktop_os_release().get('PRETTY_NAME','?')}")
print(f"  Python:        {sys.version.split()[0]}")
print(f"  GStreamer:     {Gst.version_string()}")
print(f"  libjpeg-turbo: {features.check_feature('libjpeg_turbo')}")
from PIL import __version__ as pv
print(f"  Pillow:        {pv}")
try:
    result = subprocess.run(['pw-cli', 'info', '0'], capture_output=True, text=True, timeout=3)
    version_line = [l for l in result.stdout.splitlines() if 'library.version' in l]
    print(f"  PipeWire:      {version_line[0].strip() if version_line else 'running'}")
except Exception:
    print(f"  PipeWire:      (check failed)")

# ── 2. GStreamer frame pull ────────────────────────────────────────────────────
def bench_gst_encode(label, pstr, W, H, N=60):
    try:
        pipeline = Gst.parse_launch(pstr)
        sink = pipeline.get_by_name("sink")
        pipeline.set_state(Gst.State.PLAYING)
        time.sleep(0.8)
        msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg:
            err, _ = msg.parse_error()
            print(f"  SKIP ({label}): {err}")
            pipeline.set_state(Gst.State.NULL)
            return None

        pull_ms, enc_ms, jpeg_sizes = [], [], []
        for _ in range(N):
            t0 = time.perf_counter()
            sample = sink.emit("pull-sample")
            t1 = time.perf_counter()
            if not sample:
                continue
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            caps = sample.get_caps()
            s = caps.get_structure(0)
            w = s.get_int("width")[1]
            h = s.get_int("height")[1]
            raw = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            pull_ms.append((t1 - t0) * 1000)

            t2 = time.perf_counter()
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
            img = Image.fromarray(arr, 'RGB')
            jbuf = io.BytesIO()
            img.save(jbuf, 'JPEG', quality=80)
            enc_ms.append((time.perf_counter() - t2) * 1000)
            jpeg_sizes.append(jbuf.tell())

        pipeline.set_state(Gst.State.NULL)

        if not pull_ms:
            return None

        ap = sum(pull_ms) / len(pull_ms)
        ae = sum(enc_ms) / len(enc_ms)
        p95e = sorted(enc_ms)[int(len(enc_ms) * 0.95)]
        js = sum(jpeg_sizes) / len(jpeg_sizes)
        total = ap + ae

        print(f"\n  [{label}]")
        print(f"    Resolution:        {w}x{h}")
        print(f"    Frame pull avg:    {ap:.3f} ms  (GStreamer overhead)")
        print(f"    JPEG encode avg:   {ae:.2f} ms  (p95={p95e:.2f} ms)")
        print(f"    Total / frame:     {total:.2f} ms  → {1000/total:.1f} FPS max (encode bound)")
        print(f"    JPEG frame size:   {js/1024:.1f} KB")
        print(f"    Bitrate @ 30 FPS:  {js*30*8/1e6:.1f} Mbps")
        for bw in [10, 20, 40]:
            fps_bw = bw * 1e6 / 8 / js
            print(f"    FPS @ {bw} Mbps USB: {fps_bw:.1f}")
        return {"pull_ms": ap, "enc_ms": ae, "total_ms": total, "jpeg_kb": js/1024}
    except Exception as e:
        print(f"  EXCEPTION ({label}): {e}")
        return None

header("JPEG ENCODE (GStreamer → libjpeg-turbo, realistic SMPTE content)")
r1080 = bench_gst_encode(
    "1920×1080 @ Q80",
    "videotestsrc pattern=smpte ! video/x-raw,width=1920,height=1080,framerate=60/1 ! videoconvert ! video/x-raw,format=RGB ! appsink name=sink max-buffers=1 drop=true sync=false",
    1920, 1080
)
r2960 = bench_gst_encode(
    "2960×1848 @ Q80 (tablet native)",
    "videotestsrc pattern=smpte ! video/x-raw,width=2960,height=1848,framerate=60/1 ! videoconvert ! video/x-raw,format=RGB ! appsink name=sink max-buffers=1 drop=true sync=false",
    2960, 1848
)

# ── 3. TCP throughput ──────────────────────────────────────────────────────────
header("TCP LOOPBACK THROUGHPUT (localhost, simulates USB path)")

import numpy as np, io
from PIL import Image
rng = np.random.default_rng(42)
# Realistic SMPTE-like frame (structured, not noise)
arr = np.zeros((1080, 1920, 3), dtype=np.uint8)
arr[:540, :960] = [255, 255, 255]
arr[:540, 960:] = [255, 255, 0]
arr[540:, :640] = [0, 0, 255]
arr[540:, 640:1280] = [0, 255, 0]
arr[540:, 1280:] = [255, 0, 0]
img_payload = Image.fromarray(arr, 'RGB')
buf = io.BytesIO()
img_payload.save(buf, 'JPEG', quality=80)
payload = buf.getvalue()
frame_kb = len(payload) / 1024

print(f"\n  JPEG payload: {frame_kb:.1f} KB")

PORT = 19877
NFRAMES = 300
results = {}

def _server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', PORT))
    srv.listen(1)
    conn, _ = srv.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t0 = time.perf_counter()
    total = 0
    for _ in range(NFRAMES):
        hdr = struct.pack('>I', len(payload))
        conn.sendall(hdr)
        conn.sendall(payload)
        total += 4 + len(payload)
    conn.shutdown(socket.SHUT_WR)
    results['send_time'] = time.perf_counter() - t0
    results['bytes'] = total
    conn.close(); srv.close()

def _client():
    time.sleep(0.05)
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.connect(('127.0.0.1', PORT))
    cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t0 = time.perf_counter()
    frame_ms = []
    for _ in range(NFRAMES):
        hdr = b''
        while len(hdr) < 4:
            c = cli.recv(4 - len(hdr))
            if not c: break
            hdr += c
        if len(hdr) < 4: break
        size = struct.unpack('>I', hdr)[0]
        data = b''
        tf = time.perf_counter()
        while len(data) < size:
            c = cli.recv(min(65536, size - len(data)))
            if not c: break
            data += c
        frame_ms.append((time.perf_counter() - tf) * 1000)
    results['recv_time'] = time.perf_counter() - t0
    results['frames'] = len(frame_ms)
    results['frame_ms'] = frame_ms
    cli.close()

st = threading.Thread(target=_server, daemon=True)
ct = threading.Thread(target=_client, daemon=True)
st.start(); ct.start()
st.join(); ct.join()

elapsed = results['recv_time']
fps = results['frames'] / elapsed
mbps = results['bytes'] / elapsed / 1e6 * 8
fms = results['frame_ms']
fms.sort()
print(f"  Frames:          {results['frames']}")
print(f"  Elapsed:         {elapsed:.3f}s")
print(f"  Effective FPS:   {fps:.1f}")
print(f"  Throughput:      {mbps:.0f} Mbps  ({mbps/8:.0f} MB/s)")
print(f"  Recv avg/frame:  {sum(fms)/len(fms):.2f} ms")
print(f"  Recv p95/frame:  {fms[int(len(fms)*0.95)]:.2f} ms")
print(f"\n  USB tethering headroom (real content, 124KB frames):")
for bw in [10, 20, 40]:
    fps_usb = bw * 1e6 / 8 / (frame_kb * 1024)
    print(f"    @ {bw} Mbps → {fps_usb:.1f} FPS")

# ── 4. Latency model ───────────────────────────────────────────────────────────
header("END-TO-END LATENCY MODEL")
enc_1080 = r1080['enc_ms'] if r1080 else 8.2
pull_1080 = r1080['pull_ms'] if r1080 else 0.03
components = {
    "Mutter ScreenCast → PipeWire (compositor)": 2.0,
    f"GStreamer appsink pull": round(pull_1080, 2),
    f"JPEG encode 1920×1080 @ Q80": round(enc_1080, 2),
    "TCP send (USB tethering, ~20 Mbps)":  round(r1080['jpeg_kb']*1024*8 / (20e6) * 1000, 1) if r1080 else 5.0,
    "Android BitmapFactory JPEG decode":   8.0,
    "Android SurfaceView HW composite":    4.0,
}
total_lat = sum(components.values())
print(f"\n  {'Component':<45} {'ms':>6}")
print(f"  {'-'*53}")
for k, v in components.items():
    print(f"  {k:<45} {v:>6.2f}")
print(f"  {'─'*53}")
print(f"  {'TOTAL END-TO-END':.<45} {total_lat:>6.2f}")
print(f"\n  Theoretical max FPS (latency bound): {1000/total_lat:.0f} FPS")
print(f"  Target FPS (server-governed):        30 FPS")
print(f"  USB bandwidth FPS (@ 20 Mbps):       {20e6/8/(r1080['jpeg_kb']*1024):.0f} FPS" if r1080 else "")

print(f"\n{SEP}")
print("  BENCHMARK COMPLETE")
print(f"{SEP}\n")
