"""Test plumbing: a Fetcher backed by a URL -> response table instead of the network."""

from __future__ import annotations

import httpx

from gis_observer.http import Fetcher


def make_fetcher(routes: dict) -> Fetcher:
    """Routes map a URL (matched with its query, then without) to a response.

    Values: dict/list -> JSON; str -> XML or HTML by content; httpx.Response as-is;
    callable(request) -> any of those.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        full = str(request.url)
        for key in (full, full.split("?")[0]):
            if key in routes:
                value = routes[key]
                if callable(value):
                    value = value(request)
                return _response(value)
        return httpx.Response(404, text="not found")

    return Fetcher(transport=httpx.MockTransport(handler), politeness=0, min_delay=0, max_retries=0)


def _response(value) -> httpx.Response:
    if isinstance(value, httpx.Response):
        return value
    if isinstance(value, dict | list):
        return httpx.Response(200, json=value)
    text = str(value)
    ctype = "text/xml" if text.lstrip().startswith("<?xml") else "text/html; charset=utf-8"
    return httpx.Response(200, text=text, headers={"content-type": ctype})


async def collect(harvester, endpoint) -> list:
    return [item async for item in harvester.harvest(endpoint)]
