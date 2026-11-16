"""The landing zone: every response from a harvest, kept as fetched.

Same idea as the 2024 Airflow harvester writing each service's JSON to S3 under
{date}/{type}/{server}/{service}.json: parsing can change later (new fields, fixed
bugs) and the catalog can be rebuilt from these files without re-fetching anything.
Layout: <raw_dir>/<platform>/<endpoint id>/<UTC timestamp>.jsonl.gz, one response a line.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from .http import HttpGetter, Response
from .models import Endpoint
from .util import utcnow


class RawCapture:
    """Wraps a fetcher and keeps every response that passes through it."""

    def __init__(self, http: HttpGetter) -> None:
        self._http = http
        self.records: list[dict] = []

    async def get(self, url: str, *, params=None, max_bytes: int | None = None) -> Response:
        resp = await self._http.get(url, params=params, max_bytes=max_bytes)
        self._keep("GET", resp)
        return resp

    async def head(self, url: str) -> Response:
        resp = await self._http.head(url)
        self._keep("HEAD", resp)
        return resp

    def _keep(self, method: str, resp: Response) -> None:
        self.records.append(
            {
                "method": method,
                "url": resp.url,
                "status": resp.status,
                "fetched_at": utcnow(),
                "content_type": resp.headers.get("content-type"),
                "last_modified": resp.headers.get("last-modified"),
                "error": resp.error,
                "tls_insecure": resp.tls_insecure or None,
                "body": resp.text if resp.content else None,
            }
        )

    def write(self, path: Path) -> Path | None:
        if not self.records:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path


def raw_path(raw_dir: Path, endpoint: Endpoint, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return Path(raw_dir) / endpoint.platform.value / endpoint.id / f"{stamp}.jsonl.gz"
