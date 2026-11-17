"""The landing zone: every response from a harvest, kept as fetched.

Same idea as the 2024 Airflow harvester writing each service's JSON to S3 under
{date}/{type}/{server}/{service}.json: parsing can change later (new fields, fixed
bugs) and the catalog can be rebuilt from these files without re-fetching anything.
Layout: <raw_dir>/<platform>/<endpoint id>/<UTC timestamp>.jsonl.gz, one response a line.

Each record keeps what a replay needs to hand a harvester the same `Response` again:
the request as the harvester made it (`request_url` + `params`, since `url` is where
redirects ended up), every response header (the file harvester hashes Content-Length
and ETag), and the body byte for byte: `body` when it is valid UTF-8, else `body_b64`
(an ISO-8859-1 capabilities document must reach the XML parser undamaged).
"""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .http import HttpGetter, Response
from .models import Endpoint
from .util import utcnow

DROP_HEADERS = {"set-cookie"}


class RawCapture:
    """Wraps a fetcher and keeps every response that passes through it."""

    def __init__(self, http: HttpGetter) -> None:
        self._http = http
        self.records: list[dict] = []

    async def get(self, url: str, *, params=None, max_bytes: int | None = None) -> Response:
        resp = await self._http.get(url, params=params, max_bytes=max_bytes)
        self._keep("GET", url, params, resp)
        return resp

    async def head(self, url: str) -> Response:
        resp = await self._http.head(url)
        self._keep("HEAD", url, None, resp)
        return resp

    def _keep(self, method: str, request_url: str, params, resp: Response) -> None:
        record = {
            "method": method,
            "request_url": request_url,
            "params": params or None,
            "url": resp.url,
            "status": resp.status,
            "fetched_at": utcnow(),
            "headers": {k: v for k, v in resp.headers.items() if k not in DROP_HEADERS},
            "error": resp.error,
            "tls_insecure": resp.tls_insecure or None,
            "elapsed": round(resp.elapsed, 3),
        }
        if resp.content:
            try:
                record["body"] = resp.content.decode("utf-8")
            except UnicodeDecodeError:
                record["body_b64"] = base64.b64encode(resp.content).decode("ascii")
        self.records.append(record)

    def write(self, path: Path) -> Path | None:
        if not self.records:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path


def read_raw(path: Path) -> Iterator[tuple[dict, Response]]:
    """Each record in a raw file, with the `Response` it was captured from rebuilt."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if "body_b64" in record:
                content = base64.b64decode(record["body_b64"])
            else:
                content = (record.get("body") or "").encode("utf-8")
            yield (
                record,
                Response(
                    url=record["url"],
                    status=record["status"],
                    headers=record.get("headers") or {},
                    content=content,
                    elapsed=record.get("elapsed") or 0.0,
                    error=record.get("error"),
                    tls_insecure=bool(record.get("tls_insecure")),
                ),
            )


def raw_path(raw_dir: Path, endpoint: Endpoint, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return Path(raw_dir) / endpoint.platform.value / endpoint.id / f"{stamp}.jsonl.gz"
