# TetherLink

TetherLink turns an Android tablet into a wired second monitor for your PC using USB Tethering. A lightweight Python server captures your screen and streams compressed JPEG frames at up to 30 FPS over a local TCP connection. The Android client receives the frames and displays them fullscreen — no Wi-Fi, no cloud, no latency spike.

The system deliberately keeps the implementation simple: a plain TCP socket carries a stream of size-prefixed JPEG frames. This makes the Milestone 1 build easy to understand, easy to debug, and easy to extend. USB Tethering provides a private 192.168.42.x subnet with roughly 1–5 ms round-trip latency, which is far better than Wi-Fi for a streaming use case.

---

## Architecture

```
┌─────────────────────────────────┐         USB Tethering (192.168.42.x)
│           PC (Server)           │ ──────────────────────────────────────►
│                                 │                                         │
│  Screen  →  PIL/mss capture     │    TCP :8080   ┌────────────────────┐  │
│          →  JPEG encode (Q=80)  │ ─────────────► │  Android Tablet    │  │
│          →  [4B size][JPEG data]│                │  (Client)          │  │
│          →  TCP send @ 30 FPS   │                │                    │  │
└─────────────────────────────────┘                │  recv → decode     │  │
                                                   │  → ImageView       │  │
                                                   └────────────────────┘  │
◄──────────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1 — Start the Python server

```bash
# Linux / macOS
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tetherlink_server.py

# Windows
python tetherlink_server_windows.py
```

### 2 — Enable USB Tethering on Android

Settings → Network → Hotspot & Tethering → USB Tethering → **ON**

### 3 — Find your PC's tethering IP

```bash
# Linux / macOS
ip addr show | grep 192.168.42

# Windows
ipconfig | findstr 192.168.42
```

The IP is typically `192.168.42.129`.

### 4 — Update and launch the Android app

Open `android/app/src/main/java/com/tetherlink/MainActivity.kt`, set `SERVER_IP` to your PC's IP, build & run the app.

---

## Features

- Zero-configuration USB Tethering transport (no Wi-Fi required)
- 30 FPS MJPEG stream at configurable quality
- Multi-client support (multiple tablets simultaneously)
- Fullscreen landscape display on Android
- Graceful disconnect / reconnect handling
- Clean thread-per-client server architecture

---

## Tech Stack

| Layer    | Technology                              |
|----------|-----------------------------------------|
| Server   | Python 3.8+, Pillow 10.1.0, mss 9.0.1  |
| Transport| TCP socket, USB Tethering               |
| Client   | Kotlin 1.9.20, Android SDK 34           |
| Async    | Kotlin Coroutines (Dispatchers.IO)      |

---

## Roadmap

| Milestone | Description                                      |
|-----------|--------------------------------------------------|
| **M1**    | ✅ MJPEG over USB Tethering (this release)        |
| M2        | Configurable IP / port from app Settings UI      |
| M3        | Hardware-accelerated H.264 encoding (MediaCodec) |
| M4        | Touch input forwarding (tablet → PC mouse)       |
| M5        | Multi-monitor support (choose which screen)      |
| M6        | Auto-discovery of server on LAN                  |
