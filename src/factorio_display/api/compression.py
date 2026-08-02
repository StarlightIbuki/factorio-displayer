"""Response compression middleware (gzip + brotli).

Negotiates ``Accept-Encoding`` (prefers brotli when available), buffers text
responses over a minimum size, and compresses them transparently.  Responses
that already carry a ``Content-Encoding`` header (e.g. pre-compressed artifact
downloads) are passed through untouched.
"""

from __future__ import annotations

import gzip

from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:  # pragma: no cover - environment dependent
    import brotli

    _HAS_BROTLI = True
except Exception:  # pylint: disable=broad-exception-caught
    brotli = None  # type: ignore[assignment]
    _HAS_BROTLI = False

_MINIMUM_SIZE = 1024
_COMPRESS_LEVEL = 5

_COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "image/svg+xml",
)


def _is_compressible(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return ct.startswith(_COMPRESSIBLE_PREFIXES)


class _Compressor:
    """Buffers the response body, then compresses it if worthwhile."""

    def __init__(self, app: ASGIApp, minimum_size: int, use_br: bool, level: int, send: Send) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.use_br = use_br
        self.level = level
        self.send = send
        self.initial_message: Message = {}
        self.buffer = bytearray()
        self.passthrough = False
        self.started = False

    async def __call__(self, scope: Scope, receive: Receive) -> None:
        await self.app(scope, receive, self._send_wrapper)

    async def _send_wrapper(self, message: Message) -> None:
        msg_type = message["type"]
        if msg_type == "http.response.start":
            self.initial_message = message
            headers = dict(message.get("headers", []))
            if b"content-encoding" in headers:
                # Already encoded (pre-compressed artifact) — pass through.
                self.passthrough = True
            return
        if msg_type != "http.response.body":
            return

        if self.passthrough:
            if not self.started:
                await self.send(self.initial_message)
                self.started = True
            await self.send(message)
            return

        self.buffer += message.get("body", b"")
        if message.get("more_body", False):
            return

        headers = dict(self.initial_message.get("headers", []))
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        length = len(self.buffer)
        if (
            length >= self.minimum_size
            and _is_compressible(content_type)
            and b"content-encoding" not in headers
        ):
            if self.use_br and _HAS_BROTLI:
                body = brotli.compress(bytes(self.buffer), quality=self.level)
                encoding = b"br"
            else:
                body = gzip.compress(bytes(self.buffer), compresslevel=self.level)
                encoding = b"gzip"
            headers[b"content-encoding"] = encoding
            headers[b"content-length"] = str(len(body)).encode("latin-1")
            vary = headers.get(b"vary")
            if vary is not None and b"accept-encoding" not in vary:
                headers[b"vary"] = vary + b", accept-encoding"
            else:
                headers[b"vary"] = b"accept-encoding"
            start = dict(self.initial_message)
            start["headers"] = [(k, v) for k, v in headers.items()]
            await self.send(start)
            await self.send({"type": "http.response.body", "body": body, "more_body": False})
        else:
            await self.send(self.initial_message)
            await self.send(
                {"type": "http.response.body", "body": bytes(self.buffer), "more_body": False}
            )


class CompressionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int = _MINIMUM_SIZE,
        compresslevel: int = _COMPRESS_LEVEL,
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        accept_encoding = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"accept-encoding":
                accept_encoding = value.decode("latin-1")
        tokens = [t.strip().lower() for t in accept_encoding.split(",") if t.strip()]
        want_br = _HAS_BROTLI and "br" in tokens
        want_gzip = "gzip" in tokens or "x-gzip" in tokens
        if not want_br and not want_gzip:
            await self.app(scope, receive, send)
            return

        compressor = _Compressor(
            self.app, self.minimum_size, want_br, self.compresslevel, send
        )
        await compressor(scope, receive)
