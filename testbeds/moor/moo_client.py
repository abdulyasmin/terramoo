"""Tiny line client for the mooR telnet host, used by provision.py.

Stdlib only. Commands are bracketed with PREFIX/SUFFIX markers so each
command's output can be read back exactly, without timing guesses.
"""

import socket
import time
import uuid

HOST, PORT = "127.0.0.1", 17003


class Moo:
    def __init__(self, host=HOST, port=PORT, timeout=15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.timeout = timeout
        self.buf = b""

    # -- raw io -------------------------------------------------------
    def _fill(self, deadline):
        self.sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not data:
            raise ConnectionError("connection closed by server")
        self.buf += strip_telnet(data)
        return True

    def send(self, line):
        self.sock.sendall(line.encode() + b"\r\n")

    def drain(self, quiet=0.7):
        """Read until the server has been silent for `quiet` seconds."""
        while True:
            deadline = time.monotonic() + quiet
            if not self._fill(deadline):
                break
        out, self.buf = self.buf, b""
        return out.decode(errors="replace")

    def read_until(self, marker, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        m = marker.encode()
        while m not in self.buf:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no {marker!r}; got {self.buf[-500:]!r}")
            self._fill(deadline)
        before, _, self.buf = self.buf.partition(m)
        return before.decode(errors="replace")

    # -- session ------------------------------------------------------
    def login(self, name, password=""):
        self.drain(1.0)  # welcome banner
        self.send(f"connect {name} {password}".rstrip())
        text = self.drain(1.5)
        if "*** Connected ***" not in text and "*** Created ***" not in text:
            raise RuntimeError(f"login as {name} failed:\n{text}")
        return text

    def eval(self, expr, timeout=None):
        """Run `;expr`; return its output lines (markers stripped)."""
        tag = uuid.uuid4().hex[:12]
        pre, suf = f"<<pre-{tag}>>", f"<<suf-{tag}>>"
        self.send(f"PREFIX {pre}")
        self.send(f"SUFFIX {suf}")
        self.send(";" + expr)
        self.read_until(pre, timeout)
        body = self.read_until(suf, timeout)
        self.send("PREFIX")
        self.send("SUFFIX")
        return [l for l in body.replace("\r", "").split("\n") if l != ""]

    def close(self):
        try:
            self.send("@quit")
        finally:
            self.sock.close()


def strip_telnet(data):
    """Remove telnet IAC sequences (the host may negotiate options)."""
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        b = data[i]
        if b != 0xFF:
            out.append(b)
            i += 1
        elif i + 1 < n and data[i + 1] == 0xFF:
            out.append(0xFF)
            i += 2
        elif i + 1 < n and data[i + 1] == 0xFA:  # subnegotiation until IAC SE
            j = data.find(b"\xff\xf0", i + 2)
            i = n if j < 0 else j + 2
        else:
            i += 3
    return bytes(out)
