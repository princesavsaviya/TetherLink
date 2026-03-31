"""
TetherLink — UDP Discovery Broadcaster
Announces the server on the local network every 2 seconds.
Android app listens for these broadcasts to find the server automatically.

Packet format (JSON):
  {
    "app":        "TetherLink",
    "name":       "Prince's OMEN",   ← hostname
    "ip":         "10.90.14.99",
    "port":       8080,
    "resolution": "2960x1848",
    "version":    "1.0"
  }
"""

import json
import logging
import socket
import threading
import time
import platform

log = logging.getLogger("TetherLink.Discovery")

BROADCAST_PORT    = 8765
BROADCAST_INTERVAL = 2.0  # seconds


def get_local_ips() -> list[str]:
    """Get all non-loopback IPv4 addresses on this machine."""
    ips = []
    try:
        import netifaces
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for a in addrs[netifaces.AF_INET]:
                    ip = a.get("addr", "")
                    if ip and not ip.startswith("127."):
                        ips.append(ip)
    except ImportError:
        # Fallback without netifaces
        try:
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            if not ip.startswith("127."):
                ips.append(ip)
        except Exception:
            pass
    return ips


class DiscoveryBroadcaster:
    """
    Broadcasts server presence via UDP on all network interfaces.
    Runs in a background daemon thread.
    """

    def __init__(self, port: int, width: int, height: int):
        self._port       = port
        self._resolution = f"{width}x{height}"
        self._running    = False
        self._thread     = None
        self._name       = socket.gethostname()

    def _make_packet(self) -> bytes:
        payload = {
            "app":        "TetherLink",
            "name":       self._name,
            "port":       self._port,
            "resolution": self._resolution,
            "version":    "1.0",
        }
        return json.dumps(payload).encode("utf-8")

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)

        log.info("Broadcasting presence on UDP port %d every %.0fs",
                 BROADCAST_PORT, BROADCAST_INTERVAL)

        while self._running:
            packet = self._make_packet()
            try:
                # Broadcast on all interfaces
                sock.sendto(packet, ("255.255.255.255", BROADCAST_PORT))
            except Exception as e:
                log.debug("Broadcast error: %s", e)
            time.sleep(BROADCAST_INTERVAL)

        sock.close()

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._broadcast_loop, daemon=True
        )
        self._thread.start()
        log.info("Discovery broadcaster started — device name: %s", self._name)

    def stop(self):
        self._running = False
