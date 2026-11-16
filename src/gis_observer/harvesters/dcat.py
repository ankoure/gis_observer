"""DCAT-US `data.json` feeds.

One harvester for every portal that publishes Project Open Data metadata: Socrata,
ArcGIS Hub (/api/feed/dcat-us/1.1.json), CKAN with ckanext-dcat, OpenDataSoft, and
federal agencies. Datasets are kept if a distribution is geospatial or the dataset
has a bounding box.
"""

from __future__ import annotations

from ..formats import SERVICE_FORMATS, geo_format
from ..models import Asset, Platform
from ..util import compact, parse_when, spatial_bbox, trim
from .base import Harvester


class DCATHarvester(Harvester):
    platform = Platform.DCAT

    async def harvest(self, endpoint):
        data = await self.get_json(endpoint.url, max_bytes=300_000_000)
        if data is None:
            return
        datasets = data.get("dataset") if isinstance(data, dict) else data  # v1.0 was a bare list
        if not isinstance(datasets, list):
            self.errors.append(f"{endpoint.url}: no dataset list")
            return
        for ds in datasets:
            if isinstance(ds, dict):
                for item in self._dataset(endpoint, ds):
                    yield item

    def _dataset(self, endpoint, ds: dict):
        dists = []
        for dist in ds.get("distribution") or []:
            if not isinstance(dist, dict):
                continue
            url = dist.get("downloadURL") or dist.get("accessURL")
            fmt = geo_format(dist.get("format"), url, dist.get("mediaType"))
            if url and fmt:
                dists.append((dist, url, fmt))
        bbox = spatial_bbox(ds.get("spatial"))
        if not dists and bbox is None:
            return

        ident = str(ds.get("identifier") or ds.get("landingPage") or ds.get("title"))
        key = ident if ident.startswith(("http://", "https://")) else f"{endpoint.url}#{ident}"
        publisher = ds.get("publisher")
        yield Asset(
            key=key,
            kind="dataset",
            name=ds.get("title"),
            title=ds.get("title"),
            url=ds.get("landingPage") or key,
            bbox=bbox,
            bbox_srid=4326 if bbox else None,
            description=trim(ds.get("description")),
            source_updated_at=parse_when(ds.get("modified")),
            extra=compact(
                {
                    "identifier": ds.get("identifier"),
                    "publisher": publisher.get("name")
                    if isinstance(publisher, dict)
                    else publisher,
                    "keywords": ds.get("keyword"),
                    "access_level": ds.get("accessLevel"),
                }
            ),
        )
        for dist, url, fmt in dists:
            is_service = fmt in SERVICE_FORMATS
            yield Asset(
                key=f"{key}|{url}",
                kind="service" if is_service else "file",
                name=dist.get("title") or fmt,
                title=dist.get("title"),
                url=url,
                parent_key=key,
                service_type=fmt if is_service else None,
                format=fmt,
                extra=compact({"media_type": dist.get("mediaType")}),
            )
            if is_service:
                yield self.candidate(endpoint, url, found_on=key, text=ds.get("title"))
