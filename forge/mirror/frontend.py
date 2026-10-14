#!/usr/bin/env python3
"""Credential-free frontend; copied standalone into the trusted runner image."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import struct
import threading

MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 512 * 1024 * 1024


def exact(stream, length):
    result = bytearray()
    while len(result) < length:
        part = stream.recv(min(65536, length - len(result)))
        if not part:
            raise ValueError('incomplete response')
        result.extend(part)
    return bytes(result)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        self.connection.settimeout(150)
        if not self.server.slots.acquire(blocking=False):
            self.send_error(503)
            return
        headers_sent = False
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') is not None or len(lengths) > 1
                    or lengths and not lengths[0].isdigit()):
                raise ValueError('invalid framing')
            length = int(lengths[0]) if lengths else 0
            if length > MAX_REQUEST or len(self.path) > 2048:
                raise ValueError('request limit')
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError('incomplete body')
            request = {'method': self.command, 'path': self.path, 'protocol': self.headers.get('Git-Protocol', ''),
                       'content_type': self.headers.get('Content-Type', ''), 'body_length': len(data)}
            raw = json.dumps(request, separators=(',', ':')).encode()
            if len(raw) > 8192:
                raise ValueError('header limit')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(150)
                stream.connect(self.server.broker_socket)
                stream.sendall(struct.pack('!I', len(raw)) + raw + data)
                header_size = struct.unpack('!I', exact(stream, 4))[0]
                if header_size > 8192:
                    raise ValueError('response header limit')
                response = json.loads(exact(stream, header_size))
                if type(response['body_length']) is not int or not 0 <= response['body_length'] <= MAX_RESPONSE:
                    raise ValueError('response limit')
                self.send_response(response['status'])
                for key, value in response['headers'].items():
                    if key.lower() in {'content-type', 'cache-control', 'expires', 'pragma'}:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(response['body_length']))
                headers_sent = True
                self.end_headers()
                remaining = response['body_length']
                while remaining:
                    payload = exact(stream, min(65536, remaining))
                    self.wfile.write(payload)
                    remaining -= len(payload)
        except (ValueError, KeyError, OSError, TimeoutError):
            self.close_connection = True
            # A second HTTP response would corrupt an already-started Git pack.
            if not headers_sent:
                self.send_error(502)
        finally:
            self.server.slots.release()


class Frontend(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, broker_socket):
        super().__init__(address, Handler)
        self.broker_socket = broker_socket
        self.slots = threading.BoundedSemaphore(4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--port', type=int, default=80)
    args = parser.parse_args()
    with Frontend(('0.0.0.0', args.port), args.socket) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
