"""Find a robot on the local network by its device id, after its IP address changed.

Every miIO device answers a "hello" packet on UDP port 54321 with a header that carries its
device id (the cloud's `did`), unencrypted. Broadcasting one hello on each local network and
keeping the answer whose id matches finds the robot without the cloud or the token.
"""

import fcntl
import logging
import socket
import struct
import time
from collections.abc import Iterable

log = logging.getLogger(__name__)

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)
_SIOCGIFFLAGS = 0x8913
_SIOCGIFBRDADDR = 0x8919
_IFF_UP = 0x1
_IFF_BROADCAST = 0x2
_IFF_LOOPBACK = 0x8


def broadcast_addresses() -> list[str]:
    """The broadcast address of every IPv4 network this machine is on, plus the limited
    broadcast address (which only leaves through the default route)."""
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _index, name in socket.if_nameindex():
            packed = struct.pack("256s", name.encode()[:15])
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(sock, _SIOCGIFFLAGS, packed), 16)[0]
                if flags & _IFF_LOOPBACK or not flags & _IFF_UP or not flags & _IFF_BROADCAST:
                    continue
                addr = socket.inet_ntoa(fcntl.ioctl(sock, _SIOCGIFBRDADDR, packed)[20:24])
            except OSError:  # no IPv4 address on this interface
                continue
            if addr != "0.0.0.0" and addr not in found:
                found.append(addr)
    found.append("255.255.255.255")
    return found


def parse_hello_reply(data: bytes) -> int | None:
    """The device id in a hello reply, or None when `data` isn't one."""
    if len(data) < 32 or data[:2] != b"\x21\x31":
        return None
    return int.from_bytes(data[8:12], "big")


def find_robot(
    did: str | int,
    timeout: float = 3.0,
    addresses: Iterable[str] | None = None,
    sock_factory=socket.socket,
) -> str | None:
    """The IP address of the device with id `did`, or None if it didn't answer in time."""
    try:
        wanted = int(did)
    except (TypeError, ValueError):
        return None
    targets = list(addresses) if addresses is not None else broadcast_addresses()
    with sock_factory(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while (now := time.monotonic()) < deadline:
            if now >= next_send:  # resend now and then: a sleepy robot can miss one
                for target in targets:
                    try:
                        sock.sendto(HELLO, (target, PORT))
                    except OSError as err:
                        log.debug("hello to %s failed: %s", target, err)
                next_send = now + 1.0
            try:
                data, (ip, _port) = sock.recvfrom(1024)
            except TimeoutError:
                continue
            if parse_hello_reply(data) == wanted:
                return ip
    return None
