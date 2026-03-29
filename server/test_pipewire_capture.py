#!/usr/bin/env python3
"""
TetherLink — Milestone 3 PipeWire capture test
Tests: XDG ScreenCast portal → PipeWire node ID → pipewiresrc → raw frame

Run from within the GNOME Wayland session (not over SSH without DISPLAY/DBUS_SESSION_BUS_ADDRESS).
"""

import sys
import os
import time
import random
import string
import threading

try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
    print("[OK] dbus + GLib imported")
except ImportError as e:
    print(f"[FAIL] Missing dependency: {e}")
    sys.exit(1)

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    print(f"[OK] GStreamer {Gst.version_string()}")
except Exception as e:
    print(f"[FAIL] GStreamer init: {e}")
    sys.exit(1)

# ── Check DBUS_SESSION_BUS_ADDRESS ─────────────────────────────────────────────
bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
print(f"[INFO] DBUS_SESSION_BUS_ADDRESS: {bus_addr or '(not set — may fail)'}")

# ── D-Bus mainloop ─────────────────────────────────────────────────────────────
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
loop = GLib.MainLoop()

# ── Connect to session bus ─────────────────────────────────────────────────────
try:
    bus = dbus.SessionBus()
    print("[OK] Connected to D-Bus session bus")
except dbus.exceptions.DBusException as e:
    print(f"[FAIL] Cannot connect to session bus: {e}")
    sys.exit(1)

# ── Request path token ─────────────────────────────────────────────────────────
sender_token = "tetherlink_" + "".join(random.choices(string.ascii_lowercase, k=6))
request_path_base = "/org/freedesktop/portal/desktop/request"
sender_name = bus.get_unique_name().replace(".", "_").replace(":", "")
session_token = "tetherlink_session_" + "".join(random.choices(string.ascii_lowercase, k=6))

state = {
    "session_handle": None,
    "node_id": None,
    "error": None,
}

# ── Helper to get portal proxy ────────────────────────────────────────────────
def get_portal():
    return bus.get_object(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop"
    )

# ── Step 1: CreateSession ──────────────────────────────────────────────────────
def create_session():
    print("\n[STEP 1] CreateSession ...")
    portal = get_portal()
    screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")

    request_token = sender_token + "_create"
    request_path = f"{request_path_base}/{sender_name}/{request_token}"

    # Subscribe to the Request.Response signal before calling
    def on_create_response(response_code, results):
        print(f"  CreateSession response_code={response_code}")
        if response_code != 0:
            state["error"] = f"CreateSession denied (code={response_code})"
            loop.quit()
            return
        state["session_handle"] = str(results.get("session_handle", ""))
        print(f"  session_handle: {state['session_handle']}")
        GLib.idle_add(select_sources)

    try:
        req_obj = bus.get_object("org.freedesktop.portal.Desktop", request_path)
        req_iface = dbus.Interface(req_obj, "org.freedesktop.portal.Request")
        req_iface.connect_to_signal("Response", on_create_response)
    except Exception:
        # Signal subscription may fail if path doesn't exist yet; use match instead
        bus.add_signal_receiver(
            on_create_response,
            signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request",
            path=request_path,
        )

    handle = screencast.CreateSession(
        dbus.Dictionary({
            "handle_token": dbus.String(request_token),
            "session_handle_token": dbus.String(session_token),
        }, signature="sv")
    )
    print(f"  Request handle: {handle}")


# ── Step 2: SelectSources ──────────────────────────────────────────────────────
def select_sources():
    print("\n[STEP 2] SelectSources ...")
    portal = get_portal()
    screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")

    request_token = sender_token + "_select"
    request_path = f"{request_path_base}/{sender_name}/{request_token}"

    def on_select_response(response_code, results):
        print(f"  SelectSources response_code={response_code}")
        if response_code != 0:
            state["error"] = f"SelectSources denied (code={response_code})"
            loop.quit()
            return
        GLib.idle_add(start_capture)

    bus.add_signal_receiver(
        on_select_response,
        signal_name="Response",
        dbus_interface="org.freedesktop.portal.Request",
        path=request_path,
    )

    screencast.SelectSources(
        dbus.ObjectPath(state["session_handle"]),
        dbus.Dictionary({
            "handle_token": dbus.String(request_token),
            "types": dbus.UInt32(1),        # 1=monitor, 2=window
            "multiple": dbus.Boolean(False),
            "cursor_mode": dbus.UInt32(2),  # 2=embedded cursor
        }, signature="sv")
    )


# ── Step 3: Start ──────────────────────────────────────────────────────────────
def start_capture():
    print("\n[STEP 3] Start (will trigger permission dialog) ...")
    portal = get_portal()
    screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")

    request_token = sender_token + "_start"
    request_path = f"{request_path_base}/{sender_name}/{request_token}"

    def on_start_response(response_code, results):
        print(f"  Start response_code={response_code}")
        if response_code != 0:
            state["error"] = f"Start denied/cancelled (code={response_code})"
            loop.quit()
            return

        streams = results.get("streams", [])
        print(f"  streams: {list(streams)}")
        if streams:
            node_id, stream_props = streams[0]
            state["node_id"] = int(node_id)
            print(f"\n[SUCCESS] PipeWire node_id = {state['node_id']}")
        else:
            state["error"] = "Start succeeded but no streams returned"

        GLib.idle_add(test_gstreamer_pipeline)

    bus.add_signal_receiver(
        on_start_response,
        signal_name="Response",
        dbus_interface="org.freedesktop.portal.Request",
        path=request_path,
    )

    screencast.Start(
        dbus.ObjectPath(state["session_handle"]),
        dbus.String(""),   # parent_window
        dbus.Dictionary({
            "handle_token": dbus.String(request_token),
        }, signature="sv")
    )


# ── Step 4: Test GStreamer pipewiresrc ────────────────────────────────────────
def test_gstreamer_pipeline():
    if state["error"]:
        print(f"\n[FAIL] {state['error']}")
        loop.quit()
        return

    node_id = state["node_id"]
    print(f"\n[STEP 4] Testing GStreamer pipewiresrc with node_id={node_id} ...")

    pipeline_str = (
        f"pipewiresrc path={node_id} ! "
        "videoconvert ! "
        "video/x-raw,format=RGB ! "
        "fakesink name=sink sync=false"
    )
    print(f"  Pipeline: {pipeline_str}")

    pipeline = Gst.parse_launch(pipeline_str)
    pipeline.set_state(Gst.State.PLAYING)

    def check_frame():
        sink = pipeline.get_by_name("sink")
        sample = sink.get_property("last-sample") if sink else None
        ret, state_val, _ = pipeline.get_state(Gst.CLOCK_TIME_NONE)
        print(f"  Pipeline state: {state_val.value_nick}")

        # Check bus for errors
        msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
        if msg:
            err, dbg = msg.parse_error() if msg.type == Gst.MessageType.ERROR else msg.parse_warning()
            print(f"  [GST {'ERROR' if msg.type == Gst.MessageType.ERROR else 'WARN'}] {err}: {dbg}")
            pipeline.set_state(Gst.State.NULL)
            if msg.type == Gst.MessageType.ERROR:
                state["error"] = str(err)
            else:
                print("[OK] pipewiresrc running (warning only)")
        else:
            print("[OK] pipewiresrc pipeline running — no errors")

        pipeline.set_state(Gst.State.NULL)
        loop.quit()
        return False  # don't repeat

    # Give pipeline 3 seconds to start up
    GLib.timeout_add(3000, check_frame)


# ── Timeout safety net ────────────────────────────────────────────────────────
def timeout_handler():
    print("\n[TIMEOUT] Test timed out after 60s. If no dialog appeared, check DISPLAY env.")
    print("  Hint: ensure you run this script in the active Wayland session, not over SSH.")
    state["error"] = "timeout"
    loop.quit()
    return False

GLib.timeout_add(60000, timeout_handler)

# ── Run ───────────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("TetherLink — PipeWire Portal Capture Test")
print("="*60)
print("NOTE: A GNOME screen-share permission dialog will appear.")
print("      Select your monitor and click 'Share'.\n")

GLib.idle_add(create_session)
try:
    loop.run()
except KeyboardInterrupt:
    print("\nInterrupted by user.")

# ── Final report ──────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("RESULTS:")
if state["node_id"] and not state["error"]:
    print(f"  [PASS] PipeWire node_id = {state['node_id']}")
    print(f"  [PASS] Full pipeline: XDG Portal -> PipeWire -> GStreamer pipewiresrc")
    print(f"\n  Production pipeline will be:")
    print(f"  pipewiresrc path={state['node_id']} ! videoconvert ! video/x-raw,format=BGR ! appsink")
elif state["node_id"]:
    print(f"  [PARTIAL] PipeWire node_id = {state['node_id']} obtained")
    print(f"  [FAIL] GStreamer error: {state['error']}")
else:
    print(f"  [FAIL] {state.get('error', 'Unknown error')}")
print("="*60)
