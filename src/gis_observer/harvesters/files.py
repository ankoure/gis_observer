"""Direct downloads: shapefile zips, KML/KMZ, GeoJSON, GeoPackage, Google My Maps KML.

Only the headers are read (size, type, Last-Modified, ETag), so a changed file shows
up as a changed asset without downloading it.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from ..formats import geo_format
from ..models import Asset, Platform
from ..util import compact, parse_when
from .base import Harvester

FILENAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.I)


class FileHarvester(Harvester):
    platform = Platform.FILE

    async def harvest(self, endpoint):
        resp = await self.http.head(endpoint.url)
        if resp.status is None or resp.status >= 400:  # plenty of servers refuse HEAD
            resp = await self.http.get(endpoint.url, max_bytes=0)
        if resp.status is None or resp.status >= 400:
            self.errors.append(f"{endpoint.url}: {resp.error or f'HTTP {resp.status}'}")
            return
        h = resp.headers
        match = FILENAME.search(h.get("content-disposition", ""))
        name = unquote(match.group(1)) if match else PurePosixPath(urlsplit(resp.url).path).name
        length = h.get("content-length", "")
        yield Asset(
            key=endpoint.url,
            kind="file",
            name=name or None,
            url=endpoint.url,  # not resp.url: redirects often land on signed, expiring URLs
            format=endpoint.label or geo_format(None, resp.url, h.get("content-type")),
            source_updated_at=parse_when(h.get("last-modified")),
            extra=compact(
                {
                    "content_type": h.get("content-type"),
                    "content_length": int(length) if length.isdigit() else None,
                    "etag": h.get("etag"),
                    "vendor": endpoint.vendor,
                }
            ),
        )
