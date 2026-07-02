"""Minimal Source RCON client (used by Factorio) - no external dependencies.

Factorio quirk: a bare Lua expression like `rcon.print(game.tick)` is logged
as a chat message and produces no output. Prefix commands with
`/silent-command` (or `/c`) to actually execute them, e.g.:

    python3 rcon.py 127.0.0.1 27015 "$(cat .rcon-password)" "/silent-command rcon.print(game.tick)"
"""

import socket
import struct
import sys

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2


def _send_packet(sock: socket.socket, request_id: int, packet_type: int, body: str) -> None:
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    data = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("RCON connection closed unexpectedly")
        buf += chunk
    return buf


def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 5.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _send_packet(sock, 1, SERVERDATA_AUTH, password)
        req_id, _ptype, _body = _read_packet(sock)
        if req_id != 1:
            raise PermissionError("RCON auth failed")

        _send_packet(sock, 2, SERVERDATA_EXECCOMMAND, command)
        _req_id, _ptype, body = _read_packet(sock)
        return body


if __name__ == "__main__":
    host, port, password = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    command = sys.argv[4]
    print(rcon_command(host, port, password, command))
