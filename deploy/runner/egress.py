"""HTTPS CONNECT proxy: configured domains, port 443, public destinations only.

Workers have only an internal Docker network. This proxy has a separate uplink;
no host port is published. Requests cannot reach host/private control surfaces.
"""
import ipaddress
import json
import os
import select
import socket
import socketserver
import time

DOMAINS = json.loads(os.environ.get('FORGE_EGRESS_DOMAINS', '["github.com","api.github.com","*.githubusercontent.com","api.openai.com","auth.openai.com","chatgpt.com","*.chatgpt.com","api.anthropic.com","platform.claude.com","claude.ai","cli-chat-proxy.grok.com","auth.x.ai","registry.npmjs.org","pypi.org","files.pythonhosted.org"]'))


def allowed(host):
    return any(host == d or d.startswith('*.') and host.endswith(d[1:]) and host != d[2:] for d in DOMAINS)


def destinations(host, resolver=socket.getaddrinfo):
    if not allowed(host):
        raise ValueError('Domain not allowed')
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Private destinations are not allowed')
    return addresses


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            line = self.rfile.readline(4097)
            if len(line) > 4096:
                return
            method, target, version = line.decode('ascii').strip().split(' ')
            host, port = target.rsplit(':', 1)
            host = host.lower()
            if method != 'CONNECT' or port != '443' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                return
            total = 0
            while True:
                header = self.rfile.readline(4097); total += len(header)
                if total > 16384 or not header:
                    return
                if header == b'\r\n':
                    break
            addresses = destinations(host)
            # Connect to the address we validated, never resolve the name again.
            family, kind, protocol, _, address = addresses[0]
            with socket.socket(family, kind, protocol) as upstream:
                upstream.settimeout(10); upstream.connect(address)
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); self.wfile.flush()
                deadline = time.monotonic() + 3000
                peers = (self.connection, upstream)
                while time.monotonic() < deadline:
                    ready, _, _ = select.select(peers, [], [], 30)
                    if not ready:
                        continue
                    for peer in ready:
                        chunk = peer.recv(65536)
                        if not chunk:
                            return
                        (upstream if peer is self.connection else self.connection).sendall(chunk)
        except (OSError, ValueError, UnicodeError):
            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


if __name__ == '__main__':
    with Server(('0.0.0.0', 3128), Proxy) as server:
        server.serve_forever()
