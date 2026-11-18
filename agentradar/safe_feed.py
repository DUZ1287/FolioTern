"""Public HTTPS feeds with pinned, validated addresses; no proxy or redirects."""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

from .collectors import CollectionError


def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment or p.port not in (None, 443):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if any(ord(c) < 33 for c in url) or '\\' in url or '%' in host:
            raise ValueError()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (not address.is_global or getattr(address, 'is_site_local', False)):
            raise ValueError()
        netloc = '[' + host + ']' if ':' in host else host
        return urlunsplit(('https', netloc, p.path or '/', p.query, ''))
    except (ValueError, UnicodeError):
        raise ValueError('RSS 地址必须是公网 HTTPS 地址，不含凭据、片段或非标准端口') from None


def public_addresses(host):
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise CollectionError('RSS 域名解析失败') from None
    if not addresses or any(not ipaddress.ip_address(x).is_global or getattr(ipaddress.ip_address(x), 'is_site_local', False) for x in addresses):
        raise CollectionError('RSS 解析指向本机或非公网地址，已阻止')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(self.address):
                raise CollectionError('RSS 连接目标与校验地址不一致')
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_feed(url, deadline=None):
    try:
        p = urlsplit(validate_url(url))
        deadline = min(deadline or time.monotonic() + 30, time.monotonic() + 30)
        addresses = public_addresses(p.hostname)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError('RSS 请求超时')
        conn = PinnedHTTPS(p.hostname, addresses[0], min(15, remaining))
        try:
            conn.request('GET', p.path + ('?' + p.query if p.query else ''), headers={
                'User-Agent': 'AgentRadar/desktop', 'Accept-Encoding': 'identity', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'})
            response = conn.getresponse()
            if response.status != 200:
                raise CollectionError('RSS 重定向已阻止' if 300 <= response.status < 400 else f'RSS 返回 HTTP {response.status}')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise CollectionError('RSS 返回未请求的压缩内容，已阻止')
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError('RSS 请求超时')
                if conn.sock:
                    conn.sock.settimeout(min(15, remaining))
                chunk = response.read1(16384)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise CollectionError('RSS 响应超过 4 MiB')
            return bytes(data)
        finally:
            conn.close()
    except CollectionError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise CollectionError('RSS 网络或 TLS 校验失败') from None
