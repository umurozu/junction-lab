"""UDP socket / event loop helpers.

On Windows an ICMP "port unreachable" (e.g. a device sending to a center that
is not up yet) is reported on the *next* recvfrom() as WSAECONNRESET. The
default Proactor loop then stops reading that UDP socket - it goes silently
deaf. The Selector loop reports it via error_received() and keeps reading,
so services run on the Selector loop (same behaviour as Linux).
"""

from __future__ import annotations

import asyncio
import socket
import sys


def udp_socket(host: str = "0.0.0.0", port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.setblocking(False)
    return sock


async def udp_endpoint(factory, host: str = "0.0.0.0", port: int = 0, remote: tuple[str, int] | None = None):
    sock = udp_socket(host, port)
    if remote:
        sock.connect(remote)
    return await asyncio.get_running_loop().create_datagram_endpoint(factory, sock=sock)


def run(coro):
    """asyncio.run() with the Selector loop on Windows."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
