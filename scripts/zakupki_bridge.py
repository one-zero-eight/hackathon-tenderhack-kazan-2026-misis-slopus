#!/usr/bin/env python3
"""Local HTTP CONNECT bridge -> authenticated SOCKS5 with zakupki host remap to IP."""
from __future__ import annotations

import select
import socket
import threading
from pathlib import Path

import socks

ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = ROOT / "secrets" / "zakupki-proxy"
LISTEN = ("127.0.0.1", 18080)
DIRECT_IP = "94.79.51.128"
REWRITE = {
    "zakupki.mos.ru": DIRECT_IP,
    "www.zakupki.mos.ru": DIRECT_IP,
    "api.zakupki.mos.ru": DIRECT_IP,
}


def load_upstream():
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        endpoint, credentials = line.rsplit("@", 1)
        host, port = endpoint.split(":", 1)
        user, password = credentials.split(":", 1)
        return host, int(port), user, password
    raise RuntimeError(f"No proxy in {PROXY_FILE}")


UP_HOST, UP_PORT, UP_USER, UP_PASS = load_upstream()


def pipe(a: socket.socket, b: socket.socket):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for src in r:
                dst = b if src is a else a
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


def handle(client: socket.socket):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            data += chunk
            if len(data) > 65536:
                client.close()
                return
        header, _, rest = data.partition(b"\r\n\r\n")
        first = header.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = first.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            client.close()
            return
        target = parts[1]
        if ":" in target:
            host, port_s = target.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = target, 443
        connect_host = REWRITE.get(host.lower(), host)
        upstream = socks.socksocket()
        upstream.set_proxy(socks.SOCKS5, UP_HOST, UP_PORT, True, UP_USER, UP_PASS)
        upstream.settimeout(60)
        upstream.connect((connect_host, port))
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        t = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        t.start()
    except Exception:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    print(f"bridge http://{LISTEN[0]}:{LISTEN[1]} -> socks5://{UP_HOST}:{UP_PORT} remap={REWRITE}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
