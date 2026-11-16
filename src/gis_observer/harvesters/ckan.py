"""CKAN open-data portals (data.boston.gov, catalog.data.gov, many state portals).

Keeps datasets that have a geospatial resource or a `spatial` extent; skips the rest.
Resources served from a GIS server (Esri REST, WMS, WFS) are also yielded as
candidates, so the server behind them gets catalogued in full.
"""

from __future__ import annotations

from ..formats import SERVICE_FORMATS, geo_format
from ..models import Asset, Platform
from ..util import compact, parse_when, spatial_bbox, trim
from .base import Harvester


class CKANHarvester(Harvester):
    platform = Platform.CKAN
    page_size = 100
    max_datasets = 20_000  # catalog.data.gov has ~300k; cap it rather than walk it all

    async def harvest(self, endpoint):
        base = endpoint.url.rstrip("/")
        api = f"{base}/api/3/action/package_search"
        start = 0
        while start < self.max_datasets:
            data = await self.get_json(api, {"rows": self.page_size, "start": start})
            if not isinstance(data, dict) or not data.get("success"):
                if isinstance(data, dict):
                    self.errors.append(f"{api}: success=false")
                return
            result = data.get("result") or {}
            packages = result.get("results") or []
            for pkg in packages:
                for item in self._package(endpoint, base, pkg):
                    yield item
            start += len(packages)
            if not packages or start >= (result.get("count") or 0):
                return

    def _package(self, endpoint, base: str, pkg: dict):
        resources = []
        for res in pkg.get("resources") or []:
            fmt = geo_format(res.get("format"), res.get("url"), res.get("mimetype"))
            if fmt:
                resources.append((res, fmt))
        bbox = spatial_bbox(_spatial(pkg))
        if not resources and bbox is None:
            return

        key = f"{base}/dataset/{pkg.get('name') or pkg.get('id')}"
        org = pkg.get("organization") or {}
        yield Asset(
            key=key,
            kind="dataset",
            name=pkg.get("name"),
            title=pkg.get("title"),
            url=key,
            bbox=bbox,
            bbox_srid=4326 if bbox else None,
            description=trim(pkg.get("notes")),
            source_updated_at=parse_when(pkg.get("metadata_modified")),
            extra=compact(
                {
                    "id": pkg.get("id"),
                    "organization": org.get("title"),
                    "license": pkg.get("license_id"),
                    "tags": [t.get("name") for t in pkg.get("tags") or [] if t.get("name")],
                }
            ),
        )
        for res, fmt in resources:
            is_service = fmt in SERVICE_FORMATS
            url = res.get("url")
            yield Asset(
                key=f"{key}/resource/{res.get('id')}",
                kind="service" if is_service else "file",
                name=res.get("name"),
                title=res.get("name"),
                url=url,
                parent_key=key,
                service_type=fmt if is_service else None,
                format=fmt,
                description=trim(res.get("description")),
                source_updated_at=parse_when(
                    res.get("last_modified") or res.get("metadata_modified")
                ),
                extra=compact({"id": res.get("id")}),
            )
            if url and is_service:
                yield self.candidate(endpoint, url, found_on=key, text=res.get("name"))


def _spatial(pkg: dict):
    if pkg.get("spatial"):
        return pkg["spatial"]
    for extra in pkg.get("extras") or []:
        if extra.get("key") == "spatial":
            return extra.get("value")
    return None
