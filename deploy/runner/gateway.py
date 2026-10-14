"""Per-job provider tunnel and read-only package registry gateway.

No GitHub or arbitrary URL route exists. Registry requests terminate here and
use GET/HEAD upstream; CONNECT is reserved for approved model providers. Every
DNS result must be public and the socket uses the validated address directly.
"""
from contextlib import closing
import http.client
import ipaddress
import json
import re
import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import urlsplit

PROVIDERS = ('api.openai.com','auth.openai.com','chatgpt.com','*.chatgpt.com',
    'api.anthropic.com','platform.claude.com','claude.ai','cli-chat-proxy.grok.com','auth.x.ai')
REGISTRIES = frozenset({'registry.npmjs.org','registry.yarnpkg.com','pypi.org','files.pythonhosted.org'})
MAX_HEADERS = 16384
MAX_METADATA = 8*1024**2
MAX_DOWNLOAD = 128*1024**2
MAX_TUNNEL = 256*1024**2
REGISTRY_ORIGIN = 'http://forge-registry:8081'


def provider(host):
    return any(host==pattern or pattern.startswith('*.') and host.endswith(pattern[1:])
               and host!=pattern[2:] for pattern in PROVIDERS)


def destinations(host, resolver=None):
    rows=(resolver or socket.getaddrinfo)(host,443,type=socket.SOCK_STREAM)
    if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
        raise ValueError('destination is not public')
    return rows


def connect_public(host):
    family,kind,protocol,_,address=destinations(host)[0]
    peer=socket.socket(family,kind,protocol)
    try:
        peer.settimeout(20);peer.connect(address)
        return peer
    except BaseException:
        peer.close();raise


def registry_target(target):
    if not target.startswith('/') or any(ord(c)<32 or c=='\\' for c in target):
        raise ValueError('invalid registry target')
    host,separator,path=target[1:].partition('/')
    if host not in REGISTRIES or not separator or '#' in target:
        raise ValueError('registry not allowed')
    return host,'/'+path


def registry_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='https' or parsed.hostname not in REGISTRIES or parsed.port not in (None,443) or parsed.username or parsed.password:
        raise ValueError('registry redirect not allowed')
    return REGISTRY_ORIGIN+'/'+parsed.hostname+(parsed.path or '/')+('?' + parsed.query if parsed.query else '')+('#'+parsed.fragment if parsed.fragment else '')


def metadata(body, content_type):
    def rewrite(value):
        if isinstance(value,dict):return {key:rewrite(item) for key,item in value.items()}
        if isinstance(value,list):return [rewrite(item) for item in value]
        if isinstance(value,str) and value.startswith('https://'):
            try:return registry_url(value)
            except ValueError:return value  # Descriptive project links grant no route.
        return value
    if 'json' in content_type:
        return json.dumps(rewrite(json.loads(body)),separators=(',',':')).encode()
    text=body.decode('utf-8')
    def link(match):
        try:return registry_url(match.group(0))
        except ValueError:return match.group(0)
    return re.sub(r'https://[^\s<>"\']+',link,text).encode()


def tls_server_name(record):
    """Bounded TLS ClientHello SNI; no different-host tunnel on a shared CDN IP."""
    if len(record)<9 or record[0]!=22 or record[5]!=1:
        raise ValueError('TLS ClientHello required')
    length=int.from_bytes(record[6:9],'big')
    if len(record)!=9+length:
        raise ValueError('fragmented ClientHello is unsupported')
    data=memoryview(record)[9:];position=34
    def take(size):
        nonlocal position
        if size<0 or position+size>len(data):raise ValueError('invalid ClientHello')
        value=bytes(data[position:position+size]);position+=size;return value
    take(int.from_bytes(take(1),'big'))
    take(int.from_bytes(take(2),'big'))
    take(int.from_bytes(take(1),'big'))
    extensions=take(int.from_bytes(take(2),'big'))
    offset=0;names=[]
    while offset<len(extensions):
        if offset+4>len(extensions):raise ValueError('invalid TLS extension')
        kind=int.from_bytes(extensions[offset:offset+2],'big');size=int.from_bytes(extensions[offset+2:offset+4],'big');offset+=4
        value=extensions[offset:offset+size];offset+=size
        if len(value)!=size:raise ValueError('invalid TLS extension')
        if kind==0:
            if len(value)<5 or int.from_bytes(value[:2],'big')!=len(value)-2 or value[2]!=0 or int.from_bytes(value[3:5],'big')!=len(value)-5:
                raise ValueError('invalid TLS name')
            names.append(value[5:].decode('ascii'))
    if len(names)!=1:raise ValueError('one TLS name required')
    return names[0].lower()


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock=self._context.wrap_socket(connect_public(self.host),server_hostname=self.host)


class Handler(socketserver.StreamRequestHandler):
    rbufsize=0  # Never strand TLS bytes in the HTTP parser's buffer.
    def reply(self,status,body=b'',headers=()):
        reason={200:'OK',400:'Bad Request',403:'Forbidden',429:'Too Many Requests',502:'Bad Gateway'}.get(status,'Response')
        self.wfile.write(f'HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: {len(body)}\r\n'.encode())
        for name,value in headers:
            if '\r' in value or '\n' in value:raise ValueError('invalid upstream header')
            self.wfile.write((name+': '+value+'\r\n').encode())
        self.wfile.write(b'\r\n'+body);self.wfile.flush()

    def read_request(self):
        line=self.rfile.readline(4097)
        if len(line)>4096:raise ValueError('request too long')
        method,target,version=line.decode('ascii').rstrip('\r\n').split(' ')
        if version not in {'HTTP/1.0','HTTP/1.1'}:raise ValueError('invalid HTTP version')
        total=0;headers={}
        while True:
            line=self.rfile.readline(4097);total+=len(line)
            if len(line)>4096 or total>MAX_HEADERS or not line:raise ValueError('invalid headers')
            if line==b'\r\n':break
            name,value=line.decode('ascii').rstrip('\r\n').split(':',1)
            name=name.lower();value=value.strip()
            if name in headers or not re.fullmatch('[a-z0-9-]+',name):raise ValueError('invalid header')
            headers[name]=value
        if headers.get('content-length','0')!='0' or 'transfer-encoding' in headers:
            raise ValueError('request body is forbidden')
        return method,target,headers

    def handle(self):
        self.connection.settimeout(20)
        try:
            method,target,headers=self.read_request()
            if self.server.mode=='provider':
                if method!='CONNECT':self.reply(403);return
                host,separator,port=target.rpartition(':')
                if not separator or port!='443' or not provider(host.lower()):self.reply(403);return
                self.tunnel(host.lower())
            else:
                if method not in {'GET','HEAD'}:self.reply(403);return
                host,path=registry_target(target)
                self.registry(method,host,path,headers)
        except (ValueError,UnicodeError):
            self.reply(403)
        except (OSError,http.client.HTTPException):
            self.reply(502)

    def tunnel(self,host):
        # Explicit refusal of forbidden CONNECT makes pinned npm use its Git
        # fallback through forge-git; silently closing the socket does not.
        with connect_public(host) as upstream:
            self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');self.wfile.flush()
            def exact(size):
                result=b''
                while len(result)<size:
                    part=self.connection.recv(size-len(result))
                    if not part:raise ValueError('incomplete TLS record')
                    result+=part
                return result
            header=exact(5);length=int.from_bytes(header[3:5],'big')
            if length>16384:raise ValueError('TLS record too large')
            hello=header+exact(length)
            if tls_server_name(hello)!=host:raise ValueError('TLS name differs from CONNECT authority')
            upstream.sendall(hello)
            deadline=time.monotonic()+3000;total=len(hello)
            while time.monotonic()<deadline and total<MAX_TUNNEL:
                ready,_,_=select.select((self.connection,upstream),[],[],20)
                for peer in ready:
                    data=peer.recv(min(65536,MAX_TUNNEL-total))
                    if not data:return
                    total+=len(data)
                    (upstream if peer is self.connection else self.connection).sendall(data)

    def registry(self,method,host,path,headers):
        with closing(PublicHTTPS(host,timeout=20,context=ssl.create_default_context())) as upstream:
            upstream.request(method,path,headers={'Accept':headers.get('accept','*/*'),'Accept-Encoding':'identity','User-Agent':'Forge-readonly-registry/1'})
            response=upstream.getresponse()
            content_type=response.getheader('Content-Type','application/octet-stream')
            if '\r' in content_type or '\n' in content_type:raise ValueError('invalid content type')
            selected=[('Content-Type',content_type)]
            if response.getheader('Location'):
                location=response.getheader('Location')
                if location.startswith('/'):location='https://'+host+location
                selected.append(('Location',registry_url(location)))
            if method=='HEAD':self.reply(response.status,headers=selected);return
            text='json' in content_type or 'html' in content_type
            maximum=MAX_METADATA if text else MAX_DOWNLOAD
            declared=response.getheader('Content-Length')
            if declared is not None and (not declared.isdigit() or int(declared)>maximum):
                raise ValueError('registry response exceeds limit')
            if text:
                body=response.read(maximum+1)
                if len(body)>maximum:raise ValueError('registry metadata exceeds limit')
                if response.status==200:body=metadata(body,content_type)
                self.reply(response.status,body,selected)
            else:
                self.wfile.write(f'HTTP/1.1 {response.status} Response\r\nConnection: close\r\nTransfer-Encoding: chunked\r\nContent-Type: {content_type}\r\n\r\n'.encode())
                total=0;deadline=time.monotonic()+300
                while total<maximum and time.monotonic()<deadline:
                    chunk=response.read(min(65536,maximum-total))
                    if not chunk:
                        self.wfile.write(b'0\r\n\r\n');self.wfile.flush();return
                    total+=len(chunk);self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                if total==maximum and not response.read(1):self.wfile.write(b'0\r\n\r\n')
                # No terminal chunk on a quota/deadline cut-off: clients must
                # see an incomplete response instead of accepting truncation.
                self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True
    request_queue_size=16
    def __init__(self,address,mode):
        self.mode=mode;self.slots=threading.BoundedSemaphore(2)
        super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,request,address):
        pass  # Never log worker headers, paths or provider bytes.


if __name__=='__main__':
    with Server(('0.0.0.0',3128),'provider') as provider_server, Server(('0.0.0.0',8081),'registry') as registry_server:
        thread=threading.Thread(target=provider_server.serve_forever,daemon=True);thread.start()
        registry_server.serve_forever()
