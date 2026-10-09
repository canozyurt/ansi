#!/usr/bin/env python3
"""Minimal AMQP 0-9-1 client for the Molecule verify: TLS handshake, PLAIN login, open a vhost.

Usage: amqp_probe.py HOST PORT CAFILE USER VHOST  (password in the AMQP_PASSWORD environment variable)
Prints OPEN-OK when the vhost was opened, or CLOSE <code> <text> when the server refused.
"""
import os
import socket
import ssl
import struct
import sys


def frame(channel, payload, ftype=1):
    return struct.pack(">BHI", ftype, channel, len(payload)) + payload + b"\xce"


def shortstr(s):
    b = s.encode()
    return struct.pack(">B", len(b)) + b


def longstr(b):
    return struct.pack(">I", len(b)) + b


def read_frame(sock):
    hdr = b""
    while len(hdr) < 7:
        chunk = sock.recv(7 - len(hdr))
        if not chunk:
            raise EOFError("connection closed")
        hdr += chunk
    ftype, channel, size = struct.unpack(">BHI", hdr)
    body = b""
    while len(body) < size + 1:
        chunk = sock.recv(size + 1 - len(body))
        if not chunk:
            raise EOFError("connection closed")
        body += chunk
    return ftype, channel, body[:-1]


def method(body):
    cls, meth = struct.unpack(">HH", body[:4])
    return cls, meth, body[4:]


def main():
    host, port, cafile, user, vhost = sys.argv[1:6]
    password = os.environ["AMQP_PASSWORD"]
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = False
    raw = socket.create_connection((host, int(port)), timeout=10)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    sock.sendall(b"AMQP\x00\x00\x09\x01")
    _, _, body = read_frame(sock)
    assert method(body)[:2] == (10, 10), "expected connection.start"
    response = b"\x00" + user.encode() + b"\x00" + password.encode()
    start_ok = struct.pack(">HH", 10, 11) + struct.pack(">I", 0) + shortstr("PLAIN") + longstr(response) + shortstr("en_US")
    sock.sendall(frame(0, start_ok))
    try:
        _, _, body = read_frame(sock)
    except EOFError:
        print("CLOSED")
        return
    cls, meth, args = method(body)
    if (cls, meth) == (10, 50):
        code = struct.unpack(">H", args[:2])[0]
        print("CLOSE %d %s" % (code, args[3:3 + args[2]].decode()))
        return
    if (cls, meth) != (10, 30):
        print("UNEXPECTED %d.%d" % (cls, meth))
        return
    channel_max, frame_max, heartbeat = struct.unpack(">HIH", args[:8])
    sock.sendall(frame(0, struct.pack(">HHHIH", 10, 31, channel_max, frame_max, 0)))
    sock.sendall(frame(0, struct.pack(">HH", 10, 40) + shortstr(vhost) + shortstr("") + b"\x00"))
    try:
        _, _, body = read_frame(sock)
    except EOFError:
        print("CLOSED")
        return
    cls, meth, args = method(body)
    if (cls, meth) == (10, 41):
        print("OPEN-OK %s" % sock.version())
    elif (cls, meth) == (10, 50):
        code = struct.unpack(">H", args[:2])[0]
        print("CLOSE %d %s" % (code, args[3:3 + args[2]].decode()))
    else:
        print("UNEXPECTED %d.%d" % (cls, meth))


if __name__ == "__main__":
    main()
