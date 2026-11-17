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

`ReplayFetcher` hands those responses back to a harvester, so `gis-observer replay`
can rebuild the catalog's history after a parser change.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections import Counter, defaultdict
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


STAMP = "%Y%m%dT%H%M%SZ"


def raw_path(raw_dir: Path, endpoint: Endpoint, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(UTC)).strftime(STAMP)
    return Path(raw_dir) / endpoint.platform.value / endpoint.id / f"{stamp}.jsonl.gz"


def raw_stamp(path: Path) -> datetime:
    """When a raw file was written (its name), which is when its harvest finished."""
    return datetime.strptime(Path(path).name.split(".")[0], STAMP).replace(tzinfo=UTC)


def raw_files(raw_dir: Path, endpoint: Endpoint, since: datetime | None = None) -> list[Path]:
    """An endpoint's raw files, oldest first, optionally only those written at or after `since`."""
    folder = Path(raw_dir) / endpoint.platform.value / endpoint.id
    files = sorted(folder.glob("*.jsonl.gz"))
    if since is not None:
        files = [f for f in files if raw_stamp(f) >= since]
    return files


def _request_key(method: str, url: str, params) -> tuple:
    """Method, requested URL and params, with params made order- and type-insensitive.

    Params go through JSON in the raw file, so a dict comes back a dict but a list of
    pairs comes back a list of lists; both reduce to the same sorted string pairs.
    """
    if not params:
        pairs = ()
    else:
        items = params.items() if isinstance(params, dict) else params
        pairs = tuple(sorted((str(k), str(v)) for k, v in items))
    return method.upper(), url, pairs


class ReplayFetcher:
    """An `HttpGetter` that answers from one raw file instead of the network.

    Requests are matched on method, requested URL and params. A URL requested more
    than once gets its captured responses in order, then the last one again. A
    request that isn't in the file (a fixed parser asking for something new) comes
    back as a failed `Response`, so the harvester records an error and the replayed
    harvest is partial rather than wrongly complete.
    """

    MISSING = "not in raw capture"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._responses: defaultdict[tuple, list[Response]] = defaultdict(list)
        self._served: Counter[tuple] = Counter()
        self.misses: list[str] = []
        self.started_at: str | None = None
        for record, resp in read_raw(self.path):
            self.started_at = self.started_at or record.get("fetched_at")
            # files from before request_url was kept only have the final URL
            url = record.get("request_url") or record["url"]
            key = _request_key(record.get("method", "GET"), url, record.get("params"))
            self._responses[key].append(resp)
        self.finished_at = raw_stamp(self.path).isoformat(timespec="seconds")
        self.started_at = self.started_at or self.finished_at

    async def get(self, url: str, *, params=None, max_bytes: int | None = None) -> Response:
        return self._serve("GET", url, params)

    async def head(self, url: str) -> Response:
        return self._serve("HEAD", url, None)

    def _serve(self, method: str, url: str, params) -> Response:
        key = _request_key(method, url, params)
        captured = self._responses.get(key)
        if not captured:
            self.misses.append(f"{method} {url}" + (f" {params}" if params else ""))
            return Response(url=url, status=None, error=self.MISSING)
        i = self._served[key]
        self._served[key] += 1
        return captured[min(i, len(captured) - 1)]
