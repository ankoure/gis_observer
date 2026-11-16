"""A polite async HTTP client shared by the crawler, the prober and every harvester.

The politeness rules come from both parent projects: the 2024 ArcGIS harvester waited
a multiple of the last response time before hitting the same server again, and
govsearch's Scrapy settings retried 429/5xx and kept per-host concurrency low. Here
each host gets one request at a time, then a pause of `politeness` x the last
response time (at least `min_delay`).

Failures come back as a `Response` with `error` set rather than as exceptions, so a
harvester can record what failed and keep going.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx

DEFAULT_USER_AGENT = os.environ.get("GIS_OBSERVER_USER_AGENT", "gis-observer/0.1")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 522, 524}
RETRY_ERRORS = ("ReadTimeout", "ConnectTimeout", "PoolTimeout", "RemoteProtocolError")


@dataclass
class Response:
    url: str  # final URL, after redirects
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed: float = 0.0
    error: str | None = None
    tls_insecure: bool = False  # only reachable with certificate checks off

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        """Parsed JSON body, or None if it isn't JSON."""
        if not self.content:
            return None
        try:
            return json.loads(self.content)
        except ValueError:
            return None


class HttpGetter(Protocol):
    async def get(self, url: str, *, params=None, max_bytes: int | None = None) -> Response: ...

    async def head(self, url: str) -> Response: ...


class Fetcher:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        politeness: float = 1.5,
        min_delay: float = 0.25,
        max_retries: int = 2,
        max_bytes: int = 50_000_000,
        insecure_tls_fallback: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.politeness = politeness
        self.min_delay = min_delay
        self.max_retries = max_retries
        self.max_bytes = max_bytes
        # Lots of government servers have broken certificate chains. Retrying without
        # verification is fine for reading public catalogs; the response is flagged.
        self.insecure_tls_fallback = insecure_tls_fallback and transport is None
        self._client_kw = {
            "headers": {"User-Agent": user_agent},
            "timeout": timeout,
            "follow_redirects": True,
        }
        self._client = httpx.AsyncClient(transport=transport, **self._client_kw)
        self._insecure: httpx.AsyncClient | None = None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_ok: dict[str, float] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._insecure is not None:
            await self._insecure.aclose()

    async def get(self, url: str, *, params=None, max_bytes: int | None = None) -> Response:
        """GET a URL. `max_bytes=0` reads headers only and never downloads the body."""
        return await self.request("GET", url, params=params, max_bytes=max_bytes)

    async def head(self, url: str) -> Response:
        return await self.request("HEAD", url, max_bytes=0)

    async def request(self, method: str, url: str, *, params=None, max_bytes=None) -> Response:
        host = (urlsplit(url).hostname or "").lower()
        limit = self.max_bytes if max_bytes is None else max_bytes
        async with self._locks[host]:
            attempt = 0
            while True:
                wait = self._next_ok.get(host, 0.0) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                resp = await self._send(method, url, params, limit)
                pause = max(self.min_delay, resp.elapsed * self.politeness)
                retry = attempt < self.max_retries and _retryable(resp)
                if retry:
                    pause = max(pause, _backoff(resp, attempt))
                self._next_ok[host] = time.monotonic() + pause
                if not retry:
                    return resp
                attempt += 1

    async def _send(self, method, url, params, limit) -> Response:
        start = time.monotonic()
        try:
            return await _stream(self._client, method, url, params, limit, start)
        except httpx.ConnectError as exc:
            if not (self.insecure_tls_fallback and _is_cert_error(exc)):
                return _failed(url, exc, start)
            try:
                resp = await _stream(self._insecure_client(), method, url, params, limit, start)
            except httpx.HTTPError as exc2:
                resp = _failed(url, exc2, start)
            resp.tls_insecure = True
            return resp
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            return _failed(url, exc, start)

    def _insecure_client(self) -> httpx.AsyncClient:
        if self._insecure is None:
            self._insecure = httpx.AsyncClient(verify=False, **self._client_kw)
        return self._insecure


async def _stream(client, method, url, params, limit, start) -> Response:
    async with client.stream(method, url, params=params) as r:
        headers = {k.lower(): v for k, v in r.headers.items()}
        chunks: list[bytes] = []
        size = 0
        error = None
        if limit > 0 and method != "HEAD":
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    error = f"body larger than {limit} bytes"
                    break
                chunks.append(chunk)
        return Response(
            url=str(r.url),
            status=r.status_code,
            headers=headers,
            content=b"".join(chunks),
            elapsed=time.monotonic() - start,
            error=error,
        )


def _failed(url: str, exc: Exception, start: float) -> Response:
    detail = f"{type(exc).__name__}: {exc}"[:300]
    return Response(url=url, status=None, elapsed=time.monotonic() - start, error=detail)


def _retryable(resp: Response) -> bool:
    return resp.status in RETRY_STATUSES or (resp.error or "").startswith(RETRY_ERRORS)


def _backoff(resp: Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return 2.0 * 2**attempt


def _is_cert_error(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    for _ in range(10):
        if seen is None:
            break
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)
