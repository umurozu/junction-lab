"""Tiny sd_notify(3) implementation (no libsystemd dependency).

Lets services run as ``Type=notify`` with ``WatchdogSec=`` under systemd.
No-op when NOTIFY_SOCKET is not set (Windows, Docker, plain shell).
"""

from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:  # type: ignore[attr-defined]
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    """Half of WatchdogSec, in seconds, or None if the watchdog is disabled."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid and int(pid) != os.getpid():
        return None
    return int(usec) / 1e6 / 2
