import asyncio

import httpx
import pytest

from gis_observer import http
from gis_observer.http import Fetcher, Response

URL = "https://gis.town.gov/arcgis/rest/services?f=json"


def run(fetcher: Fetcher, responses: list[Response], monkeypatch) -> tuple[int, list[float]]:
    """Fetch URL twice; `_send` returns `responses` in order. Returns (sends, sleeps)."""
    sent = []
    sleeps: list[float] = []

    async def fake_send(method, url, params, limit):
        sent.append(url)
        return responses[min(len(sent), len(responses)) - 1]

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(fetcher, "_send", fake_send)
    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)

    async def go():
        async with fetcher:
            await fetcher.get(URL)
            n = len(sent)
            await fetcher.get(URL)
            return n

    first_sends = asyncio.run(go())
    return first_sends, sleeps


def timeout(elapsed: float = 30.0) -> Response:
    return Response(url=URL, status=None, elapsed=elapsed, error="ReadTimeout: timed out")


def ok(elapsed: float = 0.1) -> Response:
    return Response(url=URL, status=200, elapsed=elapsed)


def test_timeout_is_retried_once_and_pause_is_capped(monkeypatch):
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    sends, sleeps = run(fetcher, [timeout(), timeout(), ok()], monkeypatch)
    assert sends == 2  # one try plus one retry, not three
    # Every pause before the next request to the host is at most 10 s, not 45 s.
    assert sleeps and max(sleeps) == pytest.approx(10.0, abs=0.5)


def test_error_statuses_still_get_full_retries(monkeypatch):
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    busy = Response(url=URL, status=503, elapsed=0.1)
    sends, _ = run(fetcher, [busy, busy, busy, ok()], monkeypatch)
    assert sends == 3


def test_slow_success_pause_is_capped(monkeypatch):
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    _, sleeps = run(fetcher, [ok(elapsed=20.0)], monkeypatch)
    assert sleeps == [pytest.approx(10.0, abs=0.5)]


def test_fast_success_keeps_proportional_pause(monkeypatch):
    fetcher = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    _, sleeps = run(fetcher, [ok(elapsed=2.0)], monkeypatch)
    assert sleeps == [pytest.approx(3.0, abs=0.5)]


def test_real_read_timeout_is_classified_as_timeout(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(http, "_backoff", lambda resp, attempt: 0.0)
    fetcher = Fetcher(transport=httpx.MockTransport(handler), politeness=0, min_delay=0)
    resp = asyncio.run(fetcher.get(URL))
    assert resp.error.startswith("ReadTimeout")
    assert len(calls) == 2
