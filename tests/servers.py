"""Local test servers: destination app, HTTP proxy (CONNECT + auth), SOCKS5."""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
import struct
from typing import List, Optional, Tuple

from aiohttp import web

# ---------------------------------------------------------------------------
# destination application


def build_target_app() -> web.Application:
    async def ok(request):
        return web.Response(
            text="<html><head><title>Example Site</title></head><body>"
            "<h1>Welcome to Example Site</h1>"
            "<p>Real content about interesting things and useful services.</p>"
            "<a href='/about'>About us</a><a href='/docs'>Docs</a>"
            "<a href='/contact'>Contact</a></body></html>",
            content_type="text/html",
        )

    async def js_heavy(request):
        return web.Response(
            text="<html><head><title>SPA App</title></head><body>"
            '<div id="root"></div><script src="/static/app.js"></script>'
            "<script>window.__NEXT_DATA__={}</script></body></html>",
            content_type="text/html",
        )

    async def article(request):
        # legitimate article that *mentions* "page not found"
        body = ("<html><head><title>How to fix page not found errors</title></head>"
                "<body><h1>How to fix page not found errors</h1>"
                "<p>" + ("A thorough guide about fixing 404 issues on your own website. " * 60) +
                "</p><a href='/x'>x</a><a href='/y'>y</a><a href='/z'>z</a>"
                "<a href='/w'>w</a><a href='/v'>v</a></body></html>")
        return web.Response(text=body, content_type="text/html")

    async def soft404(request):
        return web.Response(
            status=200,
            text="<html><head><title>404 Not Found</title></head><body>"
            "<h1>Error 404 - page not found</h1>"
            "<p>The requested url was not found. Go back to homepage.</p>"
            "</body></html>",
            content_type="text/html",
        )

    async def parked(request):
        return web.Response(
            status=200,
            text="<html><head><title>Buy this domain - example.test</title></head>"
            "<body><h1>This domain is for sale</h1>"
            "<p>Sponsored listings. Related searches. parkingcrew.</p></body></html>",
            content_type="text/html",
        )

    async def hosting_default(request):
        return web.Response(
            status=200,
            text="<html><head><title>Welcome to nginx!</title></head>"
            "<body><h1>Welcome to nginx!</h1>"
            "<p>If you see this page, the nginx web server is successfully "
            "installed and working.</p></body></html>",
            content_type="text/html",
        )

    async def suspended(request):
        return web.Response(
            status=200,
            text="<html><head><title>Account Suspended</title></head>"
            "<body><h1>This account has been suspended</h1>"
            "<p>Contact your hosting provider.</p></body></html>",
            content_type="text/html",
        )

    async def cdn_error(request):
        return web.Response(
            status=200,
            text="<html><head><title>Attention Required! | Cloudflare</title></head>"
            "<body><h1>Sorry, you have been blocked. Error 1000</h1>"
            "<p>DNS points to prohibited IP.</p></body></html>",
            content_type="text/html",
        )

    async def browser_error(request):
        return web.Response(
            status=200,
            text="<html><head><title>This site can't be reached</title></head>"
            "<body><h1>This site can't be reached</h1>"
            "<p>ERR_NAME_NOT_RESOLVED</p></body></html>",
            content_type="text/html",
        )

    async def redirect_n(request):
        n = int(request.match_info["n"])
        if n <= 1:
            raise web.HTTPFound("/ok")
        raise web.HTTPFound(f"/redirect/{n - 1}")

    async def redirect_abs(request):
        n = int(request.match_info["n"])
        host = request.host
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        if n <= 1:
            raise web.HTTPFound(f"{scheme}://{host}/ok")
        raise web.HTTPFound(f"{scheme}://{host}/redirect-abs/{n - 1}")

    async def redirect_http_to_https(request):
        raise web.HTTPFound("https://example-should-not-be-called.test/ok")

    async def loop1(request):
        raise web.HTTPFound("/loop2")

    async def loop2(request):
        raise web.HTTPFound("/loop1")

    async def relative_redirect(request):
        raise web.HTTPFound("deeper/page")

    async def deeper_page(request):
        return web.Response(
            text="<html><head><title>Deeper Page</title></head><body>Deeper!</body></html>",
            content_type="text/html",
        )

    async def status(request):
        code = int(request.match_info["code"])
        return web.Response(status=code, text=f"status {code}")

    async def head_only_405(request):
        if request.method == "HEAD":
            return web.Response(status=405)
        return web.Response(
            text="<html><head><title>Headless</title></head><body>GET works</body></html>",
            content_type="text/html",
        )

    async def get_404_head_200(request):
        if request.method == "HEAD":
            return web.Response(status=200)
        return web.Response(status=404, text="not found for GET")

    async def slow(request):
        await asyncio.sleep(float(request.query.get("t", "3")))
        return web.Response(text="slow page")

    async def reset(request):
        request.transport.abort()
        return web.Response(text="never")

    async def root(request):
        return web.Response(text="ok", content_type="text/plain")

    app = web.Application()
    r = app.router
    r.add_get("/", root)
    r.add_get("/ok", ok)
    r.add_get("/js-heavy", js_heavy)
    r.add_get("/article", article)
    r.add_get("/soft404", soft404)
    r.add_get("/parked", parked)
    r.add_get("/hosting-default", hosting_default)
    r.add_get("/suspended", suspended)
    r.add_get("/cdn-error", cdn_error)
    r.add_get("/browser-error", browser_error)
    r.add_get("/redirect/{n}", redirect_n)
    r.add_get("/redirect-abs/{n}", redirect_abs)
    r.add_get("/redirect-https-elsewhere", redirect_http_to_https)
    r.add_get("/loop1", loop1)
    r.add_get("/loop2", loop2)
    r.add_get("/relative-redirect", relative_redirect)
    r.add_get("/deeper/page", deeper_page)
    r.add_get("/status/{code}", status)
    r.add_get("/status/{code}/", status)
    r.add_get("/head-405", head_only_405)
    r.add_get("/get-404-head-200", get_404_head_200)
    r.add_get("/slow", slow)
    r.add_get("/reset", reset)
    return app


# ---------------------------------------------------------------------------
# raw HTTP forward proxy (absolute-form + CONNECT, optional basic auth)


class HttpProxyServer:
    def __init__(self, require_auth: bool = False,
                 refuse_connect: bool = False,
                 inject_error_page: bool = False):
        self.require_auth = require_auth
        self.refuse_connect = refuse_connect
        self.inject_error_page = inject_error_page
        self.server: Optional[asyncio.AbstractServer] = None
        self.port = 0
        self.tunnel_count = 0
        self.request_count = 0

    async def start(self) -> "HttpProxyServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def url(self, scheme: str = "http") -> str:
        return f"{scheme}://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
            if not request_line:
                return
            parts = request_line.decode("latin1").split()
            if len(parts) < 3:
                return
            method, target, version = parts[0], parts[1], parts[2]
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 10)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin1").partition(":")
                headers[k.strip().lower()] = v.strip()

            if self.require_auth:
                expected = base64.b64encode(b"user:pass").decode()
                if headers.get("proxy-authorization") != f"Basic {expected}":
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                 b"Proxy-Authenticate: Basic realm=\"x\"\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

            if method == "CONNECT":
                self.tunnel_count += 1
                if self.refuse_connect:
                    writer.write(b"HTTP/1.1 502 Cannot reach host\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                host, _, port_s = target.partition(":")
                try:
                    up_reader, up_writer = await asyncio.wait_for(
                        asyncio.open_connection(host, int(port_s)), 8
                    )
                except Exception:
                    writer.write(b"HTTP/1.1 503 Unreachable\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await self._pipe(reader, writer, up_reader, up_writer)
                return

            # absolute-form plain HTTP request
            self.request_count += 1
            scheme = "http"
            rest = target
            if "://" in target:
                scheme, rest = target.split("://", 1)
                rest = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
            host_header = headers.get("host", "localhost")
            fwd = (
                f"{method} {rest} {version}\r\n"
                f"Host: {host_header}\r\n"
                f"Connection: close\r\n\r\n"
            )
            dest_host = host_header.split(":")[0]
            dest_port = int(host_header.split(":")[1]) if ":" in host_header else 80
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(dest_host, dest_port), 8
                )
            except Exception:
                writer.write(b"HTTP/1.1 503 Unreachable\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            if self.inject_error_page:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
                    b"<html><head><title>Proxy Error</title></head><body>"
                    b"<h1>This site can't be reached</h1><p>ERR_CONNECTION_REFUSED</p>"
                    b"</body></html>"
                )
                await writer.drain()
                writer.close()
                return
            up_writer.write(fwd.encode("latin1"))
            await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer, initial_up=up_reader)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _pipe(self, reader, writer, up_reader, up_writer, initial_up=None):
        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pump(reader, up_writer), pump(up_reader, writer)
        )


# ---------------------------------------------------------------------------
# SOCKS server (5 and 4) with optional username/password auth


class SocksServer:
    """Minimal SOCKS4/4a/5 CONNECT server for tests."""

    def __init__(self, version: int = 5, require_auth: bool = False,
                 reject_all: bool = False):
        self.version = version
        self.require_auth = require_auth
        self.reject_all = reject_all
        self.server = None
        self.port = 0
        self.connect_count = 0

    async def start(self) -> "SocksServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def url(self) -> str:
        scheme = "socks5" if self.version == 5 else "socks4"
        if self.require_auth:
            return f"{scheme}://user:secret@127.0.0.1:{self.port}"
        return f"{scheme}://127.0.0.1:{self.port}"

    async def _handle(self, reader, writer):
        try:
            if self.version == 5:
                await self._handle5(reader, writer)
            else:
                await self._handle4(reader, writer)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _auth(self, reader, writer) -> bool:
        data = await asyncio.wait_for(reader.readexactly(2), 5)
        nmethods = data[1]
        methods = await asyncio.wait_for(reader.readexactly(nmethods), 5)
        if self.require_auth and 0x02 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return False
        if self.require_auth:
            writer.write(b"\x05\x02")
            await writer.drain()
            ver = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            ulen = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            uname = await asyncio.wait_for(reader.readexactly(ulen), 5)
            plen = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            password = await asyncio.wait_for(reader.readexactly(plen), 5)
            if uname != b"user" or password != b"secret":
                writer.write(b"\x01\x01")
                await writer.drain()
                return False
            writer.write(b"\x01\x00")
            await writer.drain()
        else:
            writer.write(b"\x05\x00")
            await writer.drain()
        return True

    async def _handle5(self, reader, writer):
        if not await self._auth(reader, writer):
            return
        head = await asyncio.wait_for(reader.readexactly(4), 5)
        cmd, atyp = head[1], head[3]
        if cmd != 1:
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        if atyp == 1:
            addr = socket.inet_ntoa(await asyncio.wait_for(reader.readexactly(4), 5))
        elif atyp == 3:
            ln = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            addr = (await asyncio.wait_for(reader.readexactly(ln), 5)).decode()
        elif atyp == 4:
            addr = socket.inet_ntop(
                socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), 5)
            )
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        port = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 5))[0]
        self.connect_count += 1
        if self.reject_all:
            writer.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addr, port), 8
            )
        except Exception:
            writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)  # host unreachable
            await writer.drain()
            return
        writer.write(b"\x05\x00\x00\x01" + b"\x00\x00\x00\x00" + struct.pack("!H", 0))
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _handle4(self, reader, writer):
        data = await asyncio.wait_for(reader.readexactly(8), 5)
        port = struct.unpack("!H", data[2:4])[0]
        addr = socket.inet_ntoa(data[4:8])
        userid = b""
        while True:
            ch = await asyncio.wait_for(reader.readexactly(1), 5)
            if ch == b"\x00":
                break
            userid += ch
        if self.require_auth and userid != b"user:secret":
            writer.write(b"\x00\x5a" + b"\x00" * 6)
            await writer.drain()
            return
        # socks4a: if addr is 0.0.0.x, a hostname follows
        if addr.startswith("0.0.0.") and addr != "0.0.0.0":
            hostname = b""
            while True:
                ch = await asyncio.wait_for(reader.readexactly(1), 5)
                if ch == b"\x00":
                    break
                hostname += ch
            addr = hostname.decode()
        self.connect_count += 1
        if self.reject_all:
            writer.write(b"\x00\x5b" + b"\x00" * 6)
            await writer.drain()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addr, port), 8
            )
        except Exception:
            writer.write(b"\x00\x5b" + b"\x00" * 6)
            await writer.drain()
            return
        writer.write(b"\x00\x5a" + b"\x00" * 6)
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _pipe(self, reader, writer, up_reader, up_writer):
        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))


# ---------------------------------------------------------------------------
# HTTPS helpers


def make_self_signed_cert(certfile: str, keyfile: str) -> None:
    import subprocess

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", keyfile, "-out", certfile, "-days", "2",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )


def ssl_server_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx
