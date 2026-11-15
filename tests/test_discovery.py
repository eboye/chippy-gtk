import socket

from chippy.core import discovery


def reply(did: int) -> bytes:
    return b"\x21\x31\x00\x20" + b"\x00" * 4 + did.to_bytes(4, "big") + b"\x00" * 20


class FakeSocket:
    """Answers each hello with the replies of every device on a pretend network."""

    def __init__(self, devices):
        self.devices = devices  # [(ip, did)]
        self.sent = []
        self.inbox = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def setsockopt(self, *_args):
        pass

    def settimeout(self, _seconds):
        pass

    def sendto(self, data, address):
        self.sent.append((data, address))
        self.inbox.extend((reply(did), (ip, discovery.PORT)) for ip, did in self.devices)

    def recvfrom(self, _size):
        if not self.inbox:
            raise TimeoutError
        return self.inbox.pop(0)


def test_parse_hello_reply():
    assert discovery.parse_hello_reply(reply(1151376566)) == 1151376566
    assert discovery.parse_hello_reply(b"\x21\x31") is None  # too short
    assert discovery.parse_hello_reply(b"\x00" * 32) is None  # not a miIO packet


def test_find_robot_picks_the_device_with_the_right_id():
    sock = FakeSocket([("192.168.1.20", 42), ("192.168.1.119", 1151376566)])
    found = discovery.find_robot(
        "1151376566", addresses=["192.168.1.255"], sock_factory=lambda *_a: sock
    )
    assert found == "192.168.1.119"
    assert sock.sent[0] == (discovery.HELLO, ("192.168.1.255", discovery.PORT))


def test_find_robot_gives_up_when_the_robot_does_not_answer():
    sock = FakeSocket([("192.168.1.20", 42)])
    found = discovery.find_robot(
        "7", timeout=0.2, addresses=["192.168.1.255"], sock_factory=lambda *_a: sock
    )
    assert found is None


def test_find_robot_needs_a_numeric_id():
    assert discovery.find_robot("", addresses=[]) is None


def test_broadcast_addresses_end_with_the_limited_broadcast():
    found = discovery.broadcast_addresses()
    assert found[-1] == "255.255.255.255"
    for addr in found:
        socket.inet_aton(addr)  # each one is a valid IPv4 address
    assert "127.255.255.255" not in found  # loopback is skipped
